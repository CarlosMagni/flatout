"""In-app update (racelab/update.py): the zip over the folder, the version check, the ops."""

from __future__ import annotations

import io
import zipfile

from racelab import update

from .test_api import make_config, op, run, started, stopped, wait_until


def zip_of(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_extract_strips_the_top_folder_and_keeps_venv_and_outside_alone(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "keep").write_text("mine")
    data = zip_of({"flatout-abc/racelab/x.py": b"new", "flatout-abc/.venv/keep": b"theirs",
                   "flatout-abc/../evil": b"no"})
    update.extract(data, tmp_path / "")
    assert (tmp_path / "racelab" / "x.py").read_bytes() == b"new"
    assert (tmp_path / ".venv" / "keep").read_text() == "mine"
    assert not (tmp_path.parent / "evil").exists()


def test_a_download_compares_its_version_file_with_github(tmp_path, monkeypatch):
    monkeypatch.setattr(update, "ROOT", tmp_path)
    monkeypatch.setattr(update, "VERSION", tmp_path / "VERSION")
    monkeypatch.setattr(update, "_latest_commit", lambda: "b" * 40)
    assert update.check()["available"] is True                  # no VERSION yet: unknown, offered
    (tmp_path / "VERSION").write_text("b" * 40 + "\n")
    assert update.check() == {"method": "zip", "current": "b" * 40, "latest": "b" * 40, "available": False}


def test_update_ops(tmp_path, monkeypatch):
    applied, fail, link_alive, apps = [], [], [], []

    def apply():
        link_alive.append(apps[0].link._thread.is_alive())  # when the download starts
        if fail:
            raise RuntimeError("GitHub out of reach")
        applied.append(1)
        return "c" * 40

    monkeypatch.setattr(update, "check", lambda: {"method": "zip", "current": None, "latest": "c" * 40,
                                                  "available": True})
    monkeypatch.setattr(update, "apply", apply)

    async def main():
        app, client = await started(make_config(tmp_path))
        apps.append(app)
        try:
            assert (await op(client, "update.check"))["result"]["available"] is True
            refused = await op(client, "update.apply")                    # no launcher to restart it
            assert refused["ok"] is False and not applied
            quit_calls = []
            app.request_quit = lambda: quit_calls.append(1)
            assert (await op(client, "race.setup", settings={"type": "practice"}))["ok"]
            assert (await op(client, "race.start"))["ok"]
            busy = await op(client, "update.apply")                       # never during a session
            assert busy["ok"] is False and "session" in busy["error"] and not applied
            assert (await op(client, "race.stop"))["ok"]
            fail.append(1)
            old_link = app.link
            failed = await op(client, "update.apply")                     # the CU was let go, then back
            assert failed["ok"] is False and "GitHub" in failed["error"] and app.restart is False
            assert app.link is not old_link
            assert await wait_until(lambda: app.link_info["state"] == "connected")
            fail.clear()
            r = await op(client, "update.apply")
            assert r["ok"] and r["result"] == {"version": "c" * 40} and app.restart is True
            assert link_alive == [False, False]                                # the CU let go before
        finally:
            await stopped(app, client)

    run(main())
