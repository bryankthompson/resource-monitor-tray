from pathlib import Path

import json

import pytest

from vision_resource_monitor import build_version, fake_producer
from vision_resource_monitor.snapshot import (
    SNAPSHOT_SCHEMA, GpuUser, Snapshot, SnapshotError, load, parse,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_load_full_fixture():
    snap = load(FIXTURES / "snapshot_active.json")
    assert snap.schema == SNAPSHOT_SCHEMA
    assert snap.host == "demo-host"
    assert snap.gpu is not None
    assert snap.gpu.name.startswith("NVIDIA")
    assert snap.gpu.util == 87.0
    assert snap.is_active
    assert snap.activity.workloads == ["tracking-run", "ollama:qwen2.5:7b"]
    assert len(snap.history) == 1
    assert snap.docker.gpu_containers[0].image.startswith("pytorch/")
    assert snap.ollama[0].name == "qwen2.5:7b"
    assert snap.host_cpu.cores_total == 28
    # The fixture predates this split: None = unknown, never 0.
    assert snap.host_cpu.user_pct is None and snap.host_cpu.sys_pct is None
    assert snap.runs[0].state == "running"
    assert snap.runs[0].percent == 42


def test_host_cpu_split_parses():
    cpu = Snapshot.from_dict({"host_cpu": {
        "pct": 25.0, "user_pct": 18.3, "sys_pct": 6.2}}).host_cpu
    assert cpu.user_pct == 18.3 and cpu.sys_pct == 6.2
    # Junk/absent halves fall to None independently (the _fo family).
    cpu2 = Snapshot.from_dict({"host_cpu": {
        "pct": 25.0, "user_pct": "nope"}}).host_cpu
    assert cpu2.user_pct is None and cpu2.sys_pct is None


def test_windows_cpu_parses():
    wc = Snapshot.from_dict({"windows_cpu": {
        "utility_pct": 48.7, "time_pct": 30.9, "cores": 28}}).windows_cpu
    assert (wc.utility_pct, wc.time_pct, wc.cores) == (48.7, 30.9, 28)
    # Absent / null (older producer, no host sweep, counter failure) → None.
    assert Snapshot.from_dict({}).windows_cpu is None
    assert Snapshot.from_dict({"windows_cpu": None}).windows_cpu is None
    # A non-dict shape must neither crash parse() (the reader's
    # last-good-frame guarantee) nor fabricate a reading.
    assert Snapshot.from_dict({"windows_cpu": True}).windows_cpu is None
    # Unusable values keep per-field honesty: None, never 0-strain.
    wc2 = Snapshot.from_dict({"windows_cpu": {
        "utility_pct": "nope", "time_pct": 30.9}}).windows_cpu
    assert wc2.utility_pct is None and wc2.time_pct == 30.9
    # A feed predating cores → None (never a guessed count); JSON `true`
    # must not fabricate 1 core (bool ⊂ int); junk → None.
    assert wc2.cores is None
    assert Snapshot.from_dict({"windows_cpu": {
        "utility_pct": 1.0, "time_pct": 1.0,
        "cores": True}}).windows_cpu.cores is None
    assert Snapshot.from_dict({"windows_cpu": {
        "utility_pct": 1.0, "time_pct": 1.0,
        "cores": "many"}}).windows_cpu.cores is None
    # Turbo pushes utility past 100 — parsed through, never clamped.
    assert Snapshot.from_dict({"windows_cpu": {
        "utility_pct": 132.4, "time_pct": 88.0}}).windows_cpu.utility_pct == 132.4


def test_windows_cpu_history_parses():
    # The cpu_history list convention: [] on absent/older feeds, tolerant of
    # junk entries, never None.
    assert Snapshot.from_dict({}).windows_cpu_history == []
    assert Snapshot.from_dict(
        {"windows_cpu_history": None}).windows_cpu_history == []
    snap = Snapshot.from_dict(
        {"windows_cpu_history": [82.2, 85.7, "x", 83.4]})
    assert snap.windows_cpu_history == [82.2, 85.7, 0.0, 83.4]


def test_empty_dict_yields_defaults():
    snap = Snapshot.from_dict({})
    assert snap.schema == SNAPSHOT_SCHEMA
    assert snap.gpus == []
    assert snap.gpu is None
    assert snap.activity is None
    assert not snap.is_active
    assert snap.docker is None
    assert snap.ollama is None
    assert snap.memory is None


def test_missing_optional_fields_are_tolerated():
    snap = Snapshot.from_dict({"gpus": [{"name": "X"}]})
    assert snap.gpu.name == "X"
    assert snap.gpu.util == 0.0        # missing numeric -> 0
    assert snap.gpu.mem_total_mib == 0.0


def test_bad_types_do_not_crash():
    snap = Snapshot.from_dict({
        "gpus": "not-a-list",
        "activity": "not-a-dict",
        "runs": [{"percent": "nope", "last_t": "bad"}],
    })
    assert snap.gpus == []
    assert snap.activity is None
    assert snap.runs[0].percent is None   # unparseable optional int -> None
    assert snap.runs[0].last_t == 0.0     # unparseable float -> 0.0


def test_wrong_schema_raises():
    with pytest.raises(SnapshotError):
        Snapshot.from_dict({"schema": "monitor-snapshot:v2"})


def test_parse_invalid_json_raises():
    with pytest.raises(SnapshotError):
        parse("{not json")


def test_parse_non_object_raises():
    with pytest.raises(SnapshotError):
        parse("[1, 2, 3]")


def test_source_version_parsed_when_present():
    snap = Snapshot.from_dict({"source_version": "abc1234"})
    assert snap.source_version == "abc1234"


def test_source_version_absent_or_empty_is_none():
    assert Snapshot.from_dict({}).source_version is None            # older producer
    assert Snapshot.from_dict({"source_version": ""}).source_version is None
    assert Snapshot.from_dict({"source_version": None}).source_version is None
    # A non-string is tolerated (coerced to None, never raises).
    assert Snapshot.from_dict({"source_version": 123}).source_version is None


def test_build_version_none_when_running_from_source():
    # No _build_version.py is generated in a source checkout, so the app
    # performs no stale check.
    assert build_version() is None


def test_update_status_line_variants(monkeypatch):
    import vision_resource_monitor as package
    monkeypatch.setattr(package, "PUBLIC_BUILD", False)
    from vision_resource_monitor import update_status_line
    assert update_status_line(None, "x").startswith("Running from source")  # no build
    assert "unknown" in update_status_line("a", None).lower()               # no source
    assert update_status_line("a", "a") == "Up to date (build a)."          # match
    s = update_status_line("a", "b")                                         # mismatch
    assert "Update available" in s and "a" in s and "b" in s


def test_wsl_rebuild_argv_and_hint(monkeypatch):
    import vision_resource_monitor as package
    monkeypatch.setattr(package, "PUBLIC_BUILD", False)
    from vision_resource_monitor import rebuild_command_hint, wsl_rebuild_argv
    argv = wsl_rebuild_argv()
    assert argv[0] == "wsl.exe" and "bash" in argv
    assert argv[-1] == "cd ~/vision-resource-monitor && vrm rebuild --relaunch"
    assert rebuild_command_hint() == "vrm rebuild"


def test_fake_producer_roundtrips_active_and_idle():
    # elapsed=10 -> rising into the active half; elapsed=0 -> idle start.
    active = Snapshot.from_dict(fake_producer._build(now=1010.0, start=1000.0, host="T"))
    assert active.schema == SNAPSHOT_SCHEMA
    assert active.gpu is not None
    idle = Snapshot.from_dict(fake_producer._build(now=1000.0, start=1000.0, host="T"))
    assert idle.gpu is not None
    # Whatever the phase, the object always round-trips cleanly.
    assert isinstance(active.gpu_util_history, list)


# ── processes attribution block (cross-port from mac; drives the Sessions panel) ──

def _processes_dict():
    return {
        "node_total": 96, "claude_total": 3,
        "sessions": [
            {"pid": 100, "label": "dir-review-cycle · task-0", "node_count": 12,
             "rss_mib": 3200.0, "age_s": 5400.0, "stale": False,
             "child_pids": [7002, 7001, 7003], "child_total": 3},
            {"pid": 101, "label": "interactive · vrm", "node_count": 9,
             "rss_mib": 2100.0, "age_s": 46800.0, "stale": True},
            {"pid": 102, "label": "interactive", "node_count": 3,
             "rss_mib": 640.0, "age_s": 300.0, "stale": False}],
        "high_cost": [
            {"pid": 900, "comm": "Code Helper", "rss_mib": 2662.0,
             "cpu_s": 5400.0, "age_s": 46800.0}],
        "load": [4.2, 3.1, 2.4], "ncpu": 28,
        "swap": {"used_mib": 400.0, "total_mib": 8192.0, "pct": 4.9,
                 "top": [{"name": "claude", "pid": 10, "swap_mib": 188.0},
                         {"name": "node", "pid": 20, "swap_mib": 40.0}]},
        "pressure": "normal", "alerts": ["1 session(s) older than 12h"],
    }


def test_processes_block_parses():
    snap = Snapshot.from_dict({"processes": _processes_dict()})
    p = snap.processes
    assert p is not None
    assert p.node_total == 96 and p.claude_total == 3
    assert [s.label for s in p.sessions][:2] == ["dir-review-cycle · task-0",
                                                 "interactive · vrm"]
    assert p.sessions[1].stale is True and p.sessions[1].node_count == 9
    assert p.sessions[1].age_s == 46800.0
    assert p.high_cost[0].comm == "Code Helper" and p.high_cost[0].cpu_s == 5400.0
    assert p.load == [4.2, 3.1, 2.4] and p.ncpu == 28
    assert p.swap.total_mib == 8192.0 and p.pressure == "normal"
    # Swap now carries pct + the largest private-swap holders (a floor).
    assert p.swap.pct == 4.9
    assert [(h.name, h.pid, h.swap_mib) for h in p.swap.top] == [
        ("claude", 10, 188.0), ("node", 20, 40.0)]
    assert "1 session(s) older than 12h" in p.alerts


def test_session_idle_fields_parse_safely():
    snap = Snapshot.from_dict({"processes": {"sessions": [
        {"last_event_age_s": 3120.5, "swap_mib": 24.5, "idle": True},
        {"last_event_age_s": None, "swap_mib": "bad", "idle": "true"},
        {"last_event_age_s": float("inf"), "swap_mib": True, "idle": 1},
        {},
    ]}})
    a, b, c, d = snap.processes.sessions
    assert (a.last_event_age_s, a.swap_mib, a.idle) == (3120.5, 24.5, True)
    assert (b.last_event_age_s, b.swap_mib, b.idle) == (None, None, None)
    assert (c.last_event_age_s, c.swap_mib, c.idle) == (None, None, None)
    assert (d.last_event_age_s, d.swap_mib, d.idle) == (None, None, None)


def test_session_child_pid_index_parses():
    p = Snapshot.from_dict({"processes": _processes_dict()}).processes
    # Producer order (fattest first) is preserved; the true count rides along.
    assert p.sessions[0].child_pids == [7002, 7001, 7003]
    assert p.sessions[0].child_total == 3
    # A feed predating the child index (no fields): empty index + UNKNOWN total —
    # root-only matching, never a fabricated "no children".
    assert p.sessions[1].child_pids == [] and p.sessions[1].child_total is None


def test_session_child_pid_index_junk_safe():
    s = Snapshot.from_dict({"processes": {"sessions": [
        {"pid": 1, "label": "x",
         # junk / non-positive entries drop (a fabricated 0 would let a "0"
         # needle match every session); bools drop EXPLICITLY (bool is an int
         # subclass — a JSON `true` would otherwise fabricate pid 1; review
         # find); out-of-range ints drop (a 400-digit int would land in the
         # via-child row note); inf must not raise (the _i/_io OverflowError
         # family)
         "child_pids": [7001, "nope", None, 0, -4, True, False, 10 ** 40,
                        float("inf"), 7002],
         "child_total": float("inf")}]}}).processes.sessions[0]
    assert s.child_pids == [7001, 7002]
    assert s.child_total is None
    # `"child_total": true` -> 1 with an empty list would fire the truncation
    # caveat off pure junk — bools read as unknown, never a count.
    s2 = Snapshot.from_dict({"processes": {"sessions": [
        {"pid": 1, "label": "x", "child_pids": [], "child_total": True}]}}
    ).processes.sessions[0]
    assert s2.child_total is None


def test_session_ports_census_parses_with_none_vs_empty_contract():
    # None-vs-[] is contract: census-didn't-run (absent / junk / non-list)
    # parses None — the renderer must never read it as "no ports" — while a
    # real [] survives as ran-but-owns-nothing. Entries share the child_pids
    # bool guard (JSON `true` would fabricate port 1) plus the valid port
    # range; port_total keeps the bool-reads-as-unknown rule.
    rows = Snapshot.from_dict({"processes": {"sessions": [
        {"pid": 1, "label": "a", "ports": [5173, 8080], "port_total": 2},
        {"pid": 2, "label": "b", "ports": [], "port_total": 0},
        {"pid": 3, "label": "c"},                              # pre-census feed
        {"pid": 4, "label": "d", "ports": "junk", "port_total": True},
        {"pid": 5, "label": "e",
         "ports": [8080, "nope", None, 0, -4, True, False, 70000,
                   float("inf"), 443],
         "port_total": float("inf")}]}}).processes.sessions
    assert rows[0].ports == [5173, 8080] and rows[0].port_total == 2
    assert rows[1].ports == [] and rows[1].port_total == 0
    assert rows[2].ports is None and rows[2].port_total is None
    assert rows[3].ports is None and rows[3].port_total is None
    assert rows[4].ports == [8080, 443] and rows[4].port_total is None


def test_session_cockpit_metadata_is_optional_and_tolerant():
    rows = Snapshot.from_dict({"processes": {"sessions": [
        {"cockpit": "vrm", "lane": "ui", "role": "lane", "state": "busy",
         "name": "vrm-ui", "domain": "code-dev"},
        {},
        {"cockpit": 42, "lane": [], "role": "unknown", "state": "working",
         "name": False, "domain": {}}]}}).processes.sessions
    assert (rows[0].cockpit, rows[0].lane, rows[0].role, rows[0].state,
            rows[0].name, rows[0].domain) == (
                "vrm", "ui", "lane", "busy", "vrm-ui", "code-dev")
    assert all(getattr(rows[1], key) is None for key in (
        "cockpit", "lane", "role", "state", "name", "domain"))
    assert all(getattr(rows[2], key) is None for key in (
        "cockpit", "lane", "role", "state", "name", "domain"))


def test_swap_parses_without_pct_or_top():
    # An older producer sends only used/total — pct None, top empty, no crash.
    sw = Snapshot.from_dict({"processes": {
        "swap": {"used_mib": 400.0, "total_mib": 8192.0}}}).processes.swap
    assert sw.used_mib == 400.0 and sw.pct is None and sw.top == []
    # A malformed holder row falls back to a defaulted SwapHolder.
    sw2 = Snapshot.from_dict({"processes": {
        "swap": {"used_mib": 1.0, "total_mib": 2.0, "top": ["nope", {"name": "x"}]}}}
    ).processes.swap
    assert [h.name for h in sw2.top] == ["?", "x"]


def test_processes_absent_on_a_gpu_feed():
    # A GPU-only feed (the box fixture predates the block) parses with no crash and
    # leaves processes None — the Sessions panel then shows "no session data".
    snap = load(FIXTURES / "snapshot_active.json")
    assert snap.processes is None


def test_processes_bad_types_do_not_crash():
    # Non-dict processes -> None; a malformed session row falls back to defaults.
    assert Snapshot.from_dict({"processes": "nope"}).processes is None
    snap = Snapshot.from_dict({"processes": {
        "node_total": "bad", "sessions": ["not-a-dict"], "load": "nope",
        "swap": "x", "pressure": 5}})
    p = snap.processes
    assert p.node_total == 0                  # unparseable int -> default
    assert p.sessions[0].label == "?"         # non-dict row -> defaulted Session
    assert p.load is None and p.swap is None   # non-list/non-dict optionals -> None
    assert p.pressure is None                  # non-string optional -> None


def test_fake_producer_carries_a_processes_block():
    snap = Snapshot.from_dict(fake_producer._build(now=1010.0, start=1000.0, host="T"))
    assert snap.processes is not None
    assert snap.processes.claude_total == len(snap.processes.sessions)
    assert any(s.stale for s in snap.processes.sessions)
    assert snap.processes.high_cost and snap.processes.node_total > 0


# ── forecast + contributors (pre-thrash early-warning; drives the Forecast panel) ──

def _forecast_dict():
    return {
        "level": "warn", "eta_min": 8.3, "signal": "reclaimable headroom",
        "reason": "reclaimable headroom nearly exhausted and shrinking",
        "headroom_mib": 1400.0, "compressed_slope_mib_s": 2.9,
    }


def _contributors_list():
    return [
        {"kind": "memory_overcommit", "rank": 1, "label": "memory overcommit",
         "summary": "reclaimable headroom 1.4G, exhausted ~8 min",
         "action": "reclaim RAM now (~8 min of headroom left) — reduce the "
                   "contributors below before the compressor/swap engage",
         "metric": {"eta_min": 8.3, "headroom_mib": 1400.0}},
        {"kind": "stale_high_cost", "rank": 2, "label": "stale high-cost process",
         "summary": "1 stale high-cost process (Code Helper (Renderer))",
         "action": "restart or close Code Helper (Renderer) [PID 8123] to reclaim ~2.6G",
         "metric": {"count": 1, "top_pid": 8123, "top_rss_mib": 2662.0}},
    ]


def test_forecast_and_contributors_parse():
    snap = Snapshot.from_dict(
        {"forecast": _forecast_dict(), "contributors": _contributors_list()})
    fc = snap.forecast
    assert fc is not None
    assert fc.level == "warn" and fc.eta_min == 8.3
    assert fc.signal == "reclaimable headroom" and fc.headroom_mib == 1400.0
    assert fc.compressed_slope_mib_s == 2.9
    assert [c.kind for c in snap.contributors] == ["memory_overcommit",
                                                   "stale_high_cost"]
    top = snap.contributors[0]
    assert top.rank == 1 and top.label == "memory overcommit"
    assert top.summary.startswith("reclaimable headroom")
    assert "reclaim RAM now" in top.action
    assert top.metric["headroom_mib"] == 1400.0        # metric kept as-is


def test_forecast_absent_and_contributors_default_on_a_gpu_feed():
    # A GPU-only feed (or an older producer / --no-forecast) simply lacks the
    # fields — forecast stays None, contributors is an empty list (not None), so
    # the panel shows "no forecast data" rather than crashing.
    snap = load(FIXTURES / "snapshot_active.json")
    assert snap.forecast is None
    assert snap.contributors == []
    assert Snapshot.from_dict({}).forecast is None
    assert Snapshot.from_dict({}).contributors == []


def test_forecast_quiet_frame_parses_at_level_ok():
    # A healthy tick: level ok with the numeric fields absent still parses cleanly.
    snap = Snapshot.from_dict({"forecast": {"level": "ok"}})
    assert snap.forecast.level == "ok"
    assert snap.forecast.eta_min is None and snap.forecast.headroom_mib is None
    assert snap.forecast.reason == ""


def test_forecast_contributors_bad_types_do_not_crash():
    # Non-dict forecast -> None; non-list contributors -> []; a malformed entry
    # falls back to a defaulted Contributor.
    assert Snapshot.from_dict({"forecast": "nope"}).forecast is None
    assert Snapshot.from_dict({"contributors": "nope"}).contributors == []
    snap = Snapshot.from_dict({
        "forecast": {"level": 5, "eta_min": "bad", "headroom_mib": "x"},
        "contributors": ["not-a-dict", {"rank": "bad", "metric": "x"}]})
    assert snap.forecast.level == "ok"          # non-string level -> default
    assert snap.forecast.eta_min is None        # unparseable optional float -> None
    assert snap.forecast.headroom_mib is None
    assert snap.contributors[0].label == "?"    # non-dict entry -> defaulted
    assert snap.contributors[1].rank == 0       # unparseable int -> default
    assert snap.contributors[1].metric == {}    # non-dict metric -> {}


def test_fake_producer_forecast_cycles_ok_watch_warn():
    # The synthetic feed's headroom dips to ~1G at the cycle peak, so the forecast
    # level tracks the phase: idle start = ok, quarter-phase = watch, peak = warn
    # with a worst-first contributors list (the live-demo path for the panel).
    ok = Snapshot.from_dict(fake_producer._build(now=1000.0, start=1000.0, host="T"))
    assert ok.forecast.level == "ok" and ok.contributors == []
    warn = Snapshot.from_dict(fake_producer._build(now=1020.0, start=1000.0, host="T"))
    assert warn.forecast.level == "warn"
    assert [c.rank for c in warn.contributors] == [1, 2, 3]
    assert all(c.action for c in warn.contributors)     # every entry has an action
    assert warn.forecast.eta_min is not None


# ── watched ports (producer `ports`; drives the Ports panel) ─────────────────────

def test_ports_field_parses():
    snap = Snapshot.from_dict({"ports": [
        {"label": "vscode", "port": 8080, "conns": 3, "listening": True,
         "owner": "node", "bytes_in_rate": 1200.0, "bytes_out_rate": 800.0,
         "active": True, "active_s": 42.0, "sustained": False},
        {"label": "ssh", "port": 22}]})
    assert len(snap.ports) == 2
    p = snap.ports[0]
    assert p.label == "vscode" and p.port == 8080 and p.conns == 3
    assert p.listening is True and p.owner == "node"
    assert p.bytes_in_rate == 1200.0 and p.active is True and p.active_s == 42.0
    # A sparse row defaults the rest (owner None, rates None, not listening).
    assert snap.ports[1].port == 22 and snap.ports[1].conns == 0
    assert snap.ports[1].listening is False and snap.ports[1].owner is None
    assert snap.ports[1].bytes_in_rate is None


def test_ports_absent_or_bad_types():
    assert Snapshot.from_dict({}).ports == []                  # older feed / none watched
    assert Snapshot.from_dict({"ports": "nope"}).ports == []   # non-list -> []
    snap = Snapshot.from_dict({"ports": ["not-a-dict", {"port": "bad"}]})
    assert snap.ports[0].label == "?"         # non-dict entry -> defaulted Port
    assert snap.ports[1].port == 0            # unparseable int -> 0


def test_port_connections_parse():
    snap = Snapshot.from_dict({"ports": [
        {"label": "ssh", "port": 22, "conns": 3, "connections": [
            {"peer": "127.0.0.1:50504", "process": "devtunnel", "pid": 313,
             "bytes_in": 2823778, "bytes_out": 5037332, "state": "established"},
            {"peer": "10.0.0.9:40001"},           # sparse: unattributed far end
        ]}]})
    (p,) = snap.ports
    a, b = p.connections
    assert a.peer == "127.0.0.1:50504" and a.process == "devtunnel" and a.pid == 313
    assert a.bytes_in == 2823778 and a.bytes_out == 5037332
    assert b.process is None and b.pid is None and b.bytes_in is None
    assert b.state == "established"
    assert p.conns - len(p.connections) == 1     # the "+N more" remainder


def test_port_connections_absent_or_bad_types():
    # An older producer emits no `connections` field at all.
    assert Snapshot.from_dict({"ports": [{"port": 22}]}).ports[0].connections == []
    snap = Snapshot.from_dict({"ports": [
        {"port": 22, "connections": "nope"},
        {"port": 80, "connections": ["not-a-dict", {"pid": "bad"}]}]})
    assert snap.ports[0].connections == []
    assert snap.ports[1].connections[0].peer == "?"     # non-dict -> defaulted
    assert snap.ports[1].connections[1].pid is None     # unparseable int -> None


def test_fake_producer_carries_ports():
    snap = Snapshot.from_dict(fake_producer._build(now=1020.0, start=1000.0, host="T"))
    assert {p.label for p in snap.ports} == {"web-app", "tunnel-ssh"}
    assert all(p.listening for p in snap.ports)
    # The fake demos the connection-detail dropdown, incl. a "+N more" remainder
    # on the active port and an unattributed far end.
    by_label = {p.label: p for p in snap.ports}
    vs = by_label["web-app"]
    assert vs.conns - len(vs.connections) == 1
    assert vs.connections[1].process is None
    assert by_label["tunnel-ssh"].connections[0].process == "tunnel"


# ── whole-stack security fields ────────────────────────────────────────────────────

def test_security_stack_parses_typed_and_absent():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "security_stack": {
            "cores": 1.6, "hot": True, "hot_for_s": 360.0, "taxing": True,
            "spawns_per_min": 2400.0,
            "top": [{"group": "defender", "label": "Defender",
                     "cpu_pct": 120.0}]}}))
    ss = snap.security_stack
    assert (ss.cores, ss.hot, ss.hot_for_s, ss.taxing) == (1.6, True, 360.0, True)
    assert ss.spawns_per_min == 2400.0
    assert [(g.group, g.label, g.cpu_pct) for g in ss.top] == [
        ("defender", "Defender", 120.0)]
    # Absent (older producer / census unavailable) → None, never fabricated.
    assert parse(json.dumps({"schema": "monitor-snapshot:v1",
                             "t": 1.0})).security_stack is None


