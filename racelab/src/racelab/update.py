"""In-app update (System → Update): is a newer RaceLab on GitHub, and installing it.

A clone updates with git, fast-forward only, so local work is never touched. A download (the
repository's zip, no .git) gets the zip of the latest commit over its folder, its .venv kept, and
the commit it now holds is written to VERSION. Then the installer runs again (dependencies may have
changed) and the app restarts (__main__). On Windows the installer runs only once the app has exited,
since Windows locks a running program's files (`relaunch`, run as `python -m racelab.update`).
"""

from __future__ import annotations

import io
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

REPO = "CarlosMagni/flatout"
BRANCH = "main"
ROOT = Path(__file__).resolve().parents[3]  # racelab/src/racelab/update.py -> the repository
VERSION = ROOT / "VERSION"
TIMEOUT_S = 15


def _git(*args: str, timeout: float = 60) -> str:
    out = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=timeout)
    if out.returncode:
        raise RuntimeError((out.stderr or out.stdout).strip() or f"git {args[0]} failed")
    return out.stdout.strip()


def _latest_commit() -> str:
    req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
                                 headers={"Accept": "application/vnd.github.sha"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
        return r.read().decode().strip()


def check() -> dict[str, Any]:
    """{method: git|zip, current, latest, available}; raises when offline. A download from before
    VERSION existed has no known commit: an update is offered."""
    if (ROOT / ".git").exists():
        _git("fetch", "-q", "origin", BRANCH)
        behind = int(_git("rev-list", "--count", f"HEAD..origin/{BRANCH}"))
        return {"method": "git", "current": _git("rev-parse", "HEAD"),
                "latest": _git("rev-parse", f"origin/{BRANCH}"), "available": behind > 0}
    latest = _latest_commit()
    current = VERSION.read_text().strip() if VERSION.exists() else None
    return {"method": "zip", "current": current, "latest": latest, "available": current != latest}


def apply() -> str:
    """Install the latest version and return its commit; the app must restart to run it."""
    if (ROOT / ".git").exists():
        _git("pull", "--ff-only", "-q", "origin", BRANCH, timeout=120)
        sha = _git("rev-parse", "HEAD")
    else:
        sha = _latest_commit()
        with urllib.request.urlopen(f"https://github.com/{REPO}/archive/{sha}.zip", timeout=120) as r:
            extract(r.read(), ROOT)
        VERSION.write_text(sha + "\n")
    if sys.platform != "win32":  # Windows: in relaunch(), once this process is gone
        install()
    return sha


def install() -> None:
    out = subprocess.run([sys.executable, str(ROOT / "install.py"), "--no-launcher"],
                         capture_output=True, text=True, timeout=900)
    if out.returncode:
        raise RuntimeError("the installer failed: " + (out.stdout + out.stderr).strip()[-300:])


def relaunch(old_pid: int, command: list[str]) -> int:
    """Windows, after an update: once the old RaceLab has exited, the installer, then RaceLab again.
    It starts even if the installer failed (the error goes to update.log next to install.py)."""
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x00100000, False, old_pid)  # SYNCHRONIZE
    if handle:
        kernel32.WaitForSingleObject(handle, 30_000)
        kernel32.CloseHandle(handle)
    try:
        install()
    except (OSError, RuntimeError, subprocess.SubprocessError) as e:
        (ROOT / "update.log").write_text(str(e), encoding="utf-8")
    return subprocess.call(command)


def extract(data: bytes, root: Path) -> None:
    """GitHub's zip of the repository over `root`: its top folder stripped, nothing outside `root`
    nor in .venv. Files removed upstream stay (harmless: nothing imports them any more)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            parts = Path(info.filename).parts[1:]
            if info.is_dir() or not parts or ".." in parts or parts[0] == ".venv":
                continue
            dest = root.joinpath(*parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(z.read(info))


if __name__ == "__main__":  # python -m racelab.update <old pid> <command that starts RaceLab...>
    sys.exit(relaunch(int(sys.argv[1]), sys.argv[2:]))
