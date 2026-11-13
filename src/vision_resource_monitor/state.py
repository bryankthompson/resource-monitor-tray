"""Pure UI-state derivation from snapshots.

Everything the tray/window needs to *decide* — the overall status word, the
tray-icon appearance, the tooltip line, and which toast notifications to fire on
run boundaries — is computed here, Qt-free and unit-testable. The Qt layer only
paints what these functions return.
"""
from __future__ import annotations

import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from .reader import ReadResult
from .snapshot import Snapshot

# Shared colour code, matching the terminal monitor: green ≤50%, yellow >50%,
# red >85%. Hex chosen to read on both light and dark tray backgrounds.
GREEN = "#2ea043"
YELLOW = "#d29922"
RED = "#f85149"
GREY = "#8b949e"

# The activity accent: hot magenta, deliberately outside the green/yellow/red
# alert ladder so "the GPU is working" pops without reading as an alarm.
HOT = "#ff2e88"
HOT_DIM = "#8f2454"   # the pulse's off-phase — only ever dims the accent

# Active-indicator styles (the Settings "Active indicator" choice; view-state).
ACTIVE_HOT = "hot"        # hot accent wherever the util ladder reads calm green
ACTIVE_PULSE = "pulse"    # hot accent + the tray icon pulses while ACTIVE
ACTIVE_SUBTLE = "subtle"  # legacy: plain util_color (calm green)
ACTIVE_STYLES = (ACTIVE_HOT, ACTIVE_PULSE, ACTIVE_SUBTLE)
DEFAULT_ACTIVE_STYLE = ACTIVE_HOT

# Status the whole app is in.
STALE = "stale"
NODATA = "nodata"
ACTIVE = "active"
IDLE = "idle"

# Per-tier availability for the panels' memory tiers (see memory_tiers();
# since the 2026-07-29 regroup the wsl tier renders in the WSL section, the
# windows/docker/disk tiers in the Windows-host section).
MEM_OK = "ok"                  # the producer collected the tier — render its data
MEM_UNAVAILABLE = "unavailable"  # the producer tried and couldn't — warn + reason
MEM_NONE = "none"             # legitimately empty (e.g. Docker up, no containers)


def util_color(pct: float) -> str:
    if pct > 85:
        return RED
    if pct > 50:
        return YELLOW
    return GREEN


def busy_color(pct: float, marker: float | None) -> str:
    """The win cpu row's colour: red at or above the busy marker (equal is
    red), else yellow above 50, else green. No marker = plain util_color."""
    if marker is None:
        return util_color(pct)
    if pct >= marker:
        return RED
    return YELLOW if pct > 50 else GREEN


def active_color(pct: float, style: str = DEFAULT_ACTIVE_STYLE) -> str:
    """Colour for an ACTIVE indicator: the util ladder with its calm-green rung
    swapped for the hot accent — green is the rung that fails to catch the eye
    (it reads like idle). Yellow/red keep their high-util alarm meaning."""
    c = util_color(pct)
    if style != ACTIVE_SUBTLE and c == GREEN:
        return HOT
    return c


@dataclass
class IconSpec:
    """What the tray icon should look like this frame (Qt paints it)."""
    level: str                 # ACTIVE / IDLE / STALE / NODATA
    color: str                 # ring/fill colour
    fill: float = 0.0          # 0..1 — how much of the icon reads as "filled"
    text: str = ""             # short overlay, e.g. "87"


def overall_status(result: ReadResult) -> str:
    snap = result.snapshot
    if snap is None:
        return NODATA
    if result.stale:
        return STALE
    if not snap.gpus:
        return NODATA
    return ACTIVE if snap.is_active else IDLE


def icon_spec(result: ReadResult, style: str = DEFAULT_ACTIVE_STYLE,
              pulse_on: bool = False) -> IconSpec:
    status = overall_status(result)
    snap = result.snapshot
    if status in (STALE, NODATA) or snap is None:
        return IconSpec(level=status, color=GREY, fill=0.0, text="")
    gpu = snap.gpu
    util = gpu.util if gpu else 0.0
    color = util_color(util)
    if status == ACTIVE:
        color = active_color(util, style)
        # The pulse only ever dims the accent — a yellow/red alarm never blinks.
        if style == ACTIVE_PULSE and pulse_on and color == HOT:
            color = HOT_DIM
    return IconSpec(
        level=status,
        color=color,
        fill=min(max(util / 100.0, 0.0), 1.0),
        text=f"{util:.0f}",
    )


def tooltip(result: ReadResult) -> str:
    """One-line summary for the tray tooltip / hover."""
    status = overall_status(result)
    snap = result.snapshot
    if status == NODATA:
        reason = (result.error or "no data") if result else "no data"
        return f"Vision Resource Monitor — {reason}"
    if status == STALE:
        age = f"{result.age_s:.0f}s" if result.age_s is not None else "?"
        return f"Vision Resource Monitor — stale (last update {age} ago)"
    gpu = snap.gpu
    head = f"GPU {gpu.util:.0f}% · {gpu.mem_used_mib/1024:.1f}/{gpu.mem_total_mib/1024:.1f}G" if gpu else "GPU —"
    if snap.is_active and snap.activity:
        wl = ", ".join(snap.activity.workloads)
        return f"{head} · ACTIVE" + (f" ({wl})" if wl else "")
    return f"{head} · idle"


# Stated causes for the spawn census's per-side status tokens (the producer's
# honesty channel). An unknown token renders AS ITSELF — degraded
# wording must never be blank or guessed.
_SPAWN_CAUSES_WSL = {
    "warming": "warming",
    "no-ps": "no ps sweep",
    "read-failed": "watermark read failed",
    "capped": "over sanity cap — sample dropped",
}
_SPAWN_CAUSES_WIN = {
    "warming": "warming",
    "no-sweep": "no host sweep",
    "read-failed": "census failed",
}


def _fin_or_none(v):
    """A rate fit to render: non-finite (json Infinity/NaN survive the
    tolerant parse) reads as unavailable, never as '~inf/min'."""
    return v if v is not None and math.isfinite(v) else None


def _spawn_side_brief(side, *, wsl: bool) -> str:
    """One side's compact reading. A degraded status never HIDES co-present
    metrics — the producer keeps nulls per-metric by design (e.g. --no-procs
    still reports the watermark rate): metrics first, cause appended. Pure;
    shared by spawn_headline and panel_populate_report."""
    alloc = _fin_or_none(side.alloc_per_min) if wsl else None
    visible = _fin_or_none(side.visible_per_min)
    self_rate = _fin_or_none(side.self_per_min) if wsl else None
    brief = None
    if alloc is not None:
        brief = f"~{alloc:.0f}/min tasks"
        extras = []
        if visible is not None:
            extras.append(f"seen {visible:.0f}")
        if self_rate is not None:
            extras.append(f"self {self_rate:.0f}")
        if extras:
            brief += " (" + " · ".join(extras) + ")"
    elif visible is not None:
        brief = f"~{visible:.0f}/min seen"
        if self_rate is not None:
            brief += f" (self {self_rate:.0f})"
    elif self_rate is not None:
        brief = f"self {self_rate:.0f}/min"
    if side.status == "ok":
        return brief or "unavailable"
    causes = _SPAWN_CAUSES_WSL if wsl else _SPAWN_CAUSES_WIN
    cause = causes.get(side.status) or side.status or "unavailable"
    return f"{brief} · {cause}" if brief else cause


def spawn_headline(sp) -> tuple[str, str]:
    """The Scanners-panel exec-pressure headline for a PRESENT Spawns reading:
    (text, tooltip). The inline top is the wsl census capped at 3; the tooltip
    carries both full top lists plus the honesty caveats (tasks-not-processes,
    win survivors undercount + its ~1/sweep conhost self-noise floor). Never
    returns empty text — every degraded side states its cause."""
    text = ("spawns: wsl " + _spawn_side_brief(sp.wsl, wsl=True)
            + " · win " + _spawn_side_brief(sp.win, wsl=False))
    # count>0 drops only tolerant-parse junk — the producer never emits an
    # empty bucket, and a defaulted "? ×0" row is not a top spawner.
    wsl_top = [t for t in sp.wsl.top if t.count > 0]
    win_top = [t for t in sp.win.top if t.count > 0]
    if wsl_top:
        text += " — top: " + ", ".join(
            f"{_spawn_top_label(t)} ×{t.count}" for t in wsl_top[:3])
    tip_lines = [
        "wsl rate counts TASKS (Linux allocates threads from the same pid "
        "space); 'seen'/'self' are process-level.",
        "win counts host-sweep survivors only (no watermark on Windows) — "
        "an undercount with a stated ~1/sweep conhost self-noise floor; "
        "the per-minute figure scales with the host cadence.",
    ]
    if wsl_top:
        tip_lines.append("wsl top: " + ", ".join(
            f"{_spawn_top_label(t)} ×{t.count}" for t in wsl_top[:8]))
    if win_top:
        tip_lines.append("win top: " + ", ".join(
            f"{t.label} ×{t.count}" for t in win_top[:8]))
    return text, "\n".join(tip_lines)