def test_security_stack_junk_tolerant():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "security_stack": {"cores": "hot", "hot": "yes", "hot_for_s": None,
                           "taxing": 0, "spawns_per_min": 10 ** 400,
                           "top": "nope"}}))
    ss = snap.security_stack
    assert ss is not None
    assert ss.cores is None and ss.hot is True and ss.taxing is False
    assert ss.spawns_per_min is None      # huge int -> None (the _fo guard)
    assert ss.top == []
    # A non-dict field is no reading at all.
    assert parse(json.dumps({"schema": "monitor-snapshot:v1", "t": 1.0,
                             "security_stack": [1]})).security_stack is None


# ── stuck-process / run-queue probe ────────────────────────────────────────────────

def test_stuck_field_parses():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "stuck": {
            "procs_running": 27, "procs_blocked": 2, "d_count": 3, "ncpu": 26,
            "wedged": [{"pid": 3026899, "comm": "find", "for_s": 55.2},
                       {"pid": 41, "comm": "dockerd", "for_s": 31.0}],
            "load1_per_core": 1.4, "cpu_pct": 91.4,
            "sampler_ms": {"nvidia-smi": 284.2, "docker": 2400.0},
            "queue_saturated": True, "wait_diverged": True,
            "sampler_slow": False, "cpu_saturated": True,
        }}))
    st = snap.stuck
    assert (st.procs_running, st.procs_blocked, st.d_count, st.ncpu) == (27, 2, 3, 26)
    assert [(w.pid, w.comm, w.for_s) for w in st.wedged] == [
        (3026899, "find", 55.2), (41, "dockerd", 31.0)]
    assert st.load1_per_core == 1.4
    assert st.cpu_pct == 91.4
    assert st.sampler_ms == {"nvidia-smi": 284.2, "docker": 2400.0}
    assert st.queue_saturated and st.wait_diverged and not st.sampler_slow
    assert st.cpu_saturated is True


