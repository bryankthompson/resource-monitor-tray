"""Vision Resource Monitor — a Windows system-tray client for the WSL-side
resource monitor's ``monitor-snapshot:v1`` feed.

The package splits cleanly in two so the logic stays testable without a display:

* ``snapshot`` / ``reader`` / ``state`` / ``instancelock`` — pure, Qt-free.
* ``ui`` — the PySide6 tray icon, window, and app loop (imports Qt lazily).
"""
from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"

# Public-build flag. A dev tree keeps its live actions (process release, ProcMon
# capture, ask-Claude launch, the wsl.exe rebuild + update banner). The public
# export flips this to True; ``--demo`` turns the same actions off at runtime
# (``enable_demo``). Every live action asks ``live_actions_enabled()``.
PUBLIC_BUILD = True
LIVE_ACTIONS_OFF_REASON = ("Disabled in the public/demo build: it acts on the "
                           "machine the monitor runs on.")
_demo = False


def enable_demo() -> None:
    """Mark this process as a ``--demo`` run (live actions off until exit)."""
    global _demo
    _demo = True


def live_actions_enabled() -> bool:
    """True only in a dev tree outside ``--demo``: process release, RunAs ProcMon
    capture, ask-Claude launch and the wsl.exe rebuild/update nag are available."""
    return not (PUBLIC_BUILD or _demo)


# Default snapshot location. The WSL producer writes here (over /mnt/c) so the
# Windows consumer reads a native local path; both sides resolve the same env
# var when set. Overridable via --snapshot / $VRM_SNAPSHOT_PATH.
SNAPSHOT_ENV = "VRM_SNAPSHOT_PATH"
# The private producer still writes to LEGACY_DATA_DIR; public builds get an
# application-named directory. Keep the legacy literal here for compatibility.
LEGACY_DATA_DIR = "vision-tracking"
PUBLIC_DATA_DIR = "vision-resource-monitor"


def _state_base() -> str:
    """The per-user state root: LOCALAPPDATA on Windows, XDG_STATE_HOME / ~/.local
    /state elsewhere. Shared by the lock and log locations."""
    return os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME") \
        or str(Path.home() / ".local" / "state")


def default_snapshot_path() -> Path:
    env = os.environ.get(SNAPSHOT_ENV)
    if env:
        return Path(env).expanduser()
    data_dir = PUBLIC_DATA_DIR if PUBLIC_BUILD else LEGACY_DATA_DIR
    return Path(_state_base()) / data_dir / "status.json"


def default_activity_log_path(snapshot_path=None) -> Path:
    """The GPU-activity JSONL the producer writes next to the snapshot
    (``<snapshot dir>/gpu-activity.jsonl``) — what the tray/window 'Open
    activity log' item opens. Derived from the snapshot path so a custom
    --snapshot keeps both files in the same folder."""
    snap = Path(snapshot_path) if snapshot_path else default_snapshot_path()
    return snap.parent / "gpu-activity.jsonl"


def default_monitor_config_path(snapshot_path=None) -> Path:
    """The ``monitor-config:v1`` JSON the app writes next to the snapshot
    (``<snapshot dir>/monitor-config.json``) — the producer reads it (its
    ``--config``) to apply the sample cadences the user tuned in Settings.
    Derived from the snapshot path so a custom --snapshot keeps the config file in
    the same folder both sides already share (over /mnt/c on Windows)."""
    snap = Path(snapshot_path) if snapshot_path else default_snapshot_path()
    return snap.parent / "monitor-config.json"


def default_lock_path() -> Path:
    return Path(_state_base()) / "vision-resource-monitor" / "instance.lock"


def default_log_path() -> Path:
    """Rotating activity-log location: ``<state>/vision-resource-monitor/logs/
    vrm.log`` (``%LOCALAPPDATA%\\vision-resource-monitor\\logs\\vrm.log`` on
    Windows)."""
    return Path(_state_base()) / "vision-resource-monitor" / "logs" / "vrm.log"


def build_version():
    """The git short-SHA this native build was stamped with at build time
    (the native build writes ``_build_version.py`` into the frozen exe), or
    ``None`` when running from source — dev, tests, or the WSLg window — where
    there is no frozen build that could be stale. The app compares
    it to the producer's live ``source_version`` to prompt a rebuild on drift."""
    try:
        from ._build_version import BUILD_VERSION
    except Exception:  # noqa: BLE001 — absent in source checkouts; never fatal
        return None
    version = str(BUILD_VERSION or "").strip()
    return version or None


# ── native rebuild helpers (the app's "Rebuild now" action) ──────────────────────
# The frozen Windows exe rebuilds itself by shelling `vrm rebuild` inside WSL. The
# argv/hint/status text are kept here (Qt-free) so they are importable + unit-testable
# without a display.
WSL_REPO_DEFAULT = "~/vision-resource-monitor"


def wsl_rebuild_argv(repo=WSL_REPO_DEFAULT):
    """argv to launch ``vrm rebuild --relaunch`` inside the default WSL distro from
    the Windows exe (via ``wsl.exe``). Run detached, so it survives the exe being
    stopped mid-rebuild (the rebuild stops + reinstalls + relaunches the app)."""
    return ["wsl.exe", "bash", "-lc", f"cd {repo} && vrm rebuild --relaunch"]


def rebuild_command_hint():
    """The command to run by hand in a WSL terminal to update the app — the
    fallback shown when the in-app 'Rebuild now' can't launch ``wsl.exe``."""
    return "vrm rebuild"


def update_status_line(build, source):
    """A human status line comparing the frozen build SHA to the producer's live
    source SHA. Qt-free so the About dialog and tests can share it. A public/demo
    build has no producer checkout to be stale against, so it states its version."""
    if not live_actions_enabled():
        return f"Version {__version__}"
    if not build:
        return "Running from source (no build stamp) — update check disabled."
    if not source:
        return f"Build {build}. Live source version unknown (older producer / no feed)."
    if build == source:
        return f"Up to date (build {build})."
    return (f"Update available — source is at {source}, this build is {build}. "
            "Use Rebuild now, or run `vrm rebuild`.")
