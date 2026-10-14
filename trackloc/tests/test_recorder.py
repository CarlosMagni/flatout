"""Recorder, reader and the live == replay guarantee (spec item f)."""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

pytest.importorskip("cv2")

from trackloc import bench  # noqa: E402
from trackloc.live import Live, Stream  # noqa: E402
from trackloc.pipeline import Pipeline, PipelineConfig, run_stream  # noqa: E402
from trackloc.recorder import JsonlSink, Recorder, RecordingReader, load_inputs  # noqa: E402
from trackloc.synth import CameraSpec  # noqa: E402

SMALL = CameraSpec(width=640, height=360)


def test_round_trip_is_lossless_with_ffv1_and_keeps_the_stream_order(tmp_path):
    rng = np.random.default_rng(0)
    frames = [rng.integers(0, 255, (72, 96, 3), dtype=np.uint8) for _ in range(12)]
    rec = Recorder(tmp_path / "r", fps=30.0, size=(96, 72), codec="ffv1", meta={"x": 1})
    seq = 0
    for i, img in enumerate(frames):
        seq += 1
        assert rec.log_frame(seq, 100.0 + i / 30, 100.0 + i / 30 + 0.016, img)
        if i % 4 == 3:
            seq += 1
            rec.log_input(seq, {"type": "crossing", "t": 100.0 + i / 30 + 0.02, "slot": 1, "sector": 1, "cu_ts": 1000 + i * 33, "recv": 1.0})
    seq += 1
    rec.log_input(seq, {"type": "throttle", "t": 101.0, "slot": 1, "applied": 7})
    rec.mark("empty_start")
    rec.log_raw("event", "lap", {"slot": 1})
    rec.close()
    rd = RecordingReader(tmp_path / "r")
    assert rd.meta["x"] == 1 and rd.meta["frames"] == 12 and rd.meta["codec"] == "ffv1" and rd.meta["clock"]["events"] == 3
    got = list(rd.items())
    kinds = [k for k, _ in got]
    assert kinds == ["frame"] * 4 + ["input"] + ["frame"] * 4 + ["input"] + ["frame"] * 4 + ["input"] + ["input"]
    assert all(np.array_equal(a, p[1]) for a, p in zip(frames, (p for k, p in got if k == "frame")))  # bit-exact
    assert [m["type"] for k, m in got if k == "input"] == ["crossing", "crossing", "crossing", "throttle"]
    assert rd.marks("empty_start") and (tmp_path / "r" / "racelab.jsonl").read_text().count("lap") == 1
    assert load_inputs(tmp_path / "r")[0]["cu_ts"] == 1000 + 3 * 33


def test_a_torn_last_line_after_a_crash_is_ignored(tmp_path):
    rec = Recorder(tmp_path / "r", fps=30.0, size=(32, 24))
    rec.log_frame(1, 1.0, 1.0, np.zeros((24, 32, 3), np.uint8))
    rec.log_input(2, {"type": "crossing", "t": 1.0, "slot": 0, "cu_ts": 5})
    rec.close()
    with open(tmp_path / "r" / "inputs.jsonl", "a") as f:
        f.write('{"type": "crossing", "t": 2.0, "sl')  # power cut
    assert len(RecordingReader(tmp_path / "r").inputs()) == 1


def test_clock_offset_estimate_is_stored(tmp_path):
    rec = Recorder(tmp_path / "r", fps=30.0, size=(32, 24))
    for k in range(10):
        rec.log_input(k + 1, {"type": "crossing", "t": 50.0 + k + (0.01 if k == 4 else 0.08), "slot": 0, "cu_ts": 1000 * k})
    rec.close()
    off = json.loads((tmp_path / "r" / "meta.json").read_text())["clock"]["offset_s"]
    assert off == pytest.approx(50.0 + 4 + 0.01 - 4.0, abs=1e-6)  # host - cu at the least delayed event


def test_a_full_writer_queue_drops_the_frame_and_says_so(tmp_path):
    rec = Recorder(tmp_path / "r", fps=30.0, size=(640, 360), queue_frames=1)
    img = np.zeros((360, 640, 3), np.uint8)
    results = [rec.log_frame(i, float(i), float(i), img) for i in range(200)]
    rec.close()
    assert not all(results) and rec.dropped == results.count(False)
    assert len(RecordingReader(tmp_path / "r").frame_log) == results.count(True)  # the log matches the video