def test_stuck_absent_is_none():
    from vision_resource_monitor.snapshot import parse
    import json
    assert parse(json.dumps({"schema": "monitor-snapshot:v1", "t": 1.0})).stuck is None


def test_stuck_bad_types_do_not_crash():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "stuck": {"procs_running": "lots", "procs_blocked": None,
                  "wedged": "nope", "sampler_ms": [1, 2],
                  "load1_per_core": "high", "queue_saturated": "yes",
                  "cpu_pct": "hot"}}))
    st = snap.stuck
    assert st is not None
    assert st.procs_running is None and st.procs_blocked is None
    assert st.wedged == [] and st.sampler_ms == {}
    assert st.load1_per_core is None
    assert st.queue_saturated is True   # truthy string -> flag honoured
    # The #205 additions: junk busy% -> None; absent flag -> False (old producer)
    assert st.cpu_pct is None and st.cpu_saturated is False
    # a non-dict field is no probe at all
    snap = parse(json.dumps({"schema": "monitor-snapshot:v1", "t": 1.0,
                             "stuck": [1, 2]}))
    assert snap.stuck is None
    # A huge JSON int literal must degrade, never OverflowError past the
    # reader's last-good-frame guard (the _i/_io egress lesson — the float
    # helpers were missed then; review fold on #74).
    snap = parse(json.dumps({"schema": "monitor-snapshot:v1", "t": 1.0,
                             "stuck": {"cpu_pct": 10 ** 400,
                                       "load1_per_core": 10 ** 400}}))
    assert snap.stuck.cpu_pct is None and snap.stuck.load1_per_core is None


