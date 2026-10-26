"""
Protocolo SpinDisplay F56, reconstruido del SpinDisplay.exe decompilado y verificado en hardware.
Detalle completo en FINDINGS.md.
"""
import os
import socket
import struct
import threading
import time

HEAD = 0x68
PRO_HEADER = 0xFA

CMD_UDP_BROADCAST = 0x00
CMD_UDP_CONNECT = 0x01
CMD_CONNECT = 0x10
CMD_QUERY_VERSION = 0x11
CMD_QUERY_PARAMETER = 0x12
CMD_BRIGHTNESS = 0x13
CMD_HEART = 0x14
CMD_ONOFF = 0x17
CMD_PROJECTION = 0x37

PORT_BROADCAST = 50100
PORT_UDP_REPLY = 50105
PORT_CONTROL = 50110
PORT_DATA = 50115

IMAGE_SIZE = 672
BRIGHTNESS_MAX = 5  # 0..5 = 50%..100% (SettingWindow: 50 + n*10)


def crc16(data: bytes) -> bytes:
    """CRC-16/MODBUS, byte bajo primero (igual que F20AD.Utils.Crc16)."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes((crc & 0xFF, crc >> 8))


def frame(cmd: int, payload: bytes = b"") -> bytes:
    body = bytes((HEAD, cmd)) + struct.pack("<H", len(payload)) + payload
    return body + crc16(body)


class FrameReader:
    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes):
        self.buf += data
        out = []
        while True:
            if self.buf and self.buf[0] != HEAD:
                self.buf.clear()
            if len(self.buf) < 4:
                break
            total = struct.unpack_from("<H", self.buf, 2)[0] + 6
            if len(self.buf) < total:
                break
            out.append(bytes(self.buf[:total]))
            del self.buf[:total]
        return out


def cstr(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("utf-8", "replace")


def parse_info(f: bytes) -> dict:
    """Subconjunto de Receive.AnalyticInfo. Omite a propósito las contraseñas Wi-Fi que devuelve el equipo."""
    if len(f) < 330:
        return {}
    info = {
        "sn": f[12:18].hex(":").upper(),
        "device_name": cstr(f[18:42]),
        "device_model": cstr(f[43:67]),
        "max_fps": f[72] or 20,
        "content_type": f[73],
        "diam": struct.unpack_from("<h", f, 74)[0],
        "projection_disable": (struct.unpack_from("<H", f, 80)[0] >> 3) & 1,
        "brightness": f[294],
    }
    return info


def local_ipv4s():
    ips = set()
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(res[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def announce():
    """Broadcast de descubrimiento. Observado: el F56 rechaza el TCP 50110 si no recibió uno hace poco."""
    pkt = frame(CMD_UDP_BROADCAST, struct.pack("<I", int.from_bytes(os.urandom(4), "little")))
    for ip in local_ipv4s():
        bcast = ".".join(ip.split(".")[:3] + ["255"])
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            tx.bind((ip, 0))
            tx.sendto(pkt, (bcast, PORT_BROADCAST))
        except OSError:
            pass
        finally:
            tx.close()


def parse_udp_reply(data: bytes):
    if len(data) < 45 or data[0] != HEAD or data[1] != CMD_UDP_CONNECT:
        return None
    return {
        "name": cstr(data[6:30]),
        "mac": data[31:37].hex(":").upper(),
        "type": data[37],
        # 255 = libre; 1/2/3 = ya tiene cliente PC/Android/iPhone
        "connect_status": data[4],
        "password": bool(data[5]),
    }


class Device:
    """Una sesión con un F56: canal de control (50110) y, mientras se proyecta, canal de datos (50115)."""

    def __init__(self, ip: str, log=print):
        self.ip = ip
        self.log = log
        self.ctrl = None
        self.data = None
        self.reader = FrameReader()
        self.pending = []
        self.info = {}
        self.alive = False
        self.lock = threading.Lock()
        self.projection_ok = threading.Event()
        self.projecting = False
        # estado reportado por el equipo
        self.power = None          # respuesta a 0x17
        self.brightness = None     # 0..5
        self.rtt_ms = None         # ida y vuelta del heartbeat
        self._heart_sent = 0.0
        # envío
        self.bytes_sent = 0
        self.frames_sent = 0
        self.frames_dropped = 0
        self.max_send_ms = 0.0
        self.send_error = None
        self.sending = False
        self.slot = None
        self.slot_cv = threading.Condition()

    # ------------------------------------------------ control

    def send_cmd(self, pkt: bytes):
        with self.lock:
            self.ctrl.sendall(pkt)

    def _wait_for(self, cmd: int, timeout: float) -> bytes:
        end = time.time() + timeout
        while True:
            for i, f in enumerate(self.pending):
                if f[1] == cmd:
                    return self.pending.pop(i)
            left = end - time.time()
            if left <= 0:
                raise TimeoutError(f"sin respuesta al comando 0x{cmd:02X}")
            self.ctrl.settimeout(left)
            data = self.ctrl.recv(4096)
            if not data:
                raise ConnectionError("el equipo cerró el canal de control")
            self.pending.extend(self.reader.feed(data))

    def connect(self, attempts: int = 4):
        # Observado: tras cerrar una sesión el F56 suele rechazar el siguiente intento y aceptar el posterior.
        last = None
        for n in range(1, attempts + 1):
            try:
                self._connect_once()
                return
            except (OSError, TimeoutError) as e:
                last = e
                if self.ctrl:
                    self.ctrl.close()
                self.ctrl = None
                self.reader = FrameReader()
                self.pending = []
                time.sleep(1.5)
        raise ConnectionError(f"no se pudo conectar tras {attempts} intentos: {last}")

    def _connect_once(self):
        announce()
        time.sleep(0.5)
        self.ctrl = socket.create_connection((self.ip, PORT_CONTROL), timeout=5)
        self.ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # Observado: el F56 corta la conexión si recibe un Connect repetido; se manda una sola vez.
        self.send_cmd(frame(CMD_CONNECT, b"\x01"))
        self._wait_for(CMD_CONNECT, 3)
        self.send_cmd(frame(CMD_QUERY_VERSION))
        self._wait_for(CMD_QUERY_VERSION, 3)
        self.send_cmd(frame(CMD_QUERY_PARAMETER))
        self.info = parse_info(self._wait_for(CMD_QUERY_PARAMETER, 3))
        self.brightness = self.info.get("brightness")
        self.alive = True
        self.ctrl.settimeout(None)
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=self._heart_loop, daemon=True).start()

    def _rx_loop(self):
        try:
            while self.alive:
                data = self.ctrl.recv(4096)
                if not data:
                    break
                for f in self.reader.feed(data):
                    self._on_frame(f)
        except OSError:
            pass
        self.alive = False

    def _on_frame(self, f: bytes):
        cmd = f[1]
        if cmd == CMD_PROJECTION and len(f) >= 7:
            if f[4] == 0:
                self.projection_ok.set()
            else:
                self.projection_ok.clear()
        elif cmd == CMD_HEART:
            if self._heart_sent:
                self.rtt_ms = (time.time() - self._heart_sent) * 1000
                self._heart_sent = 0.0
        elif cmd == CMD_ONOFF and len(f) >= 7:
            self.power = f[4]
        elif cmd == CMD_BRIGHTNESS and len(f) >= 7:
            self.brightness = f[4]
        elif cmd == CMD_QUERY_PARAMETER:
            info = parse_info(f)
            if info:
                self.info = info

    def _heart_loop(self):
        while self.alive:
            try:
                self._heart_sent = time.time()
                self.send_cmd(frame(CMD_HEART))
            except OSError:
                self.alive = False
                break
            time.sleep(2)

    def set_power(self, on: bool):
        """0x17 Switch: lo que en SpinDisplay son los botones Play/Stop principales (motor)."""
        self.send_cmd(frame(CMD_ONOFF, b"\x01" if on else b"\x00"))
        self.power = 1 if on else 0

    def set_brightness(self, level: int):
        level = max(0, min(BRIGHTNESS_MAX, int(level)))
        self.send_cmd(frame(CMD_BRIGHTNESS, bytes((level,))))
        self.brightness = level

    # ------------------------------------------------ proyección

    def start_projection(self, timeout: float = 10):
        self.projection_ok.clear()
        self.data = socket.create_connection((self.ip, PORT_DATA), timeout=5)
        self.data.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.data.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 102400)
        self.data.settimeout(10)
        end = time.time() + timeout
        while not self.projection_ok.is_set() and time.time() < end and self.alive:
            self.send_cmd(frame(CMD_PROJECTION, b"\x01"))
            self.projection_ok.wait(2)
        if not self.projection_ok.is_set():
            self._close_data()
            raise TimeoutError("el equipo no aceptó Projection(1)")
        self.send_error = None
        self.sending = True
        self.projecting = True
        threading.Thread(target=self._send_loop, daemon=True).start()

    def stop_projection(self):
        self.sending = False
        self.projecting = False
        with self.slot_cv:
            self.slot_cv.notify_all()
        try:
            if self.ctrl and self.alive:
                self.send_cmd(frame(CMD_PROJECTION, b"\x00"))
        except OSError:
            pass
        self._close_data()

    def _close_data(self):
        if self.data:
            try:
                self.data.close()
            except OSError:
                pass
        self.data = None

    def send_jpeg(self, jpeg: bytes):
        """Deja el cuadro como el más reciente; si el anterior no salió aún, se descarta (sin acumular retraso)."""
        with self.slot_cv:
            if self.slot is not None:
                self.frames_dropped += 1
            self.slot = jpeg
            self.slot_cv.notify()

    def _send_loop(self):
        while self.alive and self.sending:
            with self.slot_cv:
                while self.slot is None and self.alive and self.sending:
                    self.slot_cv.wait(0.5)
                jpeg, self.slot = self.slot, None
            if jpeg is None or not self.sending:
                continue
            n = len(jpeg)
            t = time.time()
            try:
                self.data.sendall(bytes((PRO_HEADER, n & 0xFF, (n >> 8) & 0xFF, (n >> 16) & 0xFF)) + jpeg)
            except (OSError, AttributeError) as e:
                if self.sending:
                    self.send_error = e
                break
            self.max_send_ms = max(self.max_send_ms, (time.time() - t) * 1000)
            self.bytes_sent += n + 4
            self.frames_sent += 1

    def close(self):
        if self.projecting:
            self.stop_projection()
        self.alive = False
        self.sending = False
        if self.ctrl:
            try:
                self.ctrl.close()
            except OSError:
                pass
        self._close_data()
