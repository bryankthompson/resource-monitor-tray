"""The ``monitor-snapshot:v1`` contract.

The producer (the WSL-side resource monitor) collects *and classifies*
everything the UI needs each tick and serializes it as one JSON object. This
module is the consumer-side view of that object: tolerant dataclasses plus a
``parse``/``load`` front door. Nothing here imports Qt — it is pure data so it
stays unit-testable headless.

Parsing is deliberately forgiving, matching the producer's own stance: a record
written before a field existed simply lacks it, and readers treat missing fields
as empty rather than erroring. A snapshot that cannot be understood at all raises
``SnapshotError`` so the reader can fall back to the last good frame.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

SNAPSHOT_SCHEMA = "monitor-snapshot:v1"


class SnapshotError(ValueError):
    """Raised when bytes/JSON cannot be understood as a monitor snapshot."""


# ── coercion helpers (never raise; fall back to the given default) ──────────────

def _f(value, default=0.0):
    # OverflowError: float(<400-digit int literal>) — json.loads happily
    # yields huge ints, and an uncaught raise here escapes parse() past the
    # reader's last-good-frame guarantee. Same class the _i/_io helpers were
    # patched for on the egress surface (2026-07-23); the float pair was
    # missed then (review find on the #74 diff, 2026-07-24).
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _fo(value):
    """Float or None — for genuinely-optional numeric fields."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _fo_finite(value):
    """Finite optional float for values that will be displayed as durations."""
    if isinstance(value, bool):
        return None
    result = _fo(value)
    return result if result is not None and math.isfinite(result) else None


def _i(value, default=0):
    # OverflowError: int(float("inf")) — json.loads accepts Infinity, and an
    # uncaught raise here escapes parse() past the reader's last-good-frame
    # guarantee (review find on the egress surface, 2026-07-23; NaN was
    # already safe via ValueError).
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _io(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _s(value, default=""):
    return value if isinstance(value, str) else default


def _so(value):
    """Str or None — for genuinely-optional string fields (e.g. pressure)."""
    return value if isinstance(value, str) else None


def _list(value):
    return value if isinstance(value, list) else []


def _dict(value):
    return value if isinstance(value, dict) else {}


# ── leaf records ────────────────────────────────────────────────────────────────

@dataclass
class Gpu:
    name: str = "?"
    util: float = 0.0
    mem_used_mib: float = 0.0
    mem_total_mib: float = 0.0
    temp_c: float = 0.0
    power_w: float = 0.0
    power_limit_w: float = 0.0

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            name=_s(d.get("name"), "?"),
            util=_f(d.get("util")),
            mem_used_mib=_f(d.get("mem_used_mib")),
            mem_total_mib=_f(d.get("mem_total_mib")),
            temp_c=_f(d.get("temp_c")),
            power_w=_f(d.get("power_w")),
            power_limit_w=_f(d.get("power_limit_w")),
        )


@dataclass
class GpuUser:
    """One Windows-side dedicated-VRAM holder (producer top-level ``gpu_users``):
    from the GPU perf counters piggybacked on the host PowerShell sweep — the
    only per-process VRAM source reachable from WSL (nvidia-smi compute-apps is
    blind under GPU-PV). Container names ride ``activity.workloads`` instead;
    bare-WSL host processes cannot appear in either (root-only visibility)."""
    pid: int = 0
    name: str = "?"
    vram_mib: float = 0.0

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            pid=_i(d.get("pid")),
            name=_s(d.get("name"), "?"),
            vram_mib=_f(d.get("vram_mib")),
        )


@dataclass
class Activity:
    """The currently-open GPU run, or absent when idle."""
    start_t: float = 0.0
    peak_util: float = 0.0
    peak_vram_mib: float = 0.0
    vram_total_mib: float = 0.0
    workloads: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            start_t=_f(d.get("start_t")),
            peak_util=_f(d.get("peak_util")),
            peak_vram_mib=_f(d.get("peak_vram_mib")),
            vram_total_mib=_f(d.get("vram_total_mib")),
            workloads=[_s(w) for w in _list(d.get("workloads"))],
        )


@dataclass
class RunRecord:
    """A completed run in the history table."""
    start_t: float = 0.0
    end_t: float = 0.0
    duration_s: float = 0.0
    peak_util: float = 0.0
    peak_vram_mib: float = 0.0
    vram_total_mib: float = 0.0
    workloads: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            start_t=_f(d.get("start_t")),
            end_t=_f(d.get("end_t")),
            duration_s=_f(d.get("duration_s")),
            peak_util=_f(d.get("peak_util")),
            peak_vram_mib=_f(d.get("peak_vram_mib")),
            vram_total_mib=_f(d.get("vram_total_mib")),
            workloads=[_s(w) for w in _list(d.get("workloads"))],
        )


@dataclass
class DockerContainer:
    name: str = "?"
    image: str = "?"
    status: str = ""
    id: str = ""

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            name=_s(d.get("name"), "?"),
            image=_s(d.get("image"), "?"),
            status=_s(d.get("status")),
            id=_s(d.get("id")),
        )


@dataclass
class DockerStatus:
    running_count: int = 0
    unknown_count: int = 0
    gpu_containers: list[DockerContainer] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            running_count=_i(d.get("running_count")),
            unknown_count=_i(d.get("unknown_count")),
            gpu_containers=[DockerContainer.from_dict(c)
                            for c in _list(d.get("gpu_containers"))],
        )


@dataclass
class OllamaModel:
    name: str = "?"
    size_gb: float = 0.0
    vram_gb: float = 0.0

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            name=_s(d.get("name"), "?"),
            size_gb=_f(d.get("size_gb")),
            vram_gb=_f(d.get("vram_gb")),
        )


@dataclass
class ProgressRun:
    """One RUNS-panel stream, already classified by the producer.

    ``state`` is one of running / stalled / died / ok / error — judged on the
    producer side (where the pid check and recorded host are meaningful), so the
    consumer never re-derives liveness.
    """
    label: str = "?"
    state: str = "running"
    percent: int | None = None
    frames: int | None = None
    message: str = ""
    last_t: float = 0.0
    started_t: float | None = None
    pid: int | None = None
    end_message: str = ""

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            label=_s(d.get("label"), "?"),
            state=_s(d.get("state"), "running"),
            percent=_io(d.get("percent")),
            frames=_io(d.get("frames")),
            message=_s(d.get("message")),
            last_t=_f(d.get("last_t")),
            started_t=_fo(d.get("started_t")),
            pid=_io(d.get("pid")),
            end_message=_s(d.get("end_message")),
        )


