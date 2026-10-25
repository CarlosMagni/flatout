"""CU firmware update: .HMF images, simulated bootloader, end-to-end flash."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from racelab.api.server import create_web_app
from racelab.app import RaceLabApp
from racelab.config import Config, SimConfig
from racelab.cu import link as link_mod
from racelab.cu import firmware as firmware_mod
from racelab.cu.firmware import (KNOWN_IMAGES, PROJECT_FIRMWARE_DIR, FirmwareLibrary, ImageError,
                                 KnownImage, parse_hmf)
from racelab.cu.unit import RaceControlUnit
from racelab.sim import Simulator
from racelab.sim import virtual_cu

MANIFEST = firmware_mod.MANIFEST_PATH  # firmware/manifest.json; images are built into firmware/build


def synthetic_hmf(lines: int = 897) -> bytes:
    """An image in .HMF format (hex lines between quotes, last one shorter), with no Carrera content."""
    rng = random.Random(339)
    body = [b"%084X" % rng.getrandbits(336) for _ in range(lines - 1)] + [b"%020X" % rng.getrandbits(80)]
    return b"".join(b'"' + line + b'"\r\n' for line in body)


SYNTHETIC = synthetic_hmf()
SYNTHETIC_SHA = hashlib.sha256(SYNTHETIC).hexdigest()


@pytest.fixture(autouse=True)
def synthetic_is_known_bot(monkeypatch):
    """Images are recognised by SHA-256 only: register the synthetic one as a bot build."""
    monkeypatch.setitem(firmware_mod.KNOWN_IMAGES, SYNTHETIC_SHA,
                        KnownImage("test-bot.1", "synthetic bot build", "recommended", bots=True))


@pytest.fixture(autouse=True)
def fast_timings(monkeypatch):
    monkeypatch.setattr(link_mod, "BOOTLOADER_WAIT_S", 0.05)
    monkeypatch.setattr(link_mod, "REBOOT_WAIT_S", 0.1)
    monkeypatch.setattr(virtual_cu, "REBOOT_S", 0.05)


def hmf(lines):
    return b"".join(b'"' + line + b'"\r\n' for line in lines)


def test_parse_synthetic_image():
    image = parse_hmf(SYNTHETIC, "renamed.HMF")
    assert len(image.lines) == 897 and len(image.lines[-1]) == 20
    assert image.label == "synthetic bot build" and image.info()["status"] == "recommended"


def test_name_proves_nothing():
    image = parse_hmf(hmf([b"0C880039"] * 40 + [b"0C88"]), "V339_bot.HMF")
    info = image.info()
    assert info["status"] == "unknown" and info["label"] == "V339_bot" and info["warning"] is None


def test_known_image_table():
    status = {sha[:8]: k.status for sha, k in KNOWN_IMAGES.items()}
    assert status["4a0a514b"] == "recommended"   # v339-bot.5: state in free RAM + firmware watchdog
    assert status["2e21d0c8"] == status["35f452f4"] == "defective"
    assert status["0ee3eb1a"] == status["1657008f"] == "defective"   # bot.3 / bot.4: state in firmware RAM
    assert all(k.warning and k.issue for k in KNOWN_IMAGES.values() if k.status == "defective")
    assert all(len(sha) == 64 for sha in KNOWN_IMAGES)


@pytest.mark.skipif(not MANIFEST.is_file(), reason="firmware/manifest.json not present (installed package)")
def test_manifest_is_consistent():
    """Unique tags, one recommended bot image, buildable entries name their files, the rest are known."""
    entries = json.loads(MANIFEST.read_text())["images"]
    tags = [e["tag"] for e in entries]
    assert len(tags) == len(set(tags))
    assert sum(e["bots"] and e["status"] == "recommended" for e in entries) == 1
    for e in entries:
        assert e["sha256"] in KNOWN_IMAGES and KNOWN_IMAGES[e["sha256"]].tag == e["tag"]
        if e["file"] is None:
            assert e["status"] in ("defective", "superseded", "stock"), e["tag"]
        else:
            assert e["file"].endswith(".hmf") and e.get("bin_sha256"), e["tag"]


@pytest.mark.skipif(not any(PROJECT_FIRMWARE_DIR.glob("*.hmf")), reason="firmware images not built (firmware/build.py)")
def test_built_images_match_manifest():
    """Images built from source (firmware/build.py) have the SHA-256 the manifest says."""
    for e in json.loads(MANIFEST.read_text())["images"]:
        if not e["file"] or not (PROJECT_FIRMWARE_DIR / e["file"]).is_file():
            continue
        data = (PROJECT_FIRMWARE_DIR / e["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == e["sha256"], e["tag"]
        info = parse_hmf(data, e["file"], source="project").info()
        assert info["tag"] == e["tag"] and info["status"] == e["status"]
        assert hashlib.sha256((PROJECT_FIRMWARE_DIR / e["bin"]).read_bytes()).hexdigest() == e["bin_sha256"]


def test_missing_images_and_build_op(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"images": [
        {"tag": "a", "file": "a.hmf"}, {"tag": "b", "file": "b.hmf"}, {"tag": "c", "file": None}]}))
    (tmp_path / "a.hmf").write_text("x")
    assert firmware_mod.missing_images(tmp_path, manifest) == ["b"]

    calls = []
    monkeypatch.setattr("racelab.app.build_images", lambda: (calls.append(1), (True, "built"))[1])
    monkeypatch.setattr("racelab.app.can_build", lambda: True)

    async def main():
        app = RaceLabApp(make_config(tmp_path))
        await app.start()
        try:
            started = app._op_fw_build({}, "test")
            assert started["state"] == "building"
            with pytest.raises(Exception):
                app._op_fw_build({}, "test")          # one build at a time
            for _ in range(100):
                if app.fw_build["state"] != "building":
                    break
                await asyncio.sleep(0.02)
            assert app.fw_build["state"] == "done" and calls == [1]
            assert app.snapshot()["firmware_build"]["state"] == "done"
        finally:
            await app.stop()

    asyncio.run(main())


@pytest.mark.parametrize("data, message", [
    (b"", "empty"),
    (bytes(range(256)) * 64, "binary"),
    (b"0C880039\r\n" * 40, "quotes"),
    (hmf([b"0C88ZZ39"] * 40), "non-hexadecimal"),
    (hmf([b"0C880039"] * 5), "lines"),
    (hmf([b"0C880039"] * 30 + [b"0C88"] + [b"0C880039"] * 10), "inconsistent"),
])
def test_parse_rejects_bad_files(data, message):
    with pytest.raises(ImageError, match=message):
        parse_hmf(data)


def test_library_upload_and_lookup(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    (local / "V0_bot.HMF").write_bytes(SYNTHETIC)
    lib = FirmwareLibrary(tmp_path / "fw", project_dir=local)
    project = lib.images()
    assert [i.source for i in project] == ["project"]
    data = hmf([b"0D" + b"%02X" % i + b"0039003A" for i in range(40)] + [b"00B8"])
    image = lib.add(data, "../../etc/my image.hmf")
    assert image.path.startswith(str(tmp_path / "fw")) and "/" not in image.name
    assert lib.get(image.id).name == image.name
    with pytest.raises(ImageError):
        lib.get("0" * 16)


@pytest.mark.parametrize("block", [None, 18])  # serial (E + line) then BLE (18-byte F blocks)
def test_flash_reaches_the_bootloader(block):
    sim = Simulator(SimConfig(bot_firmware=False, latency_ms=0, humans=[]), realtime=False)
    sim.fwu_block_size = block
    image = parse_hmf(SYNTHETIC)
    sim.bot_images = {image.lines_digest}
    cu = RaceControlUnit(sim.connect(), on_message=lambda m: None)
    with pytest.raises(ValueError):
        cu.request(b"GB")  # outside update mode: forbidden
    cu.firmware_mode = True
    with pytest.raises(ValueError):
        cu.request(b"C1")  # C: never
    cu.fwu_start()
    assert sim.bootloader
    for line in image.lines:
        cu.fwu_write(line)
    time.sleep(0.1)
    sim.step(0.01)
    assert not sim.bootloader and sim.flashed["lines"] == 897
    assert sim.cfg.bot_firmware is True  # restarted on the bot image
    sim.close()


# ------------------------------------------------------------ end to end


def make_config(tmp_path):
    cfg = Config()
    cfg.app.data_dir = str(tmp_path)
    # "bot" image available as if it had been imported (no Carrera file)
    (tmp_path / "firmware").mkdir(exist_ok=True)
    (tmp_path / "firmware" / "test_bot.HMF").write_bytes(SYNTHETIC)
    cfg.connection.transport = "sim"
    cfg.connection.reconnect_delay = 0.1
    cfg.connection.timeout = 0.3
    cfg.sim.latency_ms = cfg.sim.jitter_ms = 0
    cfg.sim.humans = []
    cfg.sim.bot_firmware = False  # CU on stock firmware: we are going to flash the bot image
    return cfg


async def started(cfg):
    app = RaceLabApp(cfg)
    client = TestClient(TestServer(create_web_app(app)))
    await client.start_server()
    await app.start()
    await wait_until(lambda: app.link_info["state"] == "connected")
    return app, client


async def op(client, name, **args):
    return await (await client.post(f"/api/{name}", json=args)).json()


async def wait_until(pred, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("condition never reached")


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 60))


def test_flash_stock_to_bot_firmware(tmp_path):
    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            assert app.link_info["bot_module"] is False          # stock firmware: no B module
            assert app.link_info["bots_supported"] is True        # ...but bots through the C table
            images = (await op(client, "firmware.images"))["result"]
            bot = next(i for i in images if i["status"] == "recommended")
            refused = await op(client, "firmware.flash", image=bot["id"])
            assert refused["ok"] is False and "FLASH" in refused["error"]
            events = []
            app.subscribe(lambda m: m.get("name") == "firmware" and events.append(m["data"]))
            r = await op(client, "firmware.flash", image=bot["id"], confirm="flash")
            assert r["ok"] and r["result"]["state"] == "running"
            blocked = await op(client, "race.setup", settings={"type": "practice"})
            assert blocked["ok"] is False and "firmware" in blocked["error"]
            await wait_until(lambda: app.fw["state"] == "done")
            assert app.fw["result"] == {"version": "5339", "bots_supported": True, "bot_module": True}
            assert app.link_info["bot_module"] is True and app.sim.flashed["lines"] == 897
            assert any(e["stage"] == "write" for e in events)
            # the CU answers normally again, bots included
            assert (await op(client, "bot.take", slot=0, driver="manual"))["ok"]
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_flash_refused_on_another_hardware(tmp_path):
    """The images are for hardware 5 (AVR): a hardware-8 CU (STM32, version 8339) never gets one."""
    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            app.link_info["version"] = "8339"
            image = app.firmware_lib.images()[0]
            r = await op(client, "firmware.flash", image=image.id, confirm="FLASH")
            assert r["ok"] is False and "hardware 8" in r["error"]
            assert app.fw["state"] != "running"
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_abort_then_recovery(tmp_path):
    async def main():
        cfg = make_config(tmp_path)
        cfg.sim.latency_ms = 2  # ~2 s of flashing: time to interrupt
        app, client = await started(cfg)
        try:
            image = app.firmware_lib.images()[0]
            assert (await op(client, "firmware.flash", image=image.id, confirm="FLASH"))["ok"]
            await wait_until(lambda: app.fw["done"] > 50)
            assert (await op(client, "firmware.abort"))["ok"]
            await wait_until(lambda: app.fw["state"] == "failed")
            assert "interrupted" in app.fw["error"]
            # the CU stayed in its bootloader: no version, the link drops
            await wait_until(lambda: app.link_info["state"] == "lost")
            assert app.sim.bootloader
            normal = await op(client, "firmware.flash", image=image.id, confirm="FLASH")
            assert normal["ok"] is False and "recovery" in normal["error"]
            r = await op(client, "firmware.flash", image=image.id, confirm="FLASH", recovery=True)
            assert r["ok"]
            await wait_until(lambda: app.fw["state"] == "done")
            assert not app.sim.bootloader and app.link_info["state"] == "connected"
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_upload_endpoint(tmp_path):
    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            bad = await client.post("/api/firmware/upload?name=x.bin", data=bytes(range(256)) * 8)
            assert bad.status == 400
            good = await client.post("/api/firmware/upload?name=V339_bot.HMF", data=SYNTHETIC)
            info = (await good.json())["result"]
            assert info["lines"] == 897
        finally:
            await app.stop()
            await client.close()

    run(main())


def test_defective_image_refused_whatever_its_name(tmp_path, monkeypatch):
    bad = hmf([b"0D" + b"%02X" % i + b"0039003A" for i in range(40)] + [b"00B8"])
    monkeypatch.setitem(firmware_mod.KNOWN_IMAGES, hashlib.sha256(bad).hexdigest(),
                        KNOWN_IMAGES["35f452f432cd1ee2443c7103d4ec49a3c91cdd2ac8cab247a40b1bb06bec71e6"])

    async def main():
        app, client = await started(make_config(tmp_path))
        try:
            info = app.add_firmware(bad, "V339_bot_fixed.HMF")  # a reassuring name changes nothing
            assert info["status"] == "defective" and "event ring" in info["warning"]
            r = await op(client, "firmware.flash", image=info["id"], confirm="FLASH")
            assert r["ok"] is False and "refused" in r["error"]
            r = await op(client, "firmware.flash", image=info["id"], confirm="FLASH", recovery=True)
            assert r["ok"] is False and app.fw["state"] == "idle"
        finally:
            await app.stop()
            await client.close()

    run(main())
