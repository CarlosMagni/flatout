"""Session recording: one JSON file per session.

```json
{"meta": {...}, "events": [{"t": 12.3, "name": "lap", "data": {...}}], "results": {...}}
```
The file is written at `end()` and, during the session, every 30 s of host
time (the `t` passed to `record`), always atomically (tmp + replace).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUTOSAVE_S = 30.0
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")  # used as a file name: no separator


def _check_id(session_id: Any) -> str:
    if not isinstance(session_id, str) or not _ID_RE.match(session_id):
        raise ValueError(f"Invalid session id: {session_id!r}")
    return session_id


class SessionRecorder:
    def __init__(self, directory: Path, *, autosave_s: float = AUTOSAVE_S) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._autosave_s = autosave_s
        self._meta: dict[str, Any] | None = None
        self._events: list[dict[str, Any]] = []
        self._last_save_t: float | None = None

    def begin(self, meta: dict[str, Any]) -> None:
        """Start recording; `meta["id"]` is required (file name)."""
        _check_id(meta.get("id"))
        if self._meta is not None:
            self._write(None)  # previous session not ended: keep what we have
        self._meta = {"started": datetime.now().astimezone().isoformat(timespec="seconds"), **meta}
        self._events = []
        self._last_save_t = None

    def record(self, name: str, data: dict[str, Any], t: float) -> None:
        if self._meta is None:
            return
        self._events.append({"t": t, "name": name, "data": data})
        if self._last_save_t is None:
            self._last_save_t = t
        elif t - self._last_save_t >= self._autosave_s:
            self._last_save_t = t
            self._write(None)

    def end(self, results: dict[str, Any]) -> Path | None:
        """Write the final file; None if there was no session or the write fails."""
        if self._meta is None:
            return None
        path = self._write(results)
        self._meta = None
        self._events = []
        self._last_save_t = None
        return path

    def list(self) -> list[dict[str, Any]]:
        """Session summaries, most recent first; unreadable files are ignored."""
        summaries = []
        for path in self._dir.glob("*.json"):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
                meta = doc["meta"]
                results = doc.get("results") or {}
                session = results.get("session") or {}
                classification = results.get("classification") or []
                winner = classification[0] if classification else None
                summaries.append(
                    {
                        "id": meta.get("id", path.stem),
                        "type": meta.get("type"),
                        "title": meta.get("title"),
                        "started": meta.get("started"),
                        "duration_s": session.get("elapsed_s"),
                        "winner": winner.get("name") if winner else None,
                        "winner_slot": winner.get("slot") if winner else None,
                        "path": str(path),
                    }
                )
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                logger.warning("session file ignored: %s", path)
        summaries.sort(key=lambda s: (str(s["started"]), s["id"]), reverse=True)
        return summaries

    def load(self, session_id: str) -> dict[str, Any]:
        """Full document of a session. FileNotFoundError if missing, ValueError if unreadable."""
        path = self._dir / f"{_check_id(session_id)}.json"
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"Unreadable session file: {path.name}") from e
        if not isinstance(doc, dict) or "meta" not in doc:
            raise ValueError(f"Unreadable session file: {path.name}")
        return doc

    def _write(self, results: dict[str, Any] | None) -> Path | None:
        assert self._meta is not None
        path = self._dir / f"{self._meta['id']}.json"
        tmp = path.with_suffix(".json.tmp")
        doc = {"meta": self._meta, "events": self._events, "results": results}
        try:
            tmp.write_text(json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.exception("cannot write session: %s", path)
            tmp.unlink(missing_ok=True)
            return None
        return path
