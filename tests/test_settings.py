import pytest

from vision_resource_monitor.reader import DEFAULT_STALE_AFTER_S
from vision_resource_monitor.settings import (
    KEY_INTERVAL, KEY_LOG_FILE, KEY_LOG_LEVEL, KEY_MUTE, KEY_SHOW_HOST,
    KEY_SHOW_MEM, KEY_STALE_AFTER, KEY_THEME, resolve_config,
)


def test_defaults_when_nothing_set(monkeypatch):
    monkeypatch.delenv("VRM_LOG_LEVEL", raising=False)
    c = resolve_config({}, {})
    assert c.interval == 2.0
    assert c.stale_after == DEFAULT_STALE_AFTER_S
    assert c.theme == "dark" and c.dark is True
    assert c.show_host is True and c.show_mem is True
    assert c.mute_notifications is False
    assert c.log_level == "INFO"
    assert c.snapshot_path == ""
    assert c.log_file == ""


def test_stored_overrides_default(monkeypatch):
    monkeypatch.delenv("VRM_LOG_LEVEL", raising=False)
    stored = {KEY_INTERVAL: 5.0, KEY_THEME: "light", KEY_SHOW_HOST: False,
              KEY_LOG_LEVEL: "DEBUG"}
    c = resolve_config({}, stored)
    assert c.interval == 5.0
    assert c.theme == "light" and c.dark is False
    assert c.show_host is False
    assert c.log_level == "DEBUG"


def test_cli_overrides_stored():
    stored = {KEY_INTERVAL: 5.0, KEY_THEME: "light", KEY_SHOW_HOST: False}
    cli = {KEY_INTERVAL: 1.0, KEY_THEME: "dark", KEY_SHOW_HOST: True}
    c = resolve_config(cli, stored)
    assert c.interval == 1.0
    assert c.theme == "dark"
    assert c.show_host is True


def test_none_cli_does_not_clobber_stored():
    c = resolve_config({KEY_INTERVAL: None, KEY_SHOW_MEM: None},
                       {KEY_INTERVAL: 3.0, KEY_SHOW_MEM: False})
    assert c.interval == 3.0
    assert c.show_mem is False


def test_string_coercion_from_ini_backend(monkeypatch):
    monkeypatch.delenv("VRM_LOG_LEVEL", raising=False)
    stored = {KEY_INTERVAL: "4.5", KEY_STALE_AFTER: "12",
              KEY_SHOW_HOST: "false", KEY_SHOW_MEM: "true"}
    c = resolve_config({}, stored)
    assert c.interval == 4.5
    assert c.stale_after == 12.0
    assert c.show_host is False
    assert c.show_mem is True


def test_mute_notifications_precedence_and_coercion(monkeypatch):
    monkeypatch.delenv("VRM_LOG_LEVEL", raising=False)
    # stored True is honoured; a CLI False overrides it; a None CLI does not.
    assert resolve_config({}, {KEY_MUTE: True}).mute_notifications is True
    assert resolve_config({KEY_MUTE: False}, {KEY_MUTE: True}).mute_notifications is False
    assert resolve_config({KEY_MUTE: None}, {KEY_MUTE: True}).mute_notifications is True
    # an ini backend stores strings — coerce them.
    assert resolve_config({}, {KEY_MUTE: "true"}).mute_notifications is True
    assert resolve_config({}, {KEY_MUTE: "0"}).mute_notifications is False


def test_log_file_precedence(monkeypatch):
    monkeypatch.delenv("VRM_LOG_LEVEL", raising=False)
    assert resolve_config({}, {}).log_file == ""                                  # default
    assert resolve_config({}, {KEY_LOG_FILE: "/a/b.jsonl"}).log_file == "/a/b.jsonl"  # stored
    assert resolve_config({KEY_LOG_FILE: "/c/d.jsonl"},
                          {KEY_LOG_FILE: "/a/b.jsonl"}).log_file == "/c/d.jsonl"   # cli > stored


def test_default_activity_log_path_sits_next_to_snapshot():
    from pathlib import Path as _P
    from vision_resource_monitor import default_activity_log_path
    assert default_activity_log_path(_P("/x/y/status.json")) == _P("/x/y/gpu-activity.jsonl")


def test_log_level_precedence_cli_stored_env(monkeypatch):
    monkeypatch.setenv("VRM_LOG_LEVEL", "WARNING")
    assert resolve_config({}, {}).log_level == "WARNING"                       # env
    assert resolve_config({}, {KEY_LOG_LEVEL: "ERROR"}).log_level == "ERROR"   # stored > env
    assert resolve_config({KEY_LOG_LEVEL: "DEBUG"},
                          {KEY_LOG_LEVEL: "ERROR"}).log_level == "DEBUG"        # cli > stored


