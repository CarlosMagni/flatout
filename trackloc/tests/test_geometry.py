"""Handedness, the marker, the map file, registration of a bumped camera."""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from trackloc import geometry as g  # noqa: E402


def test_left_of_the_driving_direction_in_image_coordinates():
    # on screen: right along the top edge, down the right edge, left along the bottom, up the left edge
    loop = np.array([[100.0, 100.0], [300.0, 100.0], [300.0, 200.0], [100.0, 200.0]])
    loop = g.resample_closed(loop, 200)
    above_top = np.array([[200.0, 90.0]])  # outside the loop, up the image: LEFT of a car driving right
    below_top = np.array([[200.0, 110.0]])  # inside: RIGHT of the car
    _, d1, s1 = g.closest_on_closed(loop, above_top)
    _, d2, s2 = g.closest_on_closed(loop, below_top)
    assert s1[0] > 0 > s2[0] and d1[0] == pytest.approx(10, abs=0.2) and d2[0] == pytest.approx(10, abs=0.2)
    assert np.allclose(g.left_normal(np.array([1.0, 0.0])), [0.0, -1.0])  # a car going right: its left is up the image


def test_lane_zero_is_the_left_lane():
    loop = g.resample_closed(np.array([[100.0, 100.0], [300.0, 100.0], [300.0, 200.0], [100.0, 200.0]]), 200)
    outer = g.resample_closed(np.array([[90.0, 90.0], [310.0, 90.0], [310.0, 210.0], [90.0, 210.0]]), 200)
    lanes = [g.Lane(outer, np.arange(200) / 200, 700.0, 0), g.Lane(loop, np.arange(200) / 200, 600.0, 1)]
    model = g.MapModel((400, 300), lanes, 650.0, None)
    # driving clockwise on screen, the outer lane is on the left: lane 0
    assert model.project(np.array([[200.0, 92.0]]))[0][0] == 0
    assert model.project(np.array([[200.0, 103.0]]))[0][0] == 1


def test_only_marker_id_0_counts():
    d = cv2.aruco.getPredefinedDictionary(g.ARUCO_DICT)
    page = np.full((400, 700), 255, np.uint8)
    page[100:260, 80:240] = cv2.aruco.generateImageMarker(d, 7, 160)  # another tag in view
    assert g.find_marker(page) is None
    page[100:260, 400:560] = cv2.aruco.generateImageMarker(d, g.MARKER_ID, 160)
    corners = g.find_marker(page)
    assert corners is not None and corners[:, 0].min() > 380  # the id-0 marker, not the id-7 one
    H = g.marker_homography(corners, 0.15)
    assert np.hypot(*(g.apply_h(H, corners)[1] - g.apply_h(H, corners)[0])) == pytest.approx(0.15, abs=1e-6)


def test_marker_pdf_is_a_valid_one_page_pdf_at_the_requested_size():
    pdf = g.marker_pdf(150.0)
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF") and b"/MediaBox" in pdf
    # 150 mm = 425.2 pt: the page content draws a square of that size
    assert b"425.197" in pdf


def test_map_json_round_trip(tmp_path):
    ang = np.linspace(0, 2 * np.pi, 120, endpoint=False)
    pts = np.column_stack([300 + 150 * np.cos(ang), 200 + 90 * np.sin(ang)])
    lane = g.Lane(pts, np.arange(120) / 120, 5.1, 0, np.full(120, 18.0))
    m = g.MapModel((640, 360), [lane], 5.1, np.eye(3) * 0.004, [0.3], [(0.6, 0.7)], {1: 0.0, 2: 0.5},
                   18.0, 0.1, 800.0, 0.02, 0.7, "marker", "test", {"x": 1}, np.zeros((36, 64, 3), np.uint8))
    m.save(tmp_path / "m.json")
    back = g.MapModel.load(tmp_path / "m.json")
    assert np.allclose(back.lanes[0].pts, pts) and back.blind == [(0.6, 0.7)] and back.anchors == {1: 0.0, 2: 0.5}
    assert back.background.shape == (36, 64, 3) and back.car_area_m2 == 0.02
    geo = back.geometry()  # the RaceLab wire format
    assert set(geo) >= {"points", "y_axis", "finish", "changers", "lane_offset", "name", "length_m"}
    assert geo["y_axis"] == "down" and geo["finish"] == 0.0 and geo["length_m"] == [5.1]


def test_registration_recovers_a_bumped_camera():
    rng = np.random.default_rng(3)
    base = cv2.GaussianBlur(rng.integers(0, 255, (360, 640, 3), dtype=np.uint8), (0, 0), 3)
    for _ in range(40):
        cv2.circle(base, tuple(int(v) for v in rng.integers(20, 600, 2)), int(rng.integers(6, 20)),
                   tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
    # the camera is bumped: a few pixels of shift, a small rotation, a bit of scale
    M = cv2.getRotationMatrix2D((320, 180), 1.5, 1.01)
    M[0, 2] += 9
    M[1, 2] -= 6
    frame = cv2.warpAffine(base, M, (640, 360), borderMode=cv2.BORDER_REFLECT)
    W, cc = g.register(base, frame)
    assert cc > 0.9
    p = np.array([[100.0, 100.0], [500.0, 300.0], [320.0, 40.0]])
    expected = (np.column_stack([p, np.ones(3)]) @ np.vstack([M, [0, 0, 1]]).T)[:, :2]
    assert np.abs(g.apply_h(W, p) - expected).max() < 1.0  # sub-pixel at 640x360
    with pytest.raises(g.RegistrationError):
        g.register(base, rng.integers(0, 255, (360, 640, 3), dtype=np.uint8))
