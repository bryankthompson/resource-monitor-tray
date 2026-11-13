from datetime import datetime

from vision_resource_monitor.reader import ReadResult
from vision_resource_monitor.snapshot import (
    Activity, ContainerMem, CpuStat, DiskRow, DockerStatus, Gpu, HostMemory,
    MemoryPanel, Port, PortConn, ProcRow, RamStat, RunRecord, Snapshot,
)
from vision_resource_monitor.state import (
    ACTIVE, ACTIVE_PULSE, ACTIVE_SUBTLE, GREEN, GREY, HOT, HOT_DIM, IDLE,
    MEM_NONE, MEM_OK, MEM_UNAVAILABLE, NODATA, RED, STALE, YELLOW, IdleStats,
    PortClient, PortClients, PortHistory, ProducerConfigTracker, ToastTracker,
    active_color, clients_note, icon_spec, format_gpu_users,
    gpu_users_gap_hint, idle_stats, memory_tiers, overall_status,
    panel_populate_report, scale_sparkline, tooltip, util_color,
)


def fresh(snap, age_s=1.0):
    return ReadResult(snapshot=snap, stale=False, age_s=age_s, error=None)


def stale(snap, age_s=30.0):
    return ReadResult(snapshot=snap, stale=True, age_s=age_s, error=None)


def gpu_snap(util, active=False, history=None):
    snap = Snapshot(host="H", t=1000.0, gpus=[Gpu(name="G", util=util,
                    mem_used_mib=8000, mem_total_mib=16000)])
    if active:
        snap.activity = Activity(start_t=900.0, peak_util=util, peak_vram_mib=9000,
                                 vram_total_mib=16000, workloads=["tracking-run"])
    snap.history = history or []
    return snap


# ── util_color / status ─────────────────────────────────────────────────────────

def test_util_color_thresholds():
    assert util_color(10) == GREEN
    assert util_color(50) == GREEN
    assert util_color(51) == YELLOW
    assert util_color(85) == YELLOW
    assert util_color(86) == RED


def test_busy_color_marker_rule():
    from vision_resource_monitor.state import busy_color
    assert busy_color(91.9, 92.0) == YELLOW
    assert busy_color(92.0, 92.0) == RED         # equal to the marker is red
    assert busy_color(100, 92.0) == RED
    assert busy_color(50, 92.0) == GREEN         # below: yellow above 50, else green
    assert busy_color(51, 92.0) == YELLOW
    assert busy_color(88, 70.0) == RED           # a lowered marker moves the red
    # 86% is red under util_color but only yellow here: the marker owns red.
    assert busy_color(86, 92.0) == YELLOW and util_color(86) == RED
    assert busy_color(86, None) == util_color(86)   # no marker = the plain ladder


def test_overall_status():
    assert overall_status(fresh(gpu_snap(80, active=True))) == ACTIVE
    assert overall_status(fresh(gpu_snap(2))) == IDLE
    assert overall_status(stale(gpu_snap(2))) == STALE
    assert overall_status(fresh(None)) == NODATA
    assert overall_status(fresh(Snapshot())) == NODATA   # no gpus


# ── icon spec ─────────────────────────────────────────────────────────────────

def test_icon_spec_active_colours_by_util():
    spec = icon_spec(fresh(gpu_snap(90, active=True)))
    assert spec.level == ACTIVE
    assert spec.color == RED
    assert spec.text == "90"
    assert 0.89 < spec.fill < 0.91


def test_icon_spec_grey_when_stale_or_nodata():
    assert icon_spec(stale(gpu_snap(90, active=True))).color == GREY
    assert icon_spec(fresh(None)).color == GREY
    assert icon_spec(fresh(None)).level == NODATA


# ── active indicator (hot accent + pulse) ────────────────────────────────────────

def test_active_color_swaps_only_the_calm_green_rung():
    assert active_color(10) == HOT             # default style: green -> hot
    assert active_color(50) == HOT
    assert active_color(51) == YELLOW          # alarms keep their meaning
    assert active_color(90) == RED
    assert active_color(10, ACTIVE_SUBTLE) == GREEN   # legacy opt-out
    assert active_color(90, ACTIVE_SUBTLE) == RED


def test_icon_spec_active_low_util_is_hot_by_default():
    spec = icon_spec(fresh(gpu_snap(10, active=True)))
    assert spec.level == ACTIVE
    assert spec.color == HOT


def test_icon_spec_subtle_keeps_legacy_green():
    spec = icon_spec(fresh(gpu_snap(10, active=True)), style=ACTIVE_SUBTLE)
    assert spec.color == GREEN


def test_icon_spec_pulse_dims_only_the_accent():
    lo = fresh(gpu_snap(10, active=True))
    assert icon_spec(lo, style=ACTIVE_PULSE, pulse_on=False).color == HOT
    assert icon_spec(lo, style=ACTIVE_PULSE, pulse_on=True).color == HOT_DIM
    hi = fresh(gpu_snap(90, active=True))   # a red alarm never blinks off
    assert icon_spec(hi, style=ACTIVE_PULSE, pulse_on=True).color == RED


def test_icon_spec_idle_and_stale_never_hot():
    assert icon_spec(fresh(gpu_snap(3))).color == GREEN          # idle stays calm
    assert icon_spec(stale(gpu_snap(90, active=True))).color == GREY


# ── tooltip ─────────────────────────────────────────────────────────────────────

def test_tooltip_variants():
    assert "ACTIVE" in tooltip(fresh(gpu_snap(80, active=True)))
    assert "idle" in tooltip(fresh(gpu_snap(3)))
    assert "stale" in tooltip(stale(gpu_snap(3)))
    assert tooltip(fresh(None)).startswith("Vision Resource Monitor")


# ── panel populate report (DEBUG diagnosis payload) ─────────────────────────────

def test_panel_report_none_snapshot():
    assert any("snapshot" in line for line in panel_populate_report(None))


def test_panel_report_flags_absent_windows_tier():
    # memory present but the windows host tier is None — the get_host_memory()
    # returned-None case the Memory-panel diagnosis is hunting for.
    snap = Snapshot(memory=MemoryPanel(windows=None,
                    wsl=[ProcRow(name="x", mem_mib=100)]))
    joined = "\n".join(panel_populate_report(snap))
    assert "memory.windows: absent" in joined
    assert "get_host_memory()" in joined
    assert "memory.wsl: 1" in joined


def test_panel_report_windows_present():
    snap = Snapshot(memory=MemoryPanel(windows=HostMemory(
        used_mib=1000, total_mib=2000, processes=[ProcRow(name="a", mem_mib=500)])))
    assert "memory.windows: present" in "\n".join(panel_populate_report(snap))


def test_panel_report_memory_absent_but_host_present():
    snap = Snapshot(host_cpu=CpuStat(pct=10),
                    host_ram=RamStat(used_mib=1, total_mib=2))
    joined = "\n".join(panel_populate_report(snap))
    assert "memory: absent" in joined
    assert "host_cpu: present" in joined
    assert "host_ram: present" in joined


# ── memory tier availability (what the window paints per tier) ──────────────────

def _tiers(snap):
    return {t.key: t for t in memory_tiers(snap)}


def test_memory_tiers_none_without_frame():
    assert memory_tiers(None) is None
    assert memory_tiers(Snapshot()) is None   # no 'memory' key -> panel "collecting…"


def test_memory_tiers_windows_unavailable_when_absent():
    # get_host_memory()-returned-None: windows tier is genuinely UNAVAILABLE and
    # carries a user-facing reason (the specific cause is in the log).
    t = _tiers(Snapshot(memory=MemoryPanel(windows=None)))
    assert t["windows"].state == MEM_UNAVAILABLE
    assert t["windows"].reason and "log" in t["windows"].reason.lower()


def test_memory_tiers_windows_ok_and_zero_total_unavailable():
    ok = _tiers(Snapshot(memory=MemoryPanel(
        windows=HostMemory(used_mib=1000, total_mib=2000))))
    assert ok["windows"].state == MEM_OK
    # windows present but total_mib=0 still can't render a gauge -> unavailable.
    zero = _tiers(Snapshot(memory=MemoryPanel(windows=HostMemory(total_mib=0))))
    assert zero["windows"].state == MEM_UNAVAILABLE


def test_memory_tiers_docker_unreachable_vs_idle():
    # Empty containers is ambiguous; snap.docker disambiguates it honestly.
    down = _tiers(Snapshot(memory=MemoryPanel(containers=[]), docker=None))
    assert down["docker"].state == MEM_UNAVAILABLE
    assert "reach" in down["docker"].reason.lower()

    idle = _tiers(Snapshot(memory=MemoryPanel(containers=[]),
                           docker=DockerStatus(running_count=0)))
    assert idle["docker"].state == MEM_NONE      # up but nothing to show — no warning
    assert idle["docker"].reason

    up = _tiers(Snapshot(memory=MemoryPanel(
        containers=[ContainerMem(name="q", mem_mib=100)]),
        docker=DockerStatus(running_count=1)))
    assert up["docker"].state == MEM_OK


def test_memory_tiers_disk_quiet_when_docker_unreachable():
    # docker already warns; disk stays silent (no reason) to avoid a duplicate.
    down = _tiers(Snapshot(memory=MemoryPanel(disk=[]), docker=None))
    assert down["disk"].state == MEM_NONE and down["disk"].reason == ""
    # docker reachable but no disk rows -> a neutral note, still not a warning.
    idle = _tiers(Snapshot(memory=MemoryPanel(disk=[]),
                           docker=DockerStatus(running_count=1)))
    assert idle["disk"].state == MEM_NONE and idle["disk"].reason
    ok = _tiers(Snapshot(memory=MemoryPanel(
        disk=[DiskRow(type="Images", size_mib=100)])))
    assert ok["disk"].state == MEM_OK


def test_memory_tiers_wsl_unavailable_when_empty():
    t = _tiers(Snapshot(memory=MemoryPanel(wsl=[])))
    assert t["wsl"].state == MEM_UNAVAILABLE
    t2 = _tiers(Snapshot(memory=MemoryPanel(wsl=[ProcRow(name="x", mem_mib=1)])))
    assert t2["wsl"].state == MEM_OK


# ── process filtering + size threshold ────────────────────────────────────────

def _procs():
    return [ProcRow(name="chrome", mem_mib=5000),
            ProcRow(name="chrome helper", mem_mib=50),   # tiny but a name match
            ProcRow(name="Memory Compression", mem_mib=2000),
            ProcRow(name="ollama", mem_mib=40, llm=True),  # tiny but an LLM
            ProcRow(name="tiny", mem_mib=200)]


def test_split_process_rows_threshold_and_llm():
    from vision_resource_monitor.state import split_process_rows
    shown, hidden = split_process_rows(_procs(), min_mib=1024.0)   # the historical cutoff
    assert [p.name for p in shown] == ["chrome", "Memory Compression", "ollama"]  # >=1G or LLM
    assert [p.name for p in hidden] == ["chrome helper", "tiny"]


def test_split_process_rows_includes_row_at_threshold():
    from vision_resource_monitor.state import split_process_rows
    exact = ProcRow(name="at threshold", mem_mib=100.0)
    shown, hidden = split_process_rows([exact], min_mib=100.0)
    assert shown == [exact]
    assert hidden == []


def test_split_process_rows_threshold_is_configurable():
    from vision_resource_monitor.state import split_process_rows
    # A lower threshold promotes small rows into shown (tiny=200M now shows;
    # chrome helper=50M is still below the 100M cutoff).
    shown, hidden = split_process_rows(_procs(), min_mib=100.0)
    assert {p.name for p in shown} == {"chrome", "Memory Compression", "ollama", "tiny"}
    assert {p.name for p in hidden} == {"chrome helper"}
    # A higher threshold buries more; the LLM row still shows regardless of size.
    shown2, hidden2 = split_process_rows(_procs(), min_mib=3000.0)
    assert {p.name for p in shown2} == {"chrome", "ollama"}
    assert {p.name for p in hidden2} == {"chrome helper", "Memory Compression", "tiny"}
    # Zero threshold shows everything.
    assert split_process_rows(_procs(), min_mib=0.0)[1] == []