def test_qsettings_roundtrip(tmp_path, monkeypatch):
    pytest.importorskip("PySide6.QtCore")
    monkeypatch.delenv("VRM_LOG_LEVEL", raising=False)
    from vision_resource_monitor.settings import load_stored, save_stored
    ini = tmp_path / "settings.ini"
    assert load_stored(path=ini) == {}          # nothing stored yet
    save_stored({KEY_INTERVAL: 7.0, KEY_THEME: "light", KEY_SHOW_HOST: False,
                 KEY_MUTE: True, KEY_LOG_FILE: "/tmp/a.jsonl",
                 KEY_LOG_LEVEL: "DEBUG"}, path=ini)
    got = load_stored(path=ini)
    assert got[KEY_INTERVAL] == 7.0
    assert got[KEY_THEME] == "light"
    assert got[KEY_SHOW_HOST] is False
    assert got[KEY_MUTE] is True
    assert got[KEY_LOG_FILE] == "/tmp/a.jsonl"
    assert got[KEY_LOG_LEVEL] == "DEBUG"
    # And it feeds resolve_config as an override layer.
    c = resolve_config({}, got)
    assert c.interval == 7.0 and c.dark is False and c.show_host is False


def test_mem_collapsed_roundtrip(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        load_mem_collapsed, save_mem_collapsed,
    )
    ini = tmp_path / "view.ini"
    assert load_mem_collapsed(path=ini) == set()      # nothing stored yet
    save_mem_collapsed({"windows", "docker"}, path=ini)
    assert load_mem_collapsed(path=ini) == {"windows", "docker"}
    # Overwrites, and an empty set clears it.
    save_mem_collapsed({"wsl"}, path=ini)
    assert load_mem_collapsed(path=ini) == {"wsl"}
    save_mem_collapsed(set(), path=ini)
    assert load_mem_collapsed(path=ini) == set()


def test_mem_min_mib_roundtrip(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        DEFAULT_MEM_MIN_MIB, load_mem_min_mib, save_mem_min_mib,
    )
    ini = tmp_path / "view.ini"
    assert load_mem_min_mib(path=ini) == DEFAULT_MEM_MIN_MIB   # historical 1 GiB default
    save_mem_min_mib(512.0, path=ini)
    assert load_mem_min_mib(path=ini) == 512.0
    save_mem_min_mib(0.0, path=ini)                            # 0 = show every process
    assert load_mem_min_mib(path=ini) == 0.0
    save_mem_min_mib(-5.0, path=ini)                           # clamped to >= 0
    assert load_mem_min_mib(path=ini) == 0.0
    # A garbage stored value falls back to the default rather than raising.
    from vision_resource_monitor.settings import KEY_MEM_MIN_MIB, _qsettings
    s = _qsettings(ini); s.setValue(KEY_MEM_MIN_MIB, "notanumber"); s.sync()
    assert load_mem_min_mib(path=ini) == DEFAULT_MEM_MIN_MIB


def test_busy_marker_pct_roundtrip_clamp_and_junk(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        DEFAULT_BUSY_MARKER_PCT, KEY_BUSY_MARKER_PCT, _qsettings,
        load_busy_marker_pct, save_busy_marker_pct,
    )
    ini = tmp_path / "view.ini"
    assert load_busy_marker_pct(path=ini) == DEFAULT_BUSY_MARKER_PCT == 92.0
    save_busy_marker_pct(85.0, path=ini)
    assert load_busy_marker_pct(path=ini) == 85.0
    save_busy_marker_pct(0.0, path=ini)                        # clamped to 1..100
    assert load_busy_marker_pct(path=ini) == 1.0
    save_busy_marker_pct(250.0, path=ini)
    assert load_busy_marker_pct(path=ini) == 100.0
    save_busy_marker_pct("junk", path=ini)                     # junk -> the default
    assert load_busy_marker_pct(path=ini) == DEFAULT_BUSY_MARKER_PCT
    # A garbage stored value falls back to the default rather than raising.
    for bad in ("notanumber", "nan"):
        s = _qsettings(ini); s.setValue(KEY_BUSY_MARKER_PCT, bad); s.sync()
        assert load_busy_marker_pct(path=ini) == DEFAULT_BUSY_MARKER_PCT


def test_port_window_roundtrip_and_clamp(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        KEY_PORT_WINDOW_S, _qsettings, load_port_window_s, save_port_window_s,
    )
    from vision_resource_monitor.state import (
        DEFAULT_PORT_WINDOW_S, PORT_WINDOW_MAX_S, PORT_WINDOW_MIN_S,
    )
    ini = tmp_path / "view.ini"
    assert load_port_window_s(path=ini) == DEFAULT_PORT_WINDOW_S   # unset -> ~3 min
    save_port_window_s(30.0, path=ini)
    assert load_port_window_s(path=ini) == 30.0
    save_port_window_s(1.0, path=ini)                              # below the floor
    assert load_port_window_s(path=ini) == PORT_WINDOW_MIN_S
    save_port_window_s(10 ** 6, path=ini)                          # above the cap
    assert load_port_window_s(path=ini) == PORT_WINDOW_MAX_S
    # A garbage stored value falls back to the default rather than raising.
    s = _qsettings(ini); s.setValue(KEY_PORT_WINDOW_S, "notanumber"); s.sync()
    assert load_port_window_s(path=ini) == DEFAULT_PORT_WINDOW_S


