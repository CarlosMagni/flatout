#!/usr/bin/env python3
"""Builds RaceLab.app (double-click launcher) at the project root.

    python3 tools/make_mac_app.py [--root DIR] [--out DIR] [--iconset DIR]

Stdlib only + the macOS tools `iconutil` and `plutil`. Idempotent: each run
replaces the previous app. The absolute path of the project root is baked into
the launcher; if the folder has moved, the launcher falls back to the folder
that contains the app.
"""

from __future__ import annotations

import argparse
import math
import os
import plistlib
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from itertools import accumulate
from pathlib import Path

APP_NAME = "RaceLab"
BUNDLE_ID = "local.racelab.app"
BLUETOOTH_USAGE = "RaceLab connects to the Carrera track's AppConnect over Bluetooth."

# --------------------------------------------------------------------------
# Icon: near-black rounded square + off-white 2×2 checkerboard (racelab/web/icon.svg)
# --------------------------------------------------------------------------

DARK = (0x0B, 0x0C, 0x0E)
LIGHT = (0xEC, 0xEB, 0xE6)
SS = 4  # supersampling per axis, downscaling by box average
BODY = 824 / 1024  # Apple icon grid: body = 824 px out of 1024 (~10 % margin)
# Geometry in SVG units (viewBox 32×32): background rx=7, checkerboard 8..24, stroke 1.5 centered on 8..24
RADIUS = 7.0
OUTLINE = (7.25, 24.75, 8.75, 23.25)  # outer min/max, inner min/max
CELLS = ((8.0, 16.0, 8.0, 16.0), (16.0, 24.0, 16.0, 24.0))  # x0, x1, y0, y1