def test_split_process_rows_name_filter_shows_all_matches():
    from vision_resource_monitor.state import split_process_rows
    # A name filter surfaces every match directly — including sub-threshold ones —
    # and buries nothing under "N more".
    shown, hidden = split_process_rows(_procs(), query="chrome", min_mib=1024.0)
    assert {p.name for p in shown} == {"chrome", "chrome helper"}
    assert hidden == []
    # Case-insensitive.
    assert {p.name for p in split_process_rows(_procs(), query="CHROME")[0]} == \
        {"chrome", "chrome helper"}
    # A whitespace-only query is treated as no filter (size split applies).
    assert split_process_rows(_procs(), query="   ", min_mib=1024.0)[1]
    # No match -> empty shown, empty hidden.
    assert split_process_rows(_procs(), query="zzz") == ([], [])


# ── idle-session stats ────────────────────────────────────────────────────────

def _day_start(y=2024, mo=7, d=17):
    """Local midnight epoch for a fixed date — computed the same way idle_stats
    bounds "today", so the tests are deterministic in any timezone."""
    return datetime(y, mo, d, 0, 0, 0).timestamp()


def test_idle_stats_none_and_empty():
    assert idle_stats(None) is None
    # No history yet: an empty summary, no idle duration to report.
    assert idle_stats(Snapshot(t=1000.0)) == IdleStats()


def test_idle_stats_idle_duration_and_today_counts():
    ds = _day_start()
    y, r1, r2 = (
        RunRecord(start_t=ds - 3 * 3600, end_t=ds - 3 * 3600 + 300, duration_s=300),  # yesterday
        RunRecord(start_t=ds + 9 * 3600, end_t=ds + 9 * 3600 + 600, duration_s=600),   # today
        RunRecord(start_t=ds + 11 * 3600, end_t=ds + 11 * 3600 + 1200, duration_s=1200),  # today
    )
    now = ds + 12 * 3600
    st = idle_stats(Snapshot(t=now, gpus=[Gpu(name="G")], history=[y, r1, r2]))
    assert st.runs_today == 2                       # only today's two runs
    assert st.active_today_s == 1800.0              # 600 + 1200
    assert st.today_complete is True                # oldest record predates today
    assert st.idle_for_s == now - (ds + 11 * 3600 + 1200)   # since last run ended


def test_idle_stats_today_incomplete_when_history_starts_today():
    # Every recorded run is from today, so earlier runs may have aged out of the
    # bounded history -> the counts are a lower bound.
    ds = _day_start()
    hist = [RunRecord(start_t=ds + 8 * 3600, end_t=ds + 8 * 3600 + 600, duration_s=600),
            RunRecord(start_t=ds + 9 * 3600, end_t=ds + 9 * 3600 + 600, duration_s=600)]
    st = idle_stats(Snapshot(t=ds + 10 * 3600, gpus=[Gpu(name="G")], history=hist))
    assert st.runs_today == 2
    assert st.today_complete is False


def test_idle_stats_no_runs_today_but_history_complete():
    ds = _day_start()
    prior = RunRecord(start_t=ds - 20 * 3600, end_t=ds - 20 * 3600 + 600, duration_s=600)
    st = idle_stats(Snapshot(t=ds + 6 * 3600, gpus=[Gpu(name="G")], history=[prior]))
    assert st.runs_today == 0 and st.active_today_s == 0.0
    assert st.today_complete is True                 # nothing today, but we'd know if there were
    assert st.idle_for_s == (ds + 6 * 3600) - (ds - 20 * 3600 + 600)


def test_idle_stats_unparseable_time_reports_gap_only():
    # A garbage snapshot clock can't bound "today"; still report the idle gap.
    huge = 1e19
    st = idle_stats(Snapshot(t=huge, gpus=[Gpu(name="G")],
                             history=[RunRecord(start_t=1.0, end_t=2.0, duration_s=1)]))
    assert st.runs_today == 0 and st.active_today_s == 0.0
    assert st.idle_for_s == huge - 2.0


# ── toast edge detection ──────────────────────────────────────────────────────

def test_toast_run_start_and_end():
    t = ToastTracker()
    assert t.update(fresh(gpu_snap(2))) == []            # idle, first frame
    started = t.update(fresh(gpu_snap(80, active=True)))
    assert len(started) == 1 and started[0].title == "GPU activity started"
    assert t.update(fresh(gpu_snap(85, active=True))) == []  # same run, no toast

    ended_snap = gpu_snap(2, history=[RunRecord(start_t=900, end_t=1000,
                          duration_s=100, peak_util=88, peak_vram_mib=10240)])
    ended = t.update(fresh(ended_snap))
    assert len(ended) == 1 and ended[0].title == "GPU idle"
    assert "peak 88%" in ended[0].body


def test_toast_new_run_reports_end_then_start():
    t = ToastTracker()
    a = gpu_snap(80, active=True)
    a.activity.start_t = 900.0
    t.update(fresh(a))
    b = gpu_snap(80, active=True, history=[RunRecord(start_t=900, end_t=1000,
                 duration_s=100, peak_util=80)])
    b.activity.start_t = 1500.0            # a different run
    toasts = t.update(fresh(b))
    assert [x.title for x in toasts] == ["GPU idle", "GPU activity started"]


def test_toast_disconnect_and_reconnect_once():
    t = ToastTracker()
    t.update(fresh(gpu_snap(2)))                    # seen live
    d1 = t.update(stale(gpu_snap(2)))
    assert len(d1) == 1 and d1[0].kind == "warning"
    d2 = t.update(stale(gpu_snap(2)))
    assert d2 == []                                # not repeated
    r = t.update(fresh(gpu_snap(2)))
    assert len(r) == 1 and r[0].title == "Monitor reconnected"


def test_toast_no_disconnect_before_first_live_frame():
    t = ToastTracker()
    assert t.update(stale(None)) == []             # never connected -> silent


# ── watched-port activity history (the Ports sparkline series) ───────────────────

def _psnap(t, ports):
    return Snapshot(host="H", t=t, ports=ports)


def test_port_history_accumulates_and_dedups_by_frame_clock():
    ph = PortHistory()
    p = Port(label="ssh", port=22, conns=1, bytes_in_rate=100.0, bytes_out_rate=50.0)
    ph.update(_psnap(1000.0, [p]))
    ph.update(_psnap(1000.0, [p]))      # the same frame re-served (a stale poll)
    s = ph.series("ssh", 22)
    assert s.kind == "rate" and s.values == [150.0]   # in+out summed, no dup
    # A one-sided rate still counts as throughput (the other side reads 0).
    ph.update(_psnap(1002.0, [Port(label="ssh", port=22, conns=1,
                                   bytes_in_rate=200.0, bytes_out_rate=None)]))
    assert ph.series("ssh", 22).values == [150.0, 200.0]


def test_port_history_conns_fallback_and_rate_preference():
    ph = PortHistory()
    # No byte rates at all (the /proc fallback): the series is the conn count.
    ph.update(_psnap(1.0, [Port(label="p", port=9, conns=2)]))
    ph.update(_psnap(2.0, [Port(label="p", port=9, conns=3)]))
    s = ph.series("p", 9)
    assert s.kind == "conns" and s.values == [2.0, 3.0]
    # One sample gaining a rate flips the series to rates; rate-less ones plot 0.
    ph.update(_psnap(3.0, [Port(label="p", port=9, conns=3, bytes_in_rate=64.0)]))
    s2 = ph.series("p", 9)
    assert s2.kind == "rate" and s2.values == [0.0, 0.0, 64.0]


def test_port_history_bounded_and_unknown_port_empty():
    ph = PortHistory(maxlen=3)
    for i in range(5):
        ph.update(_psnap(float(i + 1), [Port(label="p", port=9, conns=i)]))
    assert ph.series("p", 9).values == [2.0, 3.0, 4.0]   # oldest dropped
    assert ph.series("nope", 1).values == []


def test_port_history_prunes_removed_ports_but_survives_empty_frames():
    ph = PortHistory()
    a = Port(label="a", port=1, conns=1)
    b = Port(label="b", port=2, conns=1)
    ph.update(_psnap(1.0, [a, b]))
    # An empty ports frame (e.g. a producer restarted without config) keeps both.
    ph.update(_psnap(2.0, []))
    assert ph.series("a", 1).values and ph.series("b", 2).values
    # A non-empty frame without b prunes b — the watch set changed.
    ph.update(_psnap(3.0, [a]))
    assert ph.series("a", 1).values == [1.0, 1.0]
    assert ph.series("b", 2).values == []


def test_port_history_none_snapshot_is_noop():
    ph = PortHistory()
    ph.update(None)
    ph.update(_psnap(1.0, [Port(label="p", port=9, conns=1)]))
    ph.update(None)
    assert ph.series("p", 9).values == [1.0]


def test_port_history_time_window_prunes_old_samples():
    ph = PortHistory(window_s=10.0)
    for i in range(10):                                     # frames at t = 0, 2, … 18
        ph.update(_psnap(float(2 * i), [Port(label="p", port=9, conns=i)]))
    # The span is capped at 10 s of frame-clock: newest t=18 keeps t >= 8.
    assert ph.series("p", 9).values == [4.0, 5.0, 6.0, 7.0, 8.0, 9.0]


def test_port_history_set_window_shrink_prunes_live_and_clamps():
    from vision_resource_monitor.state import (
        DEFAULT_PORT_WINDOW_S, PORT_WINDOW_MAX_S, PORT_WINDOW_MIN_S,
    )
    ph = PortHistory()
    assert ph.window_s == DEFAULT_PORT_WINDOW_S
    for i in range(10):
        ph.update(_psnap(float(2 * i), [Port(label="p", port=9, conns=i)]))
    assert len(ph.series("p", 9).values) == 10              # default window keeps all
    ph.set_window_s(4.0)                                    # below the floor -> clamps
    assert ph.window_s == PORT_WINDOW_MIN_S
    # …and the shrink prunes immediately: newest t=18 keeps t >= 13.
    assert ph.series("p", 9).values == [7.0, 8.0, 9.0]
    ph.set_window_s(10 ** 9)                                # above the cap -> clamps
    assert ph.window_s == PORT_WINDOW_MAX_S
    assert PortHistory(window_s="junk").window_s == DEFAULT_PORT_WINDOW_S


def test_port_history_time_gap_keeps_newest_sample():
    # A long quiet gap (or a producer clock jump) prunes the stale shape but the
    # newest sample always survives — a series never empties itself.
    ph = PortHistory(window_s=5.0)
    ph.update(_psnap(0.0, [Port(label="p", port=9, conns=1)]))
    ph.update(_psnap(1000.0, [Port(label="p", port=9, conns=2)]))
    assert ph.series("p", 9).values == [2.0]


def test_port_rate_stats_window_means_peak_and_span():
    # A bursty port: mostly-0 ticks around one spike — the live readout shows 0
    # nearly always, but the window stats keep the burst legible.
    ph = PortHistory()
    rates = [(0.0, 0.0), (0.0, 0.0), (1000.0, 200.0), (0.0, 0.0), (0.0, 0.0)]
    for i, (rin, rout) in enumerate(rates):
        ph.update(_psnap(2.0 * i, [Port(label="ssh", port=22, conns=1,
                                        bytes_in_rate=rin, bytes_out_rate=rout)]))
    st = ph.rate_stats("ssh", 22)
    assert st.avg_in == 200.0 and st.avg_out == 40.0     # means over 5 samples
    assert st.peak == 1200.0                             # busiest sample, in+out
    assert st.span_s == 8.0                              # frame-clock span


def test_port_rate_stats_excludes_unmeasured_and_counts_one_sided():
    ph = PortHistory()
    # First tick unmeasured (None/None — the producer needs two ticks to rate):
    # excluded from the stats, not averaged in as 0.
    ph.update(_psnap(0.0, [Port(label="p", port=9, conns=1)]))
    ph.update(_psnap(2.0, [Port(label="p", port=9, conns=1,
                                bytes_in_rate=100.0, bytes_out_rate=50.0)]))
    ph.update(_psnap(4.0, [Port(label="p", port=9, conns=1,
                                bytes_in_rate=300.0, bytes_out_rate=None)]))
    st = ph.rate_stats("p", 9)
    assert st.avg_in == 200.0                            # (100+300)/2, None row skipped
    assert st.avg_out == 25.0                            # one-sided: missing side is 0
    assert st.peak == 300.0 and st.span_s == 2.0