def test_panel_collapsed_roundtrip(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        load_panel_collapsed, save_panel_collapsed,
    )
    ini = tmp_path / "view.ini"
    assert load_panel_collapsed(path=ini) == set()            # nothing collapsed yet
    save_panel_collapsed({"gpu", "ports"}, path=ini)
    assert load_panel_collapsed(path=ini) == {"gpu", "ports"}
    save_panel_collapsed(set(), path=ini)                     # expand-all clears it
    assert load_panel_collapsed(path=ini) == set()


def test_panel_order_roundtrip_and_reconcile(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        PANEL_IDS, load_panel_order, save_panel_order,
    )
    ini = tmp_path / "view.ini"
    assert load_panel_order(path=ini) == list(PANEL_IDS)      # default = canonical
    # A custom order round-trips; unknown ids dropped; missing canonical ids
    # rejoin at their canonical position (after the nearest canonical
    # predecessor present), NOT at the end — a future panel must land in its
    # zone even against a stored order.
    save_panel_order(["ports", "gpu", "bogus"], path=ini)
    got = load_panel_order(path=ini)
    assert got[:2] == ["ports", "gpu"]                        # honoured, in order
    assert "bogus" not in got                                 # unknown dropped
    assert set(got) == set(PANEL_IDS) and len(got) == len(PANEL_IDS)  # complete set
    # gpu's canonical successors follow it, in canonical relative order …
    assert got.index("activity") == got.index("gpu") + 1
    assert got.index("runs") == got.index("activity") + 1
    # … and the watch zone stays a contiguous tail, in canonical order.
    assert got[-4:] == ["forecast", "egress", "scanners", "stuck"]


def test_panel_order_default_regroup_and_zones():
    """The 2026-07-22 regroup: host/memory/ports consecutive (cpu+memory with
    ports directly underneath), the watch-for zone the contiguous tail."""
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import PANEL_IDS, WATCH_PANEL_IDS
    order = list(PANEL_IDS)
    i = order.index("host")
    assert order[i:i + 3] == ["host", "memory", "ports"]
    assert order[-len(WATCH_PANEL_IDS):] == ["forecast", "egress", "scanners",
                                             "stuck"]
    assert WATCH_PANEL_IDS == {"forecast", "egress", "scanners", "stuck"}


def test_runners_expanded_round_trip(tmp_path):
    """The Runners panel's expanded-groups view state (default: every group
    collapsed; the persisted set is the expanded exceptions)."""
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        load_runners_expanded, save_runners_expanded,
    )
    ini = tmp_path / "view.ini"
    assert load_runners_expanded(path=ini) == set()
    save_runners_expanded({"group-a", "group-b"}, path=ini)
    assert load_runners_expanded(path=ini) == {"group-a", "group-b"}
    save_runners_expanded(set(), path=ini)
    assert load_runners_expanded(path=ini) == set()


def test_panel_titles_host_wsl_regroup():
    """The 2026-07-29 host/WSL regroup: ids keep their old names (stored
    view-state follows the same physical panels) while the TITLES carry the
    new meaning — id "host" is the WSL section, id "memory" the Windows-host
    section."""
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import PANEL_DEFS
    titles = dict(PANEL_DEFS)
    assert titles["host"] == "WSL"
    assert titles["memory"] == "Host"


def test_panel_order_stored_pre_egress_gains_it_in_zone(tmp_path):
    """A stored order saved before the Egress panel existed reads back with
    egress inserted INSIDE the watch zone — after forecast, its canonical
    predecessor — not dangling at the end (inserts it at its canonical position
    during reconciliation). Runners (the fleet census panel) rides the
    same reconcile: a stored order predating it gains it after sessions, its
    canonical predecessor."""
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        load_panel_order, save_panel_order,
    )
    ini = tmp_path / "view.ini"
    stored = ["gpu", "activity", "runs", "host", "memory", "ports",
              "sessions", "forecast", "scanners", "stuck"]
    save_panel_order(stored, path=ini)
    assert load_panel_order(path=ini) == [
        "gpu", "activity", "runs", "host", "memory", "ports", "sessions",
        "runners", "forecast", "egress", "scanners", "stuck"]