def security_stack_headline(stack):
    """The Scanners-panel whole-stack line for a PRESENT SecurityStack reading:
    ``(text, warn, tooltip)`` — ``warn`` flips only on the
    producer's ``taxing`` judgment (never re-derived from the cores figure).
    Returns None when the field is absent (an older producer): the census
    stated-cause lines already cover the family, so no line is fabricated.
    Pure and Qt-free."""
    if stack is None:
        return None
    cores = stack.cores
    cores_txt = (f"{cores:.1f} cores" if cores is not None else "? cores")
    tip_lines = []
    if stack.top:
        tip_lines.append("groups: " + ", ".join(
            f"{g.label} {g.cpu_pct:.0f}%" if g.cpu_pct is not None else g.label
            for g in stack.top[:8]))
    if stack.spawns_per_min is not None:
        # Named for what the census measures: task allocations (processes AND
        # threads), not processes — the producer's own honesty contract.
        tip_lines.append(f"workload correlate: ~{stack.spawns_per_min:.0f} "
                         "wsl task allocations/min captured with the sweep")
    else:
        tip_lines.append("no spawn reading captured with the sweep")
    if stack.taxing:
        # The duration renders only when the producer reported one — a
        # contract-violating frame must not fabricate "sustained 0m"
        # (review fold, same rule as the hot branch below).
        dur = (f" — sustained {stack.hot_for_s / 60.0:.0f}m"
               if stack.hot_for_s is not None else "")
        return (f"⚠ security stack: {cores_txt}{dur}",
                True, "\n".join(tip_lines))
    if stack.hot and stack.hot_for_s is not None:
        # Hot but not yet sustained: the figure plus its clock, still dim —
        # only the producer's taxing judgment warns.
        return (f"security stack: {cores_txt} (hot {stack.hot_for_s / 60.0:.0f}m)",
                False, "\n".join(tip_lines))
    return f"security stack: {cores_txt}", False, "\n".join(tip_lines)


def _spawn_top_label(t) -> str:
    """A wsl top bucket in the name[pid] Copy-PID form when the producer
    attributed a root pid; bare label when unattributed or
    ambiguous across the window."""
    return f"{t.label}[{t.pid}]" if t.pid is not None else t.label


def panel_populate_report(snap: Snapshot | None) -> list[str]:
    """Qt-free, PII-safe account of which window panels/tiers will populate this
    frame and, when a tier is empty, *why*.

    This is the payload the tray app logs at DEBUG so a blank panel — notably the
    memory tiers (WSL section's wsl tier / Windows-host section's windows,
    docker and disk tiers since the 2026-07-29 regroup) — can be diagnosed
    from the log alone (e.g. the producer's
    ``get_host_memory()`` returning ``None`` shows up here as
    ``memory.windows: absent``). The conditions mirror the render branches in
    ``ui/window.py``; keep the two in sync. Reports tier presence, counts, and
    top-level totals only — never per-process values beyond the process *names*
    the snapshot already carries.
    """
    if snap is None:
        return ["snapshot: none (no frame yet)"]
    lines = [
        f"gpu: {len(snap.gpus)} device(s)" if snap.gpus else "gpu: absent",
        f"host_cpu: {'present' if snap.host_cpu else 'absent'}",
        # Three-way, mirroring the render branch (review LOW): a reading
        # whose busy figure is unusable renders the dashed row, and a log-only
        # diagnosis of that dash must not answer a plain "present".
        "windows_cpu: " + (
            "absent" if snap.windows_cpu is None
            else "present" if snap.windows_cpu.time_pct is not None
            else "present, no usable busy figure"),
        f"host_ram: {'present' if snap.host_ram else 'absent'}",
    ]
    # Forecast panel: level + ETA + contributor count only (PII-safe — no process
    # names beyond what the snapshot already carries elsewhere). Mirrors the
    # Forecast render branch in ui/window.py; keep the two in sync.
    fc = snap.forecast
    if fc is None:
        lines.append("forecast: absent (producer emitted no forecast)")
    else:
        eta = f" eta={fc.eta_min:.0f}m" if fc.eta_min is not None else ""
        lines.append(f"forecast: level={fc.level}{eta}")
    lines.append(f"contributors: {len(snap.contributors)} firing"
                 if snap.contributors else "contributors: none")
    # Stuck panel: queue depth + wedged count only (PII-safe). Mirrors the Stuck
    # render branch in ui/window.py; keep the two in sync.
    st = snap.stuck
    if st is None:
        lines.append("stuck: absent (producer emitted no stuck probe)")
    else:
        lines.append(f"stuck: queue {st.procs_running}/{st.ncpu}, "
                     f"{len(st.wedged)} wedged, "
                     f"{len(stuck_flags(st))} flag(s)")
    # Scanners panel: the three-way census state (group labels are product
    # names, PII-safe). Mirrors the Scanners render branch in ui/window.py;
    # keep the two in sync.
    sc = snap.scanners
    if sc is None:
        lines.append("scanners: absent (producer emitted no scanner census)")
    elif not sc:
        lines.append("scanners: empty (census answered, none present)")
    else:
        burning = sum(1 for r in sc if r.burning)
        lines.append(f"scanners: {len(sc)} group(s), {burning} burning")
    # Whole-stack tax headline (same panel, independent lifecycle — mirrors
    # the _stack_head render branch; keep the two in sync).
    ss = snap.security_stack
    if ss is None:
        lines.append("security_stack: absent (producer emitted no reading)")
    else:
        lines.append(f"security_stack: {ss.cores} cores, hot={ss.hot}, "
                     f"taxing={ss.taxing}")
    # Egress panel: the three-way reading state (counts only, PII-safe).
    # Mirrors the Egress render branch in ui/window.py; keep the two in sync.
    eg = snap.egress
    if eg is None:
        lines.append("egress: absent (producer emitted no egress ranking)")
    elif not eg.top:
        lines.append("egress: quiet (no wsl-side talkers in the window)")
    else:
        lines.append(f"egress: {len(eg.top)} talker(s), iface "
                     f"{'present' if eg.iface is not None else 'absent'}")
    # Runners panel: the three-way fleet-census state (counts only, PII-safe).
    # Mirrors the Runners render branch in ui/window.py; keep the two in sync.
    rf = snap.runner_fleet
    if rf is None:
        lines.append("runner_fleet: absent (producer emitted no fleet census)")
    else:
        if not rf.runners and rf.unresolved:
            line = (f"runner_fleet: unknown ({rf.unresolved} container(s) "
                    "awaiting identity)")
        elif not rf.runners:
            line = "runner_fleet: empty (docker answered, no runner containers)"
        else:
            line = (f"runner_fleet: {rf.total} runner(s), "
                    f"{rf.active_count} active")
            if rf.unresolved:
                # A mixed fleet must not read fully-known while identity
                # inspects are failing (review L2 — mirror the headline).
                line += f", {rf.unresolved} unresolved"
        # The bursts clause rides EVERY fleet state — the linger outlives
        # the runner, so an empty fleet can still carry burst evidence
        # (review H-empty-fleet).
        running = sum(1 for b in rf.bursts if b.state == "running")
        done = sum(1 for b in rf.bursts if b.state == "done")
        if running or done:
            line += f", bursts {running} running/{done} recent"
        lines.append(line)
    # The opt-in hosted burst-run watch (counts only, PII-safe). Mirrors the
    # _render_hosted_block states; keep the two in sync.
    hb = snap.hosted_bursts
    if hb is None:
        lines.append("hosted_bursts: off (opt-in watch not enabled)")
    elif hb.ok is None:
        lines.append("hosted_bursts: warming (first sweep pending)")
    elif hb.ok is False:
        lines.append("hosted_bursts: error (stated on the panel)")
    else:
        lines.append(f"hosted_bursts: {len(hb.runs)} run(s), "
                     f"{hb.unchecked} unchecked")
    # Scanners panel's spawns headline: the exec-pressure census state.
    # Mirrors spawn_headline (the render source); keep the two in sync.
    sp = snap.spawns
    if sp is None:
        lines.append("spawns: absent (producer emitted no spawn census)")
    else:
        lines.append("spawns: wsl "
                     f"{_spawn_side_brief(sp.wsl, wsl=True)} · win "
                     f"{_spawn_side_brief(sp.win, wsl=False)}")
    # The WSL section's "monitor:" self-cost note: the producer's own bill state
    # (numbers only, PII-safe). Mirrors selfcost_note (the render source);
    # keep the two in sync.
    scost = snap.self_cost
    if scost is None:
        lines.append("self_cost: absent (older producer / warming / --once)")
    else:
        win_state = ("measured" if scost.windows_pct is not None
                     else "unmeasured")
        lines.append(f"self_cost: total {scost.total_pct:.1f}% of a core "
                     f"over {scost.window_s:.0f}s, windows {win_state}")
    # Activity's vram note: the three-way gpu_users state (None = producer
    # doesn't report holders; [] = counters answered, nothing ≥ floor; rows).
    # Mirrors the _act_vram render branch in ui/window.py; keep the two in sync.
    gu = snap.gpu_users
    if gu is None:
        lines.append("gpu_users: absent (producer reports no VRAM holders)")
    elif not gu:
        lines.append("gpu_users: empty (counters answered, nothing ≥ floor)")
    else:
        lines.append(f"gpu_users: {len(gu)} holder(s)")
    mem = snap.memory
    if mem is None:
        lines.append("memory: absent (no 'memory' key in snapshot)")
        return lines
    win = mem.windows
    if win is None:
        lines.append("memory.windows: absent (producer emitted no windows host "
                     "tier — get_host_memory() likely returned None)")
    elif not win.total_mib:
        lines.append("memory.windows: present but total_mib=0 (renders as unavailable)")
    else:
        lines.append(f"memory.windows: present total={win.total_mib:.0f}MiB "
                     f"procs={len(win.processes)}")
    lines.append(f"memory.wsl: {len(mem.wsl)} proc(s)" if mem.wsl else "memory.wsl: empty")
    lines.append(f"memory.containers: {len(mem.containers)}" if mem.containers
                 else "memory.containers: empty")
    lines.append(f"memory.disk: {len(mem.disk)} row(s)" if mem.disk else "memory.disk: empty")
    return lines


def _gg(used_mib: float, total_mib: float) -> str:
    """A used/total MiB pair in the app's compact G form (9.2/15.9G)."""
    return f"{used_mib / 1024:.1f}/{total_mib / 1024:.1f}G"