def test_port_rate_stats_none_when_unrated_or_thin():
    ph = PortHistory()
    assert ph.rate_stats("nope", 1) is None              # unknown port
    # conns-only samples (the /proc fallback): never rated -> no stats.
    ph.update(_psnap(1.0, [Port(label="c", port=7, conns=2)]))
    ph.update(_psnap(2.0, [Port(label="c", port=7, conns=3)]))
    assert ph.rate_stats("c", 7) is None
    # A single rated sample is a point, not a window -> the row falls back to
    # the live readout.
    ph.update(_psnap(3.0, [Port(label="c", port=7, conns=3, bytes_in_rate=64.0)]))
    assert ph.rate_stats("c", 7) is None
    ph.update(_psnap(4.0, [Port(label="c", port=7, conns=3, bytes_in_rate=32.0)]))
    assert ph.rate_stats("c", 7) is not None


def test_port_rate_stats_follows_window_prune():
    # Once the burst falls out of the (shrunk) window, the stats forget it too —
    # the row reports the window, not all time.
    ph = PortHistory(window_s=10.0)
    ph.update(_psnap(0.0, [Port(label="p", port=9, conns=1,
                                bytes_in_rate=5000.0, bytes_out_rate=0.0)]))
    for i in range(1, 8):
        ph.update(_psnap(2.0 * i, [Port(label="p", port=9, conns=1,
                                        bytes_in_rate=10.0, bytes_out_rate=10.0)]))
    st = ph.rate_stats("p", 9)                           # burst at t=0 pruned (span 10)
    assert st.peak == 20.0 and st.avg_in == 10.0


# ── watched-port recent clients (the Ports panel "clients:" sub-line) ────────────

def _cport(label, port, conns):
    """A Port row with connection detail: conns is (peer, process, pid)."""
    return Port(label=label, port=port, conns=len(conns),
                connections=[PortConn(peer=pe, process=pr, pid=pid)
                             for pe, pr, pid in conns])


def test_port_clients_remembers_departed_hitters_with_age():
    pc = PortClients()
    hit = [_cport("ssh", 22, [("127.0.0.1:55868", "ssh", 313)])]
    pc.update(_psnap(10.0, hit))
    pc.update(_psnap(10.0, hit))                     # re-served frame: deduped
    pc.update(_psnap(14.0, [_cport("ssh", 22, [])]))  # the hitter left
    cs = pc.clients("ssh", 22)
    assert len(cs) == 1
    c = cs[0]
    assert (c.process, c.pid, c.current) == ("ssh", 313, False)
    assert c.last_seen_ago_s == 4.0
    assert c.name == "ssh[313]"
    pc.update(None)                                  # no-op, never raises
    assert pc.clients("nope", 9) == []               # unknown port empty


def test_port_clients_orders_current_first_then_recency():
    pc = PortClients()
    pc.update(_psnap(1.0, [_cport("p", 9, [("1.1.1.1:5", "old", 1)])]))
    pc.update(_psnap(3.0, [_cport("p", 9, [("1.1.1.1:6", "mid", 2)])]))
    pc.update(_psnap(5.0, [_cport("p", 9, [("1.1.1.1:7", "now", 3)])]))
    assert [c.name for c in pc.clients("p", 9)] == ["now[3]", "mid[2]", "old[1]"]
    assert [c.current for c in pc.clients("p", 9)] == [True, False, False]
    # On an equal-recency tie the attributed process outranks a peer-only
    # host — it is the more actionable "who".
    pc.update(_psnap(6.0, [_cport("p", 9, [("10.0.0.5:50501", None, None),
                                           ("1.1.1.1:8", "now", 3)])]))
    assert [c.name for c in pc.clients("p", 9)][:2] == ["now[3]", "10.0.0.5"]


def test_port_clients_unattributed_conns_key_on_peer_host():
    pc = PortClients()
    # Same far host on two ephemeral client ports → ONE client; an unknown
    # peer ("?") identifies nothing and is skipped; v6 brackets drop.
    pc.update(_psnap(1.0, [_cport("p", 9, [("10.0.0.5:50501", None, None),
                                           ("10.0.0.5:50502", None, None),
                                           ("?", None, None),
                                           ("[fe80::1]:443", None, None)])]))
    names = [c.name for c in pc.clients("p", 9)]
    assert sorted(names) == ["10.0.0.5", "fe80::1"]
    # The same host attributed later is a distinct (process-keyed) client —
    # attribution is identity, not a rename.
    pc.update(_psnap(2.0, [_cport("p", 9, [("10.0.0.5:50503", "curl", 7)])]))
    assert sorted(c.name for c in pc.clients("p", 9)) == [
        "10.0.0.5", "curl[7]", "fe80::1"]


def test_port_clients_prunes_removed_ports_but_survives_empty_frames():
    pc = PortClients()
    pc.update(_psnap(1.0, [_cport("a", 1, [("1.1.1.1:5", "x", 1)]),
                           _cport("b", 2, [("1.1.1.1:5", "y", 2)])]))
    pc.update(_psnap(2.0, []))                       # producer restart: keep
    assert pc.clients("a", 1) and pc.clients("b", 2)
    pc.update(_psnap(3.0, [_cport("a", 1, [])]))     # watch set changed
    assert pc.clients("a", 1) and not pc.clients("b", 2)


def test_port_clients_window_prune_cap_and_live_retune():
    pc = PortClients(window_s=10.0, max_clients=2)
    pc.update(_psnap(0.0, [_cport("p", 9, [("1.1.1.1:5", "a", 1)])]))
    pc.update(_psnap(20.0, [_cport("p", 9, [("1.1.1.1:5", "b", 2)])]))
    assert [c.name for c in pc.clients("p", 9)] == ["b[2]"]   # a aged out
    # The cap evicts the oldest-seen once distinct clients exceed it.
    pc.update(_psnap(21.0, [_cport("p", 9, [("1.1.1.1:5", "c", 3)])]))
    pc.update(_psnap(22.0, [_cport("p", 9, [("1.1.1.1:5", "d", 4)])]))
    assert sorted(c.name for c in pc.clients("p", 9)) == ["c[3]", "d[4]"]
    # A live window shrink re-prunes relative to the newest sighting; a
    # requested value below the shared ports-window floor clamps up to 5 s.
    pc.update(_psnap(29.0, [_cport("p", 9, [("1.1.1.1:5", "e", 5)])]))
    pc.set_window_s(0.5)                # → 5.0; d (age 7) prunes, e stays
    assert [c.name for c in pc.clients("p", 9)] == ["e[5]"]
    # Empty frames advance the clock without pruning; the accessor still
    # filters entries the clock pushed past the window.
    pc.update(_psnap(60.0, []))
    assert pc.clients("p", 9) == []


def test_clients_note_formats_names_ages_and_overflow():
    assert clients_note([]) == ""
    cs = [PortClient(process="node", pid=920, current=True),
          PortClient(peer_host="10.0.0.5", last_seen_ago_s=32.0),
          PortClient(process="ssh", last_seen_ago_s=90.0)]
    assert clients_note(cs) == ("clients: node[920]  ·  10.0.0.5 (32s ago)"
                                "  ·  ssh (1m ago)")
    many = [PortClient(process=f"p{i}", pid=i, current=True) for i in range(6)]
    note = clients_note(many)
    assert note.endswith("+2 more") and "p3[3]" in note and "p4" not in note


def test_bucket_means():
    from vision_resource_monitor.state import bucket_means
    assert bucket_means([], 42) == []
    assert bucket_means([1.0, 2.0, 3.0], 5) == [1.0, 2.0, 3.0]     # under budget: untouched
    assert bucket_means([3.0, 5.0], 0) == [3.0, 5.0]               # degenerate budget
    assert bucket_means([0.0, 0.0, 10.0, 10.0], 2) == [0.0, 10.0]  # pairwise means
    # A ragged split (7 into 3) still averages every sample exactly once.
    assert bucket_means([1, 1, 2, 2, 3, 3, 3], 3) == [1.0, 2.0, 3.0]


def test_bucket_means_burst_reads_as_bucket_average():
    # One 1-tick burst in a long quiet series folds into its bucket's mean —
    # after peak-normalization it renders as a visible 1-bucket area, not a
    # sub-pixel spike among hundreds of raw samples.
    vals = [0.0] * 100
    vals[50] = 900.0
    from vision_resource_monitor.state import bucket_means
    out = bucket_means(vals, 10)
    assert len(out) == 10
    assert out[5] == 90.0 and sum(out) == 90.0


def test_scale_sparkline():
    assert scale_sparkline([]) == []
    assert scale_sparkline([0.0, 0.0]) == [0.0, 0.0]        # quiet port: flat floor
    assert scale_sparkline([1.0, 4.0, 2.0]) == [25.0, 100.0, 50.0]
    assert scale_sparkline([-5.0, 10.0]) == [0.0, 100.0]    # negatives clamp to 0


# ── producer-config drift (a service ignoring the written monitor-config) ────────

def _tracker(grace_s=30.0, start=0.0):
    clock = {"t": start}
    trk = ProducerConfigTracker(grace_s=grace_s, now=lambda: clock["t"])
    return trk, clock


def ports_snap(n=0, interval=2.0):
    return Snapshot(host="H", t=1000.0, interval=interval,
                    ports=[Port(label=f"p{i}", port=1000 + i) for i in range(n)])


def test_producer_config_quiet_when_not_engaged():
    trk, clock = _tracker()
    r = fresh(ports_snap(0))
    for _ in range(3):
        clock["t"] += 60.0
        assert trk.update(r, engaged=False, watch_ports=["ssh:22"],
                          gpu_interval_s=5.0) is None


def test_producer_config_ports_hint_fires_after_grace():
    trk, clock = _tracker()
    r = fresh(ports_snap(0))
    kw = dict(engaged=True, watch_ports=["ssh:22"], gpu_interval_s=2.0)
    assert trk.update(r, **kw) is None            # first evidence — clock starts
    clock["t"] = 29.0
    assert trk.update(r, **kw) is None            # inside grace
    clock["t"] = 31.0
    hint = trk.update(r, **kw)
    assert hint is not None and hint.ports_stale
    assert "1 watched port(s)" in hint.text
    assert "if your chosen producer supports these settings" in hint.text


def test_producer_config_cadence_hint():
    trk, clock = _tracker()
    r = fresh(ports_snap(0, interval=2.0))
    kw = dict(engaged=True, watch_ports=[], gpu_interval_s=5.0)
    trk.update(r, **kw)
    clock["t"] = 31.0
    hint = trk.update(r, **kw)
    assert hint is not None and not hint.ports_stale
    assert "5s" in hint.text and "2s" in hint.text


def test_producer_config_clears_on_compliance_and_regraces():
    trk, clock = _tracker()
    kw = dict(engaged=True, watch_ports=["ssh:22"], gpu_interval_s=2.0)
    trk.update(fresh(ports_snap(0)), **kw)
    clock["t"] = 31.0
    assert trk.update(fresh(ports_snap(0)), **kw) is not None
    # A compliant fresh frame clears immediately (idle port still emits a record).
    assert trk.update(fresh(ports_snap(1)), **kw) is None
    # …and a new contradiction needs the full grace again.
    assert trk.update(fresh(ports_snap(0)), **kw) is None
    clock["t"] = 60.5
    assert trk.update(fresh(ports_snap(0)), **kw) is None
    clock["t"] = 62.0
    assert trk.update(fresh(ports_snap(0)), **kw) is not None


def test_producer_config_stale_frames_hold_state():
    trk, clock = _tracker()
    kw = dict(engaged=True, watch_ports=["ssh:22"], gpu_interval_s=2.0)
    trk.update(fresh(ports_snap(0)), **kw)
    clock["t"] = 120.0
    # Stale / missing frames are not evidence: still no hint, however long.
    assert trk.update(stale(ports_snap(0)), **kw) is None
    assert trk.update(fresh(None), **kw) is None
    # The next FRESH contradiction (past grace) fires…
    hint = trk.update(fresh(ports_snap(0)), **kw)
    assert hint is not None
    # …and a stale frame afterwards holds it rather than clearing it.
    assert trk.update(stale(ports_snap(1)), **kw) == hint