@dataclass
class CpuStat:
    pct: float = 0.0
    cores_active: int = 0
    cores_total: int = 0
    speed_mhz: float | None = None
    # Where the busy cycles went (producer; same /proc/stat window as pct).
    # None = split unknown (an older producer, or a reading without the
    # fields) — never guessed. NOTE the producer's pct includes iowait, so
    # user+sys can honestly read well under pct on an I/O-bound box.
    user_pct: float | None = None
    sys_pct: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            pct=_f(d.get("pct")),
            cores_active=_i(d.get("cores_active")),
            cores_total=_i(d.get("cores_total")),
            speed_mhz=_fo(d.get("speed_mhz")),
            user_pct=_fo(d.get("user_pct")),
            sys_pct=_fo(d.get("sys_pct")),
        )


@dataclass
class WindowsCpu:
    """The whole physical box's CPU strain (the producer's ``windows_cpu`` —
    Windows perf counters riding the host sweep; the WSL-side ``host_cpu``
    cannot see Windows burn). ``utility_pct`` is the frequency-aware % of
    nominal capacity — the Task Manager headline, and it can honestly exceed
    100 under turbo, so it is rendered raw, never clamped. ``time_pct`` is
    the busy fraction of core time. ``cores`` is the box's logical processor
    count (None = a producer/sweep predating it) — ``time_pct/100 × cores``
    is the cores-equivalent footprint the panel states. Optional fields: a
    field the feed doesn't carry usably parses to None, never a fabricated
    0-strain (bool-guarded — JSON `true` must not fabricate 1 core)."""
    utility_pct: float | None = None
    time_pct: float | None = None
    cores: int | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        cores = d.get("cores")
        return cls(
            utility_pct=_fo(d.get("utility_pct")),
            time_pct=_fo(d.get("time_pct")),
            cores=None if isinstance(cores, bool) else _io(cores),
        )


@dataclass
class SelfCost:
    """The producer billing its own CPU. Percentages of ONE core over
    ``window_s``. ``windows_pct`` None = no host sweep
    billed inside the window (darwin / --no-mem / a dead interop) — the
    producer's total then covers the WSL side only and the rendered note
    must state that scope, never imply a measured zero."""
    window_s: float = 0.0
    total_pct: float = 0.0
    python_pct: float = 0.0
    spawns_pct: float = 0.0
    windows_pct: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            window_s=_f(d.get("window_s")),
            total_pct=_f(d.get("total_pct")),
            python_pct=_f(d.get("python_pct")),
            spawns_pct=_f(d.get("spawns_pct")),
            windows_pct=_fo(d.get("windows_pct")),
        )


@dataclass
class RamStat:
    used_mib: float = 0.0
    total_mib: float = 0.0
    cached_mib: float = 0.0

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            used_mib=_f(d.get("used_mib")),
            total_mib=_f(d.get("total_mib")),
            cached_mib=_f(d.get("cached_mib")),
        )


@dataclass
class ProcRow:
    name: str = "?"
    mem_mib: float = 0.0
    llm: bool = False
    is_vmmem: bool = False

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            name=_s(d.get("name"), "?"),
            mem_mib=_f(d.get("mem_mib")),
            llm=bool(d.get("llm")),
            is_vmmem=bool(d.get("is_vmmem")),
        )


@dataclass
class ContainerMem:
    name: str = "?"
    mem_mib: float = 0.0
    resident_mib: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            name=_s(d.get("name"), "?"),
            mem_mib=_f(d.get("mem_mib")),
            resident_mib=_fo(d.get("resident_mib")),
        )


@dataclass
class DiskRow:
    type: str = "?"
    size_mib: float = 0.0
    reclaimable_mib: float = 0.0

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            type=_s(d.get("type"), "?"),
            size_mib=_f(d.get("size_mib")),
            reclaimable_mib=_f(d.get("reclaimable_mib")),
        )


@dataclass
class HostMemory:
    used_mib: float = 0.0
    total_mib: float = 0.0
    processes: list[ProcRow] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            used_mib=_f(d.get("used_mib")),
            total_mib=_f(d.get("total_mib")),
            processes=[ProcRow.from_dict(p) for p in _list(d.get("processes"))],
        )


@dataclass
class MemoryPanel:
    windows: HostMemory | None = None
    wsl: list[ProcRow] = field(default_factory=list)
    containers: list[ContainerMem] = field(default_factory=list)
    disk: list[DiskRow] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        win = d.get("windows")
        return cls(
            windows=HostMemory.from_dict(win) if isinstance(win, dict) else None,
            wsl=[ProcRow.from_dict(p) for p in _list(d.get("wsl"))],
            containers=[ContainerMem.from_dict(c) for c in _list(d.get("containers"))],
            disk=[DiskRow.from_dict(r) for r in _list(d.get("disk"))],
        )


# ── process attribution block (the producer's top-level `processes`) ────────────
# The producer emits a per-session Claude/node breakdown, stale high-cost
# contributors, and a human-readable alerts tier. GPU-only feeds may omit this
# optional block, leaving the Sessions panel empty.