def _dur(seconds: float) -> str:
    """A compact duration for header summaries (48s / 12m / 25h). Non-finite
    input renders '?' — the tolerant parse passes inf/nan through, and a raise
    here would abort update_from mid-frame (the producer review-H1 crash class)."""
    if not math.isfinite(seconds):
        return "?"
    s = max(0, int(round(seconds)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def _rate(bps: float) -> str:
    """A byte/s rate for header summaries (89K/s, 1.2M/s) — the Qt-free twin
    of ui/window._fmt_rate; keep the two in sync. Non-finite input formats
    (infM/s) rather than raises — the tolerant parse passes inf/nan through,
    and a raise here would abort update_from mid-frame."""
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f}M/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f}K/s"
    return f"{bps:.0f}B/s"


def repo_group_label(repo) -> str:
    """A compact group label for a served repo: the initials of the repo
    basename's hyphenated words ("image-sync-service" → "iss",
    "my-web-app" → "mwa"); a single-word repo keeps the word
    ("dashboard"). None/empty → "other" (an unattributed runner still groups
    somewhere visible, never vanishes). Pure."""
    if not repo:
        return "other"
    base = str(repo).rstrip("/").split("/")[-1]
    parts = [p for p in base.split("-") if p]
    if not parts:
        return "other"
    label = parts[0] if len(parts) == 1 else "".join(p[0] for p in parts)
    # Comma hygiene (the parse_watch_spec rule): labels persist into the
    # comma-joined ui/runners_expanded store, and a hostile/corrupt feed
    # must not corrupt it (review F2).
    return label.replace(",", "") or "other"


@dataclass
class RunnerGroup:
    """One Runners-panel repo group, grouped by served repo and expandable.
    Totals keep
    the fleet fold's honesty: None when no runner in the group had a stats
    reading (never a fabricated 0). ``bursts`` are the group's burst-runner
    sessions in producer order; ``burst_running`` counts
    the live ones for the header."""
    label: str = "other"
    repo: str | None = None
    runners: list = field(default_factory=list)
    bursts: list = field(default_factory=list)
    burst_running: int = 0
    active_count: int = 0
    cpu_pct_total: float | None = None
    mem_mib_total: float | None = None


def runner_groups(fleet) -> list:
    """Bucket a RunnerFleet's runners by served repo into RunnerGroups,
    label-sorted (stable frame-to-frame); runners keep the producer's
    active-first order within each group. Non-finite cost readings are
    skipped from the Σ (the egress non-finite rule). Bursts bucket by the
    same label — a lingering done burst whose runner already LEFT the fleet
    still gets a group (short-lifecycle visibility must not depend on the
    runner surviving), created runner-less. Qt-free."""
    groups: dict = {}

    def _group_for(repo):
        label = repo_group_label(repo)
        g = groups.get(label)
        if g is None:
            g = groups[label] = RunnerGroup(label=label, repo=repo)
        elif g.repo != repo:
            # Two distinct repos share initials (or a real repo collided
            # with the unattributed bucket): a single-repo tooltip would
            # mis-attribute the merged group — better no claim than a
            # wrong one (review F1).
            g.repo = None
        return g

    for r in fleet.runners:
        g = _group_for(r.repo)
        g.runners.append(r)
        if r.active:
            g.active_count += 1
        if r.cpu_pct is not None and math.isfinite(r.cpu_pct):
            g.cpu_pct_total = (r.cpu_pct if g.cpu_pct_total is None
                               else g.cpu_pct_total + r.cpu_pct)
        if r.mem_mib is not None and math.isfinite(r.mem_mib):
            g.mem_mib_total = (r.mem_mib if g.mem_mib_total is None
                               else g.mem_mib_total + r.mem_mib)
    for b in fleet.bursts:
        g = _group_for(b.repo)
        g.bursts.append(b)
        if b.state == "running":
            g.burst_running += 1
    return [groups[k] for k in sorted(groups)]


def panel_summaries(snap: Snapshot | None) -> dict:
    """One glanceable line per panel for its COLLAPSED section header — the
    header "changes to a summary" instead of hiding everything (maintainer ask,
    2026-07-21). Qt-free and PII-safe (totals, counts, states, product names —
    never session labels or process lists). A panel with nothing trustworthy to
    say is simply absent from the dict, so its header stays a plain title —
    a summary is never fabricated for a panel whose data isn't there. Derived
    per frame from the snapshot alone (no window state), so the window can
    apply it to every section without running the panels' render fns — the
    per-panel update-skipping (#49) is untouched: a collapsed body still skips
    its rebuild; only one header label changes."""
    out: dict = {}
    if snap is None:
        return out
    gpu = snap.gpu
    if gpu:
        out["gpu"] = (f"{gpu.util:.0f}% · "
                      f"{_gg(gpu.mem_used_mib, gpu.mem_total_mib)}")
    # Activity: mirror the tooltip's active/idle judgment (workload names are
    # already public on the tooltip; keep the header to the state + duration).
    if snap.activity is not None:
        # Mirror the body's guard: a junk/missing frame clock must not read
        # as a concrete "active 0s" the expanded panel refuses to claim.
        out["activity"] = (f"active {_dur(snap.t - snap.activity.start_t)}"
                           if snap.t else "active ?")
    else:
        out["activity"] = "idle"
    # Terminal-bad states count too: a collapsed Runs header going PLAIN at the
    # exact frame a run dies would be a dishonest absence (review L2). A frame
    # of only-ok runs stays plain — finished-fine isn't header-worthy.
    run_bits = []
    for run_state in ("running", "stalled", "died", "error"):
        n = sum(1 for r in snap.runs if r.state == run_state)
        if n:
            run_bits.append(f"{n} {run_state}")
    if run_bits:
        out["runs"] = " · ".join(run_bits)
    fc = snap.forecast
    if fc is not None or snap.contributors:
        bits = []
        if fc is not None:
            # Only a REPORTED level is claimed — a --no-forecast frame with a
            # firing contributor must not read "ok".
            s = fc.level
            if fc.level != "ok" and fc.eta_min is not None:
                s += f" ~{fc.eta_min:.0f}m"
            bits.append(s)
        if snap.contributors:
            bits.append(f"{len(snap.contributors)} contributor(s)")
        out["forecast"] = " · ".join(bits)
    # Panel id "host" is the WSL section since the 2026-07-29 regroup —
    # wsl-side bits only.
    host_bits = []
    if snap.host_cpu is not None:
        host_bits.append(f"cpu {snap.host_cpu.pct:.0f}%")
    if snap.host_ram is not None and snap.host_ram.total_mib:
        host_bits.append(
            f"ram {_gg(snap.host_ram.used_mib, snap.host_ram.total_mib)}")
    if host_bits:
        out["host"] = " · ".join(host_bits)
    # Panel id "memory" is the Windows-host section: the whole-box win cpu
    # figure (busy %, Processor Time — absent feeds add no bit, the
    # honest-header rule) leads, then the host-memory gauge.
    mem_bits = []
    if (snap.windows_cpu is not None
            and snap.windows_cpu.time_pct is not None):
        mem_bits.append(f"win {snap.windows_cpu.time_pct:.0f}%")
    win = snap.memory.windows if snap.memory is not None else None
    if win is not None and win.total_mib:
        mem_bits.append(f"{_gg(win.used_mib, win.total_mib)} used")
    if mem_bits:
        out["memory"] = " · ".join(mem_bits)
    procs = snap.processes
    if procs is not None:
        s = f"{procs.claude_total} session(s)"
        if procs.swap is not None and procs.swap.pct is not None:
            s += f" · swap {procs.swap.pct:.0f}%"
        out["sessions"] = s
    st = snap.stuck
    if st is not None:
        # The counts and the wedged census are independent (the contract says
        # counts go None when /proc/stat is unreadable while the D-state sweep
        # still works) — the acute named signal must survive missing queue
        # numbers (review L3).
        bits = []
        if st.procs_running is not None:
            s = f"queue {st.procs_running}"
            if st.ncpu:
                s += f"/{st.ncpu}"
            bits.append(s)
        if st.wedged:
            bits.append(f"{len(st.wedged)} wedged")
        if st.queue_saturated or st.cpu_saturated:
            # The producer's calibrated saturation judgment (producer split;
            # never re-derived here) — worth a word on a collapsed header.
            bits.append("saturated")
        if bits:
            out["stuck"] = " · ".join(bits)
    sc = snap.scanners
    if sc:
        # The hottest non-idle group headlines — a burning one first: the flag
        # is the producer's calibrated judgment (never re-derived here) and
        # the header honors it in EVERY state, like the body rows — a
        # still-flagged row that just flipped to burst-linger must not read
        # benign while the alerts/contributor tiers fire (review L1).
        hot = [r for r in sc if r.state in ("active", "burst")]
        if hot:
            top = max(hot, key=lambda r: (bool(r.burning),
                                          r.state == "active", r.cpu_pct))
            mark = "⚠ " if top.burning else ""
            if top.state == "active":
                out["scanners"] = f"{mark}{top.label} active · {top.cpu_pct:.0f}%"
            else:
                out["scanners"] = f"{mark}{top.label} burst"
        else:
            out["scanners"] = "idle"
    # The producer's whole-stack taxing judgment must not read benign on a
    # collapsed header while the body/alerts warn (the burning-mark rule) —
    # it leads the summary, ahead of the per-group state.
    ss = snap.security_stack
    if ss is not None and ss.taxing:
        stack_txt = (f"⚠ stack {ss.cores:.1f}c" if ss.cores is not None
                     else "⚠ stack taxing")
        out["scanners"] = (f"{stack_txt} · {out['scanners']}"
                           if out.get("scanners") else stack_txt)
    eg = snap.egress
    if eg is not None:
        # The whole-box iface ↑ headlines with its ratio; a reading without
        # the NIC block falls back to the TCP totals (no fabricated ratio);
        # a present-but-rate-less reading says nothing (never "↑—").
        bits = []
        if eg.iface is not None and eg.iface.out_bps is not None:
            bits.append(f"↑{_rate(eg.iface.out_bps)}")
            if (eg.iface.out_in_ratio is not None
                    and math.isfinite(eg.iface.out_in_ratio)):
                bits.append(f"{eg.iface.out_in_ratio:.1f}×")
        elif eg.total_out_bps is not None:
            bits.append(f"↑{_rate(eg.total_out_bps)}")
        if bits:
            out["egress"] = " · ".join(bits)
    rf = snap.runner_fleet
    if rf is not None:
        # Live bursts are header-worthy (the short-lived class the panel
        # exists to show); lingering done ones are body detail only. Counted
        # before the fleet gate: burst evidence outlives the runner, so an
        # EMPTY fleet with a running burst still summarizes (review
        # H-empty-fleet) — while a burst-less empty fleet keeps a plain
        # header (no-data honesty rule; "0 up" reads like an outage).
        running = sum(1 for b in rf.bursts if b.state == "running")
        if rf.runners:
            # Fleet size + busy count; cpu only when it carries real, finite
            # signal (a census with no stats rows must not read "cpu 0%", and
            # a corrupt-feed Infinity must not read "cpu inf%" — the egress
            # non-finite rule). A collapsed header must not claim full fleet
            # knowledge while inspects fail (unresolved bit, review L2).
            s = f"{rf.total} up"
            if rf.active_count:
                s += f" · {rf.active_count} active"
            if rf.unresolved:
                s += f" · {rf.unresolved} unresolved"
            if running:
                s += f" · {running} burst"
            if (rf.cpu_pct_total is not None
                    and math.isfinite(rf.cpu_pct_total)
                    and rf.cpu_pct_total >= 1.0):
                s += f" · cpu {rf.cpu_pct_total:.0f}%"
            out["runners"] = s
        elif running:
            out["runners"] = f"{running} burst"
    if snap.ports:
        active = sum(1 for p in snap.ports if p.active)
        s = f"{len(snap.ports)} watched"
        if active:
            s += f" · {active} active"
        out["ports"] = s
    return out