def test_producer_config_reset_restarts_grace():
    trk, clock = _tracker()
    kw = dict(engaged=True, watch_ports=["ssh:22"], gpu_interval_s=2.0)
    trk.update(fresh(ports_snap(0)), **kw)
    clock["t"] = 29.0
    trk.reset()                                    # app rewrote the config file
    trk.update(fresh(ports_snap(0)), **kw)
    clock["t"] = 58.0
    assert trk.update(fresh(ports_snap(0)), **kw) is None   # 29s into a NEW grace
    clock["t"] = 60.0
    assert trk.update(fresh(ports_snap(0)), **kw) is not None


def test_producer_config_tolerates_cadence_jitter_and_quiet_when_matching():
    trk, clock = _tracker()
    r = fresh(ports_snap(1, interval=2.2))
    for _ in range(3):
        clock["t"] += 60.0
        assert trk.update(r, engaged=True, watch_ports=["ssh:22"],
                          gpu_interval_s=2.0) is None


# ── find_pids (the right-click Copy-PID affordance's parser) ─────────────────────

def test_find_pids_matches_producer_and_app_forms():
    from vision_resource_monitor.state import find_pids
    # the producer's alert/contributor phrasing…
    assert find_pids(
        "stuck processes — find [PID 3026899] wedged in uninterruptible "
        "I/O for 55s") == [3026899]
    # …the app's bracket-suffix row form, and lowercase variants
    assert find_pids("127.0.0.1:50504  ·  devtunnel[313]  ·  Σ ↓2.7M ↑4.8M") == [313]
    assert find_pids("restart or close chrome [pid 4711] to reclaim ~5.6G") == [4711]


def test_find_pids_dedupes_preserving_order():
    from vision_resource_monitor.state import find_pids
    assert find_pids("check [PID 42], [PID 42] again, and node[43]") == [42, 43]


def test_find_pids_never_offers_bare_numbers():
    from vision_resource_monitor.state import find_pids
    # counts, rates, ports, percentages: not PIDs
    assert find_pids("3 conn  ·  ↓683B/s ↑341B/s") == []
    assert find_pids("● ssh :22  ·  92%  ·  9.2/15.9G") == []
    assert find_pids("") == []
    assert find_pids(None) == []


# ── memory-search ↔ ports tie-in (ports_held_by / format_port_holds) ─────────────

def _tie_ports():
    from vision_resource_monitor.snapshot import PortConn
    return [
        # The live shape that motivated the feature: root-owned sshd stays
        # anonymous (owner None) while its devtunnel client attributes.
        Port(label="22", port=22, conns=1, listening=True, owner=None,
             connections=[PortConn(peer="127.0.0.1:42126", process="devtunnel",
                                   pid=313)]),
        Port(label="mcp", port=10650, conns=2, listening=True,
             owner="python3:9001",
             connections=[PortConn(peer="127.0.0.1:50001", process="node",
                                   pid=7001),
                          PortConn(peer="127.0.0.1:50002", process="node",
                                   pid=7002)]),
    ]


def test_ports_held_by_connection_match_is_connected():
    from vision_resource_monitor.state import ports_held_by
    holds = ports_held_by("devtunnel", _tie_ports())
    assert [(h.label, h.port, h.relation, h.pids) for h in holds] == [
        ("22", 22, "connected", [313])]


def test_ports_held_by_owner_match_is_listening_case_insensitive():
    from vision_resource_monitor.state import ports_held_by
    holds = ports_held_by("Python3", _tie_ports())
    assert [(h.port, h.relation, h.pids) for h in holds] == [
        (10650, "listening", [9001])]


def test_ports_held_by_collects_distinct_conn_pids():
    from vision_resource_monitor.state import ports_held_by
    holds = ports_held_by("node", _tie_ports())
    assert [(h.port, h.relation, h.pids) for h in holds] == [
        (10650, "connected", [7001, 7002])]


def test_ports_held_by_exact_name_only_no_substring():
    from vision_resource_monitor.state import ports_held_by
    # "node" must not tie to "node-helper" and vice versa — equality, not substring.
    assert ports_held_by("no", _tie_ports()) == []
    assert ports_held_by("devtunnel-extra", _tie_ports()) == []


def test_ports_held_by_owner_without_listen_is_connected():
    from vision_resource_monitor.state import ports_held_by
    # The producer's outbound-watch fallback: owner = the local CLIENT of an
    # established connection, so the relation must not claim "listening".
    holds = ports_held_by("curl", [Port(label="api", port=443, conns=1,
                                        listening=False, owner="curl:512")])
    assert [(h.relation, h.pids) for h in holds] == [("connected", [512])]


def test_ports_held_by_owner_and_conn_merge_dedup_pids():
    from vision_resource_monitor.snapshot import PortConn
    from vision_resource_monitor.state import ports_held_by
    # A loopback service that also shows up as an attributed connection end:
    # one hold, the stronger (listening) relation, PIDs deduped.
    p = Port(label="loop", port=8080, conns=1, listening=True, owner="svc:100",
             connections=[PortConn(peer="127.0.0.1:5", process="svc", pid=100),
                          PortConn(peer="127.0.0.1:6", process="svc", pid=101)])
    holds = ports_held_by("svc", [p])
    assert [(h.relation, h.pids) for h in holds] == [("listening", [100, 101])]


def test_ports_held_by_tolerant_owner_forms_and_empty_inputs():
    from vision_resource_monitor.state import ports_held_by
    # Bare owner name (no :pid) still matches, with no pid claimed.
    holds = ports_held_by("sshd", [Port(label="ssh", port=22, conns=0,
                                        listening=True, owner="sshd")])
    assert [(h.relation, h.pids) for h in holds] == [("listening", [])]
    # A non-numeric tail stays part of the name, never a pid.
    holds = ports_held_by("a:b", [Port(label="x", port=1, conns=0,
                                       listening=True, owner="a:b")])
    assert [(h.relation, h.pids) for h in holds] == [("listening", [])]
    assert ports_held_by("", _tie_ports()) == []
    assert ports_held_by(None, _tie_ports()) == []
    assert ports_held_by("devtunnel", []) == []
    assert ports_held_by("devtunnel", None) == []


def test_format_port_holds_note_and_copy_pid_synergy():
    from vision_resource_monitor.state import (find_pids, format_port_holds,
                                               ports_held_by)
    ports = _tie_ports()
    note = format_port_holds(ports_held_by("devtunnel", ports))
    # A numeric label collapses to ":port"; the pid form is find_pids-compatible
    # so the right-click Copy-PID affordance works on the note.
    assert note == "ports: :22 connected [pid 313]"
    assert find_pids(note) == [313]
    assert format_port_holds(ports_held_by("python3", ports)) == \
        "ports: mcp :10650 listening [pid 9001]"
    assert format_port_holds([]) == ""


def test_format_port_holds_caps_pids_and_joins_multiple():
    from vision_resource_monitor.state import PortHold, format_port_holds
    note = format_port_holds([
        PortHold(label="mcp", port=10650, relation="connected",
                 pids=[1, 2, 3]),
        PortHold(label="22", port=22, relation="listening", pids=[]),
    ])
    assert note == ("ports: mcp :10650 connected [pid 1 · pid 2 +1]"
                    " · :22 listening")


# ── stuck-probe flags (the Stuck panel's amber lines) ─────────────────────────────

def _stuck(**kw):
    from vision_resource_monitor.snapshot import Stuck
    return Stuck(**kw)


def test_stuck_flags_quiet_and_none():
    from vision_resource_monitor.state import stuck_flags
    assert stuck_flags(None) == []
    assert stuck_flags(_stuck(procs_running=30, ncpu=26,
                              load1_per_core=2.0)) == []   # numbers never warn


def test_stuck_flags_queue_saturated_with_and_without_numbers():
    from vision_resource_monitor.state import stuck_flags
    # The producer split's wording: the queue story now says "CPU saturated"
    # (a merely-busy host must never read as "stuck"), carrying busy% when the
    # producer sent it.
    assert stuck_flags(_stuck(queue_saturated=True, procs_running=30,
                              ncpu=26, cpu_pct=91.4)) == \
        ["CPU saturated: 30 runnable on 26 cores, 91% busy"]
    assert stuck_flags(_stuck(queue_saturated=True)) == \
        ["CPU saturated: ? runnable on ? cores"]


def test_stuck_flags_cpu_saturated_sustained_idle():
    from vision_resource_monitor.state import stuck_flags
    assert stuck_flags(_stuck(cpu_saturated=True, cpu_pct=88.0)) == \
        ["CPU saturated — 88% busy sustained"]
    # An older producer never sets the flag; a flag without busy% states the
    # judgment without inventing a level (never "near-zero idle" — that would
    # overstate the 85% threshold).
    assert stuck_flags(_stuck(cpu_saturated=True)) == \
        ["CPU saturated — high busy sustained"]
    assert stuck_flags(_stuck(cpu_pct=99.0)) == []   # numbers never warn


def test_stuck_flags_divergence_and_slow_sampler():
    from vision_resource_monitor.state import stuck_flags
    assert stuck_flags(_stuck(wait_diverged=True, load1_per_core=8.4)) == \
        ["load 8.4/core while CPU is mostly idle — threads stuck in kernel waits"]
    flags = stuck_flags(_stuck(
        sampler_slow=True,
        sampler_ms={"nvidia-smi": 284.2, "docker": 3400.0}))
    # "saturated or stalling" (producer): the line must not promise a stall
    # the wedged census ruled out.
    assert flags == ["docker sampler taking 3.4s — the host is saturated "
                     "or stalling"]


def test_stuck_flags_all_active_in_order():
    from vision_resource_monitor.state import stuck_flags
    flags = stuck_flags(_stuck(
        queue_saturated=True, cpu_saturated=True, wait_diverged=True,
        sampler_slow=True, procs_running=52, ncpu=26, load1_per_core=8.4,
        cpu_pct=91.0, sampler_ms={"nvidia-smi": 5100.0}))
    assert [f.split(":")[0].split(" taking")[0] for f in flags] == [
        "CPU saturated", "CPU saturated — 91% busy sustained",
        "load 8.4/core while CPU is mostly idle — "
        "threads stuck in kernel waits", "nvidia-smi sampler"]


def test_security_stack_headline_states_and_honesty():
    from vision_resource_monitor.snapshot import SecurityStack, SecurityStackTop
    from vision_resource_monitor.state import security_stack_headline
    # Absent field: no line at all (never a fabricated cause).
    assert security_stack_headline(None) is None
    # Quiet: the summed figure, dim.
    text, warn, tip = security_stack_headline(SecurityStack(cores=0.1))
    assert text == "security stack: 0.1 cores" and warn is False
    assert "no spawn reading" in tip
    # Hot but not sustained: clocked, still dim — only `taxing` warns.
    text, warn, tip = security_stack_headline(
        SecurityStack(cores=1.2, hot=True, hot_for_s=120.0))
    assert text == "security stack: 1.2 cores (hot 2m)" and warn is False
    # Taxing: warn + duration + groups/correlate in the tooltip.
    text, warn, tip = security_stack_headline(SecurityStack(
        cores=1.6, hot=True, hot_for_s=360.0, taxing=True,
        spawns_per_min=2400.0,
        top=[SecurityStackTop(group="defender", label="Defender",
                              cpu_pct=120.0)]))
    assert text == "⚠ security stack: 1.6 cores — sustained 6m" and warn is True
    assert "Defender 120%" in tip and "2400" in tip
    # A cores-less reading states "?" rather than inventing a figure.
    text, warn, _ = security_stack_headline(SecurityStack(taxing=True,
                                                          hot_for_s=360.0))
    assert text.startswith("⚠ security stack: ? cores") and warn is True
    # A taxing frame WITHOUT a duration must not fabricate "sustained 0m"
    # (review fold — same rule as the hot branch).
    text, warn, _ = security_stack_headline(SecurityStack(cores=1.6,
                                                          taxing=True))
    assert text == "⚠ security stack: 1.6 cores" and warn is True