def test_panel_order_pre_schema_store_regroups_once(tmp_path):
    """An order stored BEFORE the regroup schema (no stamp — every pre-regroup
    build, whether the old default or a hand order like the live install's
    ports-first hack) reads back as the NEW default; an order saved by THIS
    build is stamped and preserved verbatim."""
    pytest.importorskip("PySide6.QtCore")
    from PySide6.QtCore import QSettings
    from vision_resource_monitor.settings import (
        KEY_PANEL_ORDER, PANEL_IDS, load_panel_order, save_panel_order,
    )
    ini = tmp_path / "view.ini"
    # Simulate a pre-regroup store: the order key without the schema stamp
    # (old builds' save_panel_order never wrote one). The live install's
    # actual 2026-07-22 registry value, scanners-less and ports-first.
    s = QSettings(str(ini), QSettings.IniFormat)
    s.setValue(KEY_PANEL_ORDER, "ports,gpu,activity,runs,forecast,host,"
                                "memory,sessions,stuck")
    s.sync()
    assert load_panel_order(path=ini) == list(PANEL_IDS)   # regrouped once
    # An order arranged on THIS build is stamped → the user's word, kept.
    custom = list(PANEL_IDS)
    custom[0], custom[1] = custom[1], custom[0]
    save_panel_order(custom, path=ini)
    assert load_panel_order(path=ini) == custom


def test_panel_order_dedupes_stored_ids(tmp_path):
    """Duplicate ids in a (hand-edited) stored order collapse to the first
    occurrence — a duplicate would build the same panel twice and strand the
    first copy as a floating orphan on the next live restack."""
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        PANEL_IDS, load_panel_order, save_panel_order,
    )
    ini = tmp_path / "view.ini"
    save_panel_order(["gpu", "gpu", "host", "gpu", "host"], path=ini)
    got = load_panel_order(path=ini)
    assert got.count("gpu") == 1 and got.count("host") == 1
    assert len(got) == len(PANEL_IDS) and set(got) == set(PANEL_IDS)


def test_panel_hidden_roundtrip(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        load_panel_hidden, panel_hidden_configured, save_panel_hidden,
    )
    ini = tmp_path / "view.ini"
    assert panel_hidden_configured(path=ini) is False        # never set
    assert load_panel_hidden(path=ini) == set()              # -> all shown
    save_panel_hidden({"host", "ports", "bogus"}, path=ini)   # unknown id dropped
    assert panel_hidden_configured(path=ini) is True
    assert load_panel_hidden(path=ini) == {"host", "ports"}
    save_panel_hidden(set(), path=ini)                        # show-all clears it
    assert load_panel_hidden(path=ini) == set()


def test_start_minimized_roundtrip(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        load_start_minimized, save_start_minimized,
    )
    ini = tmp_path / "view.ini"
    assert load_start_minimized(path=ini) is False    # default off
    save_start_minimized(True, path=ini)
    assert load_start_minimized(path=ini) is True      # survives the ini string round-trip
    save_start_minimized(False, path=ini)
    assert load_start_minimized(path=ini) is False