@dataclass
class Session:
    """One Claude-Code session and its node/MCP-server descendants."""
    pid: int = 0
    label: str = "?"
    node_count: int = 0
    rss_mib: float = 0.0
    age_s: float | None = None
    stale: bool = False
    last_event_age_s: float | None = None
    swap_mib: float | None = None
    idle: bool | None = None
    # The producer's child-pid index: ALL the session's descendant
    # pids, fattest first, CAPPED producer-side (32) — child_total is the TRUE
    # descendant count, so total > len(child_pids) means the list is truncated.
    # [] + None on a feed that predates the fields: the filter then matches
    # the root pid only (the pre-index behavior), never guessing children.
    child_pids: list[int] = field(default_factory=list)
    child_total: int | None = None
    # The session-ports census:
    # the subtree's LISTENING ports, nearest-session joined and CAPPED
    # producer-side (8) — port_total is the TRUE count. None = the census
    # didn't run (darwin producer / gated off / failed ss sample / a feed
    # predating the fields) vs [] = it ran and the subtree owns nothing;
    # the renderer must never read None as "no ports".
    ports: list[int] | None = None
    port_total: int | None = None
    cockpit: str | None = None
    lane: str | None = None
    role: str | None = None
    state: str | None = None
    name: str | None = None
    domain: str | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        ct = d.get("child_total")
        pt = d.get("port_total")
        rp = d.get("ports")
        def optional_text(key):
            value = d.get(key)
            return value if isinstance(value, str) else None
        role = optional_text("role")
        if role not in ("cockpit", "lane", "advisor", "drc", "router"):
            role = None
        state = optional_text("state")
        if state not in ("idle", "busy", "waiting", "shell"):
            state = None
        return cls(
            pid=_i(d.get("pid")),
            label=_s(d.get("label"), "?"),
            node_count=_i(d.get("node_count")),
            rss_mib=_f(d.get("rss_mib")),
            age_s=_fo(d.get("age_s")),
            stale=bool(d.get("stale")),
            last_event_age_s=_fo_finite(d.get("last_event_age_s")),
            swap_mib=_fo_finite(d.get("swap_mib")),
            idle=(d.get("idle") if isinstance(d.get("idle"), bool) else None),
            # Junk entries drop: bools are skipped EXPLICITLY (bool is an int
            # subclass, so a JSON `true` would coerce to 1 and fabricate
            # pid 1 — review find); the rest coerce through _i and keep only
            # a plausible pid range (a fabricated 0 would let a "0" needle
            # match every session; a 400-digit int would land verbatim in
            # the via-child row note).
            child_pids=[p for p in (_i(c) for c in _list(d.get("child_pids"))
                                    if not isinstance(c, bool))
                        if 0 < p < 2**31],
            # Same bool guard: `true` -> 1 with an empty list would fire the
            # truncation caveat off pure junk.
            child_total=None if isinstance(ct, bool) else _io(ct),
            # None-vs-[] is contract (census unknown vs truly none), so only
            # a real list parses; entries keep the same bool guard as
            # child_pids plus the valid port range.
            ports=(None if not isinstance(rp, list)
                   else [p for p in (_i(c) for c in rp
                                     if not isinstance(c, bool))
                         if 0 < p < 65536]),
            port_total=None if isinstance(pt, bool) else _io(pt),
            cockpit=optional_text("cockpit"), lane=optional_text("lane"),
            role=role, state=state, name=optional_text("name"),
            domain=optional_text("domain"),
        )


@dataclass
class HighCost:
    """A stale high-cost process — heavy + CPU-hungry + long-lived at once."""
    pid: int = 0
    comm: str = "?"
    rss_mib: float = 0.0
    cpu_s: float = 0.0
    age_s: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            pid=_i(d.get("pid")),
            comm=_s(d.get("comm"), "?"),
            rss_mib=_f(d.get("rss_mib")),
            cpu_s=_f(d.get("cpu_s")),
            age_s=_fo(d.get("age_s")),
        )


@dataclass
class SwapHolder:
    """One private-swap holder (Linux VmSwap). A FLOOR, not a full accounting —
    per-process private swap sums to well under total swap used (kernel, tmpfs/
    shmem, and shared pages aren't captured), so these name the fattest holders,
    not everything in swap."""
    name: str = "?"
    pid: int = 0
    swap_mib: float = 0.0

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(name=_s(d.get("name"), "?"), pid=_i(d.get("pid")),
                   swap_mib=_f(d.get("swap_mib")))


@dataclass
class Swap:
    used_mib: float = 0.0
    total_mib: float = 0.0
    pct: float | None = None                # used/total %, when the producer sends it
    top: list[SwapHolder] = field(default_factory=list)  # largest holders (a floor)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(used_mib=_f(d.get("used_mib")), total_mib=_f(d.get("total_mib")),
                   pct=_fo(d.get("pct")),
                   top=[SwapHolder.from_dict(t) for t in _list(d.get("top"))])


@dataclass
class Processes:
    """The host load block: node/Claude session counts + per-session detail, load
    average, swap, memory pressure, the stale high-cost contributors, and the
    human-readable alerts tier. Answers "which session is eating the box" — the
    GPU panels can't."""
    node_total: int = 0
    # The monitor-snapshot:v1 contract names this field; keep it unchanged.
    claude_total: int = 0
    sessions: list[Session] = field(default_factory=list)
    high_cost: list[HighCost] = field(default_factory=list)
    load: list[float] | None = None
    ncpu: int | None = None
    swap: Swap | None = None
    pressure: str | None = None
    alerts: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        load = d.get("load")
        sw = d.get("swap")
        return cls(
            node_total=_i(d.get("node_total")),
            claude_total=_i(d.get("claude_total")),
            sessions=[Session.from_dict(s) for s in _list(d.get("sessions"))],
            high_cost=[HighCost.from_dict(h) for h in _list(d.get("high_cost"))],
            load=[_f(v) for v in load] if isinstance(load, list) else None,
            ncpu=_io(d.get("ncpu")),
            swap=Swap.from_dict(sw) if isinstance(sw, dict) else None,
            pressure=_so(d.get("pressure")),
            alerts=[_s(a) for a in _list(d.get("alerts"))],
        )


# ── pre-thrash early-warning block (producer top-level `forecast`/`contributors`) ──
# The producer's memory-pressure early-warning: a leading-indicator `forecast`
# that fires while RAM headroom
# remains, plus a `contributors` list ranked worst-first — each carrying the
# recommended SAFE, human-executed action (warn-and-suggest ONLY; nothing here or
# in the producer ever acts). Both are optional: a GPU-only feed, an older
# producer, or `--no-forecast` simply lacks them (forecast → None, contributors →
# []). Rendered by the window's Forecast panel.

@dataclass
class Forecast:
    """The memory-pressure forecast. ``level`` escalates ok → watch → warn; at
    ``warn`` the producer also prepends the warning to ``processes.alerts``. The
    numeric fields are genuinely optional (None while the forecaster is warming or
    on a healthy tick), so a quiet frame still parses as level ``ok`` with them
    absent."""
    level: str = "ok"                          # ok | watch | warn
    eta_min: float | None = None               # projected minutes to zero headroom
    signal: str | None = None                  # the leading signal that fired
    reason: str = ""                           # human-readable one-liner
    headroom_mib: float | None = None          # reclaimable headroom (free+cached)
    compressed_slope_mib_s: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            level=_s(d.get("level"), "ok"),
            eta_min=_fo(d.get("eta_min")),
            signal=_so(d.get("signal")),
            reason=_s(d.get("reason")),
            headroom_mib=_fo(d.get("headroom_mib")),
            compressed_slope_mib_s=_fo(d.get("compressed_slope_mib_s")),
        )


