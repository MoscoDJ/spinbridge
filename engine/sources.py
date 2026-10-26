"""Fuente de video: receptor Spout (emisor de Resolume Advanced Output)."""
import array
import threading
import time

import cv2
import numpy as np


class SpoutSource:
    def __init__(self, sender=None, log=print):
        self.log = log
        self.sender = sender         # None = el primero disponible
        self.active_sender = None
        self.senders = []
        self.frame = None            # último cuadro BGR
        self.frame_id = 0
        self.frame_time = 0.0
        self.size = None
        self.gl_renderer = None
        self.input_fps = 0.0
        self.last_content_time = 0.0  # último cuadro con algo distinto de negro
        self.alive = True
        self._switch = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    @property
    def status(self):
        if self.active_sender is None:
            return "sin emisor" if not self.senders else "conectando"
        if time.time() - self.frame_time > 2:
            # el emisor existe pero no llegan cuadros: casi siempre Resolume y el bridge en GPUs distintas
            return "sin cuadros"
        if time.time() - self.last_content_time > 2:
            # llegan cuadros pero todos negros: en Resolume no hay nada reproduciéndose en esa pantalla
            return "negro"
        return "ok"

    def set_sender(self, name):
        self.sender = name or None
        self._switch.set()

    def _loop(self):
        import SpoutGL
        from OpenGL import GL
        while self.alive:
            try:
                self._receive(SpoutGL, GL)
            except Exception as e:  # el hilo nunca debe morir en pleno show
                self.log(f"[spout] error: {e}; reintentando")
                time.sleep(1)

    def _receive(self, SpoutGL, GL):
        with SpoutGL.SpoutReceiver() as r:
            r.createOpenGL()
            self.gl_renderer = (GL.glGetString(GL.GL_RENDERER) or b"?").decode(errors="replace")
            self.log(f"[spout] OpenGL en: {self.gl_renderer}")
            self._switch.clear()
            target = None
            buf = None
            w = h = 0
            last_list = 0.0
            fps_count, fps_t = 0, time.time()
            while self.alive and not self._switch.is_set():
                now = time.time()
                if now - last_list > 2:
                    self.senders = list(r.getSenderList() or [])
                    last_list = now
                    want = self.sender or (self.senders[0] if self.senders else None)
                    if want != target and (want in self.senders):
                        target = want
                        r.setReceiverName(target)
                        buf = None
                        self.active_sender = target
                        self.log(f"[spout] recibiendo de '{target}'")
                    elif target and target not in self.senders:
                        self.active_sender = None
                if not target:
                    time.sleep(0.2)
                    continue
                # Resolume no usa frame sync; limitar a ~60 lecturas/s para no saturar la GPU/CPU
                time.sleep(1 / 60)
                r.waitFrameSync(target, 100)
                ok = r.receiveImage(buf, GL.GL_RGBA, False, 0)
                if r.isUpdated():
                    w, h = r.getSenderWidth(), r.getSenderHeight()
                    buf = array.array("B", bytes(w * h * 4))
                    self.size = (w, h)
                    self.log(f"[spout] '{target}' {w}x{h}")
                    continue
                if buf is not None and ok:
                    rgba = np.frombuffer(buf, np.uint8).reshape(h, w, 4)
                    self.frame = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
                    if self.frame[::8, ::8].max() > 8:
                        self.last_content_time = time.time()
                    self.frame_id += 1
                    self.frame_time = time.time()
                    fps_count += 1
                if now - fps_t >= 1:
                    self.input_fps = fps_count / (now - fps_t)
                    fps_count, fps_t = 0, now

    def stop(self):
        self.alive = False