def test_gpu_users_parses_none_empty_and_rows():
    base = {"schema": "monitor-snapshot:v1", "t": 1.0}
    # absent field (older producer / counters unavailable) → None, never []
    assert parse(json.dumps(base)).gpu_users is None
    # explicit null → None too (the producer's "unavailable" value)
    assert parse(json.dumps({**base, "gpu_users": None})).gpu_users is None
    # [] = counters answered, nothing ≥ floor — distinct from None
    assert parse(json.dumps({**base, "gpu_users": []})).gpu_users == []
    snap = parse(json.dumps({**base, "gpu_users": [
        {"pid": 32360, "name": "QmlRenderer", "vram_mib": 7917.0},
        {"pid": 21176, "name": "NVIDIA Overlay", "vram_mib": 232.0},
    ]}))
    assert snap.gpu_users == [
        GpuUser(pid=32360, name="QmlRenderer", vram_mib=7917.0),
        GpuUser(pid=21176, name="NVIDIA Overlay", vram_mib=232.0),
    ]
    # junk rows coerce fail-open, never raise
    junk = parse(json.dumps({**base, "gpu_users": [{"pid": "x"}, "notadict"]}))
    assert junk.gpu_users == [GpuUser(pid=0, name="?", vram_mib=0.0),
                              GpuUser(pid=0, name="?", vram_mib=0.0)]