@dataclass
class Contributor:
    """One ranked thrash contributor. ``summary`` is the detector's own alert text
    (single source of truth) and ``action`` the recommended SAFE human step (may be
    empty for an unknown kind). ``metric`` is a small free-form dict of that kind's
    key numbers, kept as-is so the UI can read it without a per-kind schema."""
    kind: str = "?"       # memory_overcommit | docker_idle_burn | stale_high_cost | session_fleet_growth
    rank: int = 0         # 1-based, worst-first
    label: str = "?"
    summary: str = ""
    action: str = ""
    metric: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            kind=_s(d.get("kind"), "?"),
            rank=_i(d.get("rank")),
            label=_s(d.get("label"), "?"),
            summary=_s(d.get("summary")),
            action=_s(d.get("action")),
            metric=_dict(d.get("metric")),
        )


# ── stuck-process / run-queue probe (producer top-level `stuck`) ─────────────────
# The freeze-forensics measurement: run-queue depth ("is the CPU being hammered"),
# a persistence-gated census of processes wedged in uninterruptible sleep, the
# load-vs-CPU divergence ratio, and the producer's own sampler self-timing —
# plus four calibrated flags the producer judged (persistence-gated; the app
# never re-derives thresholds; `cpu_saturated` + the busy% arrived with the
# calibrated saturation split). Optional: an older producer or `--no-stuck`
# simply lacks the field. Escalation (alerts line, ranked contributor) rides the
# existing `processes.alerts` / `contributors` tiers; this block is the
# structured panel surface.

@dataclass
class StuckWedged:
    """One wedged process: continuously in uninterruptible sleep (Linux ``D`` /
    macOS ``U``) past the producer's persistence gate — genuinely stuck, not a
    sub-tick I/O blip. ``for_s`` is how long it has been wedged."""
    pid: int = 0
    comm: str = "?"
    for_s: float = 0.0

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            pid=_i(d.get("pid")),
            comm=_s(d.get("comm"), "?"),
            for_s=_f(d.get("for_s")),
        )


@dataclass
class Stuck:
    """The stuck-process / run-queue probe. The counts are genuinely optional
    (``None`` when the producer couldn't read them — e.g. no ``/proc/stat``);
    ``wedged`` is bounded by the producer (top-N, longest-wedged first). The
    four booleans are the producer's calibrated, persistence-gated judgments —
    the numbers alone never warn. ``cpu_pct``/``cpu_saturated`` carry busy%
    and sustained near-zero-idle judgment; an older producer simply lacks
    them."""
    procs_running: int | None = None
    procs_blocked: int | None = None
    d_count: int | None = None
    ncpu: int | None = None
    wedged: list[StuckWedged] = field(default_factory=list)
    load1_per_core: float | None = None
    cpu_pct: float | None = None
    sampler_ms: dict = field(default_factory=dict)
    queue_saturated: bool = False
    wait_diverged: bool = False
    sampler_slow: bool = False
    cpu_saturated: bool = False

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            procs_running=_io(d.get("procs_running")),
            procs_blocked=_io(d.get("procs_blocked")),
            d_count=_io(d.get("d_count")),
            ncpu=_io(d.get("ncpu")),
            wedged=[StuckWedged.from_dict(w) for w in _list(d.get("wedged"))],
            load1_per_core=_fo(d.get("load1_per_core")),
            cpu_pct=_fo(d.get("cpu_pct")),
            sampler_ms={str(k): _f(v)
                        for k, v in _dict(d.get("sampler_ms")).items()},
            queue_saturated=bool(d.get("queue_saturated")),
            wait_diverged=bool(d.get("wait_diverged")),
            sampler_slow=bool(d.get("sampler_slow")),
            cpu_saturated=bool(d.get("cpu_saturated")),
        )


# ── Windows scanner census (producer top-level `scanners`) ────────────────────────
# One row per scanner group present on the host (the Defender family, the Search
# indexer): the producer's burst-edge reading — group %CPU-of-a-core, the
# calibrated state (active / burst-linger / idle) with its timing, and `burning`
# (active ≥ 5 min AND still hot), which also escalates via the alerts /
# contributors tiers. Drives the Scanners panel.

@dataclass
class Scanner:
    """One scanner group's census row. ``state`` and ``burning`` are the
    producer's calibrated, hysteresis-gated judgments — the app never
    re-derives thresholds from the raw %."""
    group: str = "?"
    label: str = "?"
    procs: list[str] = field(default_factory=list)
    proc_count: int = 0
    rss_mib: float = 0.0
    cpu_pct: float = 0.0
    state: str = "idle"
    active_s: float | None = None
    ago_s: float | None = None
    burning: bool = False

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            group=_s(d.get("group"), "?"),
            label=_s(d.get("label"), "?"),
            procs=[_s(p) for p in _list(d.get("procs"))],
            proc_count=_i(d.get("proc_count")),
            rss_mib=_f(d.get("rss_mib")),
            cpu_pct=_f(d.get("cpu_pct")),
            state=_s(d.get("state"), "idle"),
            active_s=_fo(d.get("active_s")),
            ago_s=_fo(d.get("ago_s")),
            burning=bool(d.get("burning")),
        )


@dataclass
class SecurityStackTop:
    """One nonzero group inside the whole-stack reading (hottest first)."""
    group: str = "?"
    label: str = "?"
    cpu_pct: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(group=_s(d.get("group"), "?"), label=_s(d.get("label"), "?"),
                   cpu_pct=_fo(d.get("cpu_pct")))


@dataclass
class SecurityStack:
    """The whole-stack security tax: every
    census group above summed to ONE cores figure, with the producer's
    hysteresis-gated ``hot``/``taxing`` judgments — the app never re-derives
    thresholds. ``spawns_per_min`` is the workload correlate the producer
    captured with the sweep (None = no reading). The field is None while the
    scanner census is unavailable or on a producer predating it."""
    cores: float | None = None
    hot: bool = False
    hot_for_s: float | None = None
    taxing: bool = False
    spawns_per_min: float | None = None
    top: list[SecurityStackTop] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            cores=_fo(d.get("cores")),
            hot=bool(d.get("hot")),
            hot_for_s=_fo(d.get("hot_for_s")),
            taxing=bool(d.get("taxing")),
            spawns_per_min=_fo(d.get("spawns_per_min")),
            top=[SecurityStackTop.from_dict(g) for g in _list(d.get("top"))],
        )


# ── watched ports (producer top-level `ports`; the --watch-port / config feed) ────
# One record per watched port. The set is configured live
# from the app's Settings "Watched ports" field via the monitor-config:v1 channel;
# `ports` is [] when nothing is watched. Drives the Ports panel.

