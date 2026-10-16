"""`python -m trackloc serve`: the page's server, its guards, one action at a time, and the safety of the actions it runs.

The server is run for real on 127.0.0.1 (aiohttp test client). The camera is a fake source; RaceLab is either a real
RaceLab app (the `racelab_factory` fixture, with the virtual CU or none) or a fake link that records every call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import numpy as np
import pytest

pytest.importorskip("cv2")
pytest.importorskip("aiohttp")

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from trackloc import bench, serve as sv_mod  # noqa: E402
from trackloc.__main__ import build_parser  # noqa: E402
from trackloc.preview import allow_loopback  # noqa: E402
from trackloc.serve import TOKEN_HEADER, Serve, make_app  # noqa: E402
from trackloc.synth import CameraSpec, T0  # noqa: E402

CAM = CameraSpec(width=640, height=360)


# ----------------------------------------------------------------------------- fakes

class FakeSource:
    """A camera that shows one picture at `fps` (or whatever `Replay` pushes)."""

    def __init__(self, image=None, fps: float = 30.0) -> None:
        self.image = image if image is not None else np.full((360, 640, 3), 90, np.uint8)
        self.fps = fps
        self.cb = None
        self.on_start = None
        self._stop = threading.Event()
        self._thread = None
        self.stopped = False

    def info(self):
        return {"width": self.image.shape[1], "height": self.image.shape[0], "fps": self.fps}

    def start(self, cb):
        self.cb = cb

        def loop():
            while not self._stop.is_set():
                now = time.monotonic()
                cb(now, now, self.image.copy())
                self._stop.wait(1 / self.fps)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self.stopped = True
        self._stop.set()


class FakeLink:
    """The part of the RaceLab link `drive` and `identify` use; every call is recorded."""

    def __init__(self, replay=None, **status) -> None:
        self.calls = []
        self.raw = None
        self.put = None
        self.replay = replay
        self.state = {"session": "idle", "estop": False, "link": "connected", **status}
        self.act_hook = None
        self.attempts = []
        self.closed = False

    def start_inputs(self, put):
        self.put = put
        if self.replay:
            self.replay.try_start()

    def ensure_session(self):
        return False

    def stop_session(self):
        self.calls.append(("stop_session",))

    def claim(self, slot, free=False):
        self.calls.append(("claim", slot))

    def act(self, slot, throttle=None, lane=False):
        self.attempts.append(throttle)
        if self.act_hook:
            self.act_hook(self)
        self.calls.append(("act", slot, throttle, lane))

    def unclaim(self, slot):
        self.calls.append(("unclaim", slot))

    def estop(self):
        self.calls.append(("estop",))

    def status(self):
        return dict(self.state)

    def close(self):
        self.closed = True

    def clear_track(self):
        pass

    def handed_back(self, slot=0):
        """The last act of the slot is throttle 0 and the unclaim came after it."""
        mine = [c for c in self.calls if len(c) > 1 and c[1] == slot and c[0] in ("act", "unclaim")]
        zero = [i for i, c in enumerate(mine) if c[0] == "act" and c[2] == 0]
        return bool(mine) and mine[-1][0] == "unclaim" and bool(zero) and zero[-1] < len(mine) - 1


class Replay:
    """A synthetic discovery lap played into the camera and the link on ONE clock (the session's)."""

    def __init__(self, sess, pace: float = 0.008) -> None:
        self.sess, self.pace = sess, pace
        self.source = FakeSource(fps=30.0)
        self.source.image = sess.render(0)[1]
        self.link = FakeLink(self)
        self._lock = threading.Lock()
        self._started = False
        self._stop = threading.Event()
        replay = self

        def start(cb):
            replay.cb = cb
            replay.try_start()

        self.source.start = start
        self.source.stop = lambda: replay._stop.set()
        self.cb = None

    def try_start(self):
        with self._lock:
            if self._started or self.cb is None or self.link.put is None:
                return
            self._started = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        fps = self.sess.cam.fps
        for kind, payload in self.sess.stream():
            if self._stop.is_set():
                return
            if kind == "frame":
                t, img = payload
                self.cb(t, t + 0.5 / fps, img)
                time.sleep(self.pace)
            else:
                self.link.put({**payload})


class FakeClient:
    """A RaceLab connection that records `request`s (the connector card)."""

    def __init__(self, refuse: str | None = None) -> None:
        self.requests = []
        self.refuse = refuse
        self.closed = False

    def request(self, op, /, **args):
        from racelab.client import RaceLabError

        if self.refuse:
            raise RaceLabError(self.refuse)
        self.requests.append((op, args))

    def close(self):
        self.closed = True


# ----------------------------------------------------------------------------- harness

def make_serve(tmp_path, *, factory=None, connect=None, card_connect=None, extra=()):
    args = build_parser().parse_args(["serve", "--racelab-url", "ws://127.0.0.1:1/ws", *extra])
    return Serve(args, "en", tmp_path, camera_factory=factory or (lambda spec: FakeSource()), connect=connect,
                 card_connect=card_connect or (lambda url, token: FakeClient()), card_period=0.05)


def run(sv, body):
    """Serve `sv` for real, run `await body(client, post)` against it, then shut down like Ctrl-C does."""

    async def main():
        app = make_app(sv)
        async with TestClient(TestServer(app)) as client:
            allow_loopback(app, client.server.port)
            sv.port = client.server.port
            sv.card.start()

            async def post(path, body=None, *, token=True, **kw):
                headers = {TOKEN_HEADER: sv.token} if token else {}
                headers.update(kw.pop("headers", {}))
                r = await client.post(path, json=body if body is not None else {}, headers=headers, **kw)
                text = await r.text()
                try:
                    return r.status, json.loads(text)
                except ValueError:
                    return r.status, text

            async def until(pred, timeout=30.0):
                end = time.monotonic() + timeout
                while not pred():
                    assert time.monotonic() < end, "timeout"
                    await asyncio.sleep(0.05)

            try:
                return await body(client, post, until)
            finally:
                await asyncio.get_running_loop().run_in_executor(None, sv.shutdown)

    return asyncio.run(main())


async def open_camera(post):
    code, _ = await post("/api/camera", {"camera": "1"})
    assert code == 200


# ----------------------------------------------------------------------------- the page and the guards

def test_status_and_the_page(tmp_path):
    sv = make_serve(tmp_path)

    async def body(client, post, until):
        r = await client.get("/status")
        st = await r.json()
        assert r.status == 200 and st["status"] == "idle" and st["action"] is None and st["camera"]["open"] is False
        assert st["map"] is None and st["latency"] is None and "preview" in st
        page = await (await client.get("/")).text()
        assert "Camera connector" in page and "__MESSAGES__" not in page and "X-Trackloc-Token" in page
        csp = (await client.get("/")).headers["Content-Security-Policy"]
        assert "nonce-" in csp and "default-src 'none'" in csp
        assert (await client.get("/devices")).status == 200

    run(sv, body)


def test_every_post_needs_the_token_a_json_body_and_a_same_site_loopback_request(tmp_path):
    sv = make_serve(tmp_path)

    async def body(client, post, until):
        assert (await post("/api/stop", token=False))[0] == 403  # no token
        assert (await post("/api/stop", headers={TOKEN_HEADER: "wrong"}))[0] == 403
        assert (await post("/api/stop", headers={"Sec-Fetch-Site": "cross-site"}))[0] == 403
        assert (await post("/api/stop", headers={"Origin": "http://evil.example"}))[0] == 403
        assert (await post("/api/stop", headers={"Host": "evil.example"}))[0] == 421
        assert (await client.get("/status", headers={"Host": "evil.example"})).status == 421  # DNS rebinding, GET too
        assert (await client.get("/status", headers={"Sec-Fetch-Site": "cross-site"})).status == 403
        r = await client.post("/api/stop", data="x=1", headers={TOKEN_HEADER: sv.token, "Content-Type": "application/x-www-form-urlencoded"})
        assert r.status == 415
        r = await client.post("/api/stop", data="plain", headers={TOKEN_HEADER: sv.token, "Content-Type": "text/plain"})
        assert r.status == 415
        r = await client.post("/api/map", data="{not json", headers={TOKEN_HEADER: sv.token, "Content-Type": "application/json"})
        assert r.status == 400
        r = await client.post("/api/map", data="[1]", headers={TOKEN_HEADER: sv.token, "Content-Type": "application/json"})
        assert r.status == 400
        assert (await client.put("/api/stop", headers={TOKEN_HEADER: sv.token})).status == 405
        assert (await post("/api/stop"))[0] == 200  # right token, JSON: fine (nothing runs: a no-op)

    run(sv, body)


def test_the_token_is_random_per_run_and_the_server_never_listens_beyond_loopback(tmp_path):
    a, b = make_serve(tmp_path), make_serve(tmp_path)
    assert a.token != b.token and len(a.token) >= 24
    import inspect

    assert "0.0.0.0" not in inspect.getsource(sv_mod) and "TCPSite" not in inspect.getsource(sv_mod)  # preview.serve binds 127.0.0.1


# ----------------------------------------------------------------------------- the camera and validation

def test_the_camera_picker_and_what_it_refuses(tmp_path):
    seen = []
    sv = make_serve(tmp_path, factory=lambda spec: seen.append(spec) or FakeSource())

    async def body(client, post, until):
        assert (await post("/api/record", {}))[0] == 400  # no camera yet
        for bad in ("file:///etc/passwd", "/etc/passwd", "ftp://host/x", "", 5, "rtsp://"):
            assert (await post("/api/camera", {"camera": bad}))[0] == 400, bad
        assert seen == []
        code, j = await post("/api/camera", {"camera": "rtsp://192.168.1.20:8554/live"})
        assert code == 200 and j["status"]["camera"]["open"] and seen == ["rtsp://192.168.1.20:8554/live"]
        code, j = await post("/api/camera", {"camera": None})
        assert code == 200 and not j["status"]["camera"]["open"]

    run(sv, body)


def test_values_are_validated_here_and_a_moving_action_needs_a_confirmation(tmp_path):
    sv = make_serve(tmp_path)

    async def body(client, post, until):
        await open_camera(post)
        ok = {"confirm": True, "slot": 0, "throttle": 4}
        for bad in ({**ok, "confirm": False}, {k: v for k, v in ok.items() if k != "confirm"},  # no confirmation
                    {**ok, "throttle": 9}, {**ok, "throttle": 0}, {**ok, "throttle": 4.5}, {**ok, "throttle": True},  # cap 8
                    {**ok, "slot": 6}, {**ok, "slot": -1}, {**ok, "slot": "0"}, {**ok, "lanes": 3},
                    {**ok, "background": "x"}, {**ok, "marker_mm": 0}, {**ok, "marker_mm": 1e9}, {**ok, "latency_ms": -1}):
            code, j = await post("/api/map", bad)
            assert code == 400 and j["ok"] is False, bad
        assert (await post("/api/identify", {"confirm": True, "slots": [0]}))[0] == 400  # no map yet
        sv.map_path = "m.json"  # (validation only: nothing is started on a 400)
        for bad in ({"confirm": True, "slots": []}, {"confirm": True, "slots": [0, 0]}, {"confirm": True, "slots": [7]},
                    {"slots": [0]}, {"confirm": True}):
            assert (await post("/api/identify", bad))[0] == 400, bad
        for bad in ({"identify": True, "slots": [], "confirm": True}, {"identify": True, "slots": [0]}, {"latency_ms": 5000},
                    {"identify": "yes"}):
            assert (await post("/api/run", bad))[0] == 400, bad
        assert (await post("/api/record", {"duration": 0}))[0] == 400
        assert not sv.runner.busy and sv.status()["status"] == "idle"

    run(sv, body)


def test_a_second_action_gets_409_and_stop_works_after_a_reload(tmp_path):
    sv = make_serve(tmp_path)

    async def body(client, post, until):
        await open_camera(post)
        code, j = await post("/api/record", {})
        assert code == 200 and j["status"]["status"] == "recording"
        assert (await post("/api/record", {}))[0] == 409
        assert (await post("/api/map", {"confirm": True, "slot": 0}))[0] == 409
        assert (await post("/api/camera", {"camera": None}))[0] == 409  # the camera is in use
        st = await (await client.get("/status")).json()  # "after a reload": all the state is on the server
        assert st["action"] == "record" and st["status"] == "recording"
        code, _ = await post("/api/stop")
        assert code == 200
        await until(lambda: not sv.runner.busy)
        st = await (await client.get("/status")).json()
        assert st["status"] == "idle" and st["error"] is None and st["last"]["action"] == "record" and st["last"]["stopped"]
        assert (tmp_path).exists() and any(p.name.endswith("-record") for p in tmp_path.iterdir())
        assert (await post("/api/record", {}))[0] == 200  # free again
        await post("/api/stop")

    run(sv, body)


# ----------------------------------------------------------------------------- the connector card

def test_the_card_follows_the_status_and_is_cleared_on_disconnect(tmp_path):
    client_box = []

    def connect(url, token):
        c = FakeClient()
        client_box.append(c)
        return c

    sv = make_serve(tmp_path, card_connect=connect)

    async def body(client, post, until):
        await until(lambda: client_box and client_box[0].requests)
        op, args = client_box[0].requests[0]
        assert op == "connector.set" and args["name"] == "Camera (trackloc)" and args["status"] == "idle"
        assert args["ui_url"] == f"http://127.0.0.1:{sv.port}/?k={sv.token}" and len(args["ui_url"]) <= 300 and len(args["name"]) <= 40
        await open_camera(post)
        await post("/api/record", {})
        await until(lambda: client_box[0].requests[-1][1]["status"] == "recording")
        await post("/api/stop")
        await until(lambda: client_box[0].requests[-1][1]["status"] == "idle")
        seen = [r[1]["status"] for r in client_box[0].requests]
        assert seen[0] == "idle" and "recording" in seen and all(len(s) <= 80 for s in seen)

    run(sv, body)
    assert client_box[0].closed  # RaceLab clears the card when the connection closes


def test_an_old_racelab_gets_one_warning_and_the_page_keeps_working(tmp_path, caplog):
    box = []
    sv = make_serve(tmp_path, card_connect=lambda url, token: box.append(FakeClient(refuse="unknown command: connector.set")) or box[-1])

    async def body(client, post, until):
        with caplog.at_level(logging.WARNING, logger="trackloc.serve"):
            await until(lambda: sv.card.state == "old")
            await open_camera(post)
            await post("/api/record", {})
            await asyncio.sleep(0.3)
            await post("/api/stop")
        assert [r for r in caplog.records if "connector.set" in r.getMessage()].__len__() == 1
        assert (await (await client.get("/status")).json())["racelab"]["card"] == "old"

    run(sv, body)


def test_the_card_connects_when_racelab_starts_after_us(tmp_path):
    attempts = []
    ok = FakeClient()

    def connect(url, token):
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("not up yet")
        return ok

    sv = make_serve(tmp_path, card_connect=connect)

    async def body(client, post, until):
        await until(lambda: ok.requests)
        assert len(attempts) >= 3 and sv.card.state == "ok"

    run(sv, body)


# ----------------------------------------------------------------------------- the actions, and the safety of the car

@pytest.fixture(scope="module")
def disc():
    return bench.discovery(CAM, noise=1.0)


@pytest.fixture(scope="module")
def map_file(disc, tmp_path_factory):
    model = bench.map_from(disc)
    path = tmp_path_factory.mktemp("map") / "track_map.json"
    model.save(path)
    return str(path), model


def test_map_through_the_page_drives_records_builds_and_hands_the_car_back(tmp_path, disc):
    rp = Replay(disc)
    sv = make_serve(tmp_path, factory=lambda spec: rp.source, connect=lambda a: rp.link)

    async def body(client, post, until):
        await open_camera(post)
        code, j = await post("/api/map", {"confirm": True, "slot": 0, "throttle": 4, "lanes": 2, "marker_mm": 150, "latency_ms": 60})
        assert code == 200 and j["status"]["status"] == "mapping"
        st = await (await client.get("/status")).json()
        assert st["action"] == "map"
        await until(lambda: not sv.runner.busy, timeout=120)
        st = await (await client.get("/status")).json()
        assert st["error"] is None, st["log"]
        m = st["map"]
        assert m and 4.0 < m["length_m"] < 9.0 and len(m["lanes"]) == 2 and m["path"].endswith("track_map.json")
        assert st["last"]["action"] == "map" and st["last"]["ok"] and st["status"] == "idle"

    run(sv, body)
    link = rp.link
    assert link.handed_back(0) and link.closed
    assert ("claim", 0) in link.calls and max(c[2] or 0 for c in link.calls if c[0] == "act") <= 4 + 1  # never above throttle (+ a stall bump)
    assert any(c[0] == "act" and c[3] for c in link.calls)  # the lane changes were armed
    assert sv.map_path and sv.map_path.startswith(str(tmp_path))  # the map lives in the out dir, in its own folder


def test_a_stop_during_the_map_hands_the_car_back(tmp_path, disc):
    rp = Replay(disc, pace=0.02)
    sv = make_serve(tmp_path, factory=lambda spec: rp.source, connect=lambda a: rp.link)

    async def body(client, post, until):
        await open_camera(post)
        await post("/api/map", {"confirm": True, "slot": 0})
        await until(lambda: ("claim", 0) in rp.link.calls)
        await asyncio.sleep(0.5)
        assert (await post("/api/stop"))[0] == 200
        await until(lambda: not sv.runner.busy, timeout=30)
        st = await (await client.get("/status")).json()
        assert st["last"]["stopped"] and st["error"] is None and st["map"] is None

    run(sv, body)
    assert rp.link.handed_back(0) and rp.link.closed


def test_an_exception_inside_the_map_still_releases_the_car(tmp_path, disc, monkeypatch):
    from trackloc.live import Live

    rp = Replay(disc)
    calls = {"n": 0}
    real = Live.progress

    def boom(self):
        calls["n"] += 1
        if calls["n"] > 40:  # a bug in the middle of the lap, after the car was started
            raise ValueError("boom")
        return real(self)

    monkeypatch.setattr(Live, "progress", boom)
    statuses = []
    sv = make_serve(tmp_path, factory=lambda spec: rp.source, connect=lambda a: rp.link,
                    card_connect=lambda url, token: type("C", (FakeClient,), {"request": lambda s, op, /, **a: statuses.append(a["status"])})())

    async def body(client, post, until):
        await open_camera(post)
        await post("/api/map", {"confirm": True, "slot": 0})
        await until(lambda: not sv.runner.busy, timeout=60)
        st = await (await client.get("/status")).json()
        assert st["status"] == "error" and "boom" in st["error"] and st["map"] is None
        await until(lambda: statuses and statuses[-1] == "error")
        assert (await post("/api/record", {}))[0] == 200  # not stuck busy
        await post("/api/stop")

    run(sv, body)
    assert rp.link.handed_back(0) and rp.link.closed
    assert "mapping" in statuses and statuses[-1] in ("error", "idle", "recording")


def test_a_lost_link_during_the_map_cuts_the_car(tmp_path, disc):
    rp = Replay(disc)

    def die(link):
        if len(link.attempts) >= 6:  # every act fails from now on, the zero too: only RaceLab's dead-man switch can cut the car
            raise ConnectionError("link lost")

    rp.link.act_hook = die
    sv = make_serve(tmp_path, factory=lambda spec: rp.source, connect=lambda a: rp.link)

    async def body(client, post, until):
        await open_camera(post)
        await post("/api/map", {"confirm": True, "slot": 0})
        await until(lambda: not sv.runner.busy, timeout=60)
        st = await (await client.get("/status")).json()
        assert st["status"] == "error" and "taken from us" in st["error"]

    run(sv, body)
    link = rp.link
    assert link.attempts[-1] == 0 and ("unclaim", 0) in link.calls and link.closed  # the zero was tried, the slot given back


def test_closing_the_server_stops_and_joins_the_running_action(tmp_path, disc):
    """Ctrl-C of `serve`: the action's `finally` (throttle 0, unclaim) must have run before the process may exit."""
    rp = Replay(disc, pace=0.02)
    sv = make_serve(tmp_path, factory=lambda spec: rp.source, connect=lambda a: rp.link)

    async def body(client, post, until):
        await open_camera(post)
        await post("/api/map", {"confirm": True, "slot": 0})
        await until(lambda: ("claim", 0) in rp.link.calls)

    run(sv, body)  # run() calls shutdown() = stop + join, as run_serve does
    assert not sv.runner.busy and rp.link.handed_back(0) and rp.link.closed


@pytest.mark.parametrize("what", ["estop", "session"])
def test_identify_keeps_its_refusals_against_a_real_racelab(tmp_path, racelab_factory, map_file, disc, what):
    """E-STOP on, or a session running: the roll call refuses before it claims anything; nothing moves on the server."""
    from racelab.client import SyncClient

    path, _ = map_file
    server = racelab_factory("sim", time_scale=6.0)
    other = SyncClient.connect(server.url, agent="test")
    try:
        if what == "estop":
            other.estop()
        else:
            other.setup(type="practice")
            other.start()
        time.sleep(0.4)
        sv = make_serve(tmp_path, factory=lambda spec: FakeSource(image=disc.render(0)[1]),
                        extra=["--racelab-url", server.url])
        sv.map_path = path

        async def body(client, post, until):
            await open_camera(post)
            code, _ = await post("/api/identify", {"confirm": True, "slots": [0]})
            assert code == 200
            await until(lambda: not sv.runner.busy, timeout=60)
            st = await (await client.get("/status")).json()
            assert st["status"] == "error"
            assert ("E-STOP" in st["error"]) if what == "estop" else ("session" in st["error"])

        run(sv, body)
        bots = server.call(lambda: [(b.get("active"), b.get("applied")) for b in server.app.snapshot().get("bots", [])])
        assert not any(a for a, _ in bots) or all(not ap for _, ap in bots)  # no car claimed, none driven
    finally:
        other.close()


def test_map_against_a_racelab_without_a_cu_ends_in_an_error_and_moves_nothing(tmp_path, racelab_factory, disc):
    server = racelab_factory("none")
    sv = make_serve(tmp_path, factory=lambda spec: FakeSource(image=disc.render(0)[1]), extra=["--racelab-url", server.url])

    async def body(client, post, until):
        await open_camera(post)
        await post("/api/map", {"confirm": True, "slot": 0})
        await until(lambda: not sv.runner.busy, timeout=60)
        st = await (await client.get("/status")).json()
        assert st["status"] == "error" and st["error"] and st["map"] is None

    run(sv, body)
    bots = server.call(lambda: [(b.get("active"), b.get("applied")) for b in server.app.snapshot().get("bots", [])])
    assert not any(ap for _, ap in bots)


def test_identify_with_a_fake_link_names_the_parked_cars_or_reports_them(tmp_path, map_file, disc):
    path, _ = map_file
    link = FakeLink()
    sv = make_serve(tmp_path, factory=lambda spec: FakeSource(image=disc.render(0)[1]), connect=lambda a: link)
    sv.map_path = path

    async def body(client, post, until):
        await open_camera(post)
        code, _ = await post("/api/identify", {"confirm": True, "slots": [0, 1]})
        assert code == 200
        await until(lambda: not sv.runner.busy, timeout=90)
        st = await (await client.get("/status")).json()
        res = st["last"]["summary"]["results"]
        assert st["error"] is None and [r["slot"] for r in res] == [0, 1]  # nothing moves in a still picture: not identified
        assert not any(r["identified"] for r in res)

    run(sv, body)
    assert link.handed_back(0) and link.handed_back(1) and link.closed
    assert max((c[2] or 0) for c in link.calls if c[0] == "act") <= 5  # a pulse of 4, one retry step up, never above the cap 8


# ----------------------------------------------------------------------------- the page itself

def test_both_languages_have_the_same_messages_and_the_page_is_self_contained():
    m = sv_mod.messages()
    assert set(m["en"]) == set(m["fr"]) and all(m["fr"][k].strip() and m["en"][k].strip() for k in m["en"])
    html = sv_mod.page("NONCE", "fr")
    assert "http://" not in html.replace("http://...", "") and "https://" not in html and "<script src" not in html  # no CDN
    assert html.count("NONCE") == 2 and 'DEFAULT_LANG = "fr"' in html
    assert "{n}/15" in m["en"]["confirm_map"] and "{n}/15" in m["fr"]["confirm_map"] and "STOP" in m["en"]["confirm_map"]
    assert set(sv_mod.STATUS_OF.values()) | {"idle", "error"} == {k[3:] for k in m["en"] if k.startswith("st_")}