def test_window_geometry_roundtrip(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from PySide6.QtCore import QByteArray
    from vision_resource_monitor.settings import (
        load_window_geometry, save_window_geometry,
    )
    ini = tmp_path / "view.ini"
    assert load_window_geometry(path=ini) is None      # nothing saved yet
    blob = QByteArray(b"\x01\xd9\x00geometry-bytes\xfe")
    save_window_geometry(blob, path=ini)
    got = load_window_geometry(path=ini)
    assert got is not None
    assert QByteArray(got) == blob                      # binary round-trips through the ini backend


# ── producer sample cadence (monitor-config:v1 channel, app → producer) ──────────

def test_default_monitor_config_path_sits_next_to_snapshot():
    from pathlib import Path as _P
    from vision_resource_monitor import default_monitor_config_path
    assert default_monitor_config_path(_P("/x/y/status.json")) == \
        _P("/x/y/monitor-config.json")


def test_producer_cadence_roundtrip_and_clamp(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        DEFAULT_GPU_INTERVAL_S, DEFAULT_HOST_REFRESH_S, GPU_INTERVAL_MAX_S,
        GPU_INTERVAL_MIN_S, HOST_REFRESH_MAX_S, HOST_REFRESH_MIN_S,
        load_producer_cadence, save_producer_cadence,
    )
    ini = tmp_path / "view.ini"
    # Unset → the defaults.
    assert load_producer_cadence(path=ini) == (DEFAULT_GPU_INTERVAL_S,
                                               DEFAULT_HOST_REFRESH_S)
    # In-range values round-trip.
    save_producer_cadence(4.0, 30.0, path=ini)
    assert load_producer_cadence(path=ini) == (4.0, 30.0)
    # Out-of-range values are clamped on the way in.
    save_producer_cadence(0.1, 9999.0, path=ini)
    assert load_producer_cadence(path=ini) == (GPU_INTERVAL_MIN_S, HOST_REFRESH_MAX_S)
    save_producer_cadence(999.0, 0.0, path=ini)
    assert load_producer_cadence(path=ini) == (GPU_INTERVAL_MAX_S, HOST_REFRESH_MIN_S)
    # A garbage stored value falls back to the default rather than raising.
    from vision_resource_monitor.settings import KEY_GPU_INTERVAL, _qsettings
    s = _qsettings(ini); s.setValue(KEY_GPU_INTERVAL, "notanumber"); s.sync()
    assert load_producer_cadence(path=ini)[0] == DEFAULT_GPU_INTERVAL_S


def test_write_monitor_config_writes_clamped_v1(tmp_path):
    import json
    from vision_resource_monitor.settings import (
        GPU_INTERVAL_MIN_S, HOST_REFRESH_MAX_S, MONITOR_CONFIG_SCHEMA,
        write_monitor_config,
    )
    cfg = tmp_path / "nested" / "monitor-config.json"
    # Out-of-range inputs are clamped in the file the producer will read.
    write_monitor_config(cfg, gpu_interval_s=0.1, host_refresh_s=9999.0)
    data = json.loads(cfg.read_text())
    assert data["schema"] == MONITOR_CONFIG_SCHEMA
    assert data["gpu_interval_s"] == GPU_INTERVAL_MIN_S
    assert data["host_refresh_s"] == HOST_REFRESH_MAX_S
    # Atomic write leaves no temp files behind.
    assert list(cfg.parent.glob("*.tmp")) == []
    # In-range values pass through unchanged; overwrite works.
    write_monitor_config(cfg, gpu_interval_s=3.0, host_refresh_s=20.0)
    data = json.loads(cfg.read_text())
    assert data["gpu_interval_s"] == 3.0 and data["host_refresh_s"] == 20.0


def test_watch_ports_roundtrip(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import load_watch_ports, save_watch_ports
    ini = tmp_path / "view.ini"
    assert load_watch_ports(path=ini) == []                      # unset
    save_watch_ports(["vscode:8080", "22", "  "], path=ini)      # blanks dropped
    assert load_watch_ports(path=ini) == ["vscode:8080", "22"]
    save_watch_ports([], path=ini)                               # clear
    assert load_watch_ports(path=ini) == []


def test_write_monitor_config_includes_watch_ports(tmp_path):
    import json
    from vision_resource_monitor.settings import write_monitor_config
    cfg = tmp_path / "monitor-config.json"
    write_monitor_config(cfg, 3.0, 20.0, watch_ports=["vscode:8080", " ", "22"])
    assert json.loads(cfg.read_text())["watch_ports"] == ["vscode:8080", "22"]
    # Omitted watch_ports -> [] (the app writes the FULL desired state each time).
    write_monitor_config(cfg, 3.0, 20.0)
    assert json.loads(cfg.read_text())["watch_ports"] == []


def test_active_style_roundtrip(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        KEY_ACTIVE_STYLE, _qsettings, load_active_style, save_active_style,
    )
    from vision_resource_monitor.state import (
        ACTIVE_PULSE, ACTIVE_SUBTLE, DEFAULT_ACTIVE_STYLE,
    )
    ini = tmp_path / "view.ini"
    assert load_active_style(path=ini) == DEFAULT_ACTIVE_STYLE   # unset -> default
    save_active_style(ACTIVE_PULSE, path=ini)
    assert load_active_style(path=ini) == ACTIVE_PULSE
    save_active_style(ACTIVE_SUBTLE, path=ini)
    assert load_active_style(path=ini) == ACTIVE_SUBTLE
    save_active_style("neon", path=ini)                          # unknown -> default
    assert load_active_style(path=ini) == DEFAULT_ACTIVE_STYLE
    s = _qsettings(ini); s.setValue(KEY_ACTIVE_STYLE, "sparkle"); s.sync()
    assert load_active_style(path=ini) == DEFAULT_ACTIVE_STYLE   # junk on disk -> default


# ── ad-hoc watched-port spec helpers (the "+ Watch a port…" / Unwatch path) ──────

def test_parse_watch_spec_mirrors_producer_forms():
    from vision_resource_monitor.settings import parse_watch_spec
    assert parse_watch_spec("vscode:8080") == ("vscode", 8080)
    assert parse_watch_spec("22") == ("22", 22)              # bare port self-labels
    assert parse_watch_spec(" ssh : 22 ") == ("ssh", 22)     # whitespace trimmed
    assert parse_watch_spec(":443") == ("443", 443)          # empty label self-labels


def test_parse_watch_spec_rejects_malformed_and_out_of_range():
    from vision_resource_monitor.settings import parse_watch_spec
    for bad in ("", "nope", "x:", "x:port", "x:-1", "x:0", "x:65536", None):
        with pytest.raises(ValueError):
            parse_watch_spec(bad)


def test_spec_port_is_lenient():
    from vision_resource_monitor.settings import spec_port
    assert spec_port("vscode:8080") == 8080
    assert spec_port("22") == 22
    assert spec_port("junk") is None
    assert spec_port("") is None


def test_add_watch_spec_appends_never_mutates_and_dedups_by_port():
    from vision_resource_monitor.settings import add_watch_spec
    specs = ["vscode:8080"]
    out = add_watch_spec(specs, "22")
    assert out == ["vscode:8080", "22"]
    assert specs == ["vscode:8080"]                # input untouched
    # Same port under another label -> no-op (the producer dedups by port,
    # first label wins — a second spec would silently do nothing).
    assert add_watch_spec(out, "ssh:22") == out


def test_add_watch_spec_canonicalises_and_validates():
    from vision_resource_monitor.settings import add_watch_spec
    assert add_watch_spec([], "  api : 9000 ") == ["api:9000"]
    assert add_watch_spec([], " 9000 ") == ["9000"]
    with pytest.raises(ValueError):
        add_watch_spec([], "bogus")


def test_remove_watch_port_drops_every_spec_for_that_port_only():
    from vision_resource_monitor.settings import remove_watch_port
    assert remove_watch_port(["a:5", "b:5", "6"], 5) == ["6"]
    assert remove_watch_port(["6"], 7) == ["6"]              # unknown -> unchanged
    # A malformed stored spec is kept: a remove only ever removes the port it
    # names, never silently repairs the rest of the stored state.
    assert remove_watch_port(["junk", "8"], 8) == ["junk"]


def test_pending_watch_ports_diffs_desired_against_reported():
    from vision_resource_monitor.settings import pending_watch_ports
    desired = ["vscode:8080", "tmp:9999", "22", "junk", "dup:9999"]
    # 8080 is reported; 9999 + 22 are pending; junk skipped; dup collapses.
    assert pending_watch_ports(desired, [8080]) == [("tmp", 9999), ("22", 22)]
    assert pending_watch_ports(desired, [8080, 9999, 22]) == []
    assert pending_watch_ports([], []) == []
    assert pending_watch_ports(None, None) == []


def test_parse_watch_spec_rejects_commas_and_nonascii_digits():
    from vision_resource_monitor.settings import parse_watch_spec
    # The persisted QSettings form comma-joins specs — a comma label would
    # silently split into phantom specs on reload (reviewer H2).
    for bad in ("a,b:8080", ",22", "x:2,2"):
        with pytest.raises(ValueError):
            parse_watch_spec(bad)
    # .isdigit() alone admits non-ASCII digits ('²' then blows up int();
    # '١٢' would silently become port 12) — ASCII-only keeps the message
    # crafted and the behavior boring.
    for bad in ("x:²", "x:١٢"):
        with pytest.raises(ValueError):
            parse_watch_spec(bad)


def test_watch_specs_roundtrip_through_qsettings(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        add_watch_spec, load_watch_ports, save_watch_ports)
    ini = tmp_path / "wp.ini"
    specs = add_watch_spec(add_watch_spec([], "vscode:8080"), "22")
    save_watch_ports(specs, path=ini)
    # What add_watch_spec built survives the comma-joined store byte-for-byte
    # (the reviewer's H2: a comma label would come back as phantom specs).
    assert load_watch_ports(path=ini) == ["vscode:8080", "22"]


def test_clean_proc_targets_validates_and_dedups():
    from vision_resource_monitor.settings import clean_proc_targets
    raw = [
        {"side": "wsl", "pid": 501, "name": " python "},
        {"side": "windows", "pid": "2632"},
        {"side": "wsl", "pid": 501, "name": "dup"},      # dup (side, pid)
        {"side": "mars", "pid": 1},                       # bad side
        {"side": "wsl", "pid": 0},                        # non-positive
        {"side": "wsl"},                                  # no pid
        "junk",                                            # non-dict
    ]
    assert clean_proc_targets(raw) == [
        {"side": "wsl", "pid": 501, "name": "python"},
        {"side": "windows", "pid": 2632, "name": None},
    ]
    assert clean_proc_targets(None) == []


def test_write_monitor_config_includes_watch_procs(tmp_path):
    import json
    from vision_resource_monitor.settings import write_monitor_config
    path = tmp_path / "mc.json"
    write_monitor_config(path, 2.0, 10.0, watch_ports=["ssh:22"],
                         watch_procs=[{"side": "wsl", "pid": 501,
                                       "name": "python"}, "junk"])
    data = json.loads(path.read_text())
    assert data["watch_procs"] == [{"side": "wsl", "pid": 501,
                                    "name": "python"}]
    # Omitted → an explicit empty list ("target nothing"), same full-state
    # semantics as watch_ports.
    write_monitor_config(path, 2.0, 10.0)
    assert json.loads(path.read_text())["watch_procs"] == []


def test_parse_watch_proc_name_valid_forms():
    from vision_resource_monitor.settings import parse_watch_proc_name
    assert parse_watch_proc_name("wsl:node") == {
        "side": "wsl", "pid": None, "name": "node"}
    # Side is case-insensitive; the NAME keeps its case (matching lowers at
    # match time, display keeps what the user typed — the producer's rule).
    assert parse_watch_proc_name(" WINDOWS : Python ") == {
        "side": "windows", "pid": None, "name": "Python"}
    # A windows name may exceed the wsl comm limit (ProcessName isn't 15-capped).
    assert parse_watch_proc_name("windows:" + "x" * 20)["name"] == "x" * 20


def test_parse_watch_proc_name_rejects():
    import pytest
    from vision_resource_monitor.settings import (
        PROC_NAME_SPEC_MAX, WSL_COMM_MAX, parse_watch_proc_name,
    )
    for bad, why in [
        ("node", "no side prefix"),
        ("mars:node", "bad side"),
        ("wsl:", "empty name"),
        ("", "empty spec"),
        ("wsl:1234", "all-digits = a PID, not a name"),
        ("wsl:-5", "signed pid typo, never a name"),
        ("windows:+12", "signed pid typo, never a name"),
        ("wsl:a,b", "comma would split the persisted comma-join"),
        ("wsl:a\tb", "control character"),
        ("windows:" + "x" * (PROC_NAME_SPEC_MAX + 1), "too long"),
        ("wsl:" + "x" * (WSL_COMM_MAX + 1), "longer than a wsl comm"),
    ]:
        with pytest.raises(ValueError):
            parse_watch_proc_name(bad), why


def test_clean_proc_targets_name_entries():
    from vision_resource_monitor.settings import clean_proc_targets
    raw = [
        {"side": "wsl", "pid": None, "name": " node "},
        {"side": "wsl", "name": "NODE"},                  # dup (side, lowered)
        {"side": "windows", "name": "python"},
        {"side": "wsl", "name": ""},                      # empty — dropped
        {"side": "wsl", "name": "a\nb"},                  # control char — dropped
        {"side": "wsl", "name": "x" * 65},                # too long — dropped
        # The normalizer enforces the same no-comma / not-pid-like rules as
        # the typed-entry parser: a comma'd name would reload as a DIFFERENT
        # valid watch through the comma-joined QSettings form, and a bare
        # integer belongs to the pid space (proc_target_key's collision
        # argument must hold where data is normalized, not only typed).
        {"side": "wsl", "name": "a,b"},                   # comma — dropped
        {"side": "wsl", "name": "123"},                   # pid-like — dropped
        {"side": "wsl", "name": "-5"},                    # signed pid-like — dropped
        {"side": "wsl", "pid": 501, "name": "python"},    # pid entry untouched
    ]
    assert clean_proc_targets(raw) == [
        {"side": "wsl", "pid": None, "name": "node"},
        {"side": "windows", "pid": None, "name": "python"},
        {"side": "wsl", "pid": 501, "name": "python"},
    ]


def test_watch_proc_names_persist_roundtrip(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        KEY_WATCH_PROC_NAMES, _qsettings, load_watch_proc_names,
        save_watch_proc_names,
    )
    ini = tmp_path / "view.ini"
    assert load_watch_proc_names(ini) == []
    # The saver filters session pid targets out itself — callers hand the
    # full mixed list and only the NAME watches persist.
    save_watch_proc_names([
        {"side": "wsl", "pid": None, "name": "node"},
        {"side": "wsl", "pid": 501, "name": "python"},    # session-only: dropped
        {"side": "windows", "pid": None, "name": "Python"},
    ], path=ini)
    assert load_watch_proc_names(ini) == [
        {"side": "wsl", "pid": None, "name": "node"},
        {"side": "windows", "pid": None, "name": "Python"},
    ]
    # Malformed persisted tokens (hand-edited / stale) are skipped fail-open.
    s = _qsettings(ini)
    s.setValue(KEY_WATCH_PROC_NAMES, "wsl:node,garbage,mars:x,wsl:1234")
    s.sync()
    assert load_watch_proc_names(ini) == [
        {"side": "wsl", "pid": None, "name": "node"}]


def test_write_monitor_config_includes_name_entries(tmp_path):
    import json
    from vision_resource_monitor.settings import write_monitor_config
    path = tmp_path / "mc.json"
    write_monitor_config(path, 2.0, 10.0, watch_procs=[
        {"side": "wsl", "pid": None, "name": "node"},
        {"side": "windows", "pid": 2632, "name": "dwm"},
    ])
    assert json.loads(path.read_text())["watch_procs"] == [
        {"side": "wsl", "pid": None, "name": "node"},
        {"side": "windows", "pid": 2632, "name": "dwm"},
    ]


def test_rejected_port_specs_names_only_the_bad_tokens():
    # The live Settings cue: the producer drops a malformed config entry
    # SILENTLY, so a reject must be named while still editable .
    from vision_resource_monitor.settings import rejected_port_specs
    assert rejected_port_specs([]) == []
    assert rejected_port_specs(None) == []
    assert rejected_port_specs(["ssh:22", "8080"]) == []
    msgs = rejected_port_specs(["ssh:22", "foo:bar", "x:70000"])
    assert len(msgs) == 2
    assert "foo:bar" in msgs[0] and "70000" in msgs[1]


def test_rejected_proc_specs_names_only_the_bad_tokens():
    from vision_resource_monitor.settings import rejected_proc_specs
    assert rejected_proc_specs([]) == []
    assert rejected_proc_specs(["wsl:node", "windows:python"]) == []
    msgs = rejected_proc_specs(["wsl:node", "sideless", "wsl:1234"])
    assert len(msgs) == 2
    assert "sideless" in msgs[0] and "PID" in msgs[1]


def test_write_monitor_config_carries_session_ports(tmp_path):
    import json
    from vision_resource_monitor.settings import write_monitor_config
    path = tmp_path / "mc.json"
    write_monitor_config(path, 2.0, 10.0)
    assert json.loads(path.read_text())["session_ports"] is True   # default on
    write_monitor_config(path, 2.0, 10.0, session_ports=False)
    assert json.loads(path.read_text())["session_ports"] is False


def test_session_ports_roundtrip_and_default_on(tmp_path):
    pytest.importorskip("PySide6.QtCore")
    from vision_resource_monitor.settings import (
        load_session_ports, save_session_ports)
    ini = tmp_path / "settings.ini"
    assert load_session_ports(path=ini) is True          # unset → on
    save_session_ports(False, path=ini)
    assert load_session_ports(path=ini) is False
    save_session_ports(True, path=ini)
    assert load_session_ports(path=ini) is True


def test_stored_truth_config_ignores_any_cli_layer(tmp_path, monkeypatch):
    # The Settings dialog must pre-fill from the STORE alone — pre-filling
    # from the flag-merged effective config lets OK silently promote a session
    # CLI flag into the persisted store.
    pytest.importorskip("PySide6.QtCore")
    monkeypatch.delenv("VRM_LOG_LEVEL", raising=False)
    from vision_resource_monitor.settings import (
        save_stored, stored_truth_config)
    ini = tmp_path / "settings.ini"
    c0 = stored_truth_config(path=ini)                   # empty store → defaults
    assert c0.interval == 2.0
    save_stored({KEY_INTERVAL: 7.5, KEY_THEME: "light"}, path=ini)
    c = stored_truth_config(path=ini)
    assert c.interval == 7.5 and c.theme == "light"


def test_hosted_burst_repo_validation_and_roundtrip(tmp_path):
    from vision_resource_monitor.settings import (
        HOSTED_BURST_REPOS_MAX, clean_hosted_repos, load_hosted_bursts,
        rejected_hosted_repo_specs, save_hosted_bursts)
    assert clean_hosted_repos("o/r, o/r, k/s") == ["o/r", "k/s"]
    assert clean_hosted_repos(["bad repo", "a/b/c", "", None, 7]) == []
    many = [f"o/r{i}" for i in range(9)]
    assert clean_hosted_repos(many) == many[:HOSTED_BURST_REPOS_MAX]
    # The live cue names invalid tokens AND over-cap drops (the producer
    # skips both silently).
    msgs = rejected_hosted_repo_specs("plain, o/r")
    assert msgs and "plain" in msgs[0] and "not owner/repo" in msgs[0]
    over = rejected_hosted_repo_specs([f"o/r{i}" for i in range(6)])
    assert any("cap" in m for m in over)
    assert rejected_hosted_repo_specs("o/r") == []
    # Persistence round-trip validates on BOTH sides.
    pytest.importorskip("PySide6.QtCore")
    ini = tmp_path / "s.ini"
    save_hosted_bursts(["o/r", "junk entry"], path=ini)
    assert load_hosted_bursts(path=ini) == ["o/r"]
    save_hosted_bursts([], path=ini)
    assert load_hosted_bursts(path=ini) == []


def test_write_monitor_config_carries_hosted_bursts(tmp_path):
    import json
    from vision_resource_monitor.settings import write_monitor_config
    p = tmp_path / "mc.json"
    write_monitor_config(p, 2.0, 10.0, hosted_bursts=["o/r", "bad repo"])
    assert json.loads(p.read_text())["hosted_bursts"] == ["o/r"]
    # A full-state writer always carries the key — an omitted field would
    # fall the producer back to its CLI default;
    # an explicit [] is a real OFF.
    write_monitor_config(p, 2.0, 10.0)
    assert json.loads(p.read_text())["hosted_bursts"] == []