@dataclass
class PortConn:
    """One per-connection detail row: the far endpoint, the connecting
    process when the producer could see its socket (same-user; a remote or
    root-owned far end stays None), and the connection's CUMULATIVE bytes from
    the service's perspective (totals since the socket opened — not rates).
    The producer bounds the list (top 8 by bytes); ``Port.conns`` keeps the true
    count, so ``conns - len(connections)`` is the "+N more" remainder."""
    peer: str = "?"
    process: str | None = None
    pid: int | None = None
    bytes_in: int | None = None
    bytes_out: int | None = None
    state: str = "established"

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            peer=_s(d.get("peer"), "?"),
            process=_so(d.get("process")),
            pid=_io(d.get("pid")),
            bytes_in=_io(d.get("bytes_in")),
            bytes_out=_io(d.get("bytes_out")),
            state=_s(d.get("state"), "established"),
        )


@dataclass
class Port:
    label: str = "?"
    port: int = 0
    conns: int = 0
    listening: bool = False
    owner: str | None = None
    bytes_in_rate: float | None = None
    bytes_out_rate: float | None = None
    active: bool = False
    active_s: float = 0.0
    sustained: bool = False        # advisory (not calibrated) — a busy-loop tell
    # Bounded per-connection detail ([] on an older producer).
    connections: list[PortConn] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            label=_s(d.get("label"), "?"),
            port=_i(d.get("port")),
            conns=_i(d.get("conns")),
            listening=bool(d.get("listening")),
            owner=_so(d.get("owner")),
            bytes_in_rate=_fo(d.get("bytes_in_rate")),
            bytes_out_rate=_fo(d.get("bytes_out_rate")),
            active=bool(d.get("active")),
            active_s=_f(d.get("active_s")),
            sustained=bool(d.get("sustained")),
            connections=[PortConn.from_dict(c)
                         for c in _list(d.get("connections"))],
        )


# ── process→ports targets (producer top-level `proc_ports`; watch_procs-driven) ──
# One record per process the app targeted through the monitor-config channel
# (`watch_procs`); `proc_ports` is None when no target is configured (also on every
# producer that predates the field). Only ever SHOWS sockets — no alerts tier.

@dataclass
class ProcPortSocket:
    """One socket a targeted process holds. ``peer`` is None for a bound or
    listening socket (the producer strips the wildcard placeholder)."""
    proto: str = "tcp"
    state: str = "?"
    local: str = "?"
    peer: str | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            proto=_s(d.get("proto"), "tcp"),
            state=_s(d.get("state"), "?"),
            local=_s(d.get("local"), "?"),
            peer=_so(d.get("peer")),
        )


@dataclass
class ProcPortMatch:
    """One process a NAME target matched: the same
    {pid, name, sockets, sockets_total} core a pid record carries — a match
    is by definition found, so there is no tri-state here."""
    pid: int = 0
    name: str | None = None
    sockets: list[ProcPortSocket] = field(default_factory=list)
    sockets_total: int | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        socks = d.get("sockets")
        return cls(
            pid=_i(d.get("pid")),
            name=_so(d.get("name")),
            sockets=([ProcPortSocket.from_dict(s) for s in socks]
                     if isinstance(socks, list) else []),
            sockets_total=_io(d.get("sockets_total")),
        )


@dataclass
class ProcPortRecord:
    """One targeted process and every socket the producer could attribute to it.
    ``sockets`` carries the producer's None-vs-[] honesty split: None = no
    answer yet (e.g. the Windows sweep hasn't refreshed since the target was
    added), [] = answered and nothing is visible; ``note`` is the stated human
    cause whenever there is one (not found / not visible / pid-reuse warning).
    ``found`` mirrors that: True/False when answered, None while pending.

    NAME-target records carry ``kind == "name"`` with the pattern
    in ``name`` and pid 0: ``matches`` applies the same None-vs-[] split
    (None = no answer, [] = answered: nothing matched), bounded busiest-first
    with ``match_total`` keeping the true count. Pid records never carry the
    kind key — its absence is the discriminator."""
    side: str = "?"                       # "wsl" | "windows"
    pid: int = 0
    name: str | None = None               # the LIVE name when the producer saw one
    found: bool | None = None
    sockets: list[ProcPortSocket] | None = None
    sockets_total: int | None = None
    note: str | None = None
    kind: str | None = None               # "name" on name-target records
    matches: list[ProcPortMatch] | None = None
    match_total: int | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        socks = d.get("sockets")
        found = d.get("found")
        matches = d.get("matches")
        return cls(
            side=_s(d.get("side"), "?"),
            pid=_i(d.get("pid")),
            name=_so(d.get("name")),
            # Strict tri-state: junk (a string "false", a number) must not
            # coerce into a definitive True — unknown reads as unanswered.
            found=found if isinstance(found, bool) else None,
            sockets=([ProcPortSocket.from_dict(s) for s in socks]
                     if isinstance(socks, list) else None),
            sockets_total=_io(d.get("sockets_total")),
            note=_so(d.get("note")),
            kind="name" if d.get("kind") == "name" else None,
            matches=([ProcPortMatch.from_dict(m) for m in matches]
                     if isinstance(matches, list) else None),
            match_total=_io(d.get("match_total")),
        )


# ── off-box egress ranking (producer top-level `egress`) ──────────────────────────
# WHO is sending data off this box: the whole-box interface view (Windows NIC
# rates + up:down ratio — sustained out≫in is the context-re-send fingerprint)
# and the ranked per-process TCP talkers (wsl tier ONLY — ss cannot see
# Windows-side processes, the #168 limitation; Windows traffic appears in the
# whole-box counters only). Drives the Egress panel. Observes only.

@dataclass
class EgressIface:
    """The whole-box NIC view: byte rates averaged over the reading's window
    plus the upload:download ratio (``out_in_ratio`` is None when the in-rate
    is 0 — the producer never fabricates a division)."""
    in_bps: float | None = None
    out_bps: float | None = None
    out_in_ratio: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            in_bps=_fo(d.get("in_bps")),
            out_bps=_fo(d.get("out_bps")),
            out_in_ratio=_fo(d.get("out_in_ratio")),
        )


@dataclass
class EgressTalker:
    """One ranked off-box talker: byte rates over the window plus its running
    totals since producer start (the Σ that survives an idle spell)."""
    pid: int | None = None
    name: str = "?"
    in_bps: float | None = None
    out_bps: float | None = None
    total_in_mib: float | None = None
    total_out_mib: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            pid=_io(d.get("pid")),
            name=_s(d.get("name"), "?"),
            in_bps=_fo(d.get("in_bps")),
            out_bps=_fo(d.get("out_bps")),
            total_in_mib=_fo(d.get("total_in_mib")),
            total_out_mib=_fo(d.get("total_out_mib")),
        )


