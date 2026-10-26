"""Descubrimiento continuo de F56: broadcast UDP cada 2 s y escucha de respuestas en 50105."""
import socket
import threading
import time

from protocol import PORT_UDP_REPLY, announce, parse_udp_reply


class Discovery:
    def __init__(self, log=print, interval=2.0):
        self.log = log
        self.interval = interval
        self.devices = {}        # mac -> {name, ip, connect_status, last_seen}
        self.error = None
        self.alive = True
        self.lock = threading.Lock()
        self.sock = None

    def start(self):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("0.0.0.0", PORT_UDP_REPLY))
            self.sock.settimeout(0.5)
        except OSError as e:
            self.error = (f"No se pudo abrir UDP {PORT_UDP_REPLY}: {e}. "
                          "¿SpinDisplay está abierto? Ciérralo (corre como administrador).")
            self.log(self.error)
            self.sock = None
        threading.Thread(target=self._tx_loop, daemon=True).start()
        if self.sock:
            threading.Thread(target=self._rx_loop, daemon=True).start()

    def _tx_loop(self):
        while self.alive:
            announce()
            time.sleep(self.interval)

    def _rx_loop(self):
        while self.alive:
            try:
                data, addr = self.sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                time.sleep(0.5)
                continue
            dev = parse_udp_reply(data)
            if not dev:
                continue
            with self.lock:
                known = self.devices.get(dev["mac"])
                if known is None:
                    self.log(f"Detectado {dev['name']} ({dev['mac']}) en {addr[0]}")
                elif known["ip"] != addr[0]:
                    self.log(f"{dev['name']} ({dev['mac']}) cambió de IP: {known['ip']} -> {addr[0]}")
                self.devices[dev["mac"]] = {**dev, "ip": addr[0], "last_seen": time.time()}

    def touch(self, mac):
        """Un equipo con sesión activa no responde broadcasts; la sesión lo mantiene como visto."""
        with self.lock:
            if mac in self.devices:
                self.devices[mac]["last_seen"] = time.time()

    def snapshot(self):
        with self.lock:
            return {mac: dict(d) for mac, d in self.devices.items()}

    def stop(self):
        self.alive = False
        if self.sock:
            self.sock.close()