@dataclass
class MemTier:
    """One Memory-panel tier's availability, for the window to paint.

    ``state`` is MEM_OK / MEM_UNAVAILABLE / MEM_NONE; ``reason`` is a short,
    user-facing, PII-safe explanation shown when the tier is not ``ok``.
    """
    key: str          # windows | wsl | docker | disk
    state: str        # MEM_OK / MEM_UNAVAILABLE / MEM_NONE
    reason: str = ""


def memory_tiers(snap: Snapshot | None) -> list[MemTier] | None:
    """Classify each Memory-panel tier so the window can *say why* a tier is
    blank instead of silently dropping it.

    Returns ``None`` when there is no memory frame yet (the panel shows
    "collecting…"); otherwise one MemTier per tier, in render order. Pure and
    Qt-free so the window just paints the result and the classification is
    unit-tested headless.

    Honesty rules (the app must not over-claim a cause it can't observe):

    * ``windows`` — the producer sets ``memory.windows`` to ``None`` *only* when
      ``get_host_memory()`` couldn't collect it, so absent ⇒ genuinely
      UNAVAILABLE. The specific cause (e.g. a headless PATH with no
      ``powershell.exe``) lives in the log, so the reason points there.
    * ``docker`` / ``disk`` — an empty list is ambiguous between "Docker down"
      and "Docker up but idle". We disambiguate with the top-level ``snap.docker``
      (which the producer sets to ``None`` iff Docker wasn't sampled): no docker
      ⇒ UNAVAILABLE ("not reachable"); docker present but empty ⇒ NONE (a neutral
      "nothing to show", not a warning). ``disk`` stays quiet when Docker is
      already flagged unavailable, to avoid a duplicate line.
    * ``wsl`` — ``ps`` effectively always returns rows on a live distro, so an
      empty list ⇒ UNAVAILABLE.

    Mirrors the tier branches in panel_populate_report() and ui/window.py — keep
    the three in sync.
    """
    if snap is None or snap.memory is None:
        return None
    mem = snap.memory
    docker_reachable = snap.docker is not None
    tiers: list[MemTier] = []

    if mem.windows and mem.windows.total_mib:
        tiers.append(MemTier("windows", MEM_OK))
    else:
        tiers.append(MemTier("windows", MEM_UNAVAILABLE,
                             "producer returned no Windows host data — see the log for why"))

    if mem.wsl:
        tiers.append(MemTier("wsl", MEM_OK))
    else:
        tiers.append(MemTier("wsl", MEM_UNAVAILABLE, "no WSL process list from the producer"))

    if mem.containers:
        tiers.append(MemTier("docker", MEM_OK))
    elif not docker_reachable:
        tiers.append(MemTier("docker", MEM_UNAVAILABLE, "Docker not reachable"))
    else:
        tiers.append(MemTier("docker", MEM_NONE, "no containers using memory"))

    if mem.disk:
        tiers.append(MemTier("disk", MEM_OK))
    elif not docker_reachable:
        tiers.append(MemTier("disk", MEM_NONE))  # docker row already warns; stay quiet
    else:
        tiers.append(MemTier("disk", MEM_NONE, "no Docker disk usage reported"))

    return tiers


# ── memory-panel process filtering ───────────────────────────────────────────────

def split_process_rows(procs, *, query: str = "", min_mib: float = 1024.0):
    """Filter Memory-panel process rows and split them for rendering.

    Applies a case-insensitive name-substring ``query`` (empty/whitespace = no
    filter), then splits the survivors into ``(shown, hidden)``: a row is *shown*
    when its ``mem_mib`` reaches ``min_mib`` or it is an LLM process, otherwise it
    drops into the collapsible ``hidden`` group. An active ``query`` is an explicit
    search, so every match is shown directly and ``hidden`` is empty — a search
    must never re-bury a hit under the size cutoff.

    Pure and Qt-free (the window paints the result). ``min_mib`` generalises the
    historical fixed 1 GiB cutoff into a configurable threshold.
    """
    q = (query or "").strip().lower()
    if q:
        return [p for p in procs if q in p.name.lower()], []
    shown, hidden = [], []
    for p in procs:
        if p.mem_mib >= min_mib or getattr(p, "llm", False):
            shown.append(p)
        else:
            hidden.append(p)
    return shown, hidden


# ── stuck-probe condition flags (the Stuck panel's amber lines) ──────────────────

def stuck_flags(stuck) -> list[str]:
    """User-facing amber lines for the producer's calibrated stuck-probe flags.

    Only the persistence-gated booleans the producer set fire a line — the app
    never re-derives thresholds from the raw numbers (they are measurement, not
    judgment). Wording stays parallel to the producer's ``stuck_alert`` /
    ``cpu_saturation_alert`` pair (the producer split: a merely-busy host no
    longer reads as "stuck" — the queue/idle/sampler stories now carry the
    saturation wording) so the alerts tier and the panel tell one story;
    unlike the one-line alerts (which report only each family's top
    condition), the panel shows every active flag.
    Pure and Qt-free; tolerant of missing numbers (an unreadable count renders
    as ``?``, the flag still shows).
    """
    if stuck is None:
        return []
    flags = []
    busy = (f"{stuck.cpu_pct:.0f}% busy"
            if stuck.cpu_pct is not None else None)
    if stuck.queue_saturated:
        qr = stuck.procs_running if stuck.procs_running is not None else "?"
        ncpu = stuck.ncpu if stuck.ncpu is not None else "?"
        tail = f", {busy}" if busy else ""
        flags.append(f"CPU saturated: {qr} runnable on {ncpu} cores{tail}")
    if stuck.cpu_saturated:
        # busy% travels with the flag on any current producer; the fallback
        # states the judgment without inventing a level ("near-zero idle"
        # would overstate the 85% threshold).
        flags.append(f"CPU saturated — {busy or 'high busy'} sustained")
    if stuck.wait_diverged:
        lpc = (f"{stuck.load1_per_core:.1f}"
               if stuck.load1_per_core is not None else "?")
        flags.append(f"load {lpc}/core while CPU is mostly idle — threads "
                     "stuck in kernel waits")
    if stuck.sampler_slow:
        name, ms = max((stuck.sampler_ms or {"?": 0.0}).items(),
                       key=lambda kv: kv[1])
        flags.append(f"{name} sampler taking {ms / 1000:.1f}s — the host is "
                     "saturated or stalling")
    return flags


# ── memory-search ↔ ports tie-in ─────────────────────────────────────────────────

# The row note shows at most this many PIDs per held port (a "+N" marks the
# rest) — one is the overwhelmingly common case; the note must stay a note.
_MAX_HOLD_PIDS = 2


@dataclass
class PortHold:
    """One watched port a Memory-panel process holds, for the search tie-in.

    ``relation`` is ``"listening"`` when the process is the port's owner and the
    port is listening (the service side), else ``"connected"`` — which covers
    both an attributed connection into the watched port and the producer's
    outbound-watch owner fallback (there the "owner" is the local client of an
    established connection, not a listener). ``pids`` are the distinct PIDs the
    snapshot attributes to the name on this port, worst-first as the producer
    ordered them.
    """
    label: str
    port: int
    relation: str                      # "listening" | "connected"
    pids: list[int] = field(default_factory=list)


def _owner_parts(owner):
    """Split the producer's ``name:pid`` port owner into (name, pid or None).
    Tolerant: a bare name passes through, and a non-numeric tail stays part of
    the name rather than being misread as a PID."""
    if not owner:
        return None, None
    name, sep, pid = owner.rpartition(":")
    if sep and pid.isdigit():
        return name, int(pid)
    return owner, None


