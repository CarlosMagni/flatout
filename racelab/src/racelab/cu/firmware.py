"""CU firmware update images (.HMF files): reading and validation.

The .HMF format is the one used by Carrera updates: one line per block, in
ASCII hexadecimal between quotes. Its content is encoded by Carrera: it can't
be read back as a binary, only its structure can be checked. The upload
procedure is the one in `carreralib.fw` (command `GB`, then one line per `E`
command; over BLE split into 18-byte `F` blocks).
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_FILE_BYTES = 512 * 1024
MIN_LINES, MAX_LINES = 16, 4000
MAX_LINE_CHARS = 256
_HEX_LINE = re.compile(rb"[0-9A-Fa-f]+")

# when the app runs from a checkout: firmware/manifest.json lists the images (tags, SHA-256);
# the images themselves are built from source into firmware/build/ (firmware/build.py)
FIRMWARE_ROOT = Path(__file__).resolve().parents[4] / "firmware"
PROJECT_FIRMWARE_DIR = FIRMWARE_ROOT / "build"
MANIFEST_PATH = FIRMWARE_ROOT / "manifest.json"


# Images are identified by the SHA-256 of the .HMF file, never by file name: a
# renamed file keeps its verdict, and "bot" in a name proves nothing.
@dataclass(frozen=True)
class KnownImage:
    tag: str  # e.g. "v339-bot.5" (firmware/manifest.json)
    label: str
    status: str  # "recommended" | "stock" | "superseded" | "defective"
    bots: bool  # carries the bot module (B command)
    issue: str | None = None  # i18n key suffix for the UI (fw.issue.<issue>)
    warning: str | None = None


# never flashable, even without the manifest (installed package, edited file)
_DEFECTIVE = {
    "35f452f432cd1ee2443c7103d4ec49a3c91cdd2ac8cab247a40b1bb06bec71e6": KnownImage(
        "v339-bot.2", "V339 + bot module (bot.2)", "defective", True, "event_ring",
        "faulty bot build: its state overlaps the firmware's event ring (0x00E9), corrupting "
        "the throttle sent to the cars and the status frames — do not flash"),
    "2e21d0c8ebcd8c6b433dea05328bd9d94e253329b1a0fb6e3847f773efbf1a79": KnownImage(
        "v339-bot.1", "V339 + bot module (bot.1)", "defective", True, "hook_throttle",
        "faulty bot hook: the throttle overwrites the controller word address, so the bots' "
        "cars do not stop — do not flash"),
    "1657008f732b8bda2e3f1a6f2807b52e59a71b356c9151cf9f6f80768f7f56a4": KnownImage(
        "v339-bot.4", "V339 + bot module (bot.4)", "defective", True, "slot_state",
        "faulty bot build: its state sits in firmware RAM (slot status and button press bytes), so "
        "bot control flickers and throttle values are garbage — do not flash"),
    "0ee3eb1a6753dc45e4a09f58b56acca8d0e526c5cf44c9f1fbcca0976ca62c9d": KnownImage(
        "v339-bot.3", "V339 + bot module (bot.3)", "defective", True, "slot_state",
        "faulty bot build: its state sits in firmware RAM (slot status and button press bytes), so "
        "bot control flickers and throttle values are garbage — do not flash"),
}
STATUS_ORDER = {"recommended": 0, "stock": 1, "superseded": 2, "unknown": 3, "defective": 4}


def load_known_images(manifest: Path) -> dict[str, KnownImage]:
    """SHA-256 -> verdict, from firmware/manifest.json plus the built-in defective list."""
    try:
        entries = json.loads(manifest.read_text())["images"]
    except (OSError, ValueError, KeyError, TypeError):
        entries = []
    known = {}
    for e in entries:
        try:
            known[e["sha256"]] = KnownImage(e["tag"], e["label"], e["status"], bool(e["bots"]),
                                            e.get("issue"), e.get("warning"))
        except (KeyError, TypeError):
            continue
    return known | _DEFECTIVE


KNOWN_IMAGES: dict[str, KnownImage] = load_known_images(MANIFEST_PATH)


def missing_images(build_dir: Path = PROJECT_FIRMWARE_DIR, manifest: Path = MANIFEST_PATH) -> list[str]:
    """Tags of the buildable images (manifest entries with a file) not built yet."""
    try:
        entries = json.loads(manifest.read_text())["images"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [e["tag"] for e in entries if e.get("file") and not (build_dir / e["file"]).is_file()]


def can_build(root: Path = FIRMWARE_ROOT) -> bool:
    return (root / "build.py").is_file()


def build_images(root: Path = FIRMWARE_ROOT, timeout: float = 1800.0) -> tuple[bool, str]:
    """Build the images from source (firmware/build.py: pinned toolchain, Docker when needed)."""
    try:
        r = subprocess.run([sys.executable, str(root / "build.py")], capture_output=True, text=True,
                           timeout=timeout, cwd=root.parent)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


class ImageError(ValueError):
    pass


@dataclass
class FirmwareImage:
    name: str
    lines: list[bytes] = field(repr=False)
    sha256: str
    size: int
    source: str  # "project" | "imported"
    path: str | None = None

    @property
    def id(self) -> str:
        return self.sha256[:16]

    @property
    def lines_digest(self) -> str:
        """Digest of the lines sent (what the CU receives, quotes removed)."""
        return hashlib.sha256(b"\n".join(self.lines)).hexdigest()

    @property
    def known(self) -> KnownImage | None:
        return KNOWN_IMAGES.get(self.sha256)

    @property
    def label(self) -> str:
        return self.known.label if self.known else Path(self.name).stem

    def info(self) -> dict:
        known = self.known
        return {"id": self.id, "name": self.name, "label": self.label, "sha256": self.sha256,
                "size": self.size, "lines": len(self.lines), "source": self.source,
                "tag": known.tag if known else None,
                "status": known.status if known else "unknown",
                "issue": known.issue if known else None,
                "warning": known.warning if known else None}


def parse_hmf(data: bytes, name: str = "image.HMF", *, source: str = "imported",
              path: str | None = None) -> FirmwareImage:
    """Check the structure of an .HMF and return its lines, ready to send.

    Like `carreralib.fw`: quotes removed, empty lines ignored.
    """
    if len(data) > MAX_FILE_BYTES:
        raise ImageError(f"file too large ({len(data) // 1024} KB) for a CU image")
    if not data.strip():
        raise ImageError("empty file")
    if b"\x00" in data:
        raise ImageError("binary file: the .HMF (text) is needed, not the .bin")
    lines = []
    for number, raw in enumerate(data.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if not (line.startswith(b'"') and line.endswith(b'"') and len(line) >= 2):
            raise ImageError(f"line {number}: quotes expected (.HMF format)")
        line = line[1:-1]
        if not _HEX_LINE.fullmatch(line):
            raise ImageError(f"line {number}: non-hexadecimal characters")
        if len(line) % 2 or len(line) > MAX_LINE_CHARS:
            raise ImageError(f"line {number}: unexpected length ({len(line)} characters)")
        lines.append(line.upper())
    if not MIN_LINES <= len(lines) <= MAX_LINES:
        raise ImageError(f"{len(lines)} lines: not a CU image")
    body = {len(line) for line in lines[:-1]}
    if len(body) != 1:
        raise ImageError("inconsistent line lengths: truncated or modified file?")
    return FirmwareImage(name=name, lines=lines, sha256=hashlib.sha256(data).hexdigest(),
                         size=len(data), source=source, path=path)


def load_image(path: Path, source: str) -> FirmwareImage:
    return parse_hmf(path.read_bytes(), path.name, source=source, path=str(path))


class FirmwareLibrary:
    """Available images: the ones built from source (firmware/build) and the imported ones."""

    def __init__(self, upload_dir: Path, project_dir: Path = PROJECT_FIRMWARE_DIR):
        self.upload_dir = upload_dir
        self.project_dir = project_dir

    def images(self) -> list[FirmwareImage]:
        found: dict[str, FirmwareImage] = {}
        for directory, source in ((self.project_dir, "project"), (self.upload_dir, "imported")):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.[Hh][Mm][Ff]")):
                try:
                    image = load_image(path, source)
                except (ImageError, OSError):
                    continue
                found.setdefault(image.id, image)
        return sorted(found.values(), key=lambda i: STATUS_ORDER[i.info()["status"]])

    def get(self, image_id: str) -> FirmwareImage:
        for image in self.images():
            if image.id == image_id:
                return image
        raise ImageError("image not found (deleted?)")

    def add(self, data: bytes, name: str) -> FirmwareImage:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name) or "image.HMF"
        if not safe.lower().endswith(".hmf"):
            safe += ".HMF"
        image = parse_hmf(data, safe)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        path = self.upload_dir / f"{image.sha256[:8]}-{safe}"
        path.write_bytes(data)
        image.name, image.path, image.source = path.name, str(path), "imported"
        return image

    def bot_line_digests(self) -> set[str]:
        """Digests of the known bot images (the simulator uses them for its restart)."""
        return {i.lines_digest for i in self.images() if i.known and i.known.bots}