# ── exec-pressure spawn census (producer top-level `spawns`) ──────────────────────
# How many processes/tasks the box creates per minute, split by boundary side —
# the event volume the scanner fleet reacts to. Rendered as the Scanners-panel
# headline. Honesty notes travel with the render: the wsl `alloc` rate counts
# TASKS (Linux allocates threads from the same pid space); the win rate counts
# sweep SURVIVORS only (no watermark exists on Windows) and carries a stated
# ~1/sweep conhost self-noise floor, so its per-minute figure scales with the
# host cadence. Per-side `status` is the producer's stated-cause channel
# (ok|warming|no-ps|no-sweep|read-failed|capped).


@dataclass
class SpawnTop:
    """One spawner-census row: the PARENT label (item 2 upgrades it to the
    owning session and fills ``pid`` for the Copy-PID affordance)."""
    label: str = "?"
    count: int = 0
    pid: int | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            label=_s(d.get("label"), "?"),
            count=_i(d.get("count")),
            pid=_io(d.get("pid")),
        )


@dataclass
class SpawnSide:
    """One boundary side's census. The win side never carries the wsl-only
    metrics (``alloc_per_min``/``self_per_min`` stay None there)."""
    alloc_per_min: float | None = None
    visible_per_min: float | None = None
    self_per_min: float | None = None
    top: list[SpawnTop] = field(default_factory=list)
    status: str = ""
    window_s: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            alloc_per_min=_fo(d.get("alloc_per_min")),
            visible_per_min=_fo(d.get("visible_per_min")),
            self_per_min=_fo(d.get("self_per_min")),
            top=[SpawnTop.from_dict(t) for t in _list(d.get("top"))],
            status=_s(d.get("status")),
            window_s=_fo(d.get("window_s")),
        )


@dataclass
class Spawns:
    """The two-sided spawn census reading."""
    wsl: SpawnSide = field(default_factory=SpawnSide)
    win: SpawnSide = field(default_factory=SpawnSide)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        return cls(
            wsl=SpawnSide.from_dict(d.get("wsl")),
            win=SpawnSide.from_dict(d.get("win")),
        )


@dataclass
class Egress:
    """The off-box egress reading: whole-box totals over ``window_s``, the
    iface block (None when the NIC counters never answered or aged out —
    independently of the TCP side), and the talkers ranked worst-↑ first."""
    window_s: float | None = None
    total_in_bps: float | None = None
    total_out_bps: float | None = None
    iface: EgressIface | None = None
    top: list[EgressTalker] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        iface = d.get("iface")
        return cls(
            window_s=_fo(d.get("window_s")),
            total_in_bps=_fo(d.get("total_in_bps")),
            total_out_bps=_fo(d.get("total_out_bps")),
            iface=(EgressIface.from_dict(iface)
                   if isinstance(iface, dict) else None),
            top=[EgressTalker.from_dict(t) for t in _list(d.get("top"))],
        )


@dataclass
class Runner:
    """One self-hosted Actions runner container: identity from the producer's
    once-per-lifetime inspect (RUNNER_NAME/REPO_URL env), cost from the shared
    docker-stats sweep. Cost fields None = no stats row that sweep (never a
    fabricated 0); ``active`` is the producer's calibrated judgment (cpu, or
    tasks over the runner's idle floor — the app never re-derives it),
    tri-state: None = neither signal had data."""
    name: str = "?"
    container: str = "?"
    repo: str | None = None
    # The docker ps Status string verbatim ("Up 7 minutes") — restart churn
    # visible with zero parsing; None = a predating producer.
    up: str | None = None
    cpu_pct: float | None = None
    mem_mib: float | None = None
    pids: int | None = None
    pids_floor: int | None = None
    active: bool | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        act = d.get("active")
        # bool guards: JSON `true` must not fabricate cpu 1.0 / 1 task
        # (bool ⊂ int/float — the child_pids lesson, 2026-07-24).
        cpu = d.get("cpu_pct")
        mem = d.get("mem_mib")
        pids = d.get("pids")
        floor = d.get("pids_floor")
        return cls(
            name=_s(d.get("name"), "?"),
            container=_s(d.get("container"), "?"),
            repo=_so(d.get("repo")),
            up=_so(d.get("up")),
            cpu_pct=None if isinstance(cpu, bool) else _fo(cpu),
            mem_mib=None if isinstance(mem, bool) else _fo(mem),
            pids=None if isinstance(pids, bool) else _io(pids),
            pids_floor=None if isinstance(floor, bool) else _io(floor),
            active=act if isinstance(act, bool) else None,
        )


@dataclass
class RunnerBurst:
    """One burst-runner session (``runner_fleet.bursts``):
    a contiguous active span on one fleet runner — the short-lived job class
    that starts and finishes outside the session view. ``running`` carries
    ``age_s``; ``done`` carries ``dur_s`` (the producer's evidence bracket) +
    ``ended_ago_s`` and DROPS from the feed after the producer's linger. The
    app renders the frame verbatim — lifecycle (appear fast, disappear fast,
    no stale rows) is the producer's, by construction. ``start_unseen`` =
    the monitor woke mid-job, so ages/durations are FLOORS."""
    runner: str = "?"
    repo: str | None = None
    state: str = "?"
    age_s: float | None = None
    dur_s: float | None = None
    ended_ago_s: float | None = None
    start_unseen: bool = False
    peak_cpu_pct: float | None = None
    peak_mem_mib: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        age = d.get("age_s")
        dur = d.get("dur_s")
        ago = d.get("ended_ago_s")
        pc = d.get("peak_cpu_pct")
        pm = d.get("peak_mem_mib")
        return cls(
            runner=_s(d.get("runner"), "?"),
            repo=_so(d.get("repo")),
            state=_s(d.get("state"), "?"),
            age_s=None if isinstance(age, bool) else _fo(age),
            dur_s=None if isinstance(dur, bool) else _fo(dur),
            ended_ago_s=None if isinstance(ago, bool) else _fo(ago),
            # Only a literal true marks a floor — junk must not soften a
            # concrete age into a "≥" claim (nor the reverse).
            start_unseen=d.get("start_unseen") is True,
            peak_cpu_pct=None if isinstance(pc, bool) else _fo(pc),
            peak_mem_mib=None if isinstance(pm, bool) else _fo(pm),
        )


