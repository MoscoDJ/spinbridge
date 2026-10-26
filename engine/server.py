"""
SpinBridge engine: API WebSocket + HTTP.

    python server.py [--port 8765] [--host 127.0.0.1] [--watch-stdin]

WebSocket /ws
    servidor -> cliente: JSON {"type":"state", ...} 2 veces por segundo
                         binario b"C"+jpeg (vista de la fuente) y b"F"+mac(17)+jpeg (lo que recibe cada ventilador)
    cliente -> servidor: JSON {"id":n, "cmd":"run"|"stop"|"assign"|..., ...} -> {"type":"result","id":n,"ok":bool,"error":str}
HTTP (para Stream Deck / Companion / scripts)
    GET /api/state   GET|POST /api/run   GET|POST /api/stop
    GET|POST /api/power?mac=all&on=0|1   GET|POST /api/identify?mac=XX:...
"""
import argparse
import asyncio
import json
import os
import sys
import threading
import time

import cv2
from aiohttp import WSMsgType, web

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine import Engine  # noqa: E402

PREVIEW_FPS = 6


def dispatch(engine: Engine, msg: dict):
    cmd = msg.get("cmd")
    mac = msg.get("mac")
    if cmd == "run":
        engine.run()
    elif cmd == "stop":
        engine.stop()
    elif cmd == "fan_stream":
        engine.set_fan_stream(mac, msg.get("on", True))
    elif cmd == "assign":
        engine.assign(mac, msg.get("slot"))
    elif cmd == "forget":
        engine.forget(mac)
    elif cmd == "rename":
        engine.rename(mac, msg.get("name", ""))
    elif cmd == "identify":
        engine.identify(mac)
    elif cmd == "power":
        engine.power(mac, bool(msg.get("on")))
    elif cmd == "brightness":
        engine.brightness(mac, int(msg.get("level", 5)))
    elif cmd == "set_sender":
        engine.set_sender(msg.get("name"))
    elif cmd == "settings":
        engine.update_settings(msg.get("values", {}))
    elif cmd == "slots":
        engine.set_slots(msg.get("slots", []))
    elif cmd == "preview":
        pass  # se maneja por conexión
    else:
        raise ValueError(f"comando desconocido: {cmd}")


def make_app(engine: Engine):
    clients = {}  # ws -> {"preview": bool}

    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=0)
        await ws.prepare(request)
        clients[ws] = {"preview": False}
        await ws.send_str(json.dumps(engine.snapshot()))
        try:
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(m.data)
                except ValueError:
                    continue
                result = {"type": "result", "id": msg.get("id"), "ok": True}
                try:
                    if msg.get("cmd") == "preview":
                        clients[ws]["preview"] = bool(msg.get("on"))
                    else:
                        await asyncio.to_thread(dispatch, engine, msg)
                except Exception as e:
                    result.update(ok=False, error=str(e))
                    engine.log(f"error en '{msg.get('cmd')}': {e}")
                await ws.send_str(json.dumps(result))
        finally:
            clients.pop(ws, None)
        return ws

    async def broadcast_state(app):
        while True:
            await asyncio.sleep(0.5)
            if not clients:
                continue
            data = json.dumps(engine.snapshot())
            for ws in list(clients):
                try:
                    await ws.send_str(data)
                except Exception:
                    clients.pop(ws, None)

    async def broadcast_preview(app):
        sent_fan = {}
        last_src_id = -1
        while True:
            await asyncio.sleep(1 / PREVIEW_FPS)
            targets = [ws for ws, c in clients.items() if c["preview"]]
            if not targets:
                continue
            msgs = []
            src = engine.source
            frame = src.frame
            if frame is not None and src.frame_id != last_src_id:
                last_src_id = src.frame_id
                h, w = frame.shape[:2]
                tw = 1200
                small = cv2.resize(frame, (tw, max(1, int(h * tw / w))), interpolation=cv2.INTER_AREA)
                msgs.append(b"C" + cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes())
            for mac, wk in list(engine.workers.items()):
                if wk.last_jpeg is not None and sent_fan.get(mac) != wk.last_jpeg_time:
                    sent_fan[mac] = wk.last_jpeg_time
                    msgs.append(b"F" + mac.encode("ascii") + wk.last_jpeg)
            for ws in targets:
                for m in msgs:
                    try:
                        await ws.send_bytes(m)
                    except Exception:
                        clients.pop(ws, None)
                        break

    def http_cmd(cmd):
        async def handler(request):
            params = dict(request.query)
            if request.can_read_body:
                try:
                    params.update(await request.json())
                except Exception:
                    pass
            if "on" in params:
                params["on"] = str(params["on"]).lower() in ("1", "true", "on", "yes")
            try:
                await asyncio.to_thread(dispatch, engine, {"cmd": cmd, **params})
                return web.json_response({"ok": True})
            except Exception as e:
                return web.json_response({"ok": False, "error": str(e)}, status=400)
        return handler

    async def state(request):
        return web.json_response(engine.snapshot())

    async def on_startup(app):
        app["tasks"] = [asyncio.create_task(broadcast_state(app)), asyncio.create_task(broadcast_preview(app))]

    async def on_cleanup(app):
        for t in app["tasks"]:
            t.cancel()
        await asyncio.to_thread(engine.shutdown)

    app = web.Application()
    app.router.add_get("/ws", ws_handler)
    app.router.add_get("/api/state", state)
    for cmd in ("run", "stop", "power", "identify"):
        app.router.add_route("GET", f"/api/{cmd}", http_cmd(cmd))
        app.router.add_route("POST", f"/api/{cmd}", http_cmd(cmd))
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def watch_stdin(engine):
    """Si la app Electron muere, la tubería se cierra: detener todo y salir (sin procesos huérfanos)."""
    try:
        while sys.stdin.read(1):
            pass
    except Exception:
        pass
    try:
        engine.log("La interfaz se cerró; apagando engine")
        engine.shutdown()
        time.sleep(1.5)
    finally:
        os._exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--config")
    ap.add_argument("--watch-stdin", action="store_true")
    args = ap.parse_args()
    engine = Engine(args.config)
    engine.start()
    if args.watch_stdin:
        threading.Thread(target=watch_stdin, args=(engine,), daemon=True).start()
    engine.log(f"API en http://{args.host}:{args.port}")
    web.run_app(make_app(engine), host=args.host, port=args.port, print=None, handle_signals=True)


if __name__ == "__main__":
    main()
