"""Launcher: `racelab` (or `python -m racelab`).

    racelab --sim                 virtual CU (no hardware)
    racelab                       BLE AppConnect (scan for "Control_Unit")
    racelab --ble <address>       BLE AppConnect at a given address/UUID
    racelab --serial <port>       direct serial cable
    racelab --classic             classic mode (no software drivers)
    racelab --scan                list visible CUs and exit
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import urllib.request
import webbrowser
from pathlib import Path

from aiohttp import web

from . import __version__
from .config import ConfigError, load_config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="racelab", description="RaceLab: race app for Carrera Digital 124/132")
    link = p.add_mutually_exclusive_group()
    link.add_argument("--sim", action="store_true", help="virtual CU (simulator)")
    link.add_argument("--ble", nargs="?", const="", metavar="ADDRESS", help="Bluetooth AppConnect")
    link.add_argument("--serial", metavar="PORT", help="serial port")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--classic", action="store_true", help="classic mode")
    mode.add_argument("--advanced", action="store_true", help="advanced mode (software drivers, RL API)")
    p.add_argument("-c", "--config", metavar="FILE", help="TOML file (default ./racelab.toml)")
    p.add_argument("--host", help="listen interface (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="HTTP port (default 8130)")
    p.add_argument("--no-browser", action="store_true", help="do not open the browser")
    p.add_argument("--window", action="store_true",
                   help="open the UI in a dedicated window (Chrome/Brave/Edge in app mode) instead of a tab")
    p.add_argument("--data-dir", metavar="DIR", help="sessions and UI settings (default ~/.racelab)")
    p.add_argument("--stock-firmware", action="store_true", help="simulator: firmware without the B command")
    p.add_argument("--scan", action="store_true", help="list visible CUs then exit")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"racelab {__version__}")
    return p.parse_args(argv)


def build_config(args: argparse.Namespace):
    cfg = load_config(args.config, data_dir=args.data_dir)
    if args.sim:
        cfg.connection.transport = "sim"
    elif args.ble is not None:
        cfg.connection.transport, cfg.connection.device = "ble", args.ble
    elif args.serial:
        cfg.connection.transport, cfg.connection.device = "serial", args.serial
    if args.classic:
        cfg.app.mode = "classic"
    elif args.advanced:
        cfg.app.mode = "advanced"
    if args.host:
        cfg.app.host = args.host
    if args.port:
        cfg.app.port = args.port
    if args.no_browser:
        cfg.app.open_browser = False
    if args.stock_firmware:
        cfg.sim.bot_firmware = False
    return cfg


# browsers able to open a window without tabs or address bar (--app)
MAC_APP_BROWSERS = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium", "Vivaldi")
LINUX_APP_BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                      "microsoft-edge", "brave-browser", "vivaldi")


def _windows_browsers() -> list[Path]:
    roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
    rel = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
           r"BraveSoftware\Brave-Browser\Application\brave.exe"]
    return [Path(root) / r for r in rel for root in roots if root]


def open_ui(url: str, *, window: bool, profile_dir: Path) -> None:
    """Open the UI: dedicated window (Chromium browser in --app mode) if possible, else a tab."""
    if window:
        flags = [f"--app={url}", f"--user-data-dir={profile_dir}", "--no-first-run",
                 "--no-default-browser-check", "--window-size=1440,900"]
        try:
            if sys.platform == "darwin":
                for name in MAC_APP_BROWSERS:
                    if any((base / f"{name}.app").exists() for base in (Path("/Applications"), Path.home() / "Applications")):
                        profile_dir.mkdir(parents=True, exist_ok=True)
                        subprocess.Popen(["open", "-na", name, "--args", *flags])
                        return
            elif sys.platform == "win32":
                for exe in _windows_browsers():
                    if exe.exists():
                        profile_dir.mkdir(parents=True, exist_ok=True)
                        subprocess.Popen([str(exe), *flags])
                        return
            else:
                for name in LINUX_APP_BROWSERS:
                    exe = shutil.which(name)
                    if exe:
                        profile_dir.mkdir(parents=True, exist_ok=True)
                        subprocess.Popen([exe, *flags], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        return
        except OSError:
            pass  # browser vanished at the last moment: plain tab
    webbrowser.open(url)


def running_instance(host: str, port: int) -> bool:
    """Is a RaceLab already answering on this port?"""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/meta", timeout=1.5) as r:
            return "session_types" in json.loads(r.read())
    except (OSError, ValueError):
        return False


async def serve(cfg, *, window: bool = False) -> None:
    from .api.server import create_web_app
    from .app import RaceLabApp

    host = "localhost" if cfg.app.host in ("127.0.0.1", "0.0.0.0") else cfg.app.host
    url = f"http://{host}:{cfg.app.port}/"
    if running_instance(host, cfg.app.port):
        print(f"RaceLab is already running: {url}")
        if cfg.app.open_browser:
            open_ui(url, window=window, profile_dir=cfg.data_path / "window")
        return

    app = RaceLabApp(cfg)
    runner = web.AppRunner(create_web_app(app), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, cfg.app.host, cfg.app.port)
    try:
        await site.start()
    except OSError as e:
        await runner.cleanup()
        raise SystemExit(f"cannot listen on {cfg.app.host}:{cfg.app.port} ({e.strerror})")
    await app.start()

    mode = "advanced" if cfg.app.mode == "advanced" else "classic"
    print(f"RaceLab {__version__} · {mode} mode · CU: {app.link_info['transport']}")
    print(f"  UI   {url}")
    print(f"  API  {url.replace('http', 'ws')}ws   (see docs/api.md)")
    print("  Ctrl+C to quit (bots are stopped cleanly)")
    if cfg.app.open_browser:
        open_ui(url, window=window, profile_dir=cfg.data_path / "window")

    stop = asyncio.Event()
    app.request_quit = stop.set
    loop = asyncio.get_running_loop()
    # Windows: no SIGHUP, and no add_signal_handler (Ctrl+C raises KeyboardInterrupt)
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        print("\nStopping: throttle 0 on the bots, closing the link…")
        await app.stop()
        await runner.cleanup()


def scan() -> None:
    from .app import scan_devices

    result = scan_devices(6.0)
    for d in result["devices"]:
        print(f"  {d['transport']:6} {d['device']}\t{d['label']}")
    if not result["devices"]:
        print("  no CU found")
    for error in result["errors"]:
        print(f"  ! {error}")


def setup_logging(verbose: bool, data_dir: Path | None) -> None:
    """Console if there is one; otherwise (pythonw on Windows, macOS app) <data_dir>/racelab.log."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    if sys.stderr is None and data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S",
                            filename=data_dir / "racelab.log", encoding="utf-8")
    else:
        logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    for noisy in ("aiohttp.access", "bleak"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.scan:
        setup_logging(args.verbose, None)
        scan()
        return
    try:
        cfg = build_config(args)
    except ConfigError as e:
        sys.exit(f"config: {e}")
    setup_logging(args.verbose, cfg.data_path)
    try:
        asyncio.run(serve(cfg, window=args.window))
    except KeyboardInterrupt:
        pass  # Windows: Ctrl+C; the clean shutdown (bots stopped) is done in serve()


if __name__ == "__main__":
    main()
