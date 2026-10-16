"""`--camera`: an index, a part of the device name, a stream URL, or a question in a terminal."""

from __future__ import annotations

import pytest

pytest.importorskip("cv2")

from trackloc import sources
from trackloc.sources import resolve_camera

DEVICES = {0: "Caméra du MacBook Air", 1: "Caméra de « iPhone de Charles »", 2: "Caméra Desk View du MacBook Air"}


@pytest.fixture(autouse=True)
def devices(monkeypatch):
    monkeypatch.setattr(sources, "ffmpeg_devices", lambda: dict(DEVICES))


def test_an_index_is_kept():
    assert resolve_camera("1") == 1 and resolve_camera(" 2 ") == 2 and resolve_camera(0) == 0


def test_a_part_of_the_name_picks_the_device_whatever_the_case():
    assert resolve_camera("iphone") == 1 and resolve_camera("DESK VIEW") == 2


def test_an_ambiguous_or_unknown_name_says_which_cameras_exist():
    with pytest.raises(OSError, match="several cameras"):
        resolve_camera("macbook")  # the built-in camera and Desk View
    with pytest.raises(OSError, match="iPhone de Charles"):
        resolve_camera("webcam")


def test_a_stream_url_is_opened_as_is():
    assert resolve_camera("rtsp://192.168.1.20:8554/live") == "rtsp://192.168.1.20:8554/live"
    assert resolve_camera("http://phone.local:8080/video") == "http://phone.local:8080/video"


def test_no_camera_asks_in_a_terminal_and_refuses_otherwise(capsys):
    assert resolve_camera(None, ask=lambda prompt: "iphone") == 1
    assert "[1] Caméra de « iPhone de Charles »" in capsys.readouterr().out  # the list is shown before asking
    with pytest.raises(OSError, match="no camera chosen"):
        resolve_camera(None)  # not a terminal (a script): never block on a question
    with pytest.raises(OSError, match="no camera chosen"):
        resolve_camera(None, ask=lambda prompt: "")