def test_populate_report_and_summaries_carry_security_stack():
    from vision_resource_monitor.snapshot import (Scanner, SecurityStack,
                                                  Snapshot)
    from vision_resource_monitor.state import (panel_populate_report,
                                               panel_summaries)
    snap = Snapshot(t=1.0)
    assert ("security_stack: absent (producer emitted no reading)"
            in panel_populate_report(snap))
    snap.security_stack = SecurityStack(cores=1.6, hot=True, hot_for_s=360.0,
                                        taxing=True)
    assert ("security_stack: 1.6 cores, hot=True, taxing=True"
            in panel_populate_report(snap))
    # Collapsed header: the taxing judgment leads the scanners summary…
    snap.scanners = [Scanner(group="defender", label="Defender", state="idle")]
    assert panel_summaries(snap)["scanners"] == "⚠ stack 1.6c · idle"
    # …and stands alone when there is no census summary at all.
    snap.scanners = None
    assert panel_summaries(snap)["scanners"] == "⚠ stack 1.6c"
    # A quiet stack never adds a chip (the judgment, not the numbers).
    snap.security_stack = SecurityStack(cores=2.0)
    snap.scanners = [Scanner(group="defender", label="Defender", state="idle")]
    assert panel_summaries(snap)["scanners"] == "idle"


def test_populate_report_carries_stuck_line():
    from vision_resource_monitor.state import panel_populate_report
    from vision_resource_monitor.snapshot import Snapshot, StuckWedged
    snap = Snapshot(t=1.0)
    assert "stuck: absent" in "\n".join(panel_populate_report(snap))
    snap.stuck = _stuck(procs_running=27, ncpu=26, queue_saturated=True,
                        wedged=[StuckWedged(pid=1, comm="find", for_s=55.0)])
    assert "stuck: queue 27/26, 1 wedged, 1 flag(s)" in \
        "\n".join(panel_populate_report(snap))


def test_format_gpu_users_line_tooltip_and_copy_pid_form():
    from vision_resource_monitor.snapshot import GpuUser
    from vision_resource_monitor.state import find_pids
    users = [GpuUser(pid=32360, name="QmlRenderer", vram_mib=7917.0),
             GpuUser(pid=2632, name="dwm", vram_mib=2483.0),
             GpuUser(pid=21176, name="NVIDIA Overlay", vram_mib=232.0),
             GpuUser(pid=1920, name="csrss", vram_mib=157.0)]
    line, tip = format_gpu_users(users)
    assert line == ("vram: QmlRenderer[32360] 7.7G · dwm[2632] 2.4G"
                    " · NVIDIA Overlay[21176] 232M · +1 more")
    # ≥1G renders G, below renders M; the tooltip carries EVERY entry
    assert tip.splitlines() == [
        "QmlRenderer[32360] 7.7G", "dwm[2632] 2.4G",
        "NVIDIA Overlay[21176] 232M", "csrss[1920] 157M"]
    # the bracket-pid form is exactly what the Copy-PID menu matches
    assert find_pids(line) == [32360, 2632, 21176]
    short, _ = format_gpu_users(users[:2])
    assert "+" not in short  # no remainder tag when everything fits


def test_gpu_users_gap_hint_distinguishes_none_from_empty():
    assert "older build" in gpu_users_gap_hint(None)
    hint = gpu_users_gap_hint([])
    assert "WSL-side" in hint and "no Windows VRAM holder" in hint
    # honesty: the app only knows nothing was REPORTED — a container inside the
    # docker-sweep window is still possible; the wording must not rule it out
    assert "no GPU container reported" in hint and "not-yet-seen container" in hint


def test_format_gpu_users_boundary_and_junk_rows():
    from vision_resource_monitor.snapshot import GpuUser
    three = [GpuUser(pid=1, name="a", vram_mib=1024.0),
             GpuUser(pid=2, name="b", vram_mib=512.0),
             GpuUser(pid=3, name="c", vram_mib=128.0)]
    line, _ = format_gpu_users(three)
    assert "+0 more" not in line and line.endswith("c[3] 128M")  # len == shown
    # junk-coerced rows (pid 0, no figure) are dropped, real rows keep slots
    junk = GpuUser(pid=0, name="?", vram_mib=0.0)
    line, tip = format_gpu_users([junk, three[0], junk, three[1]])
    assert line == "vram: a[1] 1.0G · b[2] 512M" and "?" not in tip
    # a pid-less row with a REAL figure still carries information — kept
    line, _ = format_gpu_users([GpuUser(pid=0, name="?", vram_mib=500.0)])
    assert line == "vram: ?[0] 500M"
    # all-junk → render nothing (NOT the producer's []; no hint fall-through)
    assert format_gpu_users([junk, junk]) == ("", "")


def test_populate_report_carries_gpu_users_line():
    from vision_resource_monitor.snapshot import GpuUser, Snapshot
    snap = Snapshot(t=1.0)
    assert "gpu_users: absent (producer reports no VRAM holders)" in \
        panel_populate_report(snap)
    snap.gpu_users = []
    assert "gpu_users: empty (counters answered, nothing ≥ floor)" in \
        panel_populate_report(snap)
    snap.gpu_users = [GpuUser(pid=1, name="a", vram_mib=100.0)]
    assert "gpu_users: 1 holder(s)" in panel_populate_report(snap)


# ── process→ports target rows (format_proc_ports / proc_name_for_pid) ────────────

def test_format_proc_ports_answered_sockets_and_more():
    from vision_resource_monitor.snapshot import ProcPortRecord, ProcPortSocket
    from vision_resource_monitor.state import find_pids, format_proc_ports
    rec = ProcPortRecord(
        side="wsl", pid=582236, name="python3", found=True, sockets_total=5,
        sockets=[ProcPortSocket(proto="tcp", state="LISTEN",
                                local="0.0.0.0:39999", peer=None),
                 ProcPortSocket(proto="udp", state="UNCONN",
                                local="0.0.0.0:5353", peer=None),
                 ProcPortSocket(proto="tcp", state="ESTAB",
                                local="127.0.0.1:33779",
                                peer="127.0.0.1:51000"),
                 ProcPortSocket(proto="tcp", state="ESTAB",
                                local="a:1", peer="b:2")])
    line, tip = format_proc_ports(rec)
    # Leads with the Copy-PID form, spells the first sockets, counts the rest.
    assert line.startswith("python3[582236] (wsl)")
    assert "listen :39999" in line and "bound :5353/udp" in line
    assert "127.0.0.1:33779→127.0.0.1:51000" in line
    assert "+2 more" in line                       # 5 total, 3 spelled
    assert find_pids(line) == [582236]
    # The tooltip carries every producer-bounded socket + the beyond-bound note.
    assert "a:1→b:2" in tip and "+1 beyond the producer's bound" in tip


def test_format_proc_ports_stated_causes():
    from vision_resource_monitor.snapshot import ProcPortRecord
    from vision_resource_monitor.state import format_proc_ports
    pending = ProcPortRecord(side="windows", pid=7, name="ghost", found=None,
                             sockets=None, note="no answer yet (sweep pending)")
    line, _ = format_proc_ports(pending)
    assert "no answer yet" in line
    empty = ProcPortRecord(side="wsl", pid=9, name="quietd", found=True,
                           sockets=[], sockets_total=0,
                           note="no sockets visible to the producer")
    line2, _ = format_proc_ports(empty)
    assert "no sockets visible" in line2           # [] renders the cause, not blank
    # A definitive not-found is an ANSWER — it must not read as pending
    # (review L1: found was parsed but never consulted).
    bare = ProcPortRecord(side="wsl", pid=9, name=None, found=False,
                          sockets=None, note=None)
    line3, _ = format_proc_ports(bare)
    assert "?[9] (wsl)" in line3 and "process not found" in line3
    unknown = ProcPortRecord(side="wsl", pid=9, name=None, found=None,
                             sockets=None, note=None)
    line4, _ = format_proc_ports(unknown)
    assert "no answer yet" in line4


def test_format_proc_ports_note_beside_sockets_is_visible():
    # The pid-reuse warning is the loaded case: the visible sockets may belong
    # to the WRONG process — the note must mark the line, not hide in the
    # tooltip (review L6).
    from vision_resource_monitor.snapshot import ProcPortRecord, ProcPortSocket
    from vision_resource_monitor.state import format_proc_ports
    rec = ProcPortRecord(side="wsl", pid=9, name="cat", found=True,
                         sockets_total=1,
                         sockets=[ProcPortSocket(state="LISTEN", local="a:1")],
                         note="comm is now cat (was python — pid reused?)")
    line, tip = format_proc_ports(rec)
    assert line.endswith("⚠")
    assert "⚠ comm is now cat" in tip


def test_proc_name_for_pid_row_forms():
    from vision_resource_monitor.state import proc_name_for_pid
    assert proc_name_for_pid("devtunnel[313]  ·  Σ ↓1.2M", 313) == "devtunnel"
    # A line prefix must not be swallowed into the name (colon bounds it).
    assert proc_name_for_pid(
        "vram: QmlRenderer[32360] 7.7G · dwm[2632] 2.4G", 32360) == "QmlRenderer"
    assert proc_name_for_pid(
        "vram: QmlRenderer[32360] 7.7G · dwm[2632] 2.4G", 2632) == "dwm"
    # Spaced names survive; the "[PID N]" phrasing yields None (no row form).
    assert proc_name_for_pid("NVIDIA Overlay[21176] 232M", 21176) == "NVIDIA Overlay"
    assert proc_name_for_pid("find [PID 3026899] wedged", 3026899) is None
    assert proc_name_for_pid("", 5) is None
    # Row-decoration glyphs are never part of the name: "⚠ comm" seeded into
    # the producer's pid-reuse check would guarantee a false mismatch (H1).
    assert proc_name_for_pid("⚠ devtunnel[313]", 313) == "devtunnel"
    assert proc_name_for_pid("↳ 10.0.0.1:60000  ·  sshd[99]", 99) == "sshd"


def test_proc_target_key_forms():
    from vision_resource_monitor.state import proc_target_key
    assert proc_target_key({"side": "wsl", "pid": 5, "name": "x"}) == \
        ("pid", "wsl", 5)
    # Name keys lower (dedupe/matching is case-insensitive end to end).
    assert proc_target_key({"side": "wsl", "pid": None, "name": " Node "}) == \
        ("name", "wsl", "node")
    assert proc_target_key({"side": "windows", "name": "Py"}) == \
        ("name", "windows", "py")
    # The two spaces can't collide: pid keys carry ints, name keys strings
    # (and all-digits names are rejected at entry anyway).
    assert proc_target_key({"side": "wsl", "pid": 8021}) != \
        proc_target_key({"side": "wsl", "name": "8021"})


def _name_rec(**kw):
    from vision_resource_monitor.snapshot import ProcPortRecord
    base = {"side": "wsl", "name": "node", "kind": "name"}
    base.update(kw)
    return ProcPortRecord.from_dict(base)


def test_format_proc_name_ports_states():
    from vision_resource_monitor.state import format_proc_name_ports
    # Pending: matches None → the stated cause, no match rows.
    line, tip, rows = format_proc_name_ports(_name_rec(
        note="no answer yet (sweep pending)"))
    assert "no answer yet" in line and rows == []
    # Answered empty: the producer's own wording carries.
    line, tip, rows = format_proc_name_ports(_name_rec(
        matches=[], match_total=0, note="no matching process"))
    assert "no matching process" in line and rows == []
    # Matches: count from match_total (the TRUE count), "top N shown" when
    # bounded, rows lead with name[pid] (find_pids/#46 Copy PID compatible).
    line, tip, rows = format_proc_name_ports(_name_rec(
        match_total=12,
        matches=[{"pid": 9, "name": "node",
                  "sockets": [{"proto": "tcp", "state": "LISTEN",
                               "local": "0.0.0.0:8021", "peer": None},
                              {"proto": "tcp", "state": "ESTAB",
                               "local": "a:1", "peer": "b:2"}],
                  "sockets_total": 6},
                 {"pid": 11, "name": "nodemon", "sockets": [],
                  "sockets_total": 0}]))
    assert "12 matching processes" in line and "top 2 shown" in line
    assert rows[0][0].startswith("node[9]")
    assert "listen :8021" in rows[0][0] and "a:1→b:2" in rows[0][0]
    assert "+4 more" in rows[0][0]                       # 6 total, 2 shown
    assert rows[1][0] == "nodemon[11] — no sockets visible"
    from vision_resource_monitor.state import find_pids
    assert find_pids(rows[0][0]) == [9]
    # A note beside matches marks the header line, spelled in the tooltip.
    line, tip, rows = format_proc_name_ports(_name_rec(
        match_total=1, note="something to say",
        matches=[{"pid": 9, "name": "node", "sockets": [],
                  "sockets_total": 0}]))
    assert line.endswith("⚠") and "something to say" in tip