def test_proc_ports_parse_none_vs_records():
    from vision_resource_monitor.snapshot import Snapshot
    # Absent field (older producer / no targets) → None, never [].
    snap = Snapshot.from_dict({"schema": "monitor-snapshot:v1", "t": 1.0})
    assert snap.proc_ports is None
    snap2 = Snapshot.from_dict({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "proc_ports": [
            {"side": "wsl", "pid": 582236, "name": "python3", "found": True,
             "sockets": [{"proto": "tcp", "state": "LISTEN",
                          "local": "0.0.0.0:39999", "peer": None}],
             "sockets_total": 1, "note": None},
            {"side": "windows", "pid": 7, "name": "ghost", "found": None,
             "sockets": None, "sockets_total": None,
             "note": "no answer from the Windows sweep yet"},
            {"side": "wsl", "pid": 9, "name": "quietd", "found": True,
             "sockets": [], "sockets_total": 0, "note": "no sockets visible"},
        ]})
    a, b, c = snap2.proc_ports
    assert a.found is True and a.sockets[0].local == "0.0.0.0:39999"
    assert a.sockets[0].peer is None and a.sockets_total == 1
    # The None-vs-[] honesty split survives the parse.
    assert b.found is None and b.sockets is None
    assert c.sockets == [] and c.note == "no sockets visible"
    # Junk rows coerce, never raise; junk `found` must NOT coerce into a
    # definitive True — unknown reads as unanswered (strict tri-state).
    snap3 = Snapshot.from_dict({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "proc_ports": [{"pid": "x", "sockets": "junk", "found": "false"},
                       "garbage"]})
    assert snap3.proc_ports[0].pid == 0
    assert snap3.proc_ports[0].sockets is None
    assert snap3.proc_ports[0].found is None


def test_proc_port_record_parses_name_records():
    from vision_resource_monitor.snapshot import ProcPortRecord
    rec = ProcPortRecord.from_dict({
        "side": "wsl", "name": "node", "pid": None, "kind": "name",
        "matches": [{"pid": 9, "name": "node",
                     "sockets": [{"proto": "tcp", "state": "LISTEN",
                                  "local": "0.0.0.0:80", "peer": None}],
                     "sockets_total": 1},
                    "garbage"],
        "match_total": 12})
    assert rec.kind == "name" and rec.match_total == 12
    assert rec.matches[0].pid == 9
    assert rec.matches[0].sockets[0].local == "0.0.0.0:80"
    # A junk matches element parses to a zero-value match, never a crash.
    assert rec.matches[1].pid == 0 and rec.matches[1].sockets == []
    # None-vs-[] survives the parse: absent matches ≠ answered-empty.
    pending = ProcPortRecord.from_dict({"side": "wsl", "name": "node",
                                        "kind": "name"})
    assert pending.matches is None and pending.match_total is None
    # Junk kind never fabricates a name record; pid records stay kind-less.
    pid_rec = ProcPortRecord.from_dict({"side": "wsl", "pid": 5,
                                        "kind": "weird"})
    assert pid_rec.kind is None


# ── Windows scanner census ─────────────────────────────────────────────────────────

