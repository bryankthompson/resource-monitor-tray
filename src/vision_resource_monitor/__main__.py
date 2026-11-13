"""Entry point: ``python -m vision_resource_monitor`` / ``vision-resource-monitor``.

Wires the CLI to the Qt app. Qt is imported here (not at package import) so the
pure modules stay Qt-free for headless tests.
"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
import sys
from pathlib import Path

from . import (
    __version__, default_lock_path, default_snapshot_path, enable_demo,
)
from .instancelock import SingleInstance
from .logging_setup import resolve_log_level, setup_logging
from .reader import DEFAULT_STALE_AFTER_S
from .settings import (
    KEY_INTERVAL, KEY_LOG_FILE, KEY_LOG_LEVEL, KEY_MUTE, KEY_SHOW_HOST,
    KEY_SHOW_MEM, KEY_SNAPSHOT, KEY_STALE_AFTER, KEY_THEME, load_start_minimized,
    load_stored, resolve_config,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vision-resource-monitor",
        description="Windows system-tray client for the WSL resource monitor's "
                    "snapshot feed. Where a flag is omitted, the value falls back "
                    "to the persisted setting, then the built-in default.",
    )
    p.add_argument("--snapshot", type=Path, default=None,
                   help="Snapshot file to read (default: setting, else "
                        "$VRM_SNAPSHOT_PATH, else platform state directory/"
                        "vision-resource-monitor/status.json for public builds)")
    p.add_argument("--interval", type=float, default=None,
                   help="Poll interval in seconds (default: setting, else 2.0)")
    p.add_argument("--stale-after", type=float, default=None,
                   help="Seconds without a new snapshot before showing 'stale' "
                        f"(default: setting, else {DEFAULT_STALE_AFTER_S})")
    p.add_argument("--log-file", type=Path, default=None,
                   help="Activity-log path opened by the 'Open activity log' menu "
                        "item (default: gpu-activity.jsonl next to the snapshot; "
                        "also settable in Settings)")
    p.add_argument("--log-level", type=str.upper, default=None,
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Log verbosity (default: setting, else $VRM_LOG_LEVEL, else "
                        "INFO). DEBUG logs the full read→render pipeline, incl. "
                        "per-panel populate decisions (why a tier such as Memory "
                        "is blank). The startup record is written at every level.")
    p.add_argument("--allow-multiple", action="store_true",
                   help="Skip the single-instance guard")
    p.add_argument("--demo", action="store_true",
                   help="Run against the bundled fake producer (temp snapshot, "
                        "neutral data) with live actions off; implies --window. "
                        "Ignores --snapshot and never touches a real instance.")
    p.add_argument("--window", action="store_true",
                   help="Window-only mode: show the monitor window instead of a "
                        "system-tray icon (for desktops without a tray, e.g. WSLg). "
                        "Closing the window quits.")
    # Theme + panel toggles are tri-state (dest defaults to None) so a flag can
    # override a persisted setting in *either* direction; omitting them falls
    # back to the setting, then the built-in default.
    p.add_argument("--theme", choices=["dark", "light"], default=None,
                   help="Colour theme (default: setting, else dark).")
    p.add_argument("--dark", action="store_const", const="dark", dest="theme",
                   help="Use the dark theme (overrides a stored 'light').")
    p.add_argument("--light", action="store_const", const="light", dest="theme",
                   help="Use the light theme (overrides a stored 'dark').")
    p.add_argument("--host", action="store_const", const=True, dest="show_host",
                   help="Show the Host (CPU/RAM) panel.")
    p.add_argument("--no-host", action="store_const", const=False, dest="show_host",
                   help="Hide the Host (CPU/RAM) panel.")
    p.add_argument("--mem", action="store_const", const=True, dest="show_mem",
                   help="Show the Memory panel.")
    p.add_argument("--no-mem", action="store_const", const=False, dest="show_mem",
                   help="Hide the Memory panel.")
    p.add_argument("--mute", action="store_const", const=True,
                   dest="mute_notifications",
                   help="Mute tray notification pop-ups (toasts are still logged).")
    p.add_argument("--no-mute", action="store_const", const=False,
                   dest="mute_notifications",
                   help="Show tray notification pop-ups (overrides a stored mute).")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.set_defaults(theme=None, show_host=None, show_mem=None,
                   mute_notifications=None)
    return p


def _start_demo_feed():
    """Write one fake frame (so the first read is fresh), then keep the bundled
    fake producer writing from a daemon thread (no child process: works the same
    from source and from the frozen exe, and dies with the GUI however it exits).
    Returns (tempdir, (thread, stop_event), snapshot path)."""
    import tempfile
    import threading
    import time
    from . import fake_producer
    tmp = tempfile.TemporaryDirectory(prefix="vrm-demo-")
    snap = Path(tmp.name) / "status.json"
    fake_producer.main([str(snap), "--once"])
    stop = threading.Event()
    start = time.time()

    def loop():
        while not stop.wait(2.0):
            try:
                fake_producer.write_atomic(
                    snap, fake_producer._build(time.time(), start, "demo-host"))
            except Exception:  # noqa: BLE001 — a failed frame just goes stale
                logging.getLogger(__name__).warning("demo frame write failed",
                                                    exc_info=True)

    t = threading.Thread(target=loop, name="vrm-demo-feed", daemon=True)
    t.start()
    return tmp, (t, stop), snap


def _stop_demo_feed(tmp, feed) -> None:
    if feed is not None:
        thread, stop = feed
        stop.set()
        thread.join(timeout=5)   # no write may race the directory cleanup
    if tmp is not None:
        tmp.cleanup()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.interval is not None and args.interval <= 0:
        build_parser().error("--interval must be greater than zero")

    cli = {
        KEY_SNAPSHOT: str(args.snapshot) if args.snapshot else None,
        KEY_INTERVAL: args.interval,
        KEY_STALE_AFTER: args.stale_after,
        KEY_THEME: args.theme,
        KEY_SHOW_HOST: args.show_host,
        KEY_SHOW_MEM: args.show_mem,
        KEY_MUTE: args.mute_notifications,
        KEY_LOG_FILE: str(args.log_file) if args.log_file else None,
        KEY_LOG_LEVEL: args.log_level,
    }
    try:
        stored = load_stored()
    except Exception:  # noqa: BLE001 — a bad/absent settings store must not block startup
        stored = {}
    if args.demo:
        # Live actions off BEFORE anything can build a window; the feed is the
        # bundled fake producer writing to a temp path (cleaned up in finally).
        enable_demo()
        args.window = True
        demo_dir, demo_proc, demo_snapshot = _start_demo_feed()
        # atexit: every exit path (lock decline, no display, crash) stops the
        # fake producer and removes the temp snapshot.
        atexit.register(_stop_demo_feed, demo_dir, demo_proc)
        cli[KEY_SNAPSHOT] = str(demo_snapshot)
    config = resolve_config(cli, stored)
    if config.interval <= 0:      # a bad stored value shouldn't wedge the app
        config.interval = 2.0
    # Start-minimized is view-state (persisted separately from Config); a missing/
    # bad store must not block startup, so read it fail-open.
    try:
        start_minimized = load_start_minimized()
    except Exception:  # noqa: BLE001
        start_minimized = False

    level = resolve_log_level(config.log_level)
    log_file = setup_logging(level)
    log = logging.getLogger(__name__)
    snapshot_path = (Path(config.snapshot_path).expanduser()
                     if config.snapshot_path else default_snapshot_path())
    # Always record startup so a clean start can be confirmed at every log level.
    log.handle(log.makeRecord(
        log.name, logging.INFO, __name__, 0,
        "vision-resource-monitor %s starting — snapshot=%s interval=%.1fs "
        "stale_after=%.1fs level=%s window=%s theme=%s host=%s mem=%s "
        "mute=%s start_minimized=%s log_file=%s",
        (__version__, snapshot_path, config.interval, config.stale_after,
         logging.getLevelName(level), args.window, config.theme, config.show_host,
         config.show_mem, config.mute_notifications, start_minimized, log_file),
        None,
    ))

    lock = None
    if not args.allow_multiple:
        # A demo has its own lock file so it neither blocks nor is blocked by a
        # real tray, yet a second --demo still declines.
        lock_path = default_lock_path()
        if args.demo:
            lock_path = lock_path.with_name("demo.lock")
        lock = SingleInstance(lock_path)
        if not lock.acquire():
            log.warning("another instance holds the lock; exiting")
            print("Vision Resource Monitor is already running.", file=sys.stderr)
            return 0

    # Under WSLg, Qt's wayland backend reports a window as visible but never
    # surfaces it on the Windows desktop; XWayland (xcb) does. So if WSLg is
    # present and the user hasn't forced a platform, prefer xcb. Must be set
    # before QApplication is constructed.
    if os.environ.get("QT_QPA_PLATFORM") is None and Path("/mnt/wslg").exists():
        os.environ["QT_QPA_PLATFORM"] = "xcb"
        log.debug("WSLg detected — forcing QT_QPA_PLATFORM=xcb")

    # Import Qt lazily so --version / --help and the instance check need no display.
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtWidgets import QSystemTrayIcon
    from .ui.app import MonitorApp
    from .ui.theme import apply_theme
    from .ui.trayicon import load_app_icon

    qapp = QApplication(sys.argv[:1])
    qapp.setApplicationName("Vision Resource Monitor")
    qapp.setWindowIcon(load_app_icon())
    apply_theme(qapp, dark=config.dark)

    if args.window:
        # Window-only: the window is the app, so closing it exits.
        qapp.setQuitOnLastWindowClosed(True)
    else:
        # Tray mode: closing the window keeps the tray alive.
        qapp.setQuitOnLastWindowClosed(False)
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.error("no system tray available and --window not set; exiting")
            QMessageBox.critical(
                None, "Vision Resource Monitor",
                "No system tray is available on this desktop.\n\n"
                "Run with --window to show the monitor window instead "
                "(e.g. under WSLg).")
            return 1

    app_obj = MonitorApp(
        qapp, snapshot_path,
        interval_ms=int(config.interval * 1000),
        stale_after_s=config.stale_after,
        use_tray=not args.window,
        show_host=config.show_host,
        show_mem=config.show_mem,
        config=config,
        # The raw CLI layer, so Settings can pre-fill from stored truth while
        # cueing flag-shadowed knobs and overlaying flags on live-apply.
        cli=cli,
        diag_log_file=log_file,
        start_minimized=start_minimized,
    )
    try:
        return qapp.exec()
    finally:
        if lock is not None:
            lock.release()
        log.info("vision-resource-monitor exiting")


if __name__ == "__main__":
    sys.exit(main())