@dataclass
class HostedRun:
    """One queued/in-progress Actions run from the opt-in hosted burst-run
    watch (``hosted_bursts.runs``). ``where`` is the
    producer's join against the LOCAL fleet census: ``this-box`` /
    ``self-other-host`` / ``hosted`` / ``pending``."""
    repo: str | None = None
    workflow: str = "?"
    status: str = "?"
    where: str = "?"
    runner: str | None = None
    age_s: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        age = d.get("age_s")
        return cls(
            repo=_so(d.get("repo")),
            workflow=_s(d.get("workflow"), "?"),
            status=_s(d.get("status"), "?"),
            where=_s(d.get("where"), "?"),
            runner=_so(d.get("runner")),
            age_s=None if isinstance(age, bool) else _fo(age),
        )


@dataclass
class HostedBursts:
    """The opt-in hosted burst-run watch reading (``hosted_bursts``). Hosted
    runs have no local footprint, so the Actions API is the only honest source.
    This field is None when the watch is OFF (or unsupported); ``ok`` None
    = the watch just turned on and no sweep landed yet (warming, never an
    empty claim); ``unchecked`` counts in-progress runs whose jobs lookup
    fell past the producer's per-sweep budget — counted, never silently
    claimed classified."""
    ok: bool | None = None
    error: str | None = None
    repos: list[str] = field(default_factory=list)
    checked_ago_s: float | None = None
    runs: list[HostedRun] = field(default_factory=list)
    unchecked: int = 0
    truncated: int = 0

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        ok = d.get("ok")
        ago = d.get("checked_ago_s")
        unchecked = d.get("unchecked")
        trunc = d.get("truncated")
        return cls(
            ok=ok if isinstance(ok, bool) else None,
            error=_so(d.get("error")),
            repos=[r for r in _list(d.get("repos"))
                   if isinstance(r, str)][:8],
            checked_ago_s=None if isinstance(ago, bool) else _fo(ago),
            runs=[HostedRun.from_dict(r) for r in _list(d.get("runs"))],
            unchecked=0 if isinstance(unchecked, bool) else _i(unchecked),
            truncated=0 if isinstance(trunc, bool) else _i(trunc),
        )


@dataclass
class RunnerFleet:
    """The self-hosted Actions runner-fleet census (producer ``runner_fleet``).
    Runners arrive active-first, then name-sorted (the producer's stable
    ordering); totals None = no runner had a stats row (never a fabricated 0).
    ``unresolved`` counts running containers whose identity inspect has NOT
    answered — an empty ``runners`` with ``unresolved > 0`` is an UNKNOWN
    fleet, never "no runners" (the producer's consumer rule)."""
    runners: list[Runner] = field(default_factory=list)
    # Burst-runner sessions: running episodes then recently-ended
    # ones, producer-ordered; [] = none live and none in the linger (or a
    # predating producer — an absent key is an honest nothing either way).
    bursts: list[RunnerBurst] = field(default_factory=list)
    total: int = 0
    active_count: int = 0
    unresolved: int = 0
    cpu_pct_total: float | None = None
    mem_mib_total: float | None = None

    @classmethod
    def from_dict(cls, d):
        d = _dict(d)
        cpu = d.get("cpu_pct_total")
        mem = d.get("mem_mib_total")
        unres = d.get("unresolved")
        total = d.get("total")
        active = d.get("active_count")
        # bool guards on EVERY numeric field — a JSON `true` total would
        # fabricate a "1 up" fleet (bool ⊂ int, review H1 on this diff).
        return cls(
            runners=[Runner.from_dict(r) for r in _list(d.get("runners"))],
            bursts=[RunnerBurst.from_dict(b)
                    for b in _list(d.get("bursts"))],
            total=0 if isinstance(total, bool) else _i(total),
            active_count=0 if isinstance(active, bool) else _i(active),
            unresolved=0 if isinstance(unres, bool) else _i(unres),
            cpu_pct_total=None if isinstance(cpu, bool) else _fo(cpu),
            mem_mib_total=None if isinstance(mem, bool) else _fo(mem),
        )


# ── top-level snapshot ──────────────────────────────────────────────────────────

