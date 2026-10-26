"""Un hilo por ventilador asignado: mantiene la sesión, proyecta su slice cuando está en RUN y aplica calidad automática."""
import threading
import time

import cv2
import numpy as np

from protocol import IMAGE_SIZE, Device

SLOT_COLORS = [(60, 60, 255), (255, 200, 60), (120, 255, 80), (40, 200, 255), (255, 90, 220),
               (255, 255, 255), (0, 160, 255), (200, 120, 255)]


def identify_image(slot_id, name, t):
    img = np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), np.uint8)
    c = IMAGE_SIZE // 2
    color = SLOT_COLORS[(slot_id - 1) % len(SLOT_COLORS)] if slot_id else (255, 255, 255)
    pulse = 0.6 + 0.4 * abs(np.sin(t * 4))
    col = tuple(int(v * pulse) for v in color)
    cv2.circle(img, (c, c), c - 8, col, 18)
    text = str(slot_id) if slot_id else "?"
    scale = 14 if len(text) == 1 else 10
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_DUPLEX, scale, 40)
    cv2.putText(img, text, (c - tw // 2, c + th // 2 - 70), cv2.FONT_HERSHEY_DUPLEX, scale, col, 40, cv2.LINE_AA)
    (nw, _), _ = cv2.getTextSize(name, cv2.FONT_HERSHEY_DUPLEX, 1.6, 3)
    cv2.putText(img, name, (c - nw // 2, c + 215), cv2.FONT_HERSHEY_DUPLEX, 1.6, (255, 255, 255), 3, cv2.LINE_AA)
    return img


class FanWorker:
    def __init__(self, engine, mac):
        self.engine = engine
        self.mac = mac
        self.dev = None
        self.ip = None
        self.state = "esperando IP"
        self.error = ""
        self.reconnects = 0
        self.want_stream = False
        self.identify_until = 0.0
        self.quality = engine.cfg["quality"]
        self.budget_mbps = 0.0
        self.last_jpeg = None
        self.last_jpeg_time = 0.0
        self.alive = True
        # contadores acumulados entre sesiones
        self.sent_total = self.dropped_total = self.bytes_total = 0
        self._stats_prev = (0, 0, 0, time.time())
        self.fps = self.mbps = self.kb = self.drop_rate = 0.0
        self.mask = np.zeros((IMAGE_SIZE, IMAGE_SIZE), np.uint8)
        cv2.circle(self.mask, (IMAGE_SIZE // 2, IMAGE_SIZE // 2), IMAGE_SIZE // 2, 255, -1)
        threading.Thread(target=self._loop, daemon=True, name=f"fan-{mac}").start()

    # ------------------------------------------------ datos de configuración

    @property
    def fan_cfg(self):
        return self.engine.cfg["fans"].get(self.mac, {})

    @property
    def slot(self):
        return self.engine.slot_by_id(self.fan_cfg.get("slot"))

    @property
    def name(self):
        return self.fan_cfg.get("name") or self.mac[-5:]

    def log(self, msg):
        self.engine.log(f"[{self.name}] {msg}")

    # ------------------------------------------------ bucle de sesión

    def _loop(self):
        backoff = 2
        while self.alive:
            ip = self.engine.ip_for(self.mac)
            if not ip:
                self.state = "esperando IP"
                time.sleep(1)
                continue
            self.ip = ip
            self.dev = Device(ip, log=self.log)
            try:
                self.state = "conectando"
                self.dev.connect()
                self.state = "conectado"
                self.error = ""
                backoff = 2
                self.log(f"conectado ({ip})")
                self._session()
            except Exception as e:
                self.error = str(e)
                self.log(f"sesión terminada: {e}")
            finally:
                self._accumulate()
                self.dev.close()
            if not self.alive:
                break
            self.reconnects += 1
            self.state = "reconectando"
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)
        self.state = "detenido"

    def _accumulate(self):
        d = self.dev
        if d:
            self.sent_total += d.frames_sent
            self.dropped_total += d.frames_dropped
            self.bytes_total += d.bytes_sent
            d.frames_sent = d.frames_dropped = d.bytes_sent = 0

    def _session(self):
        d = self.dev
        cfg = self.engine.cfg
        next_t = time.time()
        last_id = -1
        last_ip_check = time.time()
        while self.alive and d.alive:
            self.engine.discovery.touch(self.mac)
            if time.time() - last_ip_check > 5:
                last_ip_check = time.time()
                ip = self.engine.ip_for(self.mac)
                if ip and ip != self.ip:
                    raise ConnectionError(f"IP cambió a {ip}")
            identifying = time.time() < self.identify_until
            should_project = (self.want_stream and self.engine.running) or identifying
            if should_project and not d.projecting:
                self.state = "iniciando proyección"
                d.start_projection()
                self.log("proyectando")
            elif not should_project and d.projecting:
                d.stop_projection()
                self.log("proyección detenida")
            if not d.projecting:
                self.state = "conectado"
                time.sleep(0.1)
                continue
            if d.send_error:
                raise ConnectionError(f"envío: {d.send_error}")
            self.state = "identificando" if identifying else "en vivo"
            img = None
            if identifying:
                img = identify_image(self.fan_cfg.get("slot"), self.name, time.time())
            else:
                src = self.engine.source
                if src.frame is not None and src.frame_id != last_id:
                    last_id = src.frame_id
                    img = self._crop(src.frame)
            if img is not None:
                if cfg.get("circle_mask", True):
                    img = cv2.bitwise_and(img, img, mask=self.mask)
                jpeg = self._encode(img)
                d.send_jpeg(jpeg)
                self.last_jpeg = jpeg
                self.last_jpeg_time = time.time()
            next_t += 1.0 / cfg["fps"]
            delay = next_t - time.time()
            if delay < -1:
                next_t = time.time()
            time.sleep(max(0, delay))
        if d.projecting:
            d.stop_projection()
        raise ConnectionError("canal de control cerrado")

    def _crop(self, frame):
        slot = self.slot
        if not slot:
            return None
        x, y, w, h = slot["rect"]
        fh, fw = frame.shape[:2]
        if x + w > fw or y + h > fh:
            self.error = f"slice {slot['rect']} fuera de la imagen {fw}x{fh}"
            return None
        img = frame[y:y + h, x:x + w]
        if w != IMAGE_SIZE or h != IMAGE_SIZE:
            img = cv2.resize(img, (IMAGE_SIZE, IMAGE_SIZE), interpolation=cv2.INTER_AREA)
        return img

    def _encode(self, img) -> bytes:
        """Calidad automática: el F56 acepta ~3.5 Mbit/s (medido); cada cuadro se ajusta a target_mbps / fps."""
        cfg = self.engine.cfg
        qmax, qmin = cfg["quality"], cfg["min_quality"]
        self.budget_mbps = self.engine.per_fan_mbps(self)
        target = self.budget_mbps * 1e6 / 8 / cfg["fps"]
        self.quality = max(qmin, min(qmax, self.quality))
        # Huffman optimizado: ~30 % más chico, PERO el F56 rechaza algunos cuadros así (barras SMPTE); apagado por defecto
        opt = int(bool(cfg.get("jpeg_optimize", False)))
        params = lambda: [cv2.IMWRITE_JPEG_QUALITY, self.quality, cv2.IMWRITE_JPEG_OPTIMIZE, opt]
        jpg = cv2.imencode(".jpg", img, params())[1]
        size = len(jpg)
        dropped = self.dev.frames_dropped
        if size > target * 1.3 and self.quality > qmin:
            self.quality = max(qmin, self.quality - max(3, int(self.quality * (1 - target / size))))
            jpg = cv2.imencode(".jpg", img, params())[1]
        elif size > target * 1.05 or dropped > getattr(self, "_last_dropped", 0):
            self.quality = max(qmin, self.quality - 1)
        elif size < target * 0.8 and self.quality < qmax:
            self.quality += 1
        self._last_dropped = dropped
        return jpg.tobytes()

    # ------------------------------------------------ comandos

    def identify(self, seconds=5):
        self.identify_until = time.time() + seconds

    def set_power(self, on):
        if not (self.dev and self.dev.alive):
            raise RuntimeError(f"{self.name} no está conectado")
        self.dev.set_power(on)
        self.log(f"motor {'ENCENDIDO' if on else 'APAGADO'}")

    def set_brightness(self, level):
        if not (self.dev and self.dev.alive):
            raise RuntimeError(f"{self.name} no está conectado")
        self.dev.set_brightness(level)
        self.log(f"brillo {50 + 10 * self.dev.brightness}%")

    def stop(self):
        self.alive = False
        if self.dev:
            self.dev.close()

    # ------------------------------------------------ estado

    def update_rates(self):
        d = self.dev
        sent = self.sent_total + (d.frames_sent if d else 0)
        nbytes = self.bytes_total + (d.bytes_sent if d else 0)
        dropped = self.dropped_total + (d.frames_dropped if d else 0)
        p_sent, p_bytes, p_drop, p_t = self._stats_prev
        dt = max(0.001, time.time() - p_t)
        self.fps = (sent - p_sent) / dt
        self.drop_rate = (dropped - p_drop) / dt
        self.mbps = (nbytes - p_bytes) * 8 / dt / 1e6
        self.kb = (nbytes - p_bytes) / max(1, sent - p_sent) / 1024 if sent > p_sent else 0.0
        self._stats_prev = (sent, nbytes, dropped, time.time())

    def snapshot(self):
        d = self.dev
        live = bool(d and d.alive)
        return {
            "mac": self.mac,
            "name": self.name,
            "ip": self.ip,
            "slot": self.fan_cfg.get("slot"),
            "state": self.state,
            "error": self.error,
            "want_stream": self.want_stream,
            "connected": live,
            "projecting": bool(d and d.projecting),
            "fps": round(self.fps, 1),
            "mbps": round(self.mbps, 2),
            "kb": round(self.kb, 1),
            "quality": self.quality,
            "budget_mbps": round(self.budget_mbps, 2),
            "sent": self.sent_total + (d.frames_sent if d else 0),
            "dropped": self.dropped_total + (d.frames_dropped if d else 0),
            "drop_rate": round(self.drop_rate, 1),
            "reconnects": self.reconnects,
            "rtt_ms": round(d.rtt_ms) if live and d.rtt_ms is not None else None,
            "power": d.power if live else None,
            "brightness": d.brightness if live else None,
            "model": d.info.get("device_model") if live else None,
        }
