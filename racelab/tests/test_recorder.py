"""SessionRecorder: round trip, autosave, listing, corrupted files."""

from __future__ import annotations

import json

import pytest

from racelab.race import SessionRecorder


def results(winner: str | None = "Carlos", elapsed: float = 83.2) -> dict:
    classification = [{"slot": 0, "name": winner, "position": 1}] if winner else []
    return {"session": {"elapsed_s": elapsed}, "classification": classification, "laps": []}


def meta(session_id: str, **extra) -> dict:
    return {"id": session_id, "type": "race", "title": "Race · 3 laps", **extra}


def test_creates_directory(tmp_path):
    target = tmp_path / "data" / "sessions"
    SessionRecorder(target)
    assert target.is_dir()


def test_roundtrip(tmp_path):
    rec = SessionRecorder(tmp_path)
    rec.begin(meta("20261010-171200-race", started="2026-10-10T17:12:00+02:00"))
    rec.record("lap", {"slot": 0, "ms": 5123}, 1.5)
    rec.record("finish", {"slot": 0, "position": 1}, 2.5)
    path = rec.end(results())
    assert path == tmp_path / "20261010-171200-race.json"
    doc = rec.load("20261010-171200-race")
    assert doc["meta"]["type"] == "race"
    assert doc["events"] == [
        {"t": 1.5, "name": "lap", "data": {"slot": 0, "ms": 5123}},
        {"t": 2.5, "name": "finish", "data": {"slot": 0, "position": 1}},
    ]
    assert doc["results"]["classification"][0]["name"] == "Carlos"
    assert json.loads(path.read_text())["meta"]["id"] == "20261010-171200-race"
    assert list(tmp_path.glob("*.tmp")) == []


def test_begin_adds_started_but_keeps_the_callers_value(tmp_path):
    rec = SessionRecorder(tmp_path)
    rec.begin({"id": "a"})
    rec.end(results())
    assert rec.load("a")["meta"]["started"]
    rec.begin({"id": "b", "started": "2026-01-01T00:00:00+00:00"})
    rec.end(results())
    assert rec.load("b")["meta"]["started"] == "2026-01-01T00:00:00+00:00"


def test_autosave_every_30_seconds_of_host_time(tmp_path):
    rec = SessionRecorder(tmp_path)
    rec.begin(meta("s1"))
    path = tmp_path / "s1.json"
    rec.record("lap", {"n": 1}, 100.0)
    rec.record("lap", {"n": 2}, 129.9)
    assert not path.exists()
    rec.record("lap", {"n": 3}, 130.0)
    assert path.exists()
    partial = json.loads(path.read_text())
    assert [e["data"]["n"] for e in partial["events"]] == [1, 2, 3]
    assert partial["results"] is None
    rec.record("lap", {"n": 4}, 150.0)  # < 30 s since the last save
    assert len(json.loads(path.read_text())["events"]) == 3
    rec.record("lap", {"n": 5}, 160.0)
    assert len(json.loads(path.read_text())["events"]) == 5
    rec.end(results())
    final = json.loads(path.read_text())
    assert len(final["events"]) == 5 and final["results"] is not None
    assert list(tmp_path.glob("*.tmp")) == []


def test_autosave_is_atomic_replace(tmp_path, monkeypatch):
    rec = SessionRecorder(tmp_path)
    rec.begin(meta("s1"))
    rec.record("a", {}, 0.0)
    rec.record("a", {}, 31.0)
    good = (tmp_path / "s1.json").read_text()

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr("racelab.race.recorder.os.replace", boom)
    rec.record("a", {}, 70.0)  # the write failure must not bubble up to the race
    assert (tmp_path / "s1.json").read_text() == good  # old file intact
    assert list(tmp_path.glob("*.tmp")) == []
    assert rec.end(results()) is None


def test_end_without_begin_and_record_without_begin(tmp_path):
    rec = SessionRecorder(tmp_path)
    rec.record("lap", {}, 1.0)
    assert rec.end(results()) is None
    assert list(tmp_path.iterdir()) == []


def test_new_begin_flushes_unfinished_session(tmp_path):
    rec = SessionRecorder(tmp_path)
    rec.begin(meta("old"))
    rec.record("lap", {}, 1.0)
    rec.begin(meta("new"))
    assert rec.load("old")["events"][0]["name"] == "lap"
    assert rec.load("old")["results"] is None
    rec.record("lap", {}, 2.0)
    rec.end(results())
    assert len(rec.load("new")["events"]) == 1


def test_list_newest_first_with_summary(tmp_path):
    rec = SessionRecorder(tmp_path)
    for sid, started, winner, elapsed in [
        ("20261010-100000-race", "2026-10-10T10:00:00+02:00", "Ana", 60.0),
        ("20261010-180000-practice", "2026-10-10T18:00:00+02:00", None, 12.5),
        ("20261009-090000-endurance", "2026-10-09T09:00:00+02:00", "Bob", 900.0),
    ]:
        rec.begin({"id": sid, "type": sid.split("-")[-1], "title": f"t-{sid}", "started": started})
        rec.end(results(winner, elapsed))
    listing = rec.list()
    assert [s["id"] for s in listing] == [
        "20261010-180000-practice",
        "20261010-100000-race",
        "20261009-090000-endurance",
    ]
    first = listing[0]
    assert first["type"] == "practice" and first["title"] == "t-20261010-180000-practice"
    assert first["started"] == "2026-10-10T18:00:00+02:00"
    assert first["duration_s"] == 12.5 and first["winner"] is None
    assert listing[1]["winner"] == "Ana" and listing[1]["winner_slot"] == 0
    assert listing[2]["path"] == str(tmp_path / "20261009-090000-endurance.json")
    json.dumps(listing)


def test_list_includes_unfinished_sessions(tmp_path):
    rec = SessionRecorder(tmp_path)
    rec.begin(meta("partial"))
    rec.record("a", {}, 0.0)
    rec.record("a", {}, 40.0)
    (summary,) = rec.list()
    assert summary["id"] == "partial" and summary["winner"] is None and summary["duration_s"] is None


def test_corrupted_files_are_skipped(tmp_path):
    rec = SessionRecorder(tmp_path)
    rec.begin(meta("good"))
    rec.end(results())
    (tmp_path / "broken.json").write_text("{not json")
    (tmp_path / "empty.json").write_text("")
    (tmp_path / "list.json").write_text("[1, 2]")
    (tmp_path / "nometa.json").write_text('{"events": []}')
    (tmp_path / "stale.json.tmp").write_text("{}")
    assert [s["id"] for s in rec.list()] == ["good"]
    with pytest.raises(ValueError):
        rec.load("broken")
    with pytest.raises(ValueError):
        rec.load("nometa")


def test_load_missing_and_unsafe_ids(tmp_path):
    rec = SessionRecorder(tmp_path)
    with pytest.raises(FileNotFoundError):
        rec.load("nope")
    for bad in ("../secret", "a/b", "", ".hidden"):
        with pytest.raises(ValueError):
            rec.load(bad)
    with pytest.raises(ValueError):
        rec.begin({"type": "race"})  # id is required
    with pytest.raises(ValueError):
        rec.begin({"id": "../x"})