@dataclass
class Snapshot:
    schema: str = SNAPSHOT_SCHEMA
    t: float = 0.0
    ts: str = ""
    host: str = ""
    interval: float = 2.0
    uptime_s: float = 0.0
    gpus: list[Gpu] = field(default_factory=list)
    gpu_util_history: list[float] = field(default_factory=list)
    activity: Activity | None = None
    history: list[RunRecord] = field(default_factory=list)
    open_run_t: float | None = None
    docker: DockerStatus | None = None
    ollama: list[OllamaModel] | None = None
    host_cpu: CpuStat | None = None
    # The whole physical box's Windows-side CPU strain (None = older producer /
    # no host sweep / counters unavailable — the host_refresh_s cadence marks
    # its freshness). Drives the Windows-host section's "win cpu" row.
    windows_cpu: WindowsCpu | None = None
    cpu_history: list[float] = field(default_factory=list)
    # Its utility trend, one sample per fresh host sweep ([] = warming /
    # older producer — the cpu_history list convention). Drives the trend
    # sparkline under the win cpu row.
    windows_cpu_history: list[float] = field(default_factory=list)
    host_ram: RamStat | None = None
    memory: MemoryPanel | None = None
    # The producer's per-session/node attribution block (None on a GPU-only feed
    # that predates it). Drives the Sessions panel.
    processes: Processes | None = None
    # The memory-pressure early-warning (None when the producer doesn't emit it) +
    # the worst-first ranked contributors ([] when nothing is firing). Drive the
    # Forecast panel.
    forecast: Forecast | None = None
    contributors: list[Contributor] = field(default_factory=list)
    # The stuck-process / run-queue probe (None when the producer doesn't emit
    # it — an older service or --no-stuck). Drives the Stuck panel.
    stuck: Stuck | None = None
    # Windows-side dedicated-VRAM holders (None = the producer doesn't report
    # them: older build / GPU counters unavailable; [] = counters answered and
    # nothing held >= the floor). Drives the Activity panel's "vram:" note.
    gpu_users: list[GpuUser] | None = None
    # The Windows scanner-burn census (None = the producer doesn't report it:
    # older build / --no-scanners / counters unavailable; [] = it answered and
    # no watched scanner runs here). Drives the Scanners panel.
    scanners: list[Scanner] | None = None
    security_stack: SecurityStack | None = None
    # The off-box egress ranking (None = the producer doesn't report it: older
    # build / --no-egress / still warming; a present reading with an empty
    # `top` is a QUIET window, not off). Drives the Egress panel.
    egress: Egress | None = None
    # The self-hosted Actions runner-fleet census (None = the producer doesn't
    # report it: older build / --no-docker / docker unavailable; a present
    # reading with runners [] = docker answered, no runner containers on the
    # box). Drives the Runners panel.
    runner_fleet: RunnerFleet | None = None
    # The opt-in hosted burst-run watch (None = watch OFF / older producer).
    # Renders under the Runners panel's groups.
    hosted_bursts: HostedBursts | None = None
    # Exec-pressure spawn census (None = older producer / --no-spawns).
    # Drives the Scanners-panel headline.
    spawns: Spawns | None = None
    # The producer's own CPU bill (None = older producer / warming — a --once
    # run never emits one). Drives the Host panel's "monitor:" note.
    self_cost: SelfCost | None = None
    # Its downsampled total_pct trend (1/min, ~2 h; [] = warming or an older
    # producer). Drives the dim sparkline under the note.
    self_cost_history: list[float] = field(default_factory=list)
    # The producer's watched-port readouts ([] when none configured). Drives the
    # Ports panel; the set is configured live from Settings via monitor-config:v1.
    ports: list[Port] = field(default_factory=list)
    # process→ports target answers (None = no target configured / a producer
    # that predates the field). Session-only targets, set through the same
    # config channel (`watch_procs`); rendered in the Ports panel.
    proc_ports: list[ProcPortRecord] | None = None
    runs: list[ProgressRun] = field(default_factory=list)
    # The git short-SHA the producer's live source is at (None when the producer
    # doesn't relay it). The app compares it to its baked-in build SHA to detect a
    # stale native build. See __init__.build_version().
    source_version: str | None = None
    log_path: str = ""
    log_status: str = ""

    @classmethod
    def from_dict(cls, d):
        if not isinstance(d, dict):
            raise SnapshotError("snapshot is not a JSON object")
        schema = _s(d.get("schema"))
        if schema and schema != SNAPSHOT_SCHEMA:
            raise SnapshotError(
                f"unsupported snapshot schema {schema!r} (expected {SNAPSHOT_SCHEMA!r})"
            )
        act = d.get("activity")
        dock = d.get("docker")
        oll = d.get("ollama")
        cpu = d.get("host_cpu")
        wcpu = d.get("windows_cpu")
        ram = d.get("host_ram")
        mem = d.get("memory")
        procs = d.get("processes")
        fc = d.get("forecast")
        stk = d.get("stuck")
        gu = d.get("gpu_users")
        sc = d.get("scanners")
        eg = d.get("egress")
        rf = d.get("runner_fleet")
        sp = d.get("spawns")
        pp = d.get("proc_ports")
        return cls(
            schema=schema or SNAPSHOT_SCHEMA,
            t=_f(d.get("t")),
            ts=_s(d.get("ts")),
            host=_s(d.get("host")),
            interval=_f(d.get("interval"), 2.0),
            uptime_s=_f(d.get("uptime_s")),
            gpus=[Gpu.from_dict(g) for g in _list(d.get("gpus"))],
            gpu_util_history=[_f(v) for v in _list(d.get("gpu_util_history"))],
            activity=Activity.from_dict(act) if isinstance(act, dict) else None,
            history=[RunRecord.from_dict(r) for r in _list(d.get("history"))],
            open_run_t=_fo(d.get("open_run_t")),
            docker=DockerStatus.from_dict(dock) if isinstance(dock, dict) else None,
            ollama=([OllamaModel.from_dict(m) for m in oll]
                    if isinstance(oll, list) else None),
            host_cpu=CpuStat.from_dict(cpu) if isinstance(cpu, dict) else None,
            windows_cpu=(WindowsCpu.from_dict(wcpu)
                         if isinstance(wcpu, dict) else None),
            cpu_history=[_f(v) for v in _list(d.get("cpu_history"))],
            windows_cpu_history=[_f(v) for v in
                                 _list(d.get("windows_cpu_history"))],
            host_ram=RamStat.from_dict(ram) if isinstance(ram, dict) else None,
            memory=MemoryPanel.from_dict(mem) if isinstance(mem, dict) else None,
            processes=Processes.from_dict(procs) if isinstance(procs, dict) else None,
            forecast=Forecast.from_dict(fc) if isinstance(fc, dict) else None,
            contributors=[Contributor.from_dict(c)
                          for c in _list(d.get("contributors"))],
            stuck=Stuck.from_dict(stk) if isinstance(stk, dict) else None,
            gpu_users=([GpuUser.from_dict(u) for u in gu]
                       if isinstance(gu, list) else None),
            scanners=([Scanner.from_dict(s) for s in sc]
                      if isinstance(sc, list) else None),
            security_stack=(SecurityStack.from_dict(d.get("security_stack"))
                            if isinstance(d.get("security_stack"), dict)
                            else None),
            egress=Egress.from_dict(eg) if isinstance(eg, dict) else None,
            runner_fleet=(RunnerFleet.from_dict(rf)
                          if isinstance(rf, dict) else None),
            hosted_bursts=(HostedBursts.from_dict(d.get("hosted_bursts"))
                           if isinstance(d.get("hosted_bursts"), dict)
                           else None),
            spawns=Spawns.from_dict(sp) if isinstance(sp, dict) else None,
            self_cost=(SelfCost.from_dict(d.get("self_cost"))
                       if isinstance(d.get("self_cost"), dict) else None),
            self_cost_history=[_f(v) for v in
                               _list(d.get("self_cost_history"))],
            ports=[Port.from_dict(p) for p in _list(d.get("ports"))],
            proc_ports=([ProcPortRecord.from_dict(r) for r in pp]
                        if isinstance(pp, list) else None),
            runs=[ProgressRun.from_dict(r) for r in _list(d.get("runs"))],
            source_version=_s(d.get("source_version")) or None,
            log_path=_s(d.get("log_path")),
            log_status=_s(d.get("log_status")),
        )

    @property
    def gpu(self) -> Gpu | None:
        """The primary GPU (the one the tray icon reflects)."""
        return self.gpus[0] if self.gpus else None

    @property
    def is_active(self) -> bool:
        return self.activity is not None


def parse(text: str) -> Snapshot:
    """Parse snapshot JSON text into a Snapshot (raises SnapshotError)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise SnapshotError(f"invalid snapshot JSON: {exc}") from exc
    return Snapshot.from_dict(data)


def load(path) -> Snapshot:
    """Read and parse a snapshot file (raises SnapshotError / OSError)."""
    return parse(Path(path).read_text(encoding="utf-8"))
