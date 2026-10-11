#!/usr/bin/env python3
"""Pre-publication safeguard: no Carrera material in what would be pushed.

Checks the files tracked by git AND those that `git add -A` would add.
Fails (exit code 1) on: any firmware image file (.hmf/.bin/.hex/.elf: images
are built from source into firmware/build/, never committed), a disassembly
listing, the official app, or the CU firmware signature outside firmware/ (the
reconstruction's sources legitimately contain the version string).

    python tools/check_public.py
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_SUFFIXES = {".hmf", ".bin", ".hex", ".elf", ".apk", ".o"}
DISASSEMBLY_SUFFIX = ".s"  # case-sensitive: .s = generated listing, .S = hand-written stubs
FORBIDDEN_DIRS = ("assets_www/", "app_carrera", "ghidra/")
FIRMWARE_DIR = "firmware/"  # the only place where images and the signature may appear
# bot builds that must never be flashed (see KNOWN_IMAGES in racelab/cu/firmware.py)
DEFECTIVE_SHA256 = {
    "2e21d0c8ebcd8c6b433dea05328bd9d94e253329b1a0fb6e3847f773efbf1a79",
    "35f452f432cd1ee2443c7103d4ec49a3c91cdd2ac8cab247a40b1bb06bec71e6",
}
SIGNATURES = (b"BBLACKB", b"digital_blackbox")  # identification string of the CU firmware
MAX_SCAN = 4 * 1024 * 1024


def candidates() -> list[str]:
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True)
        return [line for line in out.stdout.splitlines() if line]

    try:
        return sorted(set(git("ls-files") + git("ls-files", "--others", "--exclude-standard")))
    except (OSError, subprocess.CalledProcessError):
        sys.exit("check_public: this folder is not a git repository (run git init first)")


def problems(path: str) -> list[str]:
    found = []
    p = Path(path)
    in_firmware = path.startswith(FIRMWARE_DIR)
    if p.suffix.lower() in FORBIDDEN_SUFFIXES or p.suffix == DISASSEMBLY_SUFFIX:
        found.append(f"forbidden extension {p.suffix}")
    if any(path.startswith(d) or f"/{d}" in path for d in FORBIDDEN_DIRS):
        found.append("forbidden folder")
    full = ROOT / path
    if full.is_file() and full.stat().st_size <= MAX_SCAN and path != "tools/check_public.py":
        data = full.read_bytes()
        if hashlib.sha256(data).hexdigest() in DEFECTIVE_SHA256:
            found.append("known-defective firmware image")
        if not in_firmware:
            found += [f"signature {s.decode()}" for s in SIGNATURES if s in data]
    return found


def main() -> int:
    files = candidates()
    bad = {f: why for f in files if (why := problems(f))}
    if bad:
        print("PUBLICATION BLOCKED — files to remove:")
        for f, why in bad.items():
            print(f"  {f}  ({', '.join(why)})")
        return 1
    print(f"OK: {len(files)} files checked, no Carrera material.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
