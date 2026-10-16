"""`python -m trackloc preview`: the annotated frame, the marker status, and the read-only localhost page.

The frames are the synthetic camera of `trackloc.synth` (a printed marker lies on the table); the server is run
for real on 127.0.0.1 (port picked by the OS), fed by a FileSource video, and queried with http.client.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import re
import subprocess
import sys
import threading
import time

import numpy as np
import pytest

pytest.importorskip("cv2")
pytest.importorskip("aiohttp")

import cv2  # noqa: E402

from trackloc import preview as pv  # noqa: E402
from trackloc.geometry import Lane, MapModel  # noqa: E402
from trackloc.synth import CameraSpec, Scenario, Session, SynthCar  # noqa: E402


@pytest.fixture(scope="module")
def sess():
    return Session(Scenario(cars=[SynthCar(0, "bot", 0, 0.10, [(0.2, 9)])], duration=8.0, camera=CameraSpec(width=1280, height=720)))


@pytest.fixture(scope="module")
def frame(sess):
    return sess.render(0)[1]


def ellipse_map(size=(800, 450), rx=0.40, ry=0.38, cx=0.5, cy=0.5) -> MapModel:
    w, h = size
    a = np.linspace(0, 2 * np.pi, 120, endpoint=False)
    lanes = []
    for lane_id, k in ((0, 1.0), (1, 0.93)):
        pts = np.column_stack([w * (cx + rx * k * np.cos(a)), h * (cy + ry * k * np.sin(a))])
        lanes.append(Lane(pts, a / (2 * np.pi), 5.0, lane_id))
    return MapModel(size, lanes, 5.0, None)


# ----------------------------------------------------------------------------- marker status and overlay

def test_the_marker_is_found_on_a_synthetic_frame_and_measured(sess, frame):
    st = pv.marker_status(frame)
    assert st["found"] and st["hint"] is None and st["side_px"] > 48
    truth = sess.cam.project(np.column_stack([sess.marker_corners_world(), np.zeros(4)]))
    assert np.abs(np.array(st["corners"]) - truth).max() < 3.0  # the outline is where the marker really is
    small = cv2.resize(frame, (480, 270), interpolation=cv2.INTER_AREA)
    st2 = pv.marker_status(small, scale=480 / 1280)
    assert st2["found"] and abs(st2["side_px"] - st["side_px"]) < 6  # sizes are reported in the capture's pixels
    assert pv.marker_status(small, scale=480 / 1280, min_side_px=500)["hint"] == "marker_small"


def test_no_marker_in_view_is_reported(frame):
    st = pv.marker_status(np.full((450, 800, 3), 120, np.uint8))
    assert st == {"found": False, "corners": None, "side_px": None, "hint": None}
    noise = np.random.default_rng(1).integers(0, 255, (450, 800, 3), dtype=np.uint8)
    assert not pv.marker_status(noise)["found"]


def test_the_overlay_outlines_the_marker_and_writes_the_status(frame):
    p = pv.Preview(None, "en")
    out, st = p.process(frame, fps=29.6)
    assert out.shape == frame.shape and st["width"] == 1280 and st["height"] == 720 and st["fps"] == 29.6
    assert st["marker"]["found"] and st["coverage"]["hint"] == "cov_none" and st["motion"]["learning"]
    corner = np.array(st["marker"]["corners"][0]).round().astype(int)
    patch = out[corner[1] - 4:corner[1] + 5, corner[0] - 4:corner[0] + 5].reshape(-1, 3)
    assert (np.abs(patch.astype(int) - pv.GREEN).sum(1) < 60).any()  # the green outline passes through the marker's corner
    assert out[:70, :260].mean() < frame[:70, :260].mean() * 0.6  # the dark bar behind the text
    blank, st2 = p.process(np.full((450, 800, 3), 120, np.uint8))
    assert not st2["marker"]["found"] and st2["width"] == 800
    red = np.abs(blank.astype(int) - pv.RED).sum(2) < 100
    assert red.sum() > 50  # "marker not found" is written in red


def test_lane_lines_and_coverage_hints_from_the_map():
    img = np.full((450, 800, 3), 100, np.uint8)
    out, st = pv.Preview(ellipse_map(), "en").process(img)
    assert st["map"] and st["coverage"]["hint"] == "cov_ok" and st["coverage"]["cut"] == []
    assert (np.abs(out.astype(int) - pv.YELLOW).sum(2) < 80).sum() > 200 and (np.abs(out.astype(int) - pv.CYAN).sum(2) < 80).sum() > 200
    assert pv.Preview(ellipse_map(rx=0.15, ry=0.2), "en").process(img)[1]["coverage"]["hint"] == "cov_small"
    cut = pv.Preview(ellipse_map(cx=0.8), "en").process(img)[1]["coverage"]
    assert cut["hint"] == "cov_cut" and cut["cut"] == ["right"]
    big = pv.Preview(ellipse_map(size=(640, 360)), "en").process(img)[1]  # a map made at another resolution is scaled
    assert big["coverage"]["hint"] == "cov_ok"


def test_motion_is_outlined_and_a_global_change_is_flagged():
    m = pv.Motion()
    rng = np.random.default_rng(2)
    base = np.full((360, 640, 3), 90, np.uint8)
    for _ in range(pv.Motion.WARMUP + 5):
        assert m.update(np.clip(base + rng.normal(0, 1.5, base.shape), 0, 255).astype(np.uint8))["contours"] == []
    hand = base.copy()
    hand[100:160, 200:300] = 230
    r = m.update(hand)
    assert not r["learning"] and not r["global_change"] and len(r["contours"]) == 1
    x, y, w, h = cv2.boundingRect(r["contours"][0])
    assert abs(x - 200) < 12 and abs(y - 100) < 12 and abs(w - 100) < 20
    pts = m.seen_points((640, 360))
    assert pts is not None and pts[0][0] <= 205 and pts[1][0] >= 295
    r = m.update(np.clip(base + 70, 0, 255).astype(np.uint8))  # exposure hunting: everything changes
    assert r["global_change"] and r["contours"] == []


def test_coverage_status_edges_and_fill():
    pts = np.array([[5.0, 100.0], [797.0, 300.0]])
    assert pv.coverage_status(pts, (800, 450), "map")["cut"] == ["left", "right"]
    assert pv.coverage_status(np.array([[100.0, 100.0], [700.0, 300.0]]), (800, 450), "map")["hint"] == "cov_ok"
    assert pv.coverage_status(np.array([[300.0, 150.0], [500.0, 250.0]]), (800, 450), "map")["hint"] == "cov_small"
    assert pv.coverage_status(None, (800, 450), "map")["hint"] == "cov_none"
    assert pv.coverage_status(pts, (800, 450), "motion")["hint"] == "cov_motion"  # moved-so-far is information, not a verdict


# ----------------------------------------------------------------------------- texts

def test_english_and_french_have_the_same_messages_and_placeholders():
    en, fr = pv.MESSAGES["en"], pv.MESSAGES["fr"]
    assert set(en) == set(fr)
    ph = lambda s: set(re.findall(r"\{(\w+)", s))  # noqa: E731
    assert all(ph(en[k]) == ph(fr[k]) for k in en)
    for lang in ("en", "fr"):
        text = pv.checklist_text(lang)
        assert text.count("[ ]") == 6
    assert "AE/AF LOCK" in pv.checklist_text("en") and "30 or 60 fps" in pv.checklist_text("en")
    assert "AE/AF" in pv.checklist_text("fr") and "30 ou 60" in pv.checklist_text("fr")
    assert pv.ascii_fold("Verrouillage caméra « OK »").isascii()


def test_the_french_picture_is_drawn_without_unprintable_accents(frame):
    out, st = pv.Preview(None, "fr").process(frame, fps=30.0)
    assert st["marker"]["found"] and out.shape == frame.shape
    assert "marqueur OK" in pv.status_line(st, "fr") and "marker OK" in pv.status_line(st, "en")


# ----------------------------------------------------------------------------- the server

@pytest.fixture(scope="module")
def video(tmp_path_factory, sess):
    path = tmp_path_factory.mktemp("vid") / "synth.avi"
    w = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 15.0, (1280, 720))
    for k in range(0, 120, 3):
        w.write(sess.render(k)[1])
    w.release()
    return str(path)


class Running:
    """The preview with its frame feeder, analysis thread and HTTP server, on 127.0.0.1 with a free port."""

    def __init__(self, video):
        self.preview = pv.Preview(None, "en")
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.host = self.port = None
        for fn, args in ((pv.feed_video, (self.preview, video, self.stop)), (self.preview.run, (self.stop,)), (self.serve, ())):
            threading.Thread(target=fn, args=args, daemon=True).start()
        assert self.ready.wait(10), "the server did not start"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (self.preview.status().get("fps") is None or self.preview.frames < 8):
            time.sleep(0.05)

    def serve(self):
        def ready(host, port):
            self.host, self.port = host, port
            self.ready.set()

        asyncio.run(pv.serve(self.preview, 0, self.stop, ready))

    def get(self, path="/", method="GET", headers=None, read=None):
        c = http.client.HTTPConnection(self.host, self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        c.request(method, path, headers=h)
        r = c.getresponse()
        body = r.read(read) if read else r.read()
        c.close()
        return r, body


@pytest.fixture(scope="module")
def server(video):
    s = Running(video)
    yield s
    s.stop.set()
    time.sleep(0.3)


def test_the_server_listens_on_loopback_only_and_serves_the_page(server):
    assert server.host == "127.0.0.1" and pv.LOCALHOST == "127.0.0.1"
    import inspect

    assert "host" not in inspect.signature(pv.serve).parameters  # no way to expose it on another address
    r, body = server.get("/")
    html = body.decode()
    assert r.status == 200 and r.getheader("Content-Type").startswith("text/html")
    assert "/stream" in html and "Aim the camera" in html and "Viser la" in html and "AE/AF LOCK" in html  # both languages
    nonce = re.search(r"nonce-([\w-]+)", r.getheader("Content-Security-Policy")).group(1)
    assert html.count(f'nonce="{nonce}"') == 2  # the inline style and script carry this response's nonce, nothing else may run
    assert "frame-ancestors 'none'" in r.getheader("Content-Security-Policy") and "default-src 'none'" in r.getheader("Content-Security-Policy")
    assert r.getheader("Access-Control-Allow-Origin") is None and r.getheader("Cross-Origin-Resource-Policy") == "same-origin"
    assert r.getheader("X-Content-Type-Options") == "nosniff" and r.getheader("Set-Cookie") is None


def test_status_reports_the_marker_resolution_and_a_measured_fps(server):
    r, body = server.get("/status")
    st = json.loads(body)
    assert r.status == 200 and r.getheader("Content-Type").startswith("application/json")
    assert st["marker"]["found"] and st["width"] == 1280 and st["height"] == 720
    assert st["fps"] and 5 < st["fps"] < 40  # the feeder plays the 15 fps video in real time
    assert st["coverage"]["hint"] in ("cov_none", "cov_motion") and set(st["motion"]) == {"blobs", "global", "learning"}


def test_stream_is_an_mjpeg_of_annotated_frames(server):
    c = http.client.HTTPConnection(server.host, server.port, timeout=10)
    c.request("GET", "/stream", headers={"Host": f"127.0.0.1:{server.port}"})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Content-Type") == f"multipart/x-mixed-replace; boundary={pv.BOUNDARY}"
    head = r.read(400)
    assert head.startswith(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ")
    n = int(re.search(rb"Content-Length: (\d+)", head).group(1))
    body = head.split(b"\r\n\r\n", 1)[1]
    body += r.read(n - len(body))
    img = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[1] == 1280
    c.close()


def test_get_only_and_no_cross_site_use(server):
    for method in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
        r, _ = server.get("/", method)
        assert r.status == 405 and r.getheader("Allow") == "GET", method
    assert server.get("/stream", "POST")[0].status == 405
    assert server.get("/nothing")[0].status == 404
    # DNS rebinding: a name that resolves to 127.0.0.1 is not a loopback Host
    assert server.get("/status", headers={"Host": "evil.example"})[0].status == 421
    assert server.get("/status", headers={"Host": f"evil.example:{server.port}"})[0].status == 421
    assert server.get("/status", headers={"Host": f"localhost:{server.port}"})[0].status == 200
    # a page of another site: the browser says so
    assert server.get("/status", headers={"Sec-Fetch-Site": "cross-site"})[0].status == 403
    assert server.get("/stream", headers={"Sec-Fetch-Site": "same-site"})[0].status == 403
    assert server.get("/status", headers={"Origin": "http://evil.example"})[0].status == 403
    assert server.get("/status", headers={"Sec-Fetch-Site": "same-origin", "Origin": f"http://127.0.0.1:{server.port}"})[0].status == 200
    assert server.get("/", headers={"Sec-Fetch-Site": "none"})[0].status == 200  # typing the address in the browser


def test_nothing_in_the_preview_can_move_a_car():
    code = ("import sys, trackloc.preview; "
            "bad = [m for m in ('trackloc.drive', 'trackloc.link_racelab', 'trackloc.identify', 'racelab.client') if m in sys.modules]; "
            "print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "[]"


# ----------------------------------------------------------------------------- snapshots

def test_snapshots_write_an_annotated_jpeg_without_a_browser(video, tmp_path):
    p = pv.Preview(None, "en")
    stop = threading.Event()
    for fn, args in ((pv.feed_video, (p, video, stop)), (p.run, (stop,))):
        threading.Thread(target=fn, args=args, daemon=True).start()
    lines: list[str] = []
    path = tmp_path / "aim.jpg"
    try:
        while p.frames < 6:
            time.sleep(0.05)
        n = pv.run_snapshots(p, str(path), stop, period=0.05, count=2, out=lines.append)
    finally:
        stop.set()
    img = cv2.imread(str(path))
    assert n == 2 and img is not None and img.shape == (720, 1280, 3) and not (tmp_path / "aim.jpg.tmp").exists()
    assert any("marker OK" in line and "1280x720" in line for line in lines)


def test_the_command_writes_snapshots_and_prints_the_checklist(video, tmp_path, capsys):
    from trackloc.__main__ import main

    out = tmp_path / "aim.jpg"
    assert main(["preview", "--video", video, "--snapshot", str(out), "--duration", "2.5", "--lang", "fr"]) == 0
    text = capsys.readouterr().out
    assert "Check-list caméra" in text and text.count("[ ]") == 6 and "marqueur OK" in text
    assert "écriture de" in text and "snapshot" not in text.split("écriture de")[0]  # the terminal lines follow --lang too
    assert cv2.imread(str(out)).shape == (720, 1280, 3)