def test_scanners_field_parses():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "scanners": [
            {"group": "defender", "label": "Defender",
             "procs": ["MsMpEng", "NisSrv"], "proc_count": 3,
             "rss_mib": 435.6, "cpu_pct": 34.2, "state": "active",
             "active_s": 720.0, "ago_s": None, "burning": True},
            {"group": "search-indexer", "label": "Search Indexer",
             "procs": ["SearchIndexer"], "proc_count": 1, "rss_mib": 62.5,
             "cpu_pct": 0.0, "state": "burst", "active_s": None,
             "ago_s": 240.0, "burning": False},
        ]}))
    sc = snap.scanners
    assert [(r.group, r.label, r.state, r.burning) for r in sc] == [
        ("defender", "Defender", "active", True),
        ("search-indexer", "Search Indexer", "burst", False)]
    assert sc[0].procs == ["MsMpEng", "NisSrv"]
    assert (sc[0].proc_count, sc[0].rss_mib, sc[0].cpu_pct) == (3, 435.6, 34.2)
    assert sc[0].active_s == 720.0 and sc[0].ago_s is None
    assert sc[1].ago_s == 240.0 and sc[1].active_s is None


def test_scanners_none_vs_empty():
    from vision_resource_monitor.snapshot import parse
    import json
    # Absent field (older producer / --no-scanners / counters failed): None.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0})).scanners is None
    # A non-list field is no census at all.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "scanners": {"defender": 1}})).scanners is None
    # [] = the census answered and none of the watched scanners runs here.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0, "scanners": []})).scanners == []


def test_scanners_bad_types_do_not_crash():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "scanners": [{"group": None, "procs": "nope", "proc_count": "many",
                      "rss_mib": "big", "cpu_pct": None, "state": None,
                      "active_s": "long", "burning": "yes"}]}))
    r = snap.scanners[0]
    assert r.group == "?" and r.label == "?"
    assert r.procs == [] and r.proc_count == 0
    assert r.rss_mib == 0.0 and r.cpu_pct == 0.0
    assert r.state == "idle"
    assert r.active_s is None and r.ago_s is None
    assert r.burning is True    # truthy string -> the flag is honoured


# ── off-box egress ranking ─────────────────────────────────────────────────────────

def test_egress_field_parses():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "egress": {
            "window_s": 58.3, "total_in_bps": 1200.5, "total_out_bps": 88400.0,
            "iface": {"in_bps": 2400.0, "out_bps": 91000.0,
                      "out_in_ratio": 37.92},
            "top": [
                {"pid": 317, "name": "devtunnel", "in_bps": 900.0,
                 "out_bps": 87000.0, "total_in_mib": 12.4,
                 "total_out_mib": 5130.7},
                {"pid": 4242, "name": "python3.11", "in_bps": 300.5,
                 "out_bps": 1400.0, "total_in_mib": 0.9, "total_out_mib": 3.2},
            ]}}))
    eg = snap.egress
    assert eg is not None
    assert (eg.window_s, eg.total_in_bps, eg.total_out_bps) == (
        58.3, 1200.5, 88400.0)
    assert (eg.iface.in_bps, eg.iface.out_bps, eg.iface.out_in_ratio) == (
        2400.0, 91000.0, 37.92)
    assert [(t.pid, t.name) for t in eg.top] == [
        (317, "devtunnel"), (4242, "python3.11")]
    assert (eg.top[0].in_bps, eg.top[0].out_bps) == (900.0, 87000.0)
    assert (eg.top[0].total_in_mib, eg.top[0].total_out_mib) == (12.4, 5130.7)


def test_egress_none_vs_value():
    from vision_resource_monitor.snapshot import parse
    import json
    # Absent field (older producer / --no-egress / warming): None.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0})).egress is None
    # A non-dict field is no reading at all.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "egress": [1, 2]})).egress is None
    # A present reading with an empty ranking is QUIET, not off — and the
    # iface block may independently be None (NETIF never answered / aged out).
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "egress": {"window_s": 60.0, "total_in_bps": 0.0,
                    "total_out_bps": 0.0, "iface": None, "top": []}}))
    assert snap.egress is not None
    assert snap.egress.top == [] and snap.egress.iface is None


def test_egress_ratio_none_passes_through():
    # The producer emits out_in_ratio None when the in-rate is 0 (division
    # honesty) — the parse must keep it None, never coerce to 0.
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "egress": {"window_s": 60.0, "total_in_bps": 0.0,
                    "total_out_bps": 10.0,
                    "iface": {"in_bps": 0.0, "out_bps": 512.0,
                              "out_in_ratio": None},
                    "top": []}}))
    assert snap.egress.iface.out_bps == 512.0
    assert snap.egress.iface.out_in_ratio is None


def test_egress_bad_types_do_not_crash():
    from vision_resource_monitor.snapshot import parse
    import json
    import math
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "egress": {"window_s": "long", "total_in_bps": None,
                   "total_out_bps": "fast", "iface": "nope",
                   "top": [{"pid": "many", "name": None, "in_bps": "x",
                            "out_bps": None, "total_in_mib": [],
                            "total_out_mib": {}}]}}))
    eg = snap.egress
    assert eg.window_s is None
    assert eg.total_in_bps is None and eg.total_out_bps is None
    assert eg.iface is None
    t = eg.top[0]
    assert t.pid is None and t.name == "?"
    assert t.in_bps is None and t.out_bps is None
    assert t.total_in_mib is None and t.total_out_mib is None
    # json.loads accepts Infinity/NaN literals. int(inf) raises OverflowError
    # — NOT ValueError — and an uncaught raise escapes parse() past the
    # reader's last-good-frame fallback and aborts the poll tick (review
    # find, 2026-07-23: the pid parse must degrade to None, and non-finite
    # floats pass through for the render guards to drop).
    snap = parse(
        '{"schema": "monitor-snapshot:v1", "t": 1.0,'
        ' "egress": {"window_s": Infinity,'
        ' "top": [{"pid": Infinity, "name": "a"},'
        ' {"pid": NaN, "name": "b"}]}}')
    eg = snap.egress
    assert math.isinf(eg.window_s)
    assert eg.top[0].pid is None and eg.top[1].pid is None


# ── Actions runner-fleet census (producer top-level `runner_fleet`) ────────────────


def test_runner_fleet_field_parses():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "runner_fleet": {
            "runners": [
                {"name": "runner-2", "container": "runner-container-2",
                 "repo": "example-org/sample-repo",
                 "cpu_pct": 118.5, "mem_mib": 667.9, "pids": 36,
                 "pids_floor": 36, "active": True},
                {"name": "runner-1", "container": "runner-container-1",
                 "repo": "example-org/monitor-demo",
                 "cpu_pct": 0.01, "mem_mib": 132.3, "pids": 14,
                 "pids_floor": 14, "active": False},
            ],
            "total": 2, "active_count": 1,
            "cpu_pct_total": 118.51, "mem_mib_total": 800.2}}))
    rf = snap.runner_fleet
    assert rf is not None
    assert (rf.total, rf.active_count) == (2, 1)
    assert (rf.cpu_pct_total, rf.mem_mib_total) == (118.51, 800.2)
    assert [(r.name, r.container, r.active) for r in rf.runners] == [
        ("runner-2", "runner-container-2", True),
        ("runner-1", "runner-container-1", False)]
    r = rf.runners[0]
    assert (r.repo, r.cpu_pct, r.mem_mib, r.pids, r.pids_floor) == (
        "example-org/sample-repo", 118.5, 667.9, 36, 36)


