"""The public-build / --demo switch: a dev tree keeps its live actions; with the
flag (or --demo) every live action is off and its launcher is never reached.

Launchers are stubbed so a failure here can never open a real terminal, run
wsl.exe/powershell.exe, or release real processes on a WSL-interop box."""
import os
import json
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QProcess
from PySide6.QtWidgets import QApplication, QPushButton

import vision_resource_monitor as vrm
from vision_resource_monitor import fake_producer, release
from vision_resource_monitor.snapshot import Snapshot
from vision_resource_monitor.ui import app as app_mod
from vision_resource_monitor.ui import window as window_mod


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def no_launch(monkeypatch):
    """Every way an action could reach the machine fails the test."""
    def boom(*a, **k):
        raise AssertionError("a live launcher was reached")
    monkeypatch.setattr(QProcess, "startDetached", staticmethod(boom))
    monkeypatch.setattr(QProcess, "start", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(release, "read_ps_rows", boom)
    monkeypatch.setattr(release, "release_nodes", boom)
    monkeypatch.setattr(app_mod, "prepare_script", boom)
    monkeypatch.setattr(app_mod.procmonitor, "launch_argv", boom)


def _make_app(qapp, tmp_path):
    snap_file = tmp_path / "status.json"
    fake_producer.write_atomic(snap_file, fake_producer._build(1010.0, 1000.0, "T"))
    return app_mod.MonitorApp(qapp, snap_file, interval_ms=10 ** 7, use_tray=False,
                              settings_path=str(tmp_path / "settings.json"))


def test_dev_mode_keeps_live_actions(monkeypatch):
    monkeypatch.setattr(vrm, "PUBLIC_BUILD", False)
    assert vrm.live_actions_enabled() is True


@pytest.mark.parametrize("public_build", [False, True])
def test_default_snapshot_path_tracks_build_mode(monkeypatch, tmp_path,
                                                  public_build):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv(vrm.SNAPSHOT_ENV, raising=False)
    monkeypatch.setattr(vrm, "PUBLIC_BUILD", public_build)
    data_dir = (vrm.PUBLIC_DATA_DIR if public_build else vrm.LEGACY_DATA_DIR)
    if public_build:
        assert vrm.PUBLIC_DATA_DIR == "vision-resource-monitor"
    assert vrm.default_snapshot_path() == Path(tmp_path) / data_dir / "status.json"


@pytest.fixture(params=["public_build", "demo"])
def off(request, monkeypatch):
    if request.param == "public_build":
        monkeypatch.setattr(vrm, "PUBLIC_BUILD", True)
    else:
        monkeypatch.setattr(vrm, "_demo", True)


def test_flag_turns_helper_and_version_line_neutral(off):
    assert vrm.live_actions_enabled() is False
    # No "update available" nag against a producer checkout (d)
    assert vrm.update_status_line("a", "b") == f"Version {vrm.__version__}"


def test_enable_demo_sets_the_switch(monkeypatch):
    monkeypatch.setattr(vrm, "_demo", False)
    vrm.enable_demo()
    assert vrm.live_actions_enabled() is False


def test_actions_unavailable_and_launchers_unreached(off, qapp, tmp_path,
                                                      no_launch, monkeypatch):
    mapp = _make_app(qapp, tmp_path)
    win = mapp.window
    # (c) ask-claude and (b) ProcMon: no callback wired, so no UI entry point
    assert win._on_ask_claude is None
    assert win._on_open_procmon is None
    # (d) rebuild: banner button unwired, update nag never raised
    mapp._build_version = "aaaaaaa"
    snap = Snapshot.from_dict(fake_producer._build(1010.0, 1000.0, "T"))
    snap.source_version = "bbbbbbb"
    mapp._maybe_notify_new_version(snap)
    assert not win._update_banner.isVisible()
    assert mapp._notified_new_version is False
    # the methods themselves refuse even if called directly
    def unexpected_rebuild_route():
        raise AssertionError("public rebuild reached the development route")
    monkeypatch.setattr(app_mod, "wsl_rebuild_argv", unexpected_rebuild_route)
    mapp.rebuild_now()
    mapp.ask_claude(object())
    mapp.open_in_procmon(4242, "node")
    # (a) release: the fake feed's synthetic root pids cannot reach the flow
    win._last_snap = snap
    win._update_sessions(snap)
    rel = [b for b in win.findChildren(QPushButton) if b.text() == "release"]
    assert rel, "fake feed should carry release rows"
    assert all(not b.isEnabled() and vrm.LIVE_ACTIONS_OFF_REASON.split(":")[0]
               in b.toolTip() for b in rel)
    shown = []
    win._show_release_result = lambda label, outcomes, note=None: shown.append(note)
    win._release_session(5001, "x")
    assert shown == [vrm.LIVE_ACTIONS_OFF_REASON]


def test_dev_tree_wires_the_actions(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(vrm, "PUBLIC_BUILD", False)
    mapp = _make_app(qapp, tmp_path)
    assert mapp.window._on_ask_claude is not None
    assert mapp.window._on_open_procmon is not None
    assert vrm.update_status_line("a", "b").startswith("Update available")


def test_demo_feed_is_neutral_and_cleaned_up():
    from vision_resource_monitor.__main__ import _start_demo_feed, _stop_demo_feed
    tmp, feed, snap = _start_demo_feed()
    try:
        assert snap.exists() and feed[0].is_alive()
        payload = json.loads(snap.read_text())
        assert payload["schema"] == "monitor-snapshot:v1"
        assert payload["host"] == "demo-host"
        assert snap.name == "status.json"
    finally:
        _stop_demo_feed(tmp, feed)
    assert not feed[0].is_alive()
    assert not snap.exists()


def test_demo_flag_parses():
    from vision_resource_monitor.__main__ import build_parser
    assert build_parser().parse_args(["--demo"]).demo is True
    assert build_parser().parse_args([]).demo is False
