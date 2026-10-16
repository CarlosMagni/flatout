"""Cross-site request and DNS-rebinding guard of the local server."""

from __future__ import annotations

import aiohttp

from .test_api import make_config, run, started, stopped


def with_server(cfg_hook=None):
    def deco(fn):
        def test(tmp_path):
            async def main():
                cfg = make_config(tmp_path)
                if cfg_hook:
                    cfg_hook(cfg)
                app, client = await started(cfg)
                calls = []
                real = app.handle

                async def spy(op, args, who):
                    calls.append(op)
                    return await real(op, args, who)

                app.handle = spy
                try:
                    await fn(client, client.server.port, calls)
                finally:
                    await stopped(app, client)
            run(main())
        return test
    return deco


@with_server()
async def _cross_origin_ws(client, port, calls):
    for origin in ("http://evil.example", f"http://127.0.0.1:{port + 1}", "null",
                   f"https://127.0.0.1:{port}"):
        try:
            await client.ws_connect("/ws", headers={"Origin": origin})
            raise AssertionError(f"ws accepted from {origin}")
        except aiohttp.WSServerHandshakeError as e:
            assert e.status == 403, origin


test_cross_origin_ws_refused = _cross_origin_ws


@with_server()
async def _cross_origin_post(client, port, calls):
    r = await client.post("/api/estop", headers={"Origin": "http://evil.example"})
    assert r.status == 403
    assert (await r.json())["ok"] is False
    r = await client.post("/api/bot.throttle", json={"slot": 0, "value": 6},
                          headers={"Origin": "http://evil.example", "Content-Type": "text/plain"})
    assert r.status == 403
    r = await client.get("/api/state", headers={"Origin": "http://evil.example"})
    assert r.status == 403
    assert calls == []  # nothing reached the app


test_cross_origin_post_refused = _cross_origin_post


@with_server()
async def _same_origin(client, port, calls):
    for origin in (f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}/"):
        h = {"Origin": origin}
        assert (await client.get("/api/state", headers=h)).status == 200
        assert (await client.post("/api/estop", headers=h)).status == 200
        ws = await client.ws_connect("/ws", headers=h)
        assert (await ws.receive_json())["type"] == "hello"
        await ws.close()
    # the window opened on "localhost" and a LAN address behave the same
    for host in (f"localhost:{port}", "192.168.1.20:8130", f"[::1]:{port}"):
        h = {"Host": host, "Origin": f"http://{host}"}
        assert (await client.get("/api/state", headers=h)).status == 200
    assert (await client.get("/", headers={"Host": f"localhost:{port}"})).status == 200


test_same_origin_ok = _same_origin


@with_server()
async def _no_origin(client, port, calls):
    assert (await client.get("/api/state")).status == 200
    assert (await client.post("/api/estop")).status == 200
    ws = await client.ws_connect("/ws")
    assert (await ws.receive_json())["type"] == "hello"
    await ws.close()


test_no_origin_ok = _no_origin


@with_server()
async def _bad_host(client, port, calls):
    for host in ("rebind.evil.example:8130", "evil.example", "localhost.evil.example", ""):
        r = await client.get("/api/state", headers={"Host": host})
        assert r.status == 403, host
        r = await client.get("/", headers={"Host": host})
        assert r.status == 403, host
        r = await client.post("/api/estop", headers={"Host": host})
        assert r.status == 403, host
        # a rebound page is same-origin with itself: the Host check is what stops it
        h = {"Host": host, "Origin": f"http://{host}"}
        r = await client.post("/api/estop", headers=h)
        assert r.status == 403, host
    try:
        await client.ws_connect("/ws", headers={"Host": "evil.example", "Origin": "http://evil.example"})
        raise AssertionError("ws accepted on a rebinding name")
    except aiohttp.WSServerHandshakeError as e:
        assert e.status == 403
    assert calls == []


test_bad_host_refused = _bad_host


def _allow(cfg):
    cfg.api.allowed_hosts = ["Pit.local"]
    cfg.api.allowed_origins = ["http://dash.example:3000"]


@with_server(_allow)
async def _allowed(client, port, calls):
    # an allowed name is accepted as Host, with its own origin
    h = {"Host": f"pit.local:{port}", "Origin": f"http://pit.local:{port}"}
    assert (await client.post("/api/estop", headers=h)).status == 200
    # an allowed foreign origin passes (Host still checked); others do not
    h = {"Origin": "http://dash.example:3000"}
    assert (await client.get("/api/state", headers=h)).status == 200
    ws = await client.ws_connect("/ws", headers=h)
    await ws.close()
    assert (await client.get("/api/state", headers={"Origin": "http://dash.example:3001"})).status == 403
    assert (await client.get("/api/state", headers={"Origin": "http://dash.example:3000",
                                                    "Host": "evil.example"})).status == 403


test_allowed_hosts_and_origins = _allowed


def _bound_name(cfg):
    cfg.app.host = "pit-pc.lan"


@with_server(_bound_name)
async def _bound(client, port, calls):
    h = {"Host": f"pit-pc.lan:{port}", "Origin": f"http://pit-pc.lan:{port}"}
    assert (await client.get("/api/state", headers=h)).status == 200

test_bound_hostname_allowed = _bound