def ports_held_by(name, ports) -> list[PortHold]:
    """Watched ports the named process holds — as the port's owner or as an
    attributed connection (the producer proc↔socket mapping).

    Feeds the WSL section's tier search (the tier's home since the
    2026-07-29 regroup): a matched process row can then say which
    watched ports it is on. Matching is EXACT case-insensitive name equality,
    not substring: the memory rows' names and the producer's socket attribution
    both carry the kernel comm (the same 15-char truncation on both sides of
    ``ss``/``ps``), so equality is the honest relation — a substring match
    would tie "node" to every helper process. The result covers only the
    CONFIGURED watch set; an empty list means "no evidence", never "no ports"
    (the caller must not render an empty result as a claim).

    Tier honesty is the caller's job: the producer's socket view (``ss`` inside
    WSL) can never see a Windows host process, so only wsl-tier rows may be
    annotated — a same-named windows-tier row would be a different process on
    the other side of the VM boundary.
    """
    q = (name or "").strip().lower()
    if not q:
        return []
    holds: list[PortHold] = []
    for p in ports or []:
        oname, opid = _owner_parts(p.owner)
        owner_match = oname is not None and oname.lower() == q
        pids: list[int] = []
        if owner_match and opid is not None:
            pids.append(opid)
        conn_match = False
        for c in p.connections:
            if c.process and c.process.lower() == q:
                conn_match = True
                if c.pid is not None and c.pid not in pids:
                    pids.append(c.pid)
        if not owner_match and not conn_match:
            continue
        relation = "listening" if owner_match and p.listening else "connected"
        holds.append(PortHold(label=p.label, port=p.port,
                              relation=relation, pids=pids))
    return holds


def format_port_holds(holds) -> str:
    """The Memory row's dim ports note: ``ports: :22 connected [pid 313]``.

    Empty string when nothing is held — absence of the note is the only honest
    "no ports" statement, since only configured watched ports are visible. The
    ``pid N`` form is deliberately the one ``find_pids`` recognises, so the
    right-click Copy-PID affordance works on the note. A port whose label just
    restates its number collapses to ``:port``.
    """
    bits = []
    for h in holds:
        where = f":{h.port}" if h.label == str(h.port) else f"{h.label} :{h.port}"
        pid_txt = ""
        if h.pids:
            shown = h.pids[:_MAX_HOLD_PIDS]
            extra = len(h.pids) - len(shown)
            pid_txt = (" [" + " · ".join(f"pid {n}" for n in shown)
                       + (f" +{extra}" if extra else "") + "]")
        bits.append(f"{where} {h.relation}{pid_txt}")
    return ("ports: " + " · ".join(bits)) if bits else ""


# ── idle-session stats ───────────────────────────────────────────────────────────

@dataclass
class IdleStats:
    """The Activity panel's idle summary, derived from the run ``history``.

    Times are seconds. ``idle_for_s`` is time since the last run ended (``None``
    when nothing has run yet). ``runs_today`` / ``active_today_s`` cover the runs
    whose *start* falls on the same local calendar day as the snapshot's clock.

    Those counts come from the producer's **bounded** ``history`` list, so when the
    oldest record is itself from today the earlier part of the day may have already
    aged out — ``today_complete`` is then ``False`` and the counts are a lower
    bound (the window renders them with a leading ``≥``).
    """
    idle_for_s: float | None = None
    runs_today: int = 0
    active_today_s: float = 0.0
    today_complete: bool = True


def _local_day_start(t: float) -> float | None:
    """Epoch seconds of local midnight for the day containing ``t`` (``None`` if
    the timestamp can't be interpreted)."""
    try:
        dt = datetime.fromtimestamp(t)
        return dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def idle_stats(snap: Snapshot | None) -> IdleStats | None:
    """Summarise the idle GPU session from a snapshot's run ``history``.

    Returns ``None`` when there's no snapshot and an empty ``IdleStats`` when
    nothing has run yet. Pure and Qt-free so the window just paints the result and
    the derivation is unit-tested headless. Only meaningful while idle — the caller
    guards on ``snap.is_active``.
    """
    if snap is None:
        return None
    hist = snap.history
    if not hist:
        return IdleStats()
    last = hist[-1]
    idle_for = max(0.0, snap.t - last.end_t) if (snap.t and last.end_t) else None
    day_start = _local_day_start(snap.t) if snap.t else None
    if day_start is None:
        # Can't bound "today" — report the idle gap only, leave counts at zero.
        return IdleStats(idle_for_s=idle_for)
    today = [r for r in hist if r.start_t >= day_start]
    # The oldest record predating today proves history reaches back past midnight,
    # so every run today is captured; otherwise earlier runs may have aged out.
    return IdleStats(
        idle_for_s=idle_for,
        runs_today=len(today),
        active_today_s=sum(r.duration_s for r in today),
        today_complete=hist[0].start_t < day_start,
    )


# ── watched-port activity history (the Ports panel's sparkline series) ──────────

# The sparkline's TIME window is a user setting (Settings ▸ "Ports graph window",
# view-state), clamped to this range. The default keeps the historical span:
# ~3 min of shape — enough to tell a quiet port from one whose activity just
# stopped. Time-based (not sample-count) so the span survives a producer-cadence
# change.
PORT_WINDOW_MIN_S = 5.0
PORT_WINDOW_MAX_S = 600.0
DEFAULT_PORT_WINDOW_S = 180.0
# Hard per-port sample cap (the footprint rule: bound everything) — the longest
# window at the producer's fastest cadence (1 s) needs at most this many.
PORT_HISTORY_MAXLEN = 600


