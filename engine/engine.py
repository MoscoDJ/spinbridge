"""Núcleo de SpinBridge: configuración, descubrimiento, fuente Spout y un worker por ventilador."""
import collections
import json
import os
import threading
import time

from discovery import Discovery
from sources import SpoutSource
from worker import FanWorker

DEFAULT_CONFIG = {
    "spout_sender": "Arena - Screen 1",
    "fps": 20,
    "quality": 60,
    "min_quality": 20,
    "target_mbps": 2.8,        # tope por ventilador (medido: el F56 acepta ~3.5-4 Mbit/s)
    "total_mbps": 6.0,         # presupuesto total de la red Wi-Fi, repartido entre los que proyectan
    "jpeg_optimize": False,    # el decodificador del F56 rechaza algunos JPEG con Huffman optimizado (muestra barras SMPTE)
    "circle_mask": True,
    "slots": [{"id": i + 1, "rect": [i * 672, 0, 672, 672]} for i in range(5)],
    # mac -> {"name": str, "slot": int|None, "stream": bool}
    "fans": {},
}


def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "SpinBridge")
    os.makedirs(path, exist_ok=True)
    return path


class Engine:
    def __init__(self, config_path=None):
        self.config_path = config_path or os.path.join(config_dir(), "config.json")
        self.cfg = self._load_config()
        self.logs = collections.deque(maxlen=300)
        self.log_file = open(os.path.join(config_dir(), "spinbridge.log"), "a", encoding="utf-8")
        self.lock = threading.RLock()
        self.running = False
        self.workers = {}
        self.discovery = Discovery(log=self.log)
        self.source = SpoutSource(self.cfg.get("spout_sender"), log=self.log)
        self.started = time.time()

    # ------------------------------------------------ config y log

    def _load_config(self):
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if os.path.exists(self.config_path):
            with open(self.config_path, encoding="utf-8") as f:
                cfg.update(json.load(f))
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        return cfg

    def save_config(self):
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.config_path)

    def log(self, msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        self.logs.append(line)
        try:
            print(line, flush=True)
        except Exception:
            pass  # la consola (tubería de Electron) puede ya no existir
        try:
            self.log_file.write(time.strftime("%Y-%m-%d ") + line + "\n")
            self.log_file.flush()
        except Exception:
            pass

    # ------------------------------------------------ ciclo de vida

    def start(self):
        self.log(f"SpinBridge engine iniciado. Config: {self.config_path}")
        self.discovery.start()
        for mac in self.cfg["fans"]:
            self._ensure_worker(mac)
        threading.Thread(target=self._rates_loop, daemon=True).start()

    def shutdown(self):
        self.log("Cerrando: deteniendo proyección y sesiones")
        self.running = False
        for w in list(self.workers.values()):
            w.stop()
        self.discovery.stop()
        self.source.stop()

    def _rates_loop(self):
        while True:
            time.sleep(1)
            for w in list(self.workers.values()):
                w.update_rates()

    def _ensure_worker(self, mac):
        with self.lock:
            if mac not in self.workers:
                w = FanWorker(self, mac)
                w.want_stream = self.cfg["fans"][mac].get("stream", True)
                self.workers[mac] = w
            return self.workers[mac]

    # ------------------------------------------------ consultas

    def slot_by_id(self, slot_id):
        for s in self.cfg["slots"]:
            if s["id"] == slot_id:
                return s
        return None

    def per_fan_mbps(self, worker=None):
        """Medido: 5 F56 juntos comparten ~5.5-7 Mbit/s en total, con cualquier router (AX11000, GT6, UniFi).
        Cada ventilador tiene garantizada una parte igual del total y además puede usar lo que los demás
        no están usando (contenido oscuro pesa poco), sin pasar de su tope individual ni del total."""
        projecting = [w for w in self.workers.values() if w.dev and w.dev.projecting]
        total, cap = self.cfg["total_mbps"], self.cfg["target_mbps"]
        fair = total / max(1, len(projecting))
        if worker is None:
            return min(cap, fair)
        others = sum(w.mbps for w in projecting if w is not worker)
        return min(cap, max(fair, (total - others) * 0.9))

    def ip_for(self, mac):
        d = self.discovery.devices.get(mac)
        if d:
            return d["ip"]
        return self.cfg["fans"].get(mac, {}).get("last_ip")

    # ------------------------------------------------ comandos (llamados desde la API)

    def run(self):
        self.running = True
        self.log("RUN")

    def stop(self):
        self.running = False
        self.log("STOP")

    def set_fan_stream(self, mac, on):
        w = self._worker(mac)
        w.want_stream = bool(on)
        self.cfg["fans"][mac]["stream"] = bool(on)
        self.save_config()

    def assign(self, mac, slot_id):
        """Asigna un ventilador a un slot. Si el slot ya tenía otro, intercambian lugares."""
        with self.lock:
            if slot_id is not None and not self.slot_by_id(slot_id):
                raise ValueError(f"slot {slot_id} no existe")
            fans = self.cfg["fans"]
            if mac not in fans:
                dev = self.discovery.devices.get(mac, {})
                fans[mac] = {"name": dev.get("name") or mac[-5:], "slot": None, "stream": True}
            prev_slot = fans[mac].get("slot")
            for other, f in fans.items():
                if other != mac and slot_id is not None and f.get("slot") == slot_id:
                    f["slot"] = prev_slot
                    self.log(f"{f.get('name')} pasa al slot {prev_slot}")
            fans[mac]["slot"] = slot_id
            self.save_config()
            self._ensure_worker(mac)
            self.log(f"{fans[mac]['name']} asignado al slot {slot_id}")

    def forget(self, mac):
        with self.lock:
            w = self.workers.pop(mac, None)
            if w:
                w.stop()
            self.cfg["fans"].pop(mac, None)
            self.save_config()

    def rename(self, mac, name):
        self.cfg["fans"][mac]["name"] = str(name)[:24]
        self.save_config()

    def identify(self, mac):
        self._worker(mac).identify()

    def power(self, mac, on):
        targets = list(self.workers.values()) if mac == "all" else [self._worker(mac)]
        errors = []
        for w in targets:
            try:
                w.set_power(on)
            except Exception as e:
                errors.append(str(e))
        if errors:
            raise RuntimeError("; ".join(errors))

    def brightness(self, mac, level):
        targets = list(self.workers.values()) if mac == "all" else [self._worker(mac)]
        for w in targets:
            w.set_brightness(level)

    def set_sender(self, name):
        self.cfg["spout_sender"] = name or None
        self.source.set_sender(name)
        self.save_config()

    def update_settings(self, values):
        allowed = {"fps": (5, 30), "quality": (10, 95), "min_quality": (5, 95), "target_mbps": (0.5, 10),
                   "total_mbps": (1, 100)}
        for k, (lo, hi) in allowed.items():
            if k in values:
                self.cfg[k] = type(DEFAULT_CONFIG[k])(max(lo, min(hi, float(values[k]))))
        for k in ("circle_mask", "jpeg_optimize"):
            if k in values:
                self.cfg[k] = bool(values[k])
        if self.cfg["min_quality"] > self.cfg["quality"]:
            self.cfg["min_quality"] = self.cfg["quality"]
        self.save_config()

    def set_slots(self, slots):
        clean = []
        for s in slots:
            x, y, w, h = (int(v) for v in s["rect"])
            clean.append({"id": int(s["id"]), "rect": [x, y, w, h]})
        self.cfg["slots"] = clean
        self.save_config()

    def _worker(self, mac):
        w = self.workers.get(mac)
        if not w:
            raise KeyError(f"ventilador {mac} desconocido")
        return w

    # ------------------------------------------------ estado para la UI

    def snapshot(self):
        try:
            import psutil
            cpu = psutil.cpu_percent(None)
        except Exception:
            cpu = None
        found = self.discovery.snapshot()
        fans = []
        for mac, w in list(self.workers.items()):
            s = w.snapshot()
            if s["ip"]:
                self.cfg["fans"][mac]["last_ip"] = s["ip"]
            fans.append(s)
        unassigned = [
            {"mac": mac, "name": d["name"], "ip": d["ip"], "connect_status": d["connect_status"],
             "seen_ago": round(time.time() - d["last_seen"], 1)}
            for mac, d in found.items() if mac not in self.workers
        ]
        src = self.source
        return {
            "type": "state",
            "running": self.running,
            "cpu": cpu,
            "source": {
                "sender": src.active_sender,
                "wanted": self.cfg.get("spout_sender"),
                "senders": src.senders,
                "status": src.status,
                "size": src.size,
                "fps": round(src.input_fps, 1),
                "gl_renderer": src.gl_renderer,
            },
            "discovery_error": self.discovery.error,
            "settings": {k: self.cfg.get(k, DEFAULT_CONFIG[k]) for k in
                         ("fps", "quality", "min_quality", "target_mbps", "total_mbps", "circle_mask", "jpeg_optimize")},
            "per_fan_mbps": round(self.per_fan_mbps(), 2),
            "slots": self.cfg["slots"],
            "fans": fans,
            "detected": unassigned,
            "logs": list(self.logs)[-80:],
        }