def _merge(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _light_units(y: float) -> list[tuple[float, float]]:
    """x intervals (SVG units) covered by the light part at height y."""
    lo, hi, in_lo, in_hi = OUTLINE
    if not lo <= y < hi:
        return []
    if y < in_lo or y >= in_hi:
        return [(lo, hi)]
    iv = [(lo, in_lo), (in_hi, hi)]
    iv += [(x0, x1) for x0, x1, y0, y1 in CELLS if y0 <= y < y1]
    return _merge(iv)


def _to_samples(x0: float, x1: float, n: int) -> tuple[int, int]:
    """Subpixels whose center falls within [x0, x1] (pixel coordinates)."""
    a = max(0, math.ceil(x0 * SS - 0.5))
    b = min(n * SS, math.floor(x1 * SS - 0.5) + 1)
    return a, max(a, b)


def _subrow(n: int, ys: int) -> tuple[tuple[int, int], tuple[tuple[int, int], ...]]:
    """Body and light part, as subpixel intervals, for subrow ys."""
    body = n * BODY
    m = (n - body) / 2
    u = body / 32
    r = RADIUS * u
    y = (ys + 0.5) / SS
    dy = y - m
    if dy < 0 or dy > body:
        return (0, 0), ()
    edge = min(dy, body - dy)
    inset = r - math.sqrt(max(0.0, r * r - (r - edge) ** 2)) if edge < r else 0.0
    base = _to_samples(m + inset, m + body - inset, n)
    light = tuple(_to_samples(m + a * u, m + b * u, n) for a, b in _light_units(dy / u))
    return base, light


def _row_bytes(n: int, subrows) -> bytes:
    diff_b = [0] * (n * SS + 1)
    diff_l = [0] * (n * SS + 1)
    for (b0, b1), lights in subrows:
        if b1 > b0:
            diff_b[b0] += 1
            diff_b[b1] -= 1
        for a, b in lights:
            if b > a:
                diff_l[a] += 1
                diff_l[b] -= 1
    cb = list(accumulate(diff_b))
    cl = list(accumulate(diff_l))
    out = bytearray()
    total = SS * SS
    for x in range(n):
        s = x * SS
        nb = sum(cb[s:s + SS])
        if nb == 0:
            out += b"\0\0\0\0"
            continue
        nl = sum(cl[s:s + SS])
        nd = nb - nl
        for c in range(3):  # non-premultiplied color, weighted average over the covered subpixels
            out.append(int((nd * DARK[c] + nl * LIGHT[c]) / nb + 0.5))
        out.append(int(nb * 255 / total + 0.5))
    return bytes(out)


def render_png(n: int) -> bytes:
    """RGBA n×n PNG of the icon."""
    cache: dict = {}
    raw = bytearray()
    for y in range(n):
        subrows = tuple(_subrow(n, y * SS + k) for k in range(SS))
        row = cache.get(subrows)
        if row is None:
            row = cache[subrows] = _row_bytes(n, subrows)
        raw += b"\x00" + row  # "None" filter

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


ICONSET = (  # file name -> size in pixels
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
)


def write_iconset(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    pngs = {n: render_png(n) for n in sorted({n for _, n in ICONSET})}
    for name, n in ICONSET:
        (directory / name).write_bytes(pngs[n])


def make_icns(dest: Path, keep_iconset: Path | None = None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = keep_iconset or Path(tmp) / f"{APP_NAME}.iconset"
        if iconset.suffix != ".iconset" and keep_iconset is not None:
            iconset = iconset / f"{APP_NAME}.iconset"
        write_iconset(iconset)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(dest)], check=True)


# --------------------------------------------------------------------------
# Launcher
# --------------------------------------------------------------------------

LAUNCHER = r'''#!/bin/bash
# RaceLab.app: launcher generated by tools/make_mac_app.py (rebuild rather than edit).
# Starts RaceLab in the background, waits for it to answer, then returns control: no
# icon stays in the Dock. Reopening RaceLab.app while it is running only
# reopens the window (the CLI's single-instance check).
#
# Environment variables (tests, advanced use):
#   RACELAB_NO_UI=1       no window or dialog box (messages go to stderr)
#   RACELAB_PORT=NNNN     HTTP port (otherwise the one in racelab.toml, otherwise 8130)
#   RACELAB_DATA_DIR=DIR  sessions and settings (default ~/.racelab; the log is there too)

BAKED_ROOT=@ROOT@
export PYTHONUNBUFFERED=1

alert() {  # native dialog box; the text is passed as an argument (no escaping needed)
  if [ "${RACELAB_NO_UI:-}" = "1" ]; then
    printf 'RaceLab: %s\n' "$1" >&2
  else
    osascript -e 'on run argv' \
      -e 'display dialog (item 1 of argv) with title "RaceLab" buttons {"OK"} default button "OK" with icon caution' \
      -e 'end run' -- "$1" >/dev/null 2>&1
  fi
}

# --- project root: path baked in at build time, otherwise the folder that contains the app
HERE="$(cd "$(dirname "$0")" && pwd)"            # .../RaceLab.app/Contents/MacOS
REL_ROOT="$(cd "$HERE/../../.." 2>/dev/null && pwd)"
ROOT=""
for cand in "$BAKED_ROOT" "$REL_ROOT"; do
  if [ -n "$cand" ] && [ -x "$cand/.venv/bin/racelab" ]; then ROOT="$cand"; break; fi
done
if [ -z "$ROOT" ]; then
  alert "RaceLab is not installed yet: double-click \"Install RaceLab.command\" in the carrera-app folder."
  exit 1
fi
cd "$ROOT" || exit 1   # so that the project's ./racelab.toml is taken into account

# --- port to watch: RACELAB_PORT, otherwise [app] port in racelab.toml, otherwise 8130
PORT="${RACELAB_PORT:-}"
if [ -z "$PORT" ]; then
  PORT="$("$ROOT/.venv/bin/python3" -I -c '
import pathlib, tomllib
for p in (pathlib.Path("racelab.toml"), pathlib.Path("~/.racelab/racelab.toml").expanduser()):
    if p.is_file():
        print(tomllib.loads(p.read_text()).get("app", {}).get("port", 8130))
        break
' 2>/dev/null)"
fi
case "$PORT" in ''|*[!0-9]*) PORT=8130 ;; esac

# --- arguments
ARGS=()
if [ "${RACELAB_NO_UI:-}" = "1" ]; then ARGS+=(--no-browser); else ARGS+=(--window); fi
[ -n "${RACELAB_PORT:-}" ] && ARGS+=(--port "$RACELAB_PORT")
[ -n "${RACELAB_DATA_DIR:-}" ] && ARGS+=(--data-dir "$RACELAB_DATA_DIR")

# --- log
DATA_DIR="${RACELAB_DATA_DIR:-$HOME/.racelab}"
DATA_DIR="${DATA_DIR/#\~/$HOME}"
mkdir -p "$DATA_DIR" || exit 1
LOG="$DATA_DIR/racelab.log"

up()    { curl -sf --max-time 1 "http://localhost:$PORT/api/meta" >/dev/null 2>&1; }
alive() { local s; s="$(ps -o stat= -p "$1" 2>/dev/null)"; [ -n "$s" ] && [ "${s#Z}" = "$s" ]; }

# bounded log: truncate only if no instance is writing to it
if ! up && [ "$(stat -f %z "$LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
  tail -n 2000 "$LOG" >"$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi
printf '\n--- %s: launching RaceLab.app (port %s) ---\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$PORT" >>"$LOG"
START_BYTES="$(wc -c <"$LOG" | tr -d ' ')"

# --- detached start: survives the end of this launcher
nohup "$ROOT/.venv/bin/racelab" "${ARGS[@]}" </dev/null >>"$LOG" 2>&1 &
PID=$!
disown "$PID" 2>/dev/null

# --- wait (15 s max) for the server to answer
i=0
while [ "$i" -lt 30 ]; do
  up && exit 0
  if ! alive "$PID"; then
    up && exit 0   # another instance took over (the CLI just reopened the window)
    break
  fi
  sleep 0.5
  i=$((i + 1))
done

if alive "$PID"; then
  alert "RaceLab is not responding after 15 seconds (port $PORT)."$'\n\n'"Log: $LOG"
else
  TAIL="$(tail -c +"$((START_BYTES + 1))" "$LOG" 2>/dev/null | tail -n 15 | cut -c1-300)"
  alert "RaceLab stopped at startup."$'\n\n'"$TAIL"$'\n\n'"Full log: $LOG"
fi
exit 1
'''


def read_version(root: Path) -> str:
    init = root / "racelab" / "src" / "racelab" / "__init__.py"
    m = re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", init.read_text(encoding="utf-8"), re.M)
    if not m:
        sys.exit(f"make_mac_app: __version__ not found in {init}")
    return m.group(1)


def info_plist(version: str) -> bytes:
    return plistlib.dumps({
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIconFile": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": version,
        "CFBundleVersion": version,
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        # without this key, macOS kills the process on the first CoreBluetooth access
        "NSBluetoothAlwaysUsageDescription": BLUETOOTH_USAGE,
    })


def build(root: Path, out: Path, keep_iconset: Path | None = None) -> Path:
    version = read_version(root)
    app = out / f"{APP_NAME}.app"
    stage = out / f".{APP_NAME}-build.app"
    shutil.rmtree(stage, ignore_errors=True)
    macos, resources = stage / "Contents" / "MacOS", stage / "Contents" / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    try:
        plist = stage / "Contents" / "Info.plist"
        plist.write_bytes(info_plist(version))
        subprocess.run(["plutil", "-lint", str(plist)], check=True, stdout=subprocess.DEVNULL)

        launcher = macos / APP_NAME
        launcher.write_text(LAUNCHER.replace("@ROOT@", shlex.quote(str(root))), encoding="utf-8")
        launcher.chmod(0o755)

        make_icns(resources / f"{APP_NAME}.icns", keep_iconset)
        (resources / "README.txt").write_text(
            f"RaceLab.app is a simple launcher: the project (venv, code, data) is in {root}\n",
            encoding="utf-8")
        (stage / "Contents" / "PkgInfo").write_bytes(b"APPL????")

        if app.exists():
            shutil.rmtree(app)
        os.rename(stage, app)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    os.utime(app)  # this makes Finder refresh the icon
    return app


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Build RaceLab.app (double-click launcher).")
    p.add_argument("--root", type=Path, help="project root (default: parent folder of tools/)")
    p.add_argument("--out", type=Path, help="folder to write RaceLab.app into (default: the root)")
    p.add_argument("--iconset", type=Path, metavar="DIR", help="also keep the icon PNGs here")
    args = p.parse_args(argv)
    if sys.platform != "darwin" or not (shutil.which("iconutil") and shutil.which("plutil")):
        sys.exit("make_mac_app: macOS required (iconutil and plutil).")
    root = (args.root or Path(__file__).resolve().parents[1]).resolve()
    if not (root / "racelab" / "src" / "racelab" / "__init__.py").is_file():
        sys.exit(f"make_mac_app: {root} is not the project root (racelab/src/racelab/ not found).")
    out = (args.out or root).resolve()
    out.mkdir(parents=True, exist_ok=True)
    app = build(root, out, args.iconset)
    print(f"{app} built (RaceLab {read_version(root)}, project: {root})")


if __name__ == "__main__":
    main()
