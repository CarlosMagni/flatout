"""HTTP/WebSocket server: web UI + API (same protocol for the UI and agents).

- `GET  /`                 the UI
- `GET  /ws`               WebSocket (see docs/api.md) — `?agent=<name>&state_hz=<n>&token=<t>`
- `GET  /api/state|meta|laps|sessions|sessions/<id>|log`
- `POST /api/<op>`         JSON body = arguments (e.g. `POST /api/bot.throttle {"slot":0,"value":6}`)
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from ..app import OpError, RaceLabApp

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_STATE_HZ = 20.0
MAX_UPLOAD = 512 * 1024
_ids = itertools.count(1)


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False))


class WSClient:
    def __init__(self, ws: web.WebSocketResponse, name: str, state_hz: float):
        self.ws = ws
        self.name = name
        self.state_every = 1.0 / max(0.5, min(MAX_STATE_HZ, state_hz))
        self.last_state = 0.0
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=512)
        self.dropped = 0

    def push(self, msg: dict[str, Any]) -> None:
        if msg["type"] == "state":
            now = time.monotonic()
            if now - self.last_state < self.state_every * 0.95:
                return
            self.last_state = now
        try:
            self.queue.put_nowait(json.dumps(msg, ensure_ascii=False))
        except asyncio.QueueFull:
            self.dropped += 1  # client too slow: never block the loop

    async def pump(self) -> None:
        while True:
            text = await self.queue.get()
            await self.ws.send_str(text)


def create_web_app(app: RaceLabApp) -> web.Application:
    token = app.cfg.api.token
    names: set[str] = set()

    @web.middleware
    async def auth(request: web.Request, handler):
        if token and (request.path.startswith("/api") or request.path == "/ws"):
            given = request.query.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ")
            if given != token:
                return _json({"ok": False, "error": "invalid API token"}, 401)
        return await handler(request)

    @web.middleware
    async def revalidate(request: web.Request, handler):
        # the UI changes with every app update: never serve a stale version
        response = await handler(request)
        if request.path.startswith("/ui/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    async def index(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def get_state(request):
        return _json(app.snapshot())

    async def get_meta(request):
        return _json(app.meta())

    async def get_laps(request):
        slot = request.query.get("slot")
        return _json(app.engine.laps(None if slot is None else int(slot)))

    async def get_sessions(request):
        return _json(app.recorder.list())

    async def get_session(request):
        try:
            return _json(app.recorder.load(request.match_info["sid"]))
        except (FileNotFoundError, KeyError, ValueError):
            return _json({"ok": False, "error": "session not found"}, 404)

    async def get_log(request):
        return _json(list(app.log))

    async def post_firmware(request: web.Request):
        data = await request.content.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            return _json({"ok": False, "error": "file too large for a CU image"}, 413)
        try:
            info = app.add_firmware(data, request.query.get("name", "image.HMF"))
        except OpError as e:
            return _json({"ok": False, "error": str(e)}, 400)
        return _json({"ok": True, "result": info})

    async def post_op(request: web.Request):
        op = request.match_info["op"]
        try:
            args = await request.json() if request.can_read_body else {}
        except json.JSONDecodeError:
            return _json({"ok": False, "error": "invalid JSON body"}, 400)
        if not isinstance(args, dict):
            return _json({"ok": False, "error": "the body must be a JSON object"}, 400)
        client = request.headers.get("X-Agent", "rest")
        try:
            result = await app.handle(op, args, client)
        except OpError as e:
            return _json({"ok": False, "error": str(e)}, 400)
        return _json({"ok": True, "result": result})

    async def websocket(request: web.Request):
        ws = web.WebSocketResponse(heartbeat=10.0, max_msg_size=1 << 20)
        await ws.prepare(request)
        base = request.query.get("agent") or f"ui-{next(_ids)}"
        name = base
        n = 2
        while name in names:
            name, n = f"{base}#{n}", n + 1
        names.add(name)
        try:
            hz = float(request.query.get("state_hz", app.cfg.api.state_hz))
        except ValueError:
            hz = app.cfg.api.state_hz
        client = WSClient(ws, name, hz)
        unsubscribe = [app.subscribe(client.push)]
        pump = asyncio.create_task(client.pump())
        await ws.send_json({"type": "hello", "client": name, "meta": app.meta(),
                            "state": app.snapshot(), "log": list(app.log)})
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type == WSMsgType.ERROR:
                        logger.debug("ws %s : %s", name, ws.exception())
                    continue
                try:
                    req = json.loads(msg.data)
                    if not isinstance(req, dict):
                        raise ValueError
                except ValueError:
                    client.push({"type": "reply", "id": None, "ok": False, "error": "invalid JSON"})
                    continue
                rid = req.pop("id", None)
                op = req.pop("op", None)
                if op == "subscribe":
                    if "state_hz" in req:
                        client.state_every = 1.0 / max(0.5, min(MAX_STATE_HZ, float(req["state_hz"])))
                    if req.get("raw") and len(unsubscribe) == 1:
                        unsubscribe.append(app.subscribe(client.push, raw=True))
                    elif req.get("raw") is False and len(unsubscribe) == 2:
                        unsubscribe.pop()()
                    client.push({"type": "reply", "id": rid, "ok": True, "result": None})
                    continue
                try:
                    result = await app.handle(str(op), req, name)
                    reply = {"type": "reply", "id": rid, "ok": True, "result": result}
                except OpError as e:
                    reply = {"type": "reply", "id": rid, "ok": False, "error": str(e)}
                except Exception:
                    logger.exception("op %s", op)
                    reply = {"type": "reply", "id": rid, "ok": False, "error": "internal error"}
                client.push(reply)
        finally:
            for unsub in unsubscribe:
                unsub()
            pump.cancel()
            names.discard(name)
            app.client_gone(name)
        return ws

    web_app = web.Application(middlewares=[auth, revalidate])
    web_app.add_routes([
        web.get("/", index),
        web.get("/ws", websocket),
        web.get("/api/state", get_state),
        web.get("/api/meta", get_meta),
        web.get("/api/laps", get_laps),
        web.get("/api/sessions", get_sessions),
        web.get("/api/sessions/{sid}", get_session),
        web.get("/api/log", get_log),
        web.post("/api/firmware/upload", post_firmware),
        web.post("/api/{op}", post_op),
        web.static("/ui", WEB_DIR, append_version=False),
    ])
    return web_app
