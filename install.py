#!/usr/bin/env python3
"""Install RaceLab on macOS, Windows or Linux.

    macOS    double-click "Install RaceLab.command"   (or: python3 install.py)
    Windows  double-click "Install RaceLab.bat"       (or: py install.py)
    Linux    python3 install.py

Creates the Python environment in `.venv`, installs RaceLab into it, then a
shortcut: RaceLab.app (macOS), Desktop + Start menu shortcuts (Windows), an entry
in the applications menu (Linux). Safe to run again. Standard library only,
never needs administrator rights.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
TOOLS = ROOT / "tools"
WINDOWS = sys.platform == "win32"


def say(text: str = "") -> None:
    print(text, flush=True)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if WINDOWS else "bin/python")


def venv_works() -> bool:
    py = venv_python()
    if not py.exists():
        return False
    try:
        return subprocess.run([str(py), "-c", "import sys"], capture_output=True).returncode == 0
    except OSError:
        return False


def ensure_venv() -> Path:
    if venv_works():
        say("  existing environment reused")
    else:
        if VENV.exists():
            say("  unusable environment (folder moved?): recreated")
        venv.EnvBuilder(with_pip=True, clear=VENV.exists()).create(VENV)
        say("  environment created")
    return venv_python()


def pip(py: Path, *args: str) -> None:
    subprocess.run([str(py), "-m", "pip", "install", "-q", *args], check=True)


# ------------------------------------------------------------ icon

def icon_png(size: int) -> bytes:
    sys.path.insert(0, str(TOOLS))
    try:
        from make_mac_app import render_png  # same icon as RaceLab.app
    finally:
        sys.path.pop(0)
    return render_png(size)


def write_ico(dest: Path) -> Path:
    """ICO with PNG images (Windows Vista and later)."""
    sizes = (16, 32, 48, 256)
    images = [icon_png(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for size, png in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    dest.write_bytes(header + entries + data)
    return dest


# ------------------------------------------------------------ shortcuts

def launcher_macos(py: Path) -> str:
    subprocess.run([str(py), str(TOOLS / "make_mac_app.py")], check=True)
    return (f"RaceLab.app created in {ROOT}\n"
            "  drag it to the Dock or to Applications (an alias is enough)")


def _ps(path: Path) -> str:
    """Path as a single-quoted PowerShell literal."""
    return str(path).replace("'", "''")


def launcher_windows(py: Path) -> str:
    pythonw = VENV / "Scripts" / "pythonw.exe"  # no console window
    ico = write_ico(ROOT / ".venv" / "RaceLab.ico")
    script = (
        "$ws = New-Object -ComObject WScript.Shell\n"
        "foreach ($dir in @([Environment]::GetFolderPath('Desktop'), "
        "[Environment]::GetFolderPath('Programs'))) {\n"
        "  $s = $ws.CreateShortcut((Join-Path $dir 'RaceLab.lnk'))\n"
        f"  $s.TargetPath = '{_ps(pythonw)}'\n"
        "  $s.Arguments = '-m racelab --window'\n"
        f"  $s.WorkingDirectory = '{_ps(ROOT)}'\n"
        f"  $s.IconLocation = '{_ps(ico)}'\n"
        "  $s.Description = 'RaceLab'\n"
        "  $s.Save()\n"
        "}\n"
    )
    subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script], check=True)
    return 'Shortcuts "RaceLab" created on the Desktop and in the Start menu'


def launcher_linux(py: Path) -> str:
    share = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    icon = share / "icons" / "hicolor" / "256x256" / "apps" / "racelab.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    icon.write_bytes(icon_png(256))
    desktop = share / "applications" / "racelab.desktop"
    desktop.parent.mkdir(parents=True, exist_ok=True)
    desktop.write_text(
        "[Desktop Entry]\nType=Application\nName=RaceLab\n"
        "Comment=Race management and software drivers for Carrera Digital tracks\n"
        f'Exec="{VENV / "bin" / "racelab"}" --window\nPath={ROOT}\nIcon={icon}\n'
        "Terminal=false\nCategories=Game;Utility;\n", encoding="utf-8")
    desktop.chmod(0o755)
    notes = [f'"RaceLab" entry added to the applications menu ({desktop})']
    groups = subprocess.run(["id", "-nG"], capture_output=True, text=True).stdout.split()
    if "dialout" not in groups:
        notes.append("  serial cable: add yourself to the dialout group (sudo usermod -aG dialout $USER), "
                     "then log in again")
    return "\n".join(notes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install RaceLab and its launch shortcut.")
    parser.add_argument("--no-launcher", action="store_true", help="install without creating a shortcut")
    args = parser.parse_args()

    say("=== Installing RaceLab ===")
    say(f"Folder: {ROOT}")
    if sys.version_info < (3, 11):
        say(f"Python {sys.version.split()[0]} is too old: Python 3.11 or newer is required "
            "(https://www.python.org/downloads/).")
        return 1
    try:
        say("\n1/3 Python environment")
        py = ensure_venv()
        say("\n2/3 Installing RaceLab (1 to 2 minutes)")
        pip(py, "--upgrade", "pip")
        pip(py, "-e", str(ROOT / "carreralib"), "-e", str(ROOT / "racelab"))
        say("  installed")
        if args.no_launcher:
            return 0
        say("\n3/3 Launch shortcut")
        if sys.platform == "darwin":
            summary = launcher_macos(py)
        elif WINDOWS:
            summary = launcher_windows(py)
        else:
            summary = launcher_linux(py)
    except subprocess.CalledProcessError as e:
        say(f"\nFailed: {' '.join(map(str, e.cmd[:4]))}… (code {e.returncode}). "
            "Check your Internet connection, then run the installation again.")
        return 1
    say(f"  {summary}")
    say("\nAll set. On first launch, choose your CU on the start screen.")
    say("Bluetooth: accept the permission if the system asks for it.")
    browsers = ("google-chrome", "chromium", "chromium-browser", "microsoft-edge", "brave-browser")
    if sys.platform.startswith("linux") and not any(shutil.which(b) for b in browsers):
        say("Tip: with Chrome, Chromium, Edge or Brave, RaceLab opens in its own window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