def test_stream_drops_frames_when_the_consumer_lags_but_never_inputs():
    s = Stream(max_frames_backlog=3)
    img = np.zeros((4, 4, 3), np.uint8)
    ok = [s.put_frame(float(i), float(i), img) for i in range(10)]
    for i in range(5):
        s.put_input({"type": "crossing", "t": float(i), "slot": 0, "cu_ts": i})
    assert ok == [True] * 3 + [False] * 7 and s.dropped == 7
    kinds = [s.get(0.1)[1] for _ in range(8)]
    assert kinds.count("input") == 5 and kinds.count("frame") == 3


def test_jsonl_sink(tmp_path):
    sink = JsonlSink(tmp_path / "s.jsonl")
    sink.set_track({"points": [[0, 0]], "y_axis": "down"})
    sink.publish(1.5, [{"slot": 0, "s": 0.5, "lane": 0, "v": 1.0, "off": False}])
    sink.close()
    lines = [json.loads(x) for x in (tmp_path / "s.jsonl").read_text().splitlines()]
    assert lines[0]["type"] == "track" and lines[1]["cars"][0]["s"] == 0.5


@pytest.fixture(scope="module")
def small_setup():
    disc = bench.discovery(SMALL, noise=1.0)
    model = bench.map_from(disc)
    sess = bench.race(SMALL, duration=11.0, hands=(5.0,), seed=51, noise=1.0)
    return model, sess


def test_f_replay_of_a_recording_equals_the_in_memory_run_exactly(small_setup, tmp_path):
    model, sess = small_setup
    direct = Pipeline(model, PipelineConfig(latency_s=bench.LATENCY))
    run_stream(direct, sess.stream())
    bench.write_recording(sess, tmp_path / "rec", codec="ffv1")
    replay = Pipeline(model, PipelineConfig(latency_s=bench.LATENCY))
    rd = RecordingReader(tmp_path / "rec")
    run_stream(replay, rd.items())
    assert len(direct.log) == len(replay.log) > 250
    assert direct.log == replay.log  # every position of every car, every frame: identical floats
    assert direct.tracker.crossings == replay.tracker.crossings
    assert direct.clock.offset == replay.clock.offset and direct.clock.offset is not None


def test_f_the_live_machinery_logs_what_it_processes_and_replays_identically(small_setup, tmp_path):
    """Capture thread + inputs -> Stream -> one worker -> recorder + pipeline; then replay the recording."""
    model, sess = small_setup
    rec = Recorder(tmp_path / "live", fps=30.0, size=(640, 360), codec="ffv1")
    pipe = Pipeline(model, PipelineConfig(latency_s=bench.LATENCY))
    live = Live(recorder=rec, pipeline=pipe)
    live.start()

    def feed():
        for kind, payload in sess.stream():
            if kind == "frame":
                t, img = payload
                # a lagging worker or encoder would drop frames (consistently: not logged, not processed);
                # wait instead, so that the test sees the whole session. FFV1 is slower than real time here.
                while live.stream._frames_pending >= 3 or rec._q.qsize() > 10:
                    time.sleep(0.001)
                live.on_frame(t, t + 0.5 / 30, img)
            else:
                live.on_input(payload)

    th = threading.Thread(target=feed)
    th.start()
    th.join(120)
    live.stop()
    rec.close()
    assert live.error is None and live.frames > 250
    replay = Pipeline(model, PipelineConfig(latency_s=bench.LATENCY))
    run_stream(replay, RecordingReader(tmp_path / "live").items())
    assert len(pipe.log) == len(replay.log) == live.frames
    assert pipe.log == replay.log
    assert live.crossing_count(0) >= 1


def test_f_with_a_lossy_codec_the_replay_is_close_not_identical(small_setup, tmp_path):
    model, sess = small_setup
    direct = Pipeline(model, PipelineConfig(latency_s=bench.LATENCY))
    run_stream(direct, sess.stream())
    bench.write_recording(sess, tmp_path / "mjpg", codec="mjpg")
    lossy = Pipeline(model, PipelineConfig(latency_s=bench.LATENCY))
    run_stream(lossy, RecordingReader(tmp_path / "mjpg").items())
    L = model.length_c
    diffs = [abs(wrap_(a["s"] - b["s"]) * L) for ra, rb in zip(direct.log, lossy.log) for a in ra["cars"] for b in rb["cars"] if a["slot"] == b["slot"]]
    assert diffs and np.percentile(diffs, 95) < 0.02  # MJPG moves positions by millimetres, not centimetres


def wrap_(d):
    return (d + 0.5) % 1.0 - 0.5