def test_populate_report_carries_scanners_three_way_line():
    from vision_resource_monitor.state import panel_populate_report
    from vision_resource_monitor.snapshot import Scanner, Snapshot
    snap = Snapshot(t=1.0)
    assert "scanners: absent (producer emitted no scanner census)" in \
        "\n".join(panel_populate_report(snap))
    snap.scanners = []
    assert "scanners: empty (census answered, none present)" in \
        "\n".join(panel_populate_report(snap))
    snap.scanners = [Scanner(group="defender", label="Defender", burning=True),
                     Scanner(group="search-indexer", label="Search Indexer")]
    assert "scanners: 2 group(s), 1 burning" in \
        "\n".join(panel_populate_report(snap))


# ── collapsed-header panel summaries (maintainer ask 2026-07-21) ───────────────────

def test_panel_summaries_full_frame():
    from vision_resource_monitor.snapshot import (
        Activity, Contributor, CpuStat, Forecast, Gpu, HostMemory, MemoryPanel,
        Port, Processes, ProgressRun, RamStat, Scanner, Snapshot, Stuck,
        StuckWedged, Swap)
    from vision_resource_monitor.state import panel_summaries
    snap = Snapshot(t=10000.0)
    snap.gpus = [Gpu(util=87.0, mem_used_mib=9421.0, mem_total_mib=16281.0)]
    snap.activity = Activity(start_t=10000.0 - 7200.0)
    snap.runs = [ProgressRun(state="running"), ProgressRun(state="stalled"),
                 ProgressRun(state="ok")]
    snap.forecast = Forecast(level="warn", eta_min=27.0)
    snap.contributors = [Contributor(kind="memory_overcommit", rank=1)]
    snap.host_cpu = CpuStat(pct=34.0)
    snap.host_ram = RamStat(used_mib=16000.0, total_mib=32768.0)
    snap.memory = MemoryPanel(windows=HostMemory(used_mib=31949.0,
                                                 total_mib=65229.0))
    snap.processes = Processes(claude_total=8,
                               swap=Swap(used_mib=400.0, total_mib=8192.0,
                                         pct=5.0))
    snap.stuck = Stuck(procs_running=3, ncpu=26,
                       wedged=[StuckWedged(pid=1, comm="find", for_s=55.0)])
    snap.scanners = [
        Scanner(group="defender", label="Defender", cpu_pct=34.0,
                state="active", active_s=720.0, burning=True),
        Scanner(group="search-indexer", label="Search Indexer", state="idle")]
    snap.ports = [Port(label="ssh", port=22, active=True),
                  Port(label="web", port=80)]
    s = panel_summaries(snap)
    assert s["gpu"] == "87% · 9.2/15.9G"
    assert s["activity"] == "active 2h"
    assert s["runs"] == "1 running · 1 stalled"
    assert s["forecast"] == "warn ~27m · 1 contributor(s)"
    assert s["host"] == "cpu 34% · ram 15.6/32.0G"
    assert s["memory"] == "31.2/63.7G used"
    assert s["sessions"] == "8 session(s) · swap 5%"
    assert s["stuck"] == "queue 3/26 · 1 wedged"
    assert s["scanners"] == "⚠ Defender active · 34%"
    assert s["ports"] == "2 watched · 1 active"


def test_panel_summaries_stuck_saturated_chip():
    from vision_resource_monitor.snapshot import Snapshot, Stuck
    from vision_resource_monitor.state import panel_summaries
    snap = Snapshot(t=1.0)
    snap.stuck = Stuck(procs_running=52, ncpu=26, cpu_saturated=True)
    assert panel_summaries(snap)["stuck"] == "queue 52/26 · saturated"
    # Either producer judgment arms the chip — the queue census alone too.
    snap.stuck = Stuck(procs_running=52, ncpu=26, queue_saturated=True)
    assert panel_summaries(snap)["stuck"] == "queue 52/26 · saturated"
    # The chip is the producer's judgment — raw numbers alone never add it.
    snap.stuck = Stuck(procs_running=52, ncpu=26, cpu_pct=99.0)
    assert panel_summaries(snap)["stuck"] == "queue 52/26"


def test_panel_summaries_absent_data_never_fabricates():
    from vision_resource_monitor.snapshot import Scanner, Snapshot
    from vision_resource_monitor.state import panel_summaries
    # No frame at all → nothing to say for ANY panel.
    assert panel_summaries(None) == {}
    # A bare snapshot: only activity has a truthful reading (no block = idle);
    # every other panel stays absent — a plain header, never a fabricated line.
    s = panel_summaries(Snapshot(t=1.0))
    assert s == {"activity": "idle"}
    # All-idle scanners say so; a lingering burst headlines over idle rows.
    snap = Snapshot(t=1.0)
    snap.scanners = [Scanner(group="defender", label="Defender", state="idle")]
    assert panel_summaries(snap)["scanners"] == "idle"
    snap.scanners.append(Scanner(group="search-indexer", label="Search Indexer",
                                 state="burst", ago_s=240.0))
    assert panel_summaries(snap)["scanners"] == "Search Indexer burst"


def test_panel_summaries_review_folds():
    # L1: a still-burning row that flipped to burst-linger keeps its ⚠ and
    # outranks a hotter non-burning active row (the header must never read
    # benign while the alerts/contributor tiers fire).
    from vision_resource_monitor.snapshot import (
        Activity, ProgressRun, Scanner, Snapshot, Stuck, StuckWedged)
    from vision_resource_monitor.state import panel_summaries
    snap = Snapshot(t=1.0)
    snap.scanners = [
        Scanner(group="search-indexer", label="Search Indexer", cpu_pct=50.0,
                state="active"),
        Scanner(group="defender", label="Defender", cpu_pct=3.0,
                state="burst", ago_s=10.0, burning=True)]
    assert panel_summaries(snap)["scanners"] == "⚠ Defender burst"
    # L2: terminal-bad runs still summarize — a dying run must not blank the
    # collapsed header at the exact frame the body goes red.
    snap = Snapshot(t=1.0)
    snap.runs = [ProgressRun(state="died"), ProgressRun(state="error"),
                 ProgressRun(state="ok")]
    assert panel_summaries(snap)["runs"] == "1 died · 1 error"
    # L3: the wedged census survives missing queue numbers (/proc/stat
    # unreadable while the D-state sweep works).
    snap = Snapshot(t=1.0)
    snap.stuck = Stuck(wedged=[StuckWedged(pid=1, comm="find", for_s=55.0)])
    assert panel_summaries(snap)["stuck"] == "1 wedged"
    # L4: a junk frame clock renders "active ?" like the body, never a
    # fabricated "active 0s".
    snap = Snapshot(t=0.0)
    snap.activity = Activity(start_t=500.0)
    assert panel_summaries(snap)["activity"] == "active ?"


def test_panel_summaries_no_forecast_with_contributor_claims_no_level():
    # --no-forecast + a firing contributor (e.g. stuck): the summary names the
    # contributor count WITHOUT fabricating an "ok" level nobody reported.
    from vision_resource_monitor.snapshot import Contributor, Snapshot
    from vision_resource_monitor.state import panel_summaries
    snap = Snapshot(t=1.0)
    snap.contributors = [Contributor(kind="stuck_processes", rank=1)]
    assert panel_summaries(snap)["forecast"] == "1 contributor(s)"


def test_panel_summaries_non_finite_activity_never_raises():
    # inf/nan pass the tolerant parse; a raise inside panel_summaries would
    # abort update_from mid-frame (the producer review-H1 crash class).
    from vision_resource_monitor.snapshot import Activity, Snapshot
    from vision_resource_monitor.state import panel_summaries
    snap = Snapshot(t=float("inf"))
    snap.activity = Activity(start_t=1.0)
    assert panel_summaries(snap)["activity"] == "active ?"
    snap.t = float("nan")
    assert panel_summaries(snap)["activity"] == "active ?"


# ── egress panel (producer; the #66 app half) ─────────────────────────────────────

def test_populate_report_carries_egress_three_way_line():
    from vision_resource_monitor.snapshot import Egress, EgressTalker, Snapshot
    from vision_resource_monitor.state import panel_populate_report
    snap = Snapshot(t=1.0)
    assert "egress: absent (producer emitted no egress ranking)" in \
        "\n".join(panel_populate_report(snap))
    snap.egress = Egress(window_s=60.0, total_in_bps=0.0, total_out_bps=0.0)
    assert "egress: quiet (no wsl-side talkers in the window)" in \
        "\n".join(panel_populate_report(snap))
    snap.egress = Egress(
        window_s=58.0, total_in_bps=900.0, total_out_bps=87000.0,
        top=[EgressTalker(pid=317, name="devtunnel", out_bps=87000.0),
             EgressTalker(pid=4242, name="python3.11", out_bps=120.0)])
    assert "egress: 2 talker(s), iface absent" in \
        "\n".join(panel_populate_report(snap))


def test_panel_summaries_egress():
    from vision_resource_monitor.snapshot import (
        Egress, EgressIface, EgressTalker, Snapshot)
    from vision_resource_monitor.state import panel_summaries
    # Absent → no key (a plain header, never fabricated).
    assert "egress" not in panel_summaries(Snapshot(t=1.0))
    # The whole-box iface view headlines: ↑rate · ratio.
    snap = Snapshot(t=1.0)
    snap.egress = Egress(
        window_s=58.0, total_in_bps=900.0, total_out_bps=87000.0,
        iface=EgressIface(in_bps=2400.0, out_bps=91000.0, out_in_ratio=37.9),
        top=[EgressTalker(pid=317, name="devtunnel", out_bps=87000.0)])
    assert panel_summaries(snap)["egress"] == "↑89K/s · 37.9×"
    # No iface block (NETIF unavailable): fall back to the tcp totals, no
    # fabricated ratio.
    snap.egress.iface = None
    assert panel_summaries(snap)["egress"] == "↑85K/s"
    # A ratio the producer refused (in-rate 0) stays unclaimed.
    snap.egress.iface = EgressIface(in_bps=0.0, out_bps=512.0,
                                    out_in_ratio=None)
    assert panel_summaries(snap)["egress"] == "↑512B/s"
    # A non-finite ratio from a corrupt feed is dropped, never "inf×"
    # (parity with the window_s guard — review fold, 2026-07-23).
    snap.egress.iface = EgressIface(in_bps=0.0, out_bps=512.0,
                                    out_in_ratio=float("inf"))
    assert panel_summaries(snap)["egress"] == "↑512B/s"
    # A present-but-rate-less reading says nothing (never "↑—").
    snap.egress = Egress(window_s=60.0)
    assert "egress" not in panel_summaries(snap)


# ── Actions runner-fleet census report + summary (producer render half) ──────────


def test_populate_report_carries_runner_fleet_three_way_line():
    from vision_resource_monitor.snapshot import Runner, RunnerFleet, Snapshot
    from vision_resource_monitor.state import panel_populate_report
    snap = Snapshot(t=1.0)
    assert "runner_fleet: absent (producer emitted no fleet census)" in \
        "\n".join(panel_populate_report(snap))
    snap.runner_fleet = RunnerFleet(runners=[], total=0, active_count=0)
    assert "runner_fleet: empty (docker answered, no runner containers)" in \
        "\n".join(panel_populate_report(snap))
    # Empty list + unresolved inspects = an UNKNOWN fleet, never "empty".
    snap.runner_fleet = RunnerFleet(runners=[], total=0, active_count=0,
                                    unresolved=2)
    assert "runner_fleet: unknown (2 container(s) awaiting identity)" in \
        "\n".join(panel_populate_report(snap))
    snap.runner_fleet = RunnerFleet(
        runners=[Runner(name="runner-2", container="gha-runner-alpha-2",
                        cpu_pct=118.5, active=True),
                 Runner(name="runner-3", container="gha-runner-beta",
                        cpu_pct=0.0, active=False)],
        total=2, active_count=1, cpu_pct_total=118.5)
    assert "runner_fleet: 2 runner(s), 1 active" in \
        "\n".join(panel_populate_report(snap))
    # A MIXED fleet (rows + failing inspects) must not read fully-known —
    # the unresolved count rides the line (review L2).
    snap.runner_fleet.unresolved = 3
    assert "runner_fleet: 2 runner(s), 1 active, 3 unresolved" in \
        "\n".join(panel_populate_report(snap))