def test_runner_fleet_none_vs_empty():
    from vision_resource_monitor.snapshot import parse
    import json
    # Absent field (older producer / --no-docker / docker down): None.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0})).runner_fleet is None
    # A non-dict field is no reading at all.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "runner_fleet": [1]})).runner_fleet is None
    # A present reading with an empty fleet is a real answer (docker up, no
    # runner containers), distinct from None — and its totals stay None.
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "runner_fleet": {"runners": [], "total": 0, "active_count": 0,
                          "unresolved": 0,
                          "cpu_pct_total": None, "mem_mib_total": None}}))
    rf = snap.runner_fleet
    assert rf is not None and rf.runners == []
    assert rf.unresolved == 0
    assert rf.cpu_pct_total is None and rf.mem_mib_total is None
    # unresolved marks an UNKNOWN fleet (failed/pending inspects) — carried
    # through so the render never claims "no runners"; a producer predating
    # the field (or junk/bool) reads 0, never a fabricated count.
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "runner_fleet": {"runners": [], "total": 0, "active_count": 0,
                          "unresolved": 2}}))
    assert snap.runner_fleet.unresolved == 2
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "runner_fleet": {"runners": [], "unresolved": True}}))
    assert snap.runner_fleet.unresolved == 0
    # total/active_count carry the same guard — a JSON `true` total must not
    # fabricate a "1 up" fleet (review H1 on this diff).
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "runner_fleet": {"runners": [], "total": True,
                          "active_count": True}}))
    assert (snap.runner_fleet.total, snap.runner_fleet.active_count) == (0, 0)


def test_runner_bursts_and_up_parse():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "runner_fleet": {
            "runners": [
                {"name": "runner-3", "container": "runner-container-3",
                 "repo": "sample-org/sample-repo", "up": "Up 7 minutes", "cpu_pct": 105.0,
                 "mem_mib": 500.0, "pids": 42, "pids_floor": 14,
                 "active": True}],
            "bursts": [
                {"runner": "runner-3", "repo": "sample-org/sample-repo", "state": "running",
                 "age_s": 42.5, "start_unseen": True,
                 "peak_cpu_pct": 118.0, "peak_mem_mib": 900.0},
                {"runner": "runner-1", "repo": "sample-org/sample-repo", "state": "done",
                 "dur_s": 55.0, "ended_ago_s": 180.0,
                 "start_unseen": False, "peak_cpu_pct": None,
                 "peak_mem_mib": None}],
            "total": 1, "active_count": 1}}))
    rf = snap.runner_fleet
    assert rf.runners[0].up == "Up 7 minutes"
    running, done = rf.bursts
    assert (running.runner, running.state, running.age_s) == (
        "runner-3", "running", 42.5)
    assert running.start_unseen is True
    assert (running.peak_cpu_pct, running.peak_mem_mib) == (118.0, 900.0)
    assert (done.state, done.dur_s, done.ended_ago_s) == (
        "done", 55.0, 180.0)
    assert done.start_unseen is False
    assert done.peak_cpu_pct is None            # no reading, never 0
    # A predating feed (no bursts key / no up) reads honest absences.
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "runner_fleet": {"runners": [{"name": "runner-1",
                                      "container": "c"}]}}))
    assert snap.runner_fleet.bursts == []
    assert snap.runner_fleet.runners[0].up is None


def test_runner_bursts_bad_types_do_not_crash():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "runner_fleet": {
            "runners": [{"name": "h", "container": "c", "up": 7}],
            "bursts": [
                {"runner": None, "repo": 3, "state": 9, "age_s": "old",
                 "dur_s": [], "ended_ago_s": {}, "start_unseen": "yes",
                 "peak_cpu_pct": "hot", "peak_mem_mib": "big"},
                # JSON true must not fabricate ages/peaks (bool ⊂ float —
                # the child_pids lesson), nor soften start_unseen.
                {"runner": "h2", "state": "running", "age_s": True,
                 "start_unseen": 1, "peak_cpu_pct": True},
                "not-a-dict"]}}))
    rf = snap.runner_fleet
    assert rf.runners[0].up is None             # non-str up is no reading
    b = rf.bursts[0]
    assert (b.runner, b.repo, b.state) == ("?", None, "?")
    assert (b.age_s, b.dur_s, b.ended_ago_s) == (None, None, None)
    assert b.start_unseen is False              # only literal true marks a floor
    assert (b.peak_cpu_pct, b.peak_mem_mib) == (None, None)
    b2 = rf.bursts[1]
    assert (b2.age_s, b2.peak_cpu_pct) == (None, None)
    assert b2.start_unseen is False
    assert rf.bursts[2].runner == "?"           # junk entry parses inert


def test_hosted_bursts_field_parses_and_guards():
    from vision_resource_monitor.snapshot import parse
    import json
    # Absent (watch off / older producer) → None; junk container → None.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0})).hosted_bursts is None
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "hosted_bursts": [1]})).hosted_bursts is None
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "hosted_bursts": {
            "ok": True, "error": None, "repos": ["o/r"],
            "checked_ago_s": 12.5, "unchecked": 1, "truncated": 2,
            "runs": [
                {"repo": "o/r", "workflow": "tests",
                 "status": "in_progress", "where": "hosted",
                 "runner": "GitHub Actions 3", "age_s": 60.0},
                {"repo": "o/r", "workflow": "review", "status": "queued",
                 "where": "pending", "runner": None, "age_s": None}]}}))
    hb = snap.hosted_bursts
    assert (hb.ok, hb.repos, hb.checked_ago_s) == (True, ["o/r"], 12.5)
    assert (hb.unchecked, hb.truncated) == (1, 2)
    r0, r1 = hb.runs
    assert (r0.workflow, r0.where, r0.runner, r0.age_s) == (
        "tests", "hosted", "GitHub Actions 3", 60.0)
    assert (r1.where, r1.runner, r1.age_s) == ("pending", None, None)
    # Warming (ok null) and junk types stay honest — bools never fabricate
    # numbers (the child_pids lesson), junk ok reads as warming.
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "hosted_bursts": {"ok": "yes", "checked_ago_s": True,
                          "unchecked": True, "truncated": True,
                          "repos": [3, "o/r"],
                          "runs": [{"age_s": True}, "junk"]}}))
    hb = snap.hosted_bursts
    assert hb.ok is None and hb.checked_ago_s is None
    assert (hb.unchecked, hb.truncated) == (0, 0)
    assert hb.repos == ["o/r"]
    assert hb.runs[0].age_s is None and hb.runs[1].workflow == "?"