def clamp_port_window_s(value) -> float:
    """A ports-graph window coerced to a float in the safe range; garbage → the
    default. Shared by the settings layer and PortHistory (defense in depth)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_PORT_WINDOW_S
    if v != v:                                   # NaN
        return DEFAULT_PORT_WINDOW_S
    return max(PORT_WINDOW_MIN_S, min(PORT_WINDOW_MAX_S, v))


@dataclass
class PortSeries:
    """One port's recent-activity series for the sparkline: ``values`` in
    ``kind`` units — "rate" (total B/s, in+out) when the producer reports byte
    throughput for the port, else "conns" (established-connection count, all the
    Linux ``/proc/net/tcp`` fallback can offer)."""
    kind: str = "rate"
    values: list[float] = field(default_factory=list)


@dataclass
class PortRateStats:
    """Window throughput statistics for one port, from the app-side history.

    The producer's ↓/↑ rates are instantaneous — cumulative counters differenced
    over its ~2 s tick — so bursty traffic (ssh keystrokes, a devtunnel poll)
    reads 0 B/s on nearly every frame (epic #9's "rarely-populated rates"). The
    window mean answers "how busy lately", the peak "how big the bursts get".
    Rate-less samples (a producer's first tick) are unmeasured, not zero — they
    are excluded rather than averaged in. ``span_s`` is the rated samples' real
    frame-clock span, which may be shorter than the configured window while the
    history is still refilling."""
    avg_in: float
    avg_out: float
    peak: float           # busiest single sample, in+out combined (B/s)
    span_s: float


class PortHistory:
    """Rolling per-port activity, accumulated APP-SIDE from successive frames.

    The producer emits point-in-time port readouts only, so a rendered ``0`` is
    ambiguous — idle, or broken? The sparkline needs the recent series, and
    accumulating it here keeps the producer untouched: any producer that emits
    ``ports`` at all gets the graph (a producer-side history field would be
    absent on older services — the drift the stale-producer hint exists for),
    and the producer's own ports.jsonl already keeps the long history.

    Samples are deduped by the snapshot's own clock (``snap.t``): the app can
    poll faster than the producer writes, and a stale poll re-serves the last
    good frame — neither may double-count. Ports absent from a NON-empty frame
    are pruned (the watch set changed); an empty ``ports`` frame keeps every
    series (a producer restart must not wipe them). In-memory by design: view
    data, not view state — a restart starts the shape fresh.

    The series spans ``window_s`` seconds (user-set; ``set_window_s`` re-prunes
    live), measured on the frames' own clocks and dropped from the left on
    append — with ``maxlen`` as the hard sample-count backstop, so the footprint
    stays bounded whatever the window/cadence combination.
    """

    def __init__(self, window_s: float = DEFAULT_PORT_WINDOW_S,
                 maxlen: int = PORT_HISTORY_MAXLEN):
        self._window_s = clamp_port_window_s(window_s)
        self._maxlen = maxlen
        self._last_t: float | None = None
        self._samples: dict[tuple[str, int], deque] = {}

    @property
    def window_s(self) -> float:
        return self._window_s

    def set_window_s(self, window_s) -> None:
        """Adopt a new time window (clamped) and re-prune every series to it —
        shrinking applies immediately; growing refills as new frames arrive."""
        self._window_s = clamp_port_window_s(window_s)
        for dq in self._samples.values():
            self._prune(dq)

    def _prune(self, dq: deque) -> None:
        """Drop samples older than the window, relative to the series' newest
        frame clock (never the wall clock — frames carry their own time)."""
        while len(dq) > 1 and dq[-1][0] - dq[0][0] > self._window_s:
            dq.popleft()

    def update(self, snap: Snapshot | None) -> None:
        """Feed one rendered frame (once per repaint; duplicates are safe)."""
        if snap is None or snap.t == self._last_t:
            return
        self._last_t = snap.t
        if not snap.ports:
            return
        keys = set()
        for p in snap.ports:
            key = (p.label, p.port)
            keys.add(key)
            # Per-direction, None-preserving: a missing rate is "unmeasured"
            # (first producer tick / conns-only fallback), never a zero.
            rate_in = None if p.bytes_in_rate is None else float(p.bytes_in_rate)
            rate_out = None if p.bytes_out_rate is None else float(p.bytes_out_rate)
            dq = self._samples.get(key)
            if dq is None:
                dq = self._samples[key] = deque(maxlen=self._maxlen)
            dq.append((float(snap.t or 0.0), rate_in, rate_out, float(p.conns)))
            self._prune(dq)
        for gone in set(self._samples) - keys:
            del self._samples[gone]

    @staticmethod
    def _total(rate_in, rate_out):
        """Combined B/s of one sample, or None when neither direction was
        measured (one-sided readings count, the missing side as 0)."""
        if rate_in is None and rate_out is None:
            return None
        return (rate_in or 0.0) + (rate_out or 0.0)

    def series(self, label: str, port: int) -> PortSeries:
        """The plottable series for one port: throughput when any sample has it
        (rate-less samples plot as 0), else the connection count."""
        samples = self._samples.get((label, port))
        if not samples:
            return PortSeries(values=[])
        totals = [self._total(ri, ro) for _, ri, ro, _ in samples]
        if any(t is not None for t in totals):
            return PortSeries("rate", [t if t is not None else 0.0
                                       for t in totals])
        return PortSeries("conns", [conns for _, _, _, conns in samples])

    def rate_stats(self, label: str, port: int) -> PortRateStats | None:
        """Window ↓/↑ statistics for one port, or None when the history holds
        fewer than two rated samples (one sample is a point, not a window — the
        row falls back to the live instantaneous readout)."""
        samples = self._samples.get((label, port))
        if not samples:
            return None
        rated = [(t, ri or 0.0, ro or 0.0) for t, ri, ro, _ in samples
                 if ri is not None or ro is not None]
        if len(rated) < 2:
            return None
        n = len(rated)
        return PortRateStats(
            avg_in=sum(ri for _, ri, _ in rated) / n,
            avg_out=sum(ro for _, _, ro in rated) / n,
            peak=max(ri + ro for _, ri, ro in rated),
            span_s=rated[-1][0] - rated[0][0],
        )


# ── watched-port recent clients (the Ports panel's "clients:" sub-line) ─────────

# Distinct clients remembered per port (the footprint rule: bound everything).
# Eviction is oldest-last-seen, so a chatty port keeps its most recent hitters.
PORT_CLIENTS_MAX = 32
# How many client names the sub-line spells out; the rest fold into "+N more".
PORT_CLIENTS_NOTE_NAMES = 4


def _peer_host(peer: str) -> str:
    """The host part of a connection's display peer ('host:port', v6
    '[a:b::c]:port') — the client's ephemeral port is identity noise, so a
    host-only form is what a peer-keyed client dedups on."""
    host, sep, _ = (peer or "").rpartition(":")
    if not sep:
        return peer or ""
    return host.strip("[]")


@dataclass
class PortClient:
    """One distinct recent client of a watched port: the connecting process
    (``process``/``pid``) when the producer could see its socket, else the
    far-end host (``peer_host``). ``current`` means it appeared in the port's
    newest frame; a departed client carries how long ago it was last seen
    (frame-clock seconds)."""
    process: str | None = None
    pid: int | None = None
    peer_host: str | None = None
    last_seen_ago_s: float = 0.0
    current: bool = False

    @property
    def name(self) -> str:
        if self.process:
            return (f"{self.process}[{self.pid}]" if self.pid is not None
                    else self.process)
        return self.peer_host or "?"


class PortClients:
    """Rolling distinct-recent-clients memory per watched port, accumulated
    APP-SIDE from the bounded ``connections[]`` detail of successive frames —
    the "WHO hits this port" answer, readable without expanding the dropdown.

    The connection list only shows clients connected at the instant of the
    producer's ~2 s sample; a hitter that connects and leaves between frames
    vanishes without trace. Remembering every distinct client seen over the
    ports-graph window keeps short-lived hitters visible (with a last-seen
    age) — renderer-only, over data the feed already carries.

    Identity is the connecting process ``(process, pid)`` when visible, else
    the far-end HOST — never host:port, or every connection's ephemeral client
    port would mint a fresh "client". Frame discipline mirrors
    :class:`PortHistory`: samples dedupe by the snapshot's own clock, ports
    absent from a NON-empty frame are dropped (the watch set changed), an
    empty ``ports`` frame keeps every map (a producer restart must not wipe
    them), and ages are measured on the frames' clocks, never the wall clock.
    Bounded per port (``max_clients``, oldest evicted) and pruned to the
    window. Sees only the producer's bounded top-by-bytes connection list, so
    it is a floor, not a census.
    """

    def __init__(self, window_s: float = DEFAULT_PORT_WINDOW_S,
                 max_clients: int = PORT_CLIENTS_MAX):
        self._window_s = clamp_port_window_s(window_s)
        self._max = max_clients
        self._last_t: float | None = None
        # (label, port) -> {client_key: (last_seen_t, (process, pid, peer_host))}
        self._clients: dict = {}
        # (label, port) -> the client_keys present in that port's newest frame
        self._current: dict = {}

    @staticmethod
    def _identity(conn):
        """(dedup key, display fields) for one connection row, or (None, None)
        for a row with nothing to identify a client by."""
        if conn.process:
            return (("proc", conn.process, conn.pid),
                    (conn.process, conn.pid, None))
        host = _peer_host(conn.peer)
        if host and host != "?":
            return ("peer", host), (None, None, host)
        return None, None

    def set_window_s(self, window_s) -> None:
        """Adopt a new time window (clamped) and re-prune every port's map to
        it, relative to that map's own newest sighting."""
        self._window_s = clamp_port_window_s(window_s)
        for m in self._clients.values():
            if not m:
                continue
            newest = max(ts for ts, _ in m.values())
            for ck in [k for k, (ts, _) in m.items()
                       if newest - ts > self._window_s]:
                del m[ck]

    def update(self, snap: Snapshot | None) -> None:
        """Feed one rendered frame (once per repaint; duplicates are safe)."""
        if snap is None or snap.t == self._last_t:
            return
        self._last_t = snap.t
        if not snap.ports:
            return
        t = float(snap.t or 0.0)
        keys = set()
        for p in snap.ports:
            key = (p.label, p.port)
            keys.add(key)
            m = self._clients.setdefault(key, {})
            cur = set()
            for c in p.connections:
                ck, fields = self._identity(c)
                if ck is None:
                    continue
                m[ck] = (t, fields)
                cur.add(ck)
            self._current[key] = cur
            for ck in [k for k, (ts, _) in m.items()
                       if t - ts > self._window_s]:
                del m[ck]
            while len(m) > self._max:
                del m[min(m, key=lambda k: m[k][0])]
        for gone in set(self._clients) - keys:
            del self._clients[gone]
            self._current.pop(gone, None)

    def clients(self, label: str, port: int) -> list[PortClient]:
        """The port's distinct recent clients: current ones first, then by
        recency, attributed processes ahead of peer-only hosts on ties (the
        process is the more actionable "who"). Ages are relative to the
        newest fed frame; entries the advancing clock has pushed past the
        window (empty frames don't prune) are filtered here so the display
        invariant holds regardless."""
        m = self._clients.get((label, port))
        if not m:
            return []
        now = self._last_t or 0.0
        cur = self._current.get((label, port), set())
        out = []
        for ck, (ts, (proc, pid, host)) in m.items():
            age = max(0.0, now - ts)
            if age > self._window_s:
                continue
            out.append(PortClient(process=proc, pid=pid, peer_host=host,
                                  last_seen_ago_s=age, current=ck in cur))
        out.sort(key=lambda c: (not c.current, c.last_seen_ago_s,
                                c.process is None, c.name))
        return out


def _fmt_client_age(seconds: float) -> str:
    """A compact last-seen age for the clients sub-line. The ports window is
    clamped to ≤10 min, so seconds/minutes cover the whole range."""
    s = max(0, int(round(seconds)))
    return f"{s}s" if s < 60 else f"{s // 60}m"


def clients_note(clients, max_names: int = PORT_CLIENTS_NOTE_NAMES) -> str:
    """The Ports panel's compact per-port sub-line: WHO hit the port recently
    — distinct connecting processes/hosts over the graph window, current ones
    first, departed ones tagged with their last-seen age. "" when nothing has
    connected in the window (the row stays single-line). Qt-free; the window
    just paints it."""
    if not clients:
        return ""
    bits = []
    for c in clients[:max_names]:
        tag = "" if c.current else f" ({_fmt_client_age(c.last_seen_ago_s)} ago)"
        bits.append(f"{c.name}{tag}")
    more = len(clients) - max_names
    tail = f"  +{more} more" if more > 0 else ""
    return "clients: " + "  ·  ".join(bits) + tail


def bucket_means(values, n_buckets: int) -> list[float]:
    """Downsample a series by averaging into at most ``n_buckets`` equal runs.

    A long window holds far more samples than the sparkline has pixels; plotting
    them raw turns a short burst into a sub-pixel 1-tick spike. Averaging each
    bucket makes a burst read as visible area (its bucket's mean) while a
    sustained plateau keeps its height. Series at or under the budget pass
    through unchanged — bucketing must never *reduce* detail the widget could
    have drawn."""
    vals = [float(v) for v in values]
    if n_buckets <= 0 or len(vals) <= n_buckets:
        return vals
    out = []
    n = len(vals)
    for i in range(n_buckets):
        lo, hi = i * n // n_buckets, (i + 1) * n // n_buckets
        chunk = vals[lo:hi]
        if chunk:  # unreachable when n > n_buckets, kept as a guard
            out.append(sum(chunk) / len(chunk))
    return out


def scale_sparkline(values) -> list[float]:
    """Scale a non-negative series to the 0..100 range Sparkline paints,
    preserving shape (the peak maps to 100). An all-zero series — a genuinely
    quiet port — stays a flat floor line instead of dividing by zero."""
    vals = [max(0.0, float(v)) for v in values]
    peak = max(vals, default=0.0)
    if peak <= 0:
        return [0.0] * len(vals)
    return [v / peak * 100.0 for v in vals]


# ── toast / notification edge detection ─────────────────────────────────────────

@dataclass
class Toast:
    title: str
    body: str
    kind: str = "info"   # info / success / warning


def _fmt_duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


class ToastTracker:
    """Emits toasts on GPU run start/end and on producer connectivity changes.

    Fed one ``ReadResult`` per poll; returns the toasts to show for that frame.
    Run identity is the activity ``start_t`` so a new run right after another is
    reported as end-then-start rather than swallowed.
    """

    def __init__(self):
        self._run_start_t: float | None = None   # start_t of the run we last saw open
        self._seen_stale = False
        self._seen_any = False

    def update(self, result: ReadResult) -> list[Toast]:
        toasts: list[Toast] = []
        snap = result.snapshot

        # Connectivity edges.
        if result.stale or snap is None:
            if self._seen_any and not self._seen_stale:
                toasts.append(Toast("Monitor disconnected",
                                    "No fresh snapshots from the recorder.",
                                    kind="warning"))
            self._seen_stale = True
            return toasts
        if self._seen_stale:
            toasts.append(Toast("Monitor reconnected",
                                "Receiving live snapshots again.", kind="info"))
        self._seen_stale = False
        self._seen_any = True

        cur_start = snap.activity.start_t if snap.activity else None

        if cur_start == self._run_start_t:
            return toasts  # no run-boundary change

        # A run we were tracking has ended (activity gone, or replaced).
        if self._run_start_t is not None:
            toasts.append(self._ended_toast(snap))
        # A new run has opened.
        if cur_start is not None and snap.activity is not None:
            wl = ", ".join(snap.activity.workloads)
            toasts.append(Toast("GPU activity started",
                                wl or "GPU is now in use.", kind="info"))
        self._run_start_t = cur_start
        return toasts

    def _ended_toast(self, snap: Snapshot) -> Toast:
        # The just-closed run is the newest history record, if the producer
        # included it; otherwise fall back to a bare message.
        last = snap.history[-1] if snap.history else None
        if last is not None:
            body = (f"Ran {_fmt_duration(last.duration_s)}, "
                    f"peak {last.peak_util:.0f}% util / "
                    f"{last.peak_vram_mib/1024:.1f}G VRAM")
        else:
            body = "GPU is idle."
        return Toast("GPU idle", body, kind="success")


# ── stale-producer (config not applied) detection ────────────────────────────────

# The producer re-reads its --config within ~5 s, so a snapshot that still
# contradicts the written config after this
# window means the file isn't being read at all — not "hasn't noticed yet".
PRODUCER_CONFIG_GRACE_S = 30.0
# A config-honouring producer echoes the configured gpu cadence exactly (both
# sides clamp identically); anything past this slack is a real disagreement.
_CADENCE_TOLERANCE_S = 0.25


@dataclass
class ProducerConfigHint:
    """A user-facing notice that the producer is ignoring the app's config file.

    ``ports_stale`` marks the watched-ports part as the (or a) contradicted
    setting, so the Ports panel's empty-state can say "configured but not
    reported" instead of the misleading "no ports watched".
    """
    text: str
    ports_stale: bool = False


class ProducerConfigTracker:
    """Detects a producer that is not applying the app's monitor-config file.

    The app writes monitor-config:v1 (sample cadence + watched ports); a
    producer launched with ``--config`` re-reads it within seconds and its
    snapshot then reflects it — one ``ports`` entry per watched spec (even for
    an idle port; the producer emits a record per spec unconditionally) and
    ``interval`` equal to the configured gpu cadence. A producer service
    installed before the ``--config`` wiring silently ignores the file, and the
    only symptom is that contradiction — which this tracker turns into a hint
    instead of letting the Ports panel claim "no ports watched" (obs #343).

    Fed once per poll like ToastTracker; pure and Qt-free (``now`` injectable).
    ``engaged`` is whether the config file exists at all — without it the
    producer owes us nothing. Only FRESH frames count as evidence: a stale or
    missing frame neither raises nor clears the hint. A contradiction must
    persist past ``grace_s`` before the hint fires, so a just-applied Settings
    change (producer re-read ~5 s) never flickers it.
    """

    def __init__(self, grace_s=PRODUCER_CONFIG_GRACE_S, now=time.monotonic):
        self._grace_s = grace_s
        self._now = now
        self._mismatch_since: float | None = None
        self._hint: ProducerConfigHint | None = None

    def reset(self) -> None:
        """Restart the evidence clock — call when the app (re)writes the config
        file or repoints at a different snapshot/producer, so frames from the
        old state don't count against the new one."""
        self._mismatch_since = None
        self._hint = None

    def update(self, result: ReadResult, *, engaged: bool, watch_ports,
               gpu_interval_s) -> ProducerConfigHint | None:
        """Feed one poll; returns the CURRENT hint (None = producer looks fine)."""
        if not engaged:
            self.reset()
            return None
        snap = result.snapshot
        if snap is None or result.stale:
            return self._hint          # no fresh evidence either way — hold
        ports_stale = bool(watch_ports) and not snap.ports
        cadence_stale = (gpu_interval_s is not None
                         and abs(snap.interval - gpu_interval_s)
                         > _CADENCE_TOLERANCE_S)
        if not ports_stale and not cadence_stale:
            self.reset()
            return None
        now = self._now()
        if self._mismatch_since is None:
            self._mismatch_since = now
        if now - self._mismatch_since < self._grace_s:
            return self._hint          # inside grace: hold whatever we last said
        self._hint = ProducerConfigHint(
            text=self._text(ports_stale, cadence_stale, watch_ports,
                            gpu_interval_s, snap.interval),
            ports_stale=ports_stale)
        return self._hint

    @staticmethod
    def _text(ports_stale, cadence_stale, watch_ports, want_s, got_s) -> str:
        if ports_stale and cadence_stale:
            what = (f"{len(watch_ports)} watched port(s) and the {want_s:g}s "
                    "sample cadence are set, but the producer applies neither")
        elif ports_stale:
            what = (f"{len(watch_ports)} watched port(s) are set, but the "
                    "producer reports none")
        else:
            what = (f"sample cadence is set to {want_s:g}s, but the producer "
                    f"runs at {got_s:g}s")
        return (f"⚠ {what}. Check the producer documentation and restart it "
                "if your chosen producer supports these settings.")


# ── VRAM holders (producer top-level `gpu_users`; the "WHAT is loading into ────
# the VRAM" note on the Activity panel). Pure formatting — the producer already
# bounded and sorted the list (worst-first, >= 64 MiB floor, cap 8).

GPU_USERS_SHOWN = 3


def _fmt_vram(user) -> str:
    """One holder as ``name[pid] N.NG`` — the bracket-pid row form find_pids
    matches, so the right-click Copy-PID menu works on the rendered line."""
    if user.vram_mib >= 1024:
        return f"{user.name}[{user.pid}] {user.vram_mib / 1024.0:.1f}G"
    return f"{user.name}[{user.pid}] {user.vram_mib:.0f}M"


def format_gpu_users(users, shown=GPU_USERS_SHOWN) -> tuple[str, str]:
    """(line, tooltip) for a NON-EMPTY holders list: the top `shown` entries
    ``·``-joined with a ``+N more`` remainder; the tooltip carries every entry
    (the line may clip under wordwrap — the tooltip never does). Rows carrying
    no information (junk-coerced: pid 0 AND no VRAM figure) are dropped — they
    would spend visible cap slots and offer a dead "Copy PID 0"; ("", "") when
    nothing informative remains (render nothing — a filtered-empty list is NOT
    the producer's [] and must not fall through to the unattributed hint)."""
    users = [u for u in users if u.pid or u.vram_mib > 0]
    if not users:
        return "", ""
    entries = [_fmt_vram(u) for u in users]
    line = "vram: " + " · ".join(entries[:shown])
    if len(entries) > shown:
        line += f" · +{len(entries) - shown} more"
    return line, "\n".join(entries)


def gpu_users_gap_hint(gpu_users) -> str:
    """The honest empty-state for an ACTIVE frame with no workload names: WHY
    the app can't say what's on the GPU. None-vs-[] separates producer
    capability from a genuinely Windows-quiet card: with the counters answering
    and no GPU container in workloads, the remainder is a bare-WSL host
    process, which nothing reachable from the producer can name (root-only)."""
    if gpu_users is None:
        return ("unattributed — this producer doesn't report VRAM holders "
                "(older build or GPU counters unavailable)")
    # Two-line budget at the 440 px default — a third wrapped line clips the
    # section for a frame (screenshot-verified), so the wording stays ≤ ~120
    # chars. "reported" and "not-yet-seen" keep it honest: the app only knows
    # nothing was REPORTED — a container inside the docker-sweep window is
    # still possible (over-claim rule).
    return ("unattributed — no GPU container reported, no Windows VRAM holder "
            "≥64M; likely WSL-side or a not-yet-seen container")


# ── process→ports target rows (the Ports panel's "targeted" block) ───────────────

PROC_TARGET_CAP = 4          # mirrors the producer's PROC_PORTS_TARGET_CAP
_PROC_SOCKETS_SHOWN = 3      # sockets spelled onto the row; the rest ride the tooltip
# How long a target may read "querying…" before the placeholder degrades to a
# stated cause: config re-read (~5 s) + host sweep (~10 s) + slack. Past it,
# the honest reading is that this producer will never answer (predates the
# proc_ports field, or runs without --config) — a permanent "querying…"
# would be the same over-claim the watched-port empty state fixed (obs #343).
PROC_TARGET_GRACE_S = 30.0


def _fmt_proc_socket(s) -> str:
    """One socket, compact: a listener/bound socket reads ``listen :PORT``
    (the port is the story); a connected one reads ``LOCAL→PEER``."""
    if s.peer is None:
        port = s.local.rpartition(":")[2] or s.local
        verb = "listen" if s.state.lower() == "listen" else "bound"
        return f"{verb} :{port}" + ("/udp" if s.proto == "udp" else "")
    return f"{s.local}→{s.peer}"


def format_proc_ports(rec) -> tuple[str, str]:
    """(line, tooltip) for one ProcPortRecord — the Ports panel's targeted-
    process row. The line leads with the ``name[pid]`` bracket form find_pids
    matches (Copy PID works), then either the stated cause (pending / not
    found / nothing visible — the note is the producer's own wording) or the
    first few sockets with a ``+N more`` remainder. The tooltip carries every
    socket plus the note, one per line, so the row may clip but never lie."""
    who = f"{rec.name or '?'}[{rec.pid}]"
    side = rec.side if rec.side in ("wsl", "windows") else "?"
    head = f"{who} ({side})"
    if rec.sockets is None:
        # A definitive not-found is an ANSWER — it must not read as pending.
        if rec.found is False:
            cause = rec.note or "process not found"
        else:
            cause = rec.note or "no answer yet"
        return f"{head} — {cause}", f"{head}\n{cause}"
    entries = [_fmt_proc_socket(s) for s in rec.sockets]
    total = rec.sockets_total if rec.sockets_total is not None else len(entries)
    if not entries:
        cause = rec.note or "no sockets visible"
        return f"{head} — {cause}", f"{head}\n{cause}"
    line = f"{head}  ·  " + " · ".join(entries[:_PROC_SOCKETS_SHOWN])
    more = total - min(len(entries), _PROC_SOCKETS_SHOWN)
    if more > 0:
        line += f" · +{more} more"
    if rec.note:
        # A note beside visible sockets (pid-reuse being the loaded case —
        # the sockets may belong to the wrong process) must be seen, not
        # tooltip-only: mark the line, spell it in the tooltip.
        line += "  ⚠"
    tip = "\n".join([head] + entries
                    + ([f"(+{total - len(entries)} beyond the producer's bound)"]
                       if total > len(entries) else [])
                    + ([f"⚠ {rec.note}"] if rec.note else []))
    return line, tip


def proc_target_key(t) -> tuple:
    """Identity key for one watch_procs target dict: ('pid', side, pid) for
    pid targets, ('name', side, lowered-name) for name targets — the app's
    adoption-time map and the answer matching key on both. All-digits names
    are rejected at entry, so the two spaces can't collide."""
    if isinstance(t, dict) and t.get("pid") is not None:
        return ("pid", t.get("side"), t.get("pid"))
    name = (t.get("name") or "") if isinstance(t, dict) else ""
    return ("name", t.get("side") if isinstance(t, dict) else None,
            name.strip().lower())


def format_proc_name_ports(rec) -> tuple[str, str, list[tuple[str, str]]]:
    """(header_line, header_tooltip, [(match_line, match_tooltip)]) for one
    NAME-target ProcPortRecord — the Ports panel's targeted block renders the
    header then one indented row per match. The header states the pattern,
    side and match count (true count; "top N shown" when bounded), or the
    stated cause (pending / failed / no match — the producer's own wording);
    each match row leads with the ``name[pid]`` bracket form find_pids
    matches (Copy PID works) followed by its sockets, exactly like a pid
    target's row. The rows may clip but never lie: full socket lists ride
    the tooltips."""
    side = rec.side if rec.side in ("wsl", "windows") else "?"
    head = f"◎ {rec.name or '?'} ({side})"
    if rec.matches is None:
        cause = rec.note or "no answer yet"
        line = f"{head} — {cause}"
        return line, f"{head}\n{cause}", []
    total = rec.match_total if rec.match_total is not None else len(rec.matches)
    if not rec.matches:
        cause = rec.note or "no matching process"
        line = f"{head} — {cause}"
        return line, f"{head}\n{cause}", []
    line = (f"{head} — {total} matching "
            f"process{'es' if total != 1 else ''}")
    if total > len(rec.matches):
        line += f" (top {len(rec.matches)} shown)"
    tip_lines = [line]
    if rec.note:
        line += "  ⚠"
        tip_lines.append(f"⚠ {rec.note}")
    rows = []
    for m in rec.matches:
        who = f"{m.name or '?'}[{m.pid}]"
        entries = [_fmt_proc_socket(s) for s in m.sockets]
        mtotal = (m.sockets_total if m.sockets_total is not None
                  else len(entries))
        if not entries:
            # "visible", like the pid row: on windows an answered-empty match
            # means the sweep attributed nothing, not that nothing exists.
            mline = f"{who} — no sockets visible"
            mtip = f"{who}\nno sockets visible"
        else:
            mline = f"{who}  ·  " + " · ".join(entries[:_PROC_SOCKETS_SHOWN])
            more = mtotal - min(len(entries), _PROC_SOCKETS_SHOWN)
            if more > 0:
                mline += f" · +{more} more"
            mtip = "\n".join(
                [who] + entries
                + ([f"(+{mtotal - len(entries)} beyond the producer's bound)"]
                   if mtotal > len(entries) else []))
        rows.append((mline, mtip))
    return line, "\n".join(tip_lines), rows


# PIDs as the app renders them: the producer's alert/contributor phrasing
# ("… [PID 3026899] wedged …") and the app's own bracket-suffix row form
# ("devtunnel[313]"). Kept intentionally narrow — bare numbers never match, so
# byte counts / ports / percentages can't be offered as a "PID".
_PID_RE = re.compile(r"(?:\bPID\s+(\d+)\b|\[(\d+)\])", re.IGNORECASE)


def proc_name_for_pid(text, pid) -> str | None:
    """The name a rendered line pairs with ``pid`` in the row form
    ``name[pid]`` — best-effort, for seeding a Show-ports target's requested
    name (the producer resolves the LIVE name regardless; this only feeds its
    pid-reuse check and the querying placeholder). None when the line mentions
    the pid another way ("[PID 1234]") or the pairing is ambiguous. Separators
    (``·``), colons and brackets bound the name so a line prefix like
    ``vram:`` can't be swallowed into it, and the app's row-decoration glyphs
    are stripped — "⚠ comm" fed to the pid-reuse check would guarantee a
    false mismatch (review H1)."""
    m = re.search(rf"([^·:\[\]]+)\[{int(pid)}\]", text or "")
    name = m.group(1).strip("⚠●○↳▸▾ \t") if m else None
    return name or None


def find_pids(text: str) -> list[int]:
    """Distinct PIDs mentioned in one rendered line, in order of appearance.

    Feeds the right-click "Copy PID" menu: the panels rebuild on every advancing
    frame, which wipes any mouse selection, so a selection-free copy affordance
    is the reliable way to get a PID out of the app (epic #9 maintainer request).
    """
    out: list[int] = []
    for m in _PID_RE.finditer(text or ""):
        pid = int(m.group(1) or m.group(2))
        if pid not in out:
            out.append(pid)
    return out


# ── self-cost: the monitor billing its own CPU ────────────────────────────────────

SELF_CPU_WINDOW_S = 60.0    # rolling window the tray's own pct is read over
SELF_CPU_MIN_SPAN_S = 10.0  # report nothing until the window spans this


def _tray_cpu_s():
    # time.process_time, not getrusage: the resource module does not exist on
    # native Windows (the frozen exe), and process_time reads the same
    # user+sys total there via GetProcessTimes.
    return time.process_time()


class SelfCpuWindow:
    """The tray's own CPU as % of one core over a rolling window
    (``time.process_time`` deltas) — the app-side half of the producer's
    ``self_cost`` bill; the snapshot flow is one-way, so the tray measures
    itself. ``update(now)`` returns the current pct, or None while warming
    (the first call baselines; then until the window spans ``min_span_s``).
    Injectable ``usage_fn`` for tests; fail-open (a failed read keeps the
    last reading)."""

    def __init__(self, window_s=SELF_CPU_WINDOW_S,
                 min_span_s=SELF_CPU_MIN_SPAN_S, usage_fn=_tray_cpu_s):
        self.window_s = window_s
        self.min_span_s = min_span_s
        self._usage_fn = usage_fn
        self._prev = None        # (t, cumulative cpu_s)
        self._window = deque()   # (dt, cpu_s burned in dt)
        self._result = None

    def update(self, now):
        try:
            usage = self._usage_fn()
        except Exception:
            return self._result
        if self._prev is None:
            self._prev = (now, usage)
            return self._result
        prev_t, prev_u = self._prev
        dt = now - prev_t
        if dt <= 0:
            return self._result
        if dt > self.window_s:
            # A sleep/resume gap (or a UI stall longer than the whole
            # window): one window-length ~0-CPU entry would dilute the
            # reading toward 0 for the next minute while still labelled as
            # a live window — re-baseline to honest warming instead (the
            # ScannerWatch sleep-gap re-edge, app-side; review L1).
            self._prev = (now, usage)
            self._window.clear()
            self._result = None
            return self._result
        self._prev = (now, usage)
        self._window.append((dt, max(0.0, usage - prev_u)))
        while (len(self._window) > 1
               and (sum(e[0] for e in self._window) - self._window[0][0]
                    >= self.window_s)):
            self._window.popleft()
        span = sum(e[0] for e in self._window)
        if span < self.min_span_s:
            return self._result
        self._result = round(
            sum(e[1] for e in self._window) / span * 100.0, 2)
        return self._result


def selfcost_note(sc, tray_pct=None) -> str:
    """The WSL section's dim "monitor:" line — what the monitoring stack
    itself costs, every number exact (the producer's ``self_cost`` bill +
    the tray's own ``SelfCpuWindow`` reading). Qt-free for testability.

    ``sc`` None (older producer / warming / a --once feed) → ``monitor: —``.
    A reading whose ``windows_pct`` is None states the scope (``win
    unmeasured``) instead of implying a measured zero; a warming tray reads
    ``tray —``. The headline sums producer + tray."""
    if sc is None:
        return "monitor: —"
    total = sc.total_pct + (tray_pct or 0.0)
    parts = []
    if sc.windows_pct is not None:
        parts.append(f"win {sc.windows_pct:.1f}")
    parts.append(f"spawns {sc.spawns_pct:.1f}")
    parts.append(f"py {sc.python_pct:.1f}")
    parts.append(f"tray {tray_pct:.1f}" if tray_pct is not None else "tray —")
    if sc.windows_pct is None:
        parts.append("win unmeasured")
    return f"monitor: {total:.1f}% of a core — " + " · ".join(parts)