def test_panel_summaries_runners():
    from vision_resource_monitor.snapshot import Runner, RunnerFleet, Snapshot
    from vision_resource_monitor.state import panel_summaries
    # Absent → no key (a plain header, never fabricated); an EMPTY fleet
    # also stays plain — "0 up" on a collapsed header reads like an outage.
    assert "runners" not in panel_summaries(Snapshot(t=1.0))
    snap = Snapshot(t=1.0)
    snap.runner_fleet = RunnerFleet(runners=[], total=0, active_count=0)
    assert "runners" not in panel_summaries(snap)
    # Fleet with a busy runner: size · active · cpu.
    snap.runner_fleet = RunnerFleet(
        runners=[Runner(name="runner-2", active=True, cpu_pct=118.6),
                 Runner(name="runner-3", active=False, cpu_pct=0.0)],
        total=2, active_count=1, cpu_pct_total=118.6)
    assert panel_summaries(snap)["runners"] == "2 up · 1 active · cpu 119%"
    # All quiet: no "0 active", and a sub-1% total carries no cpu bit
    # (a census with no real signal must not read "cpu 0%").
    snap.runner_fleet = RunnerFleet(
        runners=[Runner(name="runner-3", active=False, cpu_pct=0.01)],
        total=1, active_count=0, cpu_pct_total=0.01)
    assert panel_summaries(snap)["runners"] == "1 up"
    # Stats sweep unavailable: totals None → size only, never "cpu —".
    snap.runner_fleet = RunnerFleet(
        runners=[Runner(name="runner-3")], total=1, active_count=0)
    assert panel_summaries(snap)["runners"] == "1 up"
    # A mixed fleet carries its unresolved count on the header (review L2);
    # a corrupt-feed Infinity total drops the cpu bit, never "cpu inf%"
    # (the egress non-finite rule, review L3).
    snap.runner_fleet = RunnerFleet(
        runners=[Runner(name="runner-3", active=False, cpu_pct=0.0)],
        total=1, active_count=0, unresolved=2,
        cpu_pct_total=float("inf"))
    assert panel_summaries(snap)["runners"] == "1 up · 2 unresolved"


# ── Runners repo grouping ───────────────────────────────────────────────────────


def test_repo_group_label():
    from vision_resource_monitor.state import repo_group_label
    assert repo_group_label("sample-org/alpha-runner-group") == "arg"
    assert repo_group_label("sample-org/beta-runner-team") == "brt"
    assert repo_group_label("sample-org/gamma-monitor-service") == "gms"
    assert repo_group_label("sample-author/delta-renderer-core") == "drc"
    # A single-word repo keeps the word; a bare org string (the producer's
    # ORG_NAME fallback) labels as itself; unattributed runners group under
    # "other" — visible, never vanished.
    assert repo_group_label("sample-org/utility") == "utility"
    assert repo_group_label("acme") == "acme"
    assert repo_group_label(None) == "other"
    assert repo_group_label("") == "other"
    assert repo_group_label("https://ghes.corp/o/some-thing") == "st"
    # Comma hygiene: labels persist into a comma-joined QSettings store —
    # a hostile/corrupt feed must not corrupt it (review F2).
    assert repo_group_label("o/we,ird") == "weird"
    assert repo_group_label("o/,") == "other"


def test_runner_groups_buckets_and_totals():
    from vision_resource_monitor.snapshot import Runner, RunnerFleet
    from vision_resource_monitor.state import runner_groups
    fleet = RunnerFleet(runners=[
        Runner(name="runner-2", repo="o/alpha-runner-group",
               cpu_pct=118.5, mem_mib=600.0, active=True),
        Runner(name="runner-1", repo="o/alpha-runner-group",
               cpu_pct=0.5, mem_mib=800.0, active=False),
        Runner(name="runner-3", repo="k/zeta-worker-xray",
               cpu_pct=0.0, mem_mib=100.0, active=False),
        Runner(name="runner-x"),                # no repo, no stats
    ], total=4, active_count=1)
    gs = runner_groups(fleet)
    # Label-sorted groups, stable frame-to-frame.
    assert [g.label for g in gs] == ["arg", "other", "zwx"]
    group_a = gs[0]
    assert [r.name for r in group_a.runners] == ["runner-2", "runner-1"]
    assert group_a.active_count == 1
    assert (group_a.cpu_pct_total, group_a.mem_mib_total) == (119.0, 1400.0)
    # A group with no readings keeps honest None totals (never 0).
    other = gs[1]
    assert other.label == "other" and other.repo is None
    assert other.cpu_pct_total is None and other.mem_mib_total is None
    # Non-finite readings never enter a Σ (the egress non-finite rule).
    from vision_resource_monitor.snapshot import RunnerFleet as _RF
    g2 = runner_groups(_RF(runners=[
        Runner(name="a", repo="o/x", cpu_pct=float("inf"),
               mem_mib=1.0)]))[0]
    assert g2.cpu_pct_total is None and g2.mem_mib_total == 1.0
    # Initials collision: two DIFFERENT repos merge into one label — the
    # group's repo (the tooltip source) nulls rather than mis-attribute the
    # merged Σ to whichever repo came first (review F1). Same for a real
    # repo colliding with the unattributed "other" bucket.
    gs3 = runner_groups(_RF(runners=[
        Runner(name="a", repo="o/sample-collision-one"),
        Runner(name="b", repo="o/second-case-object")]))
    assert [g.label for g in gs3] == ["sco"]
    assert gs3[0].repo is None and len(gs3[0].runners) == 2
    gs4 = runner_groups(_RF(runners=[
        Runner(name="a", repo="o/other"),
        Runner(name="b")]))
    assert gs4[0].label == "other" and gs4[0].repo is None


def test_runner_groups_bucket_bursts():
    from vision_resource_monitor.snapshot import (Runner, RunnerBurst,
                                                  RunnerFleet)
    from vision_resource_monitor.state import runner_groups
    fleet = RunnerFleet(
        runners=[Runner(name="runner-3", repo="o/alpha-runner-group",
                        active=True)],
        bursts=[
            RunnerBurst(runner="runner-3", repo="o/alpha-runner-group",
                        state="running", age_s=42.0),
            RunnerBurst(runner="runner-3", repo="o/alpha-runner-group",
                        state="done", dur_s=55.0, ended_ago_s=120.0),
            # A lingering done burst whose runner already LEFT the fleet:
            # its repo still gets a group (short-lifecycle visibility must
            # not depend on the runner surviving).
            RunnerBurst(runner="runner-4",
                        repo="o/zeta-worker-xray",
                        state="done", dur_s=30.0, ended_ago_s=300.0)],
        total=1, active_count=1)
    gs = runner_groups(fleet)
    assert [g.label for g in gs] == ["arg", "zwx"]
    group_a, group_c = gs
    assert [b.state for b in group_a.bursts] == ["running", "done"]
    assert group_a.burst_running == 1
    assert group_c.runners == [] and len(group_c.bursts) == 1
    assert group_c.burst_running == 0
    assert group_c.repo == "o/zeta-worker-xray"
    # The collision rule applies to burst-created groups too: a burst repo
    # colliding with an existing same-label group nulls the claim.
    gs2 = runner_groups(RunnerFleet(
        runners=[Runner(name="a", repo="o/sample-collision-one")],
        bursts=[RunnerBurst(runner="b", repo="o/second-case-object",
                            state="done", dur_s=1.0, ended_ago_s=1.0)]))
    assert [g.label for g in gs2] == ["sco"] and gs2[0].repo is None


def test_panel_summaries_runners_burst_bit():
    from vision_resource_monitor.snapshot import (Runner, RunnerBurst,
                                                  RunnerFleet, Snapshot)
    from vision_resource_monitor.state import panel_summaries
    snap = Snapshot(t=1.0)
    # A running burst is header-worthy; done-lingering ones are not (a
    # collapsed header must not read busy for ten minutes after the job),
    # and a junk state counts as NEITHER — only the producer's literal
    # "running" is a live claim.
    snap.runner_fleet = RunnerFleet(
        runners=[Runner(name="h", active=True)], total=1, active_count=1,
        bursts=[RunnerBurst(runner="h", state="running", age_s=5.0),
                RunnerBurst(runner="h", state="done", dur_s=9.0,
                            ended_ago_s=60.0),
                RunnerBurst(runner="h", state="paused", age_s=5.0)])
    assert panel_summaries(snap)["runners"] == "1 up · 1 active · 1 burst"
    snap.runner_fleet = RunnerFleet(
        runners=[Runner(name="h", active=False)], total=1, active_count=0,
        bursts=[RunnerBurst(runner="h", state="done", dur_s=9.0,
                            ended_ago_s=60.0)])
    assert panel_summaries(snap)["runners"] == "1 up"
    # Burst evidence outlives the runner: an EMPTY fleet with a running
    # burst still summarizes (review H-empty-fleet) — done-only stays
    # plain (no fabricated urgency ten minutes after the job).
    snap.runner_fleet = RunnerFleet(
        runners=[], total=0, active_count=0,
        bursts=[RunnerBurst(runner="h", state="running", age_s=5.0)])
    assert panel_summaries(snap)["runners"] == "1 burst"
    snap.runner_fleet = RunnerFleet(
        runners=[], total=0, active_count=0,
        bursts=[RunnerBurst(runner="h", state="done", dur_s=9.0,
                            ended_ago_s=60.0)])
    assert "runners" not in panel_summaries(snap)


def test_populate_report_carries_runner_bursts():
    from vision_resource_monitor.snapshot import (Runner, RunnerBurst,
                                                  RunnerFleet, Snapshot)
    from vision_resource_monitor.state import panel_populate_report
    snap = Snapshot(t=1.0)
    snap.runner_fleet = RunnerFleet(
        runners=[Runner(name="h", active=True)], total=1, active_count=1,
        bursts=[RunnerBurst(runner="h", state="running", age_s=5.0),
                RunnerBurst(runner="h", state="done", dur_s=9.0,
                            ended_ago_s=60.0)])
    report = "\n".join(panel_populate_report(snap))
    assert "runner_fleet: 1 runner(s), 1 active, bursts 1 running/1 recent" \
        in report
    # No bursts → the line stays exactly as before (no ", bursts 0/0").
    snap.runner_fleet = RunnerFleet(
        runners=[Runner(name="h", active=True)], total=1, active_count=1)
    report = "\n".join(panel_populate_report(snap))
    assert "runner_fleet: 1 runner(s), 1 active\n" in report + "\n"
    # The bursts clause rides the EMPTY-fleet state too — the linger
    # outlives the runner (review H-empty-fleet).
    snap.runner_fleet = RunnerFleet(
        runners=[], total=0, active_count=0,
        bursts=[RunnerBurst(runner="h", state="done", dur_s=9.0,
                            ended_ago_s=60.0)])
    report = "\n".join(panel_populate_report(snap))
    assert ("runner_fleet: empty (docker answered, no runner containers), "
            "bursts 0 running/1 recent") in report


def test_populate_report_carries_hosted_bursts_states():
    from vision_resource_monitor.snapshot import HostedBursts, Snapshot
    from vision_resource_monitor.state import panel_populate_report
    snap = Snapshot(t=1.0)
    assert "hosted_bursts: off (opt-in watch not enabled)" in \
        "\n".join(panel_populate_report(snap))
    snap.hosted_bursts = HostedBursts(ok=None)
    assert "hosted_bursts: warming (first sweep pending)" in \
        "\n".join(panel_populate_report(snap))
    snap.hosted_bursts = HostedBursts(ok=False, error="HTTP 401")
    assert "hosted_bursts: error (stated on the panel)" in \
        "\n".join(panel_populate_report(snap))
    snap.hosted_bursts = HostedBursts(ok=True, unchecked=2)
    assert "hosted_bursts: 0 run(s), 2 unchecked" in \
        "\n".join(panel_populate_report(snap))


# ── exec-pressure spawn census describe + headline (producer item 1) ──────────────