def test_runner_fleet_bad_types_do_not_crash():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "runner_fleet": {
            "runners": [{"name": None, "container": 7, "repo": 12,
                         "cpu_pct": "hot", "mem_mib": [], "pids": "many",
                         "pids_floor": {}, "active": "yes"}],
            "total": "x", "active_count": None,
            "cpu_pct_total": "lots", "mem_mib_total": None}}))
    rf = snap.runner_fleet
    r = rf.runners[0]
    assert (r.name, r.container, r.repo) == ("?", "?", None)
    assert (r.cpu_pct, r.mem_mib, r.pids, r.pids_floor) == (
        None, None, None, None)
    assert r.active is None                 # junk is UNKNOWN, never quiet/busy
    assert (rf.total, rf.active_count) == (0, 0)
    assert rf.cpu_pct_total is None
    # JSON `true` must not fabricate numbers (bool ⊂ int/float — the
    # child_pids lesson): every numeric field bool-guards to None, and a
    # bool `active` is the ONLY accepted busy signal.
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "runner_fleet": {
            "runners": [{"name": "r", "container": "c", "cpu_pct": True,
                         "mem_mib": True, "pids": True, "pids_floor": True,
                         "active": True}],
            "total": 1, "active_count": 1,
            "cpu_pct_total": True, "mem_mib_total": True}}))
    r = snap.runner_fleet.runners[0]
    assert (r.cpu_pct, r.mem_mib, r.pids, r.pids_floor) == (
        None, None, None, None)
    assert r.active is True
    assert snap.runner_fleet.cpu_pct_total is None
    assert snap.runner_fleet.mem_mib_total is None


# ── exec-pressure spawn census (producer top-level `spawns`) ───────────────────────


def test_spawns_field_parses():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "spawns": {
            "wsl": {"alloc_per_min": 456.9, "visible_per_min": 43.0,
                    "self_per_min": 169.6,
                    "top": [{"label": "systemd-udevd", "count": 41,
                             "pid": None},
                            {"label": "bash", "count": 1, "pid": 777}],
                    "status": "ok", "window_s": 60.0},
            "win": {"visible_per_min": 6.0,
                    "top": [{"label": "conhost", "count": 1}],
                    "status": "ok", "window_s": 10.1}}}))
    sp = snap.spawns
    assert sp is not None
    assert (sp.wsl.alloc_per_min, sp.wsl.visible_per_min,
            sp.wsl.self_per_min) == (456.9, 43.0, 169.6)
    assert [(t.label, t.count, t.pid) for t in sp.wsl.top] == [
        ("systemd-udevd", 41, None), ("bash", 1, 777)]
    assert (sp.wsl.status, sp.wsl.window_s) == ("ok", 60.0)
    # The win side never carries the wsl-only metrics.
    assert sp.win.alloc_per_min is None and sp.win.self_per_min is None
    assert (sp.win.visible_per_min, sp.win.status, sp.win.window_s) == (
        6.0, "ok", 10.1)
    assert [(t.label, t.count) for t in sp.win.top] == [("conhost", 1)]


def test_spawns_none_vs_value():
    from vision_resource_monitor.snapshot import parse
    import json
    # Absent field (older producer / --no-spawns): None.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0})).spawns is None
    # A non-dict field is no reading at all.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "spawns": [1]})).spawns is None
    # Missing sides parse to defaulted sides (tolerant), never a crash.
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0, "spawns": {}}))
    assert snap.spawns is not None
    assert snap.spawns.wsl.alloc_per_min is None
    assert snap.spawns.win.visible_per_min is None


def test_spawns_bad_types_do_not_crash():
    from vision_resource_monitor.snapshot import parse
    import json
    # The #70 lesson class: json accepts Infinity — every numeric coercion
    # must survive it (OverflowError, not just ValueError).
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "spawns": {
            "wsl": {"alloc_per_min": "fast", "visible_per_min": None,
                    "self_per_min": float("inf"),
                    "top": [{"label": None, "count": "x",
                             "pid": float("inf")}, "junk"],
                    "status": 7, "window_s": "wide"},
            "win": "broken"}}))
    sp = snap.spawns
    assert sp is not None
    assert sp.wsl.alloc_per_min is None
    assert sp.wsl.top[0].pid is None and sp.wsl.top[0].count == 0
    assert sp.win.visible_per_min is None


# ── self_cost (the producer billing its own CPU) ───────────────────────────────────


def test_self_cost_field_parses():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps({
        "schema": "monitor-snapshot:v1", "t": 1.0,
        "self_cost": {"window_s": 54.0, "total_pct": 15.15,
                      "python_pct": 0.24, "spawns_pct": 1.46,
                      "windows_pct": 13.46}}))
    sc = snap.self_cost
    assert sc is not None
    assert (sc.window_s, sc.total_pct) == (54.0, 15.15)
    assert (sc.python_pct, sc.spawns_pct, sc.windows_pct) == (
        0.24, 1.46, 13.46)


def test_self_cost_none_vs_value():
    from vision_resource_monitor.snapshot import parse
    import json
    # Absent (older producer / warming / --once): None.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0})).self_cost is None
    # A non-dict field is no reading at all.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "self_cost": [1]})).self_cost is None
    # windows_pct None = the host sweep didn't bill (WSL-only total) — must
    # pass through as None, never coerce to a measured-looking 0.
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "self_cost": {"window_s": 60.0, "total_pct": 1.7,
                       "python_pct": 0.3, "spawns_pct": 1.4,
                       "windows_pct": None}}))
    assert snap.self_cost.windows_pct is None
    assert snap.self_cost.total_pct == 1.7


def test_self_cost_bad_types_do_not_crash():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "self_cost": {"window_s": "x", "total_pct": None,
                       "python_pct": [], "spawns_pct": {},
                       "windows_pct": "nope"}}))
    sc = snap.self_cost
    assert (sc.window_s, sc.total_pct, sc.python_pct, sc.spawns_pct) == (
        0.0, 0.0, 0.0, 0.0)
    assert sc.windows_pct is None


def test_self_cost_history_parses_and_defaults_empty():
    from vision_resource_monitor.snapshot import parse
    import json
    snap = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "self_cost_history": [9.0, 9.4, 24.0]}))
    assert snap.self_cost_history == [9.0, 9.4, 24.0]
    # Absent (older producer) / non-list / garbage entries → [] / coerced.
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0})).self_cost_history == []
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "self_cost_history": "no"})).self_cost_history == []
    assert parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "self_cost_history": [1.5, "x", None]})).self_cost_history == [
        1.5, 0.0, 0.0]