def test_populate_report_carries_spawns_three_way_line():
    from vision_resource_monitor.snapshot import Snapshot, Spawns, SpawnSide
    from vision_resource_monitor.state import panel_populate_report
    snap = Snapshot(t=1.0)
    assert "spawns: absent (producer emitted no spawn census)" in \
        "\n".join(panel_populate_report(snap))
    snap.spawns = Spawns(
        wsl=SpawnSide(alloc_per_min=456.9, visible_per_min=43.0,
                      self_per_min=169.6, status="ok", window_s=60.0),
        win=SpawnSide(visible_per_min=6.0, status="ok", window_s=10.1))
    report = "\n".join(panel_populate_report(snap))
    assert "spawns: wsl ~457/min tasks" in report
    assert "win ~6/min seen" in report
    # Degraded sides carry the SAME stated-cause wording as the render (the
    # keep-in-sync convention), never bare tokens.
    snap.spawns = Spawns(wsl=SpawnSide(status="no-ps"),
                         win=SpawnSide(status="read-failed"))
    assert "spawns: wsl no ps sweep · win census failed" in \
        "\n".join(panel_populate_report(snap))


def test_spawn_headline_ok_carries_rates_and_top():
    from vision_resource_monitor.snapshot import Spawns, SpawnSide, SpawnTop
    from vision_resource_monitor.state import spawn_headline
    sp = Spawns(
        wsl=SpawnSide(alloc_per_min=456.9, visible_per_min=43.0,
                      self_per_min=169.6, status="ok", window_s=60.0,
                      top=[SpawnTop(label="systemd-udevd", count=41),
                           SpawnTop(label="bash", count=1, pid=777),
                           SpawnTop(label="sshd:", count=1),
                           SpawnTop(label="x", count=1)]),
        win=SpawnSide(visible_per_min=6.0, status="ok", window_s=10.1,
                      top=[SpawnTop(label="conhost", count=1)]))
    text, tip = spawn_headline(sp)
    assert "wsl ~457/min tasks" in text
    assert "(seen 43 · self 170)" in text
    assert "win ~6/min seen" in text
    # The inline top is the wsl census, capped at 3 — the rest lives in the
    # tooltip along with the honesty caveats.
    inline_top = text.split("top:")[1]
    assert "systemd-udevd ×41" in inline_top
    assert ", x ×1" not in inline_top     # row 4 is tooltip-only
    assert "threads" in tip           # the tasks-not-processes caveat
    assert "survivors" in tip         # the win undercount caveat
    assert "conhost" in tip           # the win top lives in the tooltip


def test_spawn_headline_states_causes_per_side():
    from vision_resource_monitor.snapshot import Spawns, SpawnSide, SpawnTop
    from vision_resource_monitor.state import spawn_headline
    text, _ = spawn_headline(Spawns(wsl=SpawnSide(status="no-ps"),
                                    win=SpawnSide(status="no-sweep")))
    assert "wsl no ps sweep" in text and "win no host sweep" in text
    text, _ = spawn_headline(Spawns(wsl=SpawnSide(status="warming"),
                                    win=SpawnSide(status="warming")))
    assert "warming" in text
    text, _ = spawn_headline(Spawns(wsl=SpawnSide(status="read-failed"),
                                    win=SpawnSide(status="read-failed")))
    assert "wsl watermark read failed" in text and "win census failed" in text
    text, _ = spawn_headline(Spawns(wsl=SpawnSide(status="capped"),
                                    win=SpawnSide(status="ok",
                                                  visible_per_min=4.0)))
    assert "sanity cap" in text and "win ~4/min seen" in text
    # An unknown status never renders blank — the raw token is the fallback.
    text, _ = spawn_headline(Spawns(wsl=SpawnSide(status="odd"),
                                    win=SpawnSide(status="")))
    assert "odd" in text and "unavailable" in text
    # A degraded status never HIDES co-present metrics: the producer keeps
    # nulls per-metric by design (--no-procs still reports the watermark),
    # so the rate renders first and the cause rides after (review H2).
    text, _ = spawn_headline(Spawns(
        wsl=SpawnSide(status="no-ps", alloc_per_min=300.0,
                      self_per_min=12.0),
        win=SpawnSide(status="ok", visible_per_min=4.0)))
    assert "wsl ~300/min tasks (self 12) · no ps sweep" in text
    # ok with only the ps view: the seen rate renders alone; ok with nothing
    # states unavailable (review L4).
    text, _ = spawn_headline(Spawns(
        wsl=SpawnSide(status="ok", visible_per_min=43.0),
        win=SpawnSide(status="ok")))
    assert "wsl ~43/min seen" in text and "win unavailable" in text
    # Non-finite rates (json Infinity survives the tolerant parse) read as
    # unavailable — never rendered raw (review L6).
    text, _ = spawn_headline(Spawns(
        wsl=SpawnSide(status="ok", alloc_per_min=float("inf")),
        win=SpawnSide(status="ok", visible_per_min=float("nan"))))
    assert "inf" not in text and "nan" not in text
    # A tolerant-parse junk top row (label "?", count 0) is not a spawner —
    # dropped from both the inline top and the tooltip (review L5).
    sp = Spawns(wsl=SpawnSide(status="ok", alloc_per_min=100.0,
                              top=[SpawnTop(label="?", count=0),
                                   SpawnTop(label="real", count=2)]),
                win=SpawnSide(status="ok"))
    text, tip = spawn_headline(sp)
    assert "? ×0" not in text and "? ×0" not in tip and "real ×2" in text
    # Item 2: an attributed bucket renders the name[pid] Copy-PID form (#46);
    # an unattributed/ambiguous one stays a bare label.
    sp = Spawns(wsl=SpawnSide(status="ok", alloc_per_min=100.0,
                              top=[SpawnTop(label="vrc", count=8, pid=4242),
                                   SpawnTop(label="(unattributed)", count=1)]),
                win=SpawnSide(status="ok"))
    text, tip = spawn_headline(sp)
    assert "vrc[4242] ×8" in text and "vrc[4242] ×8" in tip
    assert "(unattributed) ×1" in text


# ── self-cost: SelfCpuWindow + the Host panel "monitor:" note ───────────────────


def test_selfcpu_window_warming_and_exact():
    from vision_resource_monitor.state import SelfCpuWindow
    seq = [0.0, 0.5, 0.7]
    w = SelfCpuWindow(window_s=60.0, min_span_s=10.0,
                      usage_fn=lambda: seq.pop(0))
    assert w.update(1000.0) is None            # baseline
    assert w.update(1005.0) is None            # span 5 < min 10
    assert w.update(1010.0) == 7.0             # 0.7 s over 10 s = 7% of a core


def test_selfcpu_window_fail_open_and_clock_guards():
    from vision_resource_monitor.state import SelfCpuWindow
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 2:
            raise OSError("no clock")
        return 0.1 * calls["n"]

    w = SelfCpuWindow(window_s=60.0, min_span_s=10.0, usage_fn=flaky)
    w.update(1000.0)
    out = w.update(1010.0)
    assert out == 1.0                          # 0.1 s / 10 s
    assert w.update(1020.0) == out             # usage raises → holds
    assert w.update(1010.0) == out             # clock backwards → holds


def test_selfcpu_window_sleep_gap_rebaselines_to_warming():
    # A gap longer than the whole window (system sleep/resume, a wedged UI)
    # must NOT land as one ~0-CPU dilution entry that reads "tray 0.0" for
    # the next minute — the window re-baselines to honest warming and then
    # recovers with fresh entries only.
    from vision_resource_monitor.state import SelfCpuWindow
    seq = [0.0, 0.5, 0.6, 1.6]
    w = SelfCpuWindow(window_s=60.0, min_span_s=10.0,
                      usage_fn=lambda: seq.pop(0))
    w.update(1000.0)
    assert w.update(1010.0) == 5.0             # 0.5 s / 10 s
    assert w.update(5000.0) is None            # gap > window → warming
    assert w.update(5010.0) == 10.0            # fresh window only: 1.0/10 s


def test_selfcost_note_variants():
    from vision_resource_monitor.snapshot import SelfCost
    from vision_resource_monitor.state import selfcost_note
    # Block-less feed (older producer / warming / --once): dim em-dash.
    assert selfcost_note(None) == "monitor: —"
    assert selfcost_note(None, 2.5) == "monitor: —"
    # Full reading, tray known: headline sums producer + tray.
    sc = SelfCost(window_s=54.0, total_pct=15.2, python_pct=0.2,
                  spawns_pct=1.5, windows_pct=13.5)
    assert selfcost_note(sc, 0.4) == (
        "monitor: 15.6% of a core — win 13.5 · spawns 1.5 · py 0.2 · tray 0.4")
    # Tray warming: stated, never guessed; headline is producer-only.
    assert selfcost_note(sc, None) == (
        "monitor: 15.2% of a core — win 13.5 · spawns 1.5 · py 0.2 · tray —")
    # Windows unmeasured (darwin / --no-mem / dead interop): the scope is
    # STATED — no "win N" segment, and the honest suffix rides the line.
    sc2 = SelfCost(window_s=60.0, total_pct=1.7, python_pct=0.3,
                   spawns_pct=1.4, windows_pct=None)
    note = selfcost_note(sc2, 0.3)
    assert note == ("monitor: 2.0% of a core — spawns 1.4 · py 0.3 · "
                    "tray 0.3 · win unmeasured")
    assert "win 0" not in note                 # never a fabricated zero


def test_panel_populate_report_self_cost_line():
    import json
    from vision_resource_monitor.snapshot import parse
    from vision_resource_monitor.state import panel_populate_report
    absent = parse(json.dumps({"schema": "monitor-snapshot:v1", "t": 1.0}))
    assert any(l.startswith("self_cost: absent")
               for l in panel_populate_report(absent))
    measured = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "self_cost": {"window_s": 54.0, "total_pct": 15.2, "python_pct": 0.2,
                       "spawns_pct": 1.5, "windows_pct": 13.5}}))
    assert ("self_cost: total 15.2% of a core over 54s, windows measured"
            in panel_populate_report(measured))
    unmeasured = parse(json.dumps(
        {"schema": "monitor-snapshot:v1", "t": 1.0,
         "self_cost": {"window_s": 60.0, "total_pct": 1.7, "python_pct": 0.3,
                       "spawns_pct": 1.4, "windows_pct": None}}))
    assert ("self_cost: total 1.7% of a core over 60s, windows unmeasured"
            in panel_populate_report(unmeasured))


def test_panel_populate_report_windows_cpu():
    from vision_resource_monitor.snapshot import Snapshot, WindowsCpu
    snap = Snapshot(t=1.0)
    assert "windows_cpu: absent" in "\n".join(panel_populate_report(snap))
    snap.windows_cpu = WindowsCpu(utility_pct=48.7, time_pct=30.9)
    assert "windows_cpu: present\n" in "\n".join(
        panel_populate_report(snap)) + "\n"
    # A reading whose busy figure is unusable renders the dashed row — the
    # report mirrors the render branch, never a plain "present" (review
    # LOW: log-only diagnosis of a dashed row must not answer "present").
    snap.windows_cpu = WindowsCpu(utility_pct=48.7, time_pct=None)
    assert ("windows_cpu: present, no usable busy figure"
            in "\n".join(panel_populate_report(snap)))


def test_panel_summaries_windows_cpu_bit():
    # Since the 2026-07-29 host/WSL regroup the win-cpu bit rides the
    # Windows-host section's header (panel id "memory"), never the WSL
    # section's ("host").
    from vision_resource_monitor.snapshot import CpuStat, Snapshot, WindowsCpu
    from vision_resource_monitor.state import panel_summaries
    snap = Snapshot(t=1.0)
    snap.host_cpu = CpuStat(pct=34.0)
    snap.windows_cpu = WindowsCpu(utility_pct=48.7, time_pct=30.9)
    s = panel_summaries(snap)
    assert s["memory"] == "win 31%"        # busy %, not the turbo-scaled utility
    assert s["host"] == "cpu 34%"          # wsl bits only — no win bit
    snap.windows_cpu = WindowsCpu(utility_pct=132.4, time_pct=88.0)
    assert panel_summaries(snap)["memory"] == "win 88%"
    # A reading without a busy figure adds no bit (honest headers —
    # never a fabricated figure on a collapsed header).
    snap.windows_cpu = WindowsCpu(utility_pct=48.7, time_pct=None)
    assert "memory" not in panel_summaries(snap)
