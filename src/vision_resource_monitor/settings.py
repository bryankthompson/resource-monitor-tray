"""Persisted user settings + the precedence rule that merges them with CLI flags.

The ``Config`` dataclass and ``resolve_config`` are **Qt-free** and unit-testable:
they decide the effective configuration from three layers, highest priority
first —

1. an explicit **CLI flag**,
2. a persisted **setting**, then
3. the built-in **default**.

Persistence uses Qt's ``QSettings`` (the registry on Windows, an ini/plist file
elsewhere) and is imported lazily, so importing this module stays lightweight and
the pure resolution logic runs on a bare interpreter.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .logging_setup import resolve_log_level
from .reader import DEFAULT_STALE_AFTER_S

ORG = "vision-resource-monitor"
APP = "vision-resource-monitor"

KEY_SNAPSHOT = "snapshot_path"
KEY_INTERVAL = "interval"
KEY_STALE_AFTER = "stale_after"
KEY_THEME = "theme"          # "dark" | "light"
KEY_SHOW_HOST = "show_host"  # bool
KEY_SHOW_MEM = "show_mem"    # bool
KEY_LOG_LEVEL = "log_level"  # DEBUG | INFO | WARNING | ERROR
KEY_MUTE = "mute_notifications"  # bool — suppress tray notification pop-ups
KEY_LOG_FILE = "log_file"  # str — activity-log path; "" derives it from the snapshot folder

# Built-in defaults (the lowest-priority layer). ``snapshot_path=""`` means
# "derive from $VRM_SNAPSHOT_PATH / LOCALAPPDATA" at read time.
DEFAULTS = {
    KEY_SNAPSHOT: "",
    KEY_LOG_FILE: "",
    KEY_INTERVAL: 2.0,
    KEY_STALE_AFTER: DEFAULT_STALE_AFTER_S,
    KEY_THEME: "dark",
    KEY_SHOW_HOST: True,
    KEY_SHOW_MEM: True,
    KEY_LOG_LEVEL: "INFO",
    KEY_MUTE: False,
}

# The settings the dialog persists (kept in intended display order).
SETTINGS_KEYS = (KEY_LOG_LEVEL, KEY_THEME, KEY_SHOW_HOST, KEY_SHOW_MEM,
                 KEY_MUTE, KEY_INTERVAL, KEY_SNAPSHOT, KEY_LOG_FILE,
                 KEY_STALE_AFTER)


@dataclass
class Config:
    snapshot_path: str
    log_file: str
    interval: float
    stale_after: float
    theme: str
    show_host: bool
    show_mem: bool
    mute_notifications: bool
    log_level: str

    @property
    def dark(self) -> bool:
        return str(self.theme).lower() != "light"


def _as_bool(value, default=False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return default


def _as_float(value, default) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def resolve_config(cli, stored) -> Config:
    """Merge the three layers into an effective ``Config``.

    ``cli`` and ``stored`` are dicts keyed by the ``KEY_*`` names. In ``cli`` a
    value of ``None`` means "flag not given" (falls through to the next layer);
    ``stored`` simply omits keys the user never set. Log level additionally
    consults ``$VRM_LOG_LEVEL`` (below CLI + stored) and is normalised to a
    canonical level name.
    """
    cli = cli or {}
    stored = stored or {}

    def pick(key):
        cv = cli.get(key)
        if cv is not None:
            return cv
        sv = stored.get(key)
        if sv is not None:
            return sv
        return DEFAULTS[key]

    # CLI > stored > $VRM_LOG_LEVEL > INFO (resolve_log_level walks value→env→INFO).
    level_int = resolve_log_level(cli.get(KEY_LOG_LEVEL) or stored.get(KEY_LOG_LEVEL))

    return Config(
        snapshot_path=str(pick(KEY_SNAPSHOT) or ""),
        log_file=str(pick(KEY_LOG_FILE) or ""),
        interval=_as_float(pick(KEY_INTERVAL), DEFAULTS[KEY_INTERVAL]),
        stale_after=_as_float(pick(KEY_STALE_AFTER), DEFAULTS[KEY_STALE_AFTER]),
        theme="light" if str(pick(KEY_THEME)).lower() == "light" else "dark",
        show_host=_as_bool(pick(KEY_SHOW_HOST), DEFAULTS[KEY_SHOW_HOST]),
        show_mem=_as_bool(pick(KEY_SHOW_MEM), DEFAULTS[KEY_SHOW_MEM]),
        mute_notifications=_as_bool(pick(KEY_MUTE), DEFAULTS[KEY_MUTE]),
        log_level=logging.getLevelName(level_int),
    )


# ── persistence (Qt, imported lazily) ────────────────────────────────────────────

def _qsettings(path=None):
    from PySide6.QtCore import QSettings
    if path is not None:
        return QSettings(str(path), QSettings.IniFormat)
    return QSettings(ORG, APP)


def load_stored(path=None) -> dict:
    """Read persisted settings into a typed dict (only keys actually present).

    Returns ``{}`` when nothing is stored. Values are coerced to their expected
    types so a string-based backend (ini) round-trips cleanly.
    """
    s = _qsettings(path)
    out = {}
    for key in DEFAULTS:
        if s.contains(key):
            out[key] = s.value(key)
    for key in (KEY_INTERVAL, KEY_STALE_AFTER):
        if key in out:
            out[key] = _as_float(out[key], DEFAULTS[key])
    for key in (KEY_SHOW_HOST, KEY_SHOW_MEM, KEY_MUTE):
        if key in out:
            out[key] = _as_bool(out[key], DEFAULTS[key])
    return out


def save_stored(values, path=None) -> None:
    """Persist the given ``{KEY_*: value}`` pairs (unrecognised keys ignored)."""
    s = _qsettings(path)
    for key, val in values.items():
        if key in DEFAULTS:
            s.setValue(key, val)
    s.sync()


# ── UI view-state (separate from Config; not part of CLI precedence) ─────────────
# Which Memory-panel tiers the user has collapsed. This is presentation state, not
# configuration, so it lives outside DEFAULTS/resolve_config and round-trips as a
# comma-joined list of tier keys under its own QSettings key.
KEY_MEM_COLLAPSED = "ui/mem_collapsed"


def load_mem_collapsed(path=None) -> set:
    """The set of Memory tier keys the user last left collapsed ({} if none)."""
    s = _qsettings(path)
    raw = s.value(KEY_MEM_COLLAPSED, "")
    return {t for t in str(raw or "").split(",") if t}


def save_mem_collapsed(keys, path=None) -> None:
    """Persist the collapsed Memory tier keys (a set/iterable of tier names)."""
    s = _qsettings(path)
    s.setValue(KEY_MEM_COLLAPSED, ",".join(sorted(keys)))
    s.sync()


# Which Runners-panel repo groups the user has EXPANDED (the panel defaults
# every group collapsed to a summary header — maintainer ask 2026-07-29 —
# so the persisted set is the expanded exceptions; a new group arrives
# collapsed). Same comma-joined round-trip as the tier collapse above.
KEY_RUNNERS_EXPANDED = "ui/runners_expanded"


def load_runners_expanded(path=None) -> set:
    """The set of Runners group labels the user last left expanded."""
    s = _qsettings(path)
    raw = s.value(KEY_RUNNERS_EXPANDED, "")
    return {t for t in str(raw or "").split(",") if t}


def save_runners_expanded(labels, path=None) -> None:
    """Persist the expanded Runners group labels."""
    s = _qsettings(path)
    s.setValue(KEY_RUNNERS_EXPANDED, ",".join(sorted(labels)))
    s.sync()


# Which whole PANELS (GPU/Activity/Runs/Forecast/Host/Memory/Sessions/Ports) the
# user has collapsed — presentation state, sibling to the Memory-tier collapse
# above but one level up (the panel body vs. a tier within Memory). Round-trips as
# a comma-joined list of panel ids under its own key.
KEY_PANEL_COLLAPSED = "ui/panel_collapsed"


def load_panel_collapsed(path=None) -> set:
    """The set of panel ids the user last left collapsed ({} if none)."""
    s = _qsettings(path)
    raw = s.value(KEY_PANEL_COLLAPSED, "")
    return {t for t in str(raw or "").split(",") if t}


def save_panel_collapsed(ids, path=None) -> None:
    """Persist the collapsed panel ids (a set/iterable of panel ids)."""
    s = _qsettings(path)
    s.setValue(KEY_PANEL_COLLAPSED, ",".join(sorted(ids)))
    s.sync()


# The canonical panels as (id, title) in their default order — one source of truth
# for the panel set the reorder UI lists and the persisted order reconciles against.
# (The window maps these ids to its build/update methods.)
# The default order groups the stack in two zones (maintainer direction
# 2026-07-22): the SYSTEM tier first — the GPU story (gpu/activity/runs), then
# cpu+memory with ports directly underneath, then the session fleet — and the
# WATCH-FOR tier last: the panels that exist to be looked out for (forecast,
# egress, scanners, stuck).
# Ids "host"/"memory" kept their names through the 2026-07-29 host/WSL
# regroup (titles swapped meaning: "host" is the WSL section, "memory" the
# Windows-host section) so stored order/collapse/hidden state follows the
# same physical panels through the retitle.
PANEL_DEFS = (
    ("gpu", "GPU"), ("activity", "Activity"), ("runs", "Runs"),
    ("host", "WSL"), ("memory", "Host"), ("ports", "Ports"),
    ("sessions", "Sessions"), ("runners", "Runners"),
    ("forecast", "Forecast"), ("egress", "Egress"), ("scanners", "Scanners"),
    ("stuck", "Stuck"),
)
PANEL_IDS = tuple(pid for pid, _ in PANEL_DEFS)
# The watch-for zone. The window paints a dim zone caption above these when
# they sit together at the bottom of the visible stack (the default order).
WATCH_PANEL_IDS = frozenset({"forecast", "egress", "scanners", "stuck"})
KEY_PANEL_ORDER = "ui/panel_order"
# The default-order era a stored order tracks. Orders stored BEFORE the
# 2026-07-22 regroup (schema 2) read back as the NEW default once — the
# regroup was an explicit maintainer directive that supersedes both the old
# default and any pre-regroup hand order (the live install's order had ports
# hand-moved to the top precisely because the old default buried it — the
# regroup IS that fix). save_panel_order stamps the current schema, so an
# order arranged on a regrouped build persists normally.
KEY_PANEL_ORDER_SCHEMA = "ui/panel_order_schema"
PANEL_ORDER_SCHEMA = 2


def load_panel_order(path=None) -> list:
    """The persisted panel order (a list of ids), reconciled against the
    canonical set: duplicates and unknown / stale ids dropped, and any
    canonical id missing from the stored order inserted after the LAST stored
    id that canonically precedes it (so a newly-added panel lands in its zone
    — e.g. a future watch panel joins the watch tail — instead of dangling at
    the end). An order stored before the current default-order schema reads
    back as the current default (one-time regroup; the next save re-stamps).
    Default = canonical order when nothing is stored."""
    s = _qsettings(path)
    try:
        schema = int(s.value(KEY_PANEL_ORDER_SCHEMA, 1))
    except (TypeError, ValueError):
        schema = 1
    if schema < PANEL_ORDER_SCHEMA:
        return list(PANEL_IDS)
    raw = s.value(KEY_PANEL_ORDER, "")
    stored = [t.strip() for t in str(raw or "").split(",") if t.strip()]
    order = []
    for pid in stored:
        if pid in PANEL_IDS and pid not in order:
            order.append(pid)
    for pid in PANEL_IDS:
        if pid in order:
            continue
        rank = PANEL_IDS.index(pid)
        pos = 0
        for i, existing in enumerate(order):
            if PANEL_IDS.index(existing) < rank:
                pos = i + 1
        order.insert(pos, pid)
    return order


def save_panel_order(ids, path=None) -> None:
    """Persist the panel order (an iterable of ids; only canonical ids kept),
    stamped with the current default-order schema — an order arranged on this
    build is the user's word and never regrouped again."""
    s = _qsettings(path)
    order = [str(x) for x in ids if str(x) in PANEL_IDS]
    s.setValue(KEY_PANEL_ORDER, ",".join(order))
    s.setValue(KEY_PANEL_ORDER_SCHEMA, PANEL_ORDER_SCHEMA)
    s.sync()


# Which panels are HIDDEN (show/hide, unified in the Settings "Panels" list — the
# single source of truth for panel visibility, replacing the old per-panel Show
# Host/Memory checkboxes). Comma-joined ids; a panel is shown unless listed here.
KEY_PANEL_HIDDEN = "ui/panel_hidden"


def load_panel_hidden(path=None) -> set:
    """The set of panel ids the user has hidden ({} = all shown)."""
    s = _qsettings(path)
    raw = s.value(KEY_PANEL_HIDDEN, "")
    return {t for t in str(raw or "").split(",") if t in PANEL_IDS}


def save_panel_hidden(ids, path=None) -> None:
    """Persist the hidden panel ids (a set/iterable; only canonical ids kept)."""
    s = _qsettings(path)
    s.setValue(KEY_PANEL_HIDDEN, ",".join(sorted(pid for pid in ids
                                                  if pid in PANEL_IDS)))
    s.sync()


def panel_hidden_configured(path=None) -> bool:
    """Whether panel visibility has ever been saved — lets the app seed it once from
    the legacy show_host/show_mem before the unified list takes over."""
    return bool(_qsettings(path).contains(KEY_PANEL_HIDDEN))


# The memory tiers' minimum row size (MiB) — SHARED by the Windows-host
# section's tiers and the WSL section's wsl tier (2026-07-29 regroup):
# processes at/above it render directly,
# smaller ones collapse into the "N more" group. Presentation config — sibling to
# the collapsed set — so it lives here (not in Config/resolve_config: no CLI flag,
# no precedence) and rides the same per-window settings path. Defaults to the
# historical fixed 1 GiB cutoff so behaviour is unchanged until the user tunes it.
KEY_MEM_MIN_MIB = "ui/mem_min_mib"
DEFAULT_MEM_MIN_MIB = 1024.0


def load_mem_min_mib(path=None) -> float:
    """The persisted Memory-panel minimum row size in MiB (default 1024, never < 0)."""
    s = _qsettings(path)
    return max(0.0, _as_float(s.value(KEY_MEM_MIN_MIB, DEFAULT_MEM_MIN_MIB),
                              DEFAULT_MEM_MIN_MIB))


def save_mem_min_mib(mib, path=None) -> None:
    """Persist the Memory-panel minimum row size in MiB (clamped to >= 0)."""
    s = _qsettings(path)
    s.setValue(KEY_MEM_MIN_MIB, max(0.0, _as_float(mib, DEFAULT_MEM_MIN_MIB)))
    s.sync()


# The win cpu row's busy marker (% busy): a thin line on the bar, and the
# threshold from which the bar + trend turn red. View-state like the memory
# minimum — outside Config/resolve_config; applied live by the window.
KEY_BUSY_MARKER_PCT = "ui/busy_marker_pct"
DEFAULT_BUSY_MARKER_PCT = 92.0


def _clamp_busy_marker(v) -> float:
    pct = _as_float(v, DEFAULT_BUSY_MARKER_PCT)
    # NaN fails every comparison, so test finiteness explicitly.
    return min(max(pct, 1.0), 100.0) if pct == pct else DEFAULT_BUSY_MARKER_PCT


def load_busy_marker_pct(path=None) -> float:
    """The persisted busy-marker percent (default 92; junk gives the default,
    clamped to 1..100)."""
    s = _qsettings(path)
    return _clamp_busy_marker(s.value(KEY_BUSY_MARKER_PCT, DEFAULT_BUSY_MARKER_PCT))


def save_busy_marker_pct(pct, path=None) -> None:
    """Persist the busy-marker percent (clamped to 1..100)."""
    s = _qsettings(path)
    s.setValue(KEY_BUSY_MARKER_PCT, _clamp_busy_marker(pct))
    s.sync()


# The Active-indicator style: how ● ACTIVE announces itself (window header +
# Activity line + tray icon). Presentation state like the panel prefs — outside
# Config/resolve_config. Values are state.ACTIVE_STYLES: "hot" (default — the
# accent), "pulse" (accent + tray pulse), "subtle" (the legacy calm green).
KEY_ACTIVE_STYLE = "ui/active_style"


def load_active_style(path=None) -> str:
    """The persisted Active-indicator style, validated (unknown → the default)."""
    from .state import ACTIVE_STYLES, DEFAULT_ACTIVE_STYLE
    s = _qsettings(path)
    raw = str(s.value(KEY_ACTIVE_STYLE, DEFAULT_ACTIVE_STYLE) or "").strip().lower()
    return raw if raw in ACTIVE_STYLES else DEFAULT_ACTIVE_STYLE


def save_active_style(style, path=None) -> None:
    """Persist the Active-indicator style (unknown values stored as the default)."""
    from .state import ACTIVE_STYLES, DEFAULT_ACTIVE_STYLE
    s = _qsettings(path)
    style = str(style).strip().lower()
    s.setValue(KEY_ACTIVE_STYLE,
               style if style in ACTIVE_STYLES else DEFAULT_ACTIVE_STYLE)
    s.sync()


# The Ports panel's sparkline window — how many seconds of recent activity the
# per-port series spans. Presentation state like the Memory prefs (no CLI flag,
# no precedence); clamped to the shared state.PORT_WINDOW_* range on both load
# and save so a hand-edited store can't wipe the graph.
KEY_PORT_WINDOW_S = "ui/port_window_s"


def load_port_window_s(path=None) -> float:
    """The persisted Ports-graph window in seconds (clamped; default ~3 min)."""
    from .state import DEFAULT_PORT_WINDOW_S, clamp_port_window_s
    s = _qsettings(path)
    return clamp_port_window_s(s.value(KEY_PORT_WINDOW_S, DEFAULT_PORT_WINDOW_S))


def save_port_window_s(value, path=None) -> None:
    """Persist the Ports-graph window in seconds (clamped to the safe range)."""
    from .state import clamp_port_window_s
    s = _qsettings(path)
    s.setValue(KEY_PORT_WINDOW_S, clamp_port_window_s(value))
    s.sync()


# Window startup + placement — presentation/view state, siblings to the Memory
# prefs above: persisted per-window via QSettings, *outside* Config/resolve_config
# (no CLI precedence, no DEFAULTS entry). ``start_minimized`` decides whether the
# tray app opens its window on launch (default off = show it); ``window_geometry``
# is the opaque QByteArray from ``QWidget.saveGeometry()`` so the last position and
# size are restored next launch.
KEY_START_MINIMIZED = "ui/start_minimized"   # bool
KEY_WINDOW_GEOMETRY = "ui/window_geometry"   # QByteArray (save/restoreGeometry)


def load_start_minimized(path=None) -> bool:
    """Whether the tray app should start minimized to tray (default False)."""
    s = _qsettings(path)
    return _as_bool(s.value(KEY_START_MINIMIZED, False), False)


def save_start_minimized(value, path=None) -> None:
    """Persist the start-minimized preference (a bool)."""
    s = _qsettings(path)
    s.setValue(KEY_START_MINIMIZED, bool(value))
    s.sync()


def load_window_geometry(path=None):
    """The last-saved window geometry (a QByteArray for ``restoreGeometry``), or
    ``None`` when the user has no saved geometry yet (or it round-tripped empty)."""
    s = _qsettings(path)
    raw = s.value(KEY_WINDOW_GEOMETRY)
    return raw or None


def save_window_geometry(geometry, path=None) -> None:
    """Persist the window geometry (the QByteArray from ``QWidget.saveGeometry``)."""
    s = _qsettings(path)
    s.setValue(KEY_WINDOW_GEOMETRY, geometry)
    s.sync()


# ── producer sample cadence (the monitor-config:v1 channel, app → producer) ──────
# The producer can't be out-rendered: the app shows data no fresher than the
# producer writes. These two knobs let the user tune the producer's WRITE rates
# from Settings; the app persists them (dialog pre-fill) and writes them to a
# monitor-config:v1 JSON the producer re-reads live (its --config). This is the one
# place the "pure renderer" writes — a small, one-directional app→producer file.
#
# The clamp ranges match the producer's accepted GPU interval and host refresh
# limits. We clamp here too so the file we write is always in range. The
# producer clamps independently, and the dialog cannot offer an out-of-range
# value.
MONITOR_CONFIG_SCHEMA = "monitor-config:v1"
KEY_GPU_INTERVAL = "producer/gpu_interval_s"     # main GPU tick — the fast write rate
KEY_HOST_REFRESH = "producer/host_refresh_s"     # host/docker collectors — the slow one
KEY_WATCH_PORTS = "producer/watch_ports"         # comma-joined "LABEL:PORT" specs
KEY_WATCH_PROC_NAMES = "producer/watch_proc_names"  # comma-joined "side:name" specs
KEY_SESSION_PORTS = "producer/session_ports"     # bool: per-session listen-port census
# Name-target limits: the spec feeds the producer's
# matchers verbatim, and a wsl comm (also ss's users:() token) is 15 chars.
PROC_NAME_SPEC_MAX = 64
WSL_COMM_MAX = 15
DEFAULT_GPU_INTERVAL_S = 2.0
DEFAULT_HOST_REFRESH_S = 10.0
GPU_INTERVAL_MIN_S, GPU_INTERVAL_MAX_S = 1.0, 10.0
HOST_REFRESH_MIN_S, HOST_REFRESH_MAX_S = 5.0, 120.0


def _clamp(value, default, lo, hi) -> float:
    """A cadence coerced to a float in [lo, hi]; missing / non-numeric / NaN → the
    default (itself in range). Mirrors the producer's _clamp_cadence."""
    v = _as_float(value, default)
    if v != v:                                   # NaN
        return default
    return max(lo, min(hi, v))


def load_producer_cadence(path=None) -> tuple[float, float]:
    """The persisted (gpu_interval_s, host_refresh_s) the user last chose, each
    clamped to its safe range and defaulting when unset."""
    s = _qsettings(path)
    gpu = _clamp(s.value(KEY_GPU_INTERVAL, DEFAULT_GPU_INTERVAL_S),
                 DEFAULT_GPU_INTERVAL_S, GPU_INTERVAL_MIN_S, GPU_INTERVAL_MAX_S)
    host = _clamp(s.value(KEY_HOST_REFRESH, DEFAULT_HOST_REFRESH_S),
                  DEFAULT_HOST_REFRESH_S, HOST_REFRESH_MIN_S, HOST_REFRESH_MAX_S)
    return gpu, host


def save_producer_cadence(gpu_interval_s, host_refresh_s, path=None) -> None:
    """Persist the producer cadence (clamped) for the dialog to pre-fill next time."""
    s = _qsettings(path)
    s.setValue(KEY_GPU_INTERVAL, _clamp(gpu_interval_s, DEFAULT_GPU_INTERVAL_S,
                                        GPU_INTERVAL_MIN_S, GPU_INTERVAL_MAX_S))
    s.setValue(KEY_HOST_REFRESH, _clamp(host_refresh_s, DEFAULT_HOST_REFRESH_S,
                                        HOST_REFRESH_MIN_S, HOST_REFRESH_MAX_S))
    s.sync()


KEY_HOSTED_BURSTS = "producer/hosted_bursts"   # comma-joined owner/repo list
# Hosted-burst spec limits: the watch is
# OPT-IN — an empty spec is OFF (zero producer API calls).
HOSTED_BURST_REPOS_MAX = 4
HOSTED_REPO_LEN_MAX = 100


def _hosted_repo_ok(repo) -> bool:
    """One owner/repo atom, the producer's exact grammar (single slash,
    [A-Za-z0-9_.-] atoms, bounded) — mirrored so the dialog cue and the
    written config can never disagree with the producer's parse."""
    if not isinstance(repo, str) or not repo or len(repo) > HOSTED_REPO_LEN_MAX:
        return False
    head, sep, tail = repo.partition("/")
    ok_atom = lambda s: bool(s) and all(  # noqa: E731 — tiny local predicate
        c.isalnum() or c in "_.-" for c in s)
    return bool(sep) and "/" not in tail and ok_atom(head) and ok_atom(tail)


def clean_hosted_repos(specs) -> list:
    """Validated owner/repo list (order kept, dedup'd, capped) from a list or
    a comma-joined string — the producer's _parse_hosted_burst_repos mirror;
    malformed entries drop (the fail-open family policy)."""
    parts = specs.split(",") if isinstance(specs, str) else (specs or [])
    out = []
    for part in parts:
        repo = str(part).strip()
        if repo and repo not in out and _hosted_repo_ok(repo):
            out.append(repo)
            if len(out) >= HOSTED_BURST_REPOS_MAX:
                break
    return out


def rejected_hosted_repo_specs(specs) -> list:
    """The tokens clean_hosted_repos drops, named for the dialog's live cue
    (the producer skips malformed config entries SILENTLY — an unnamed
    reject reads as a watch that never answers). Over-cap entries are named
    too — they would silently never be watched."""
    parts = specs.split(",") if isinstance(specs, str) else (specs or [])
    tokens = [str(p).strip() for p in parts if str(p).strip()]
    kept = clean_hosted_repos(tokens)
    out = []
    seen = set()
    for tok in tokens:
        if tok in kept or tok in seen:
            continue
        seen.add(tok)
        if _hosted_repo_ok(tok):
            out.append(f"{tok} — over the {HOSTED_BURST_REPOS_MAX}-repo cap")
        else:
            out.append(f"{tok} — not owner/repo")
    return out


def load_hosted_bursts(path=None) -> list:
    """The persisted hosted-burst repo watch (default [] = off)."""
    s = _qsettings(path)
    raw = s.value(KEY_HOSTED_BURSTS)
    return clean_hosted_repos(raw if isinstance(raw, str) else "")


def save_hosted_bursts(repos, path=None) -> None:
    """Persist the hosted-burst repo watch (comma-joined, validated)."""
    s = _qsettings(path)
    s.setValue(KEY_HOSTED_BURSTS, ",".join(clean_hosted_repos(repos)))
    s.sync()


def load_session_ports(path=None) -> bool:
    """The persisted session-ports census toggle (default ON). Only an explicit
    stored false disables — junk fails open to on, mirroring the producer's
    config-channel read (its own gate is the last word)."""
    s = _qsettings(path)
    raw = s.value(KEY_SESSION_PORTS)
    if raw is None:
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


def save_session_ports(value, path=None) -> None:
    """Persist the session-ports census toggle for the dialog + config writes."""
    s = _qsettings(path)
    s.setValue(KEY_SESSION_PORTS, bool(value))
    s.sync()


def _clean_port_specs(specs) -> list:
    """Normalise watched-port specs to a list of trimmed non-empty 'LABEL:PORT'
    strings (the producer validates each; we just drop blanks)."""
    return [str(x).strip() for x in (specs or []) if str(x).strip()]


def stored_truth_config(path=None) -> Config:
    """The Config the PERSISTED store alone resolves to (no CLI layer) — what
    the Settings dialog must pre-fill from. Pre-filling from the flag-merged
    effective config lets a session CLI flag silently promote itself into the
    persisted store on OK. Fail-open: an
    unreadable store resolves to the defaults."""
    try:
        stored = load_stored(path)
    except Exception:  # noqa: BLE001 — a bad store must not block the dialog
        stored = {}
    return resolve_config({}, stored)


def rejected_port_specs(specs) -> list:
    """The watched-port tokens ``parse_watch_spec`` rejects, as its messages
    (each names the offending token). Pure — feeds the Settings dialog's LIVE
    cue: the producer drops a malformed config entry silently (fail-open), so
    an unnamed reject reads as a watch that never appears."""
    out = []
    for tok in _clean_port_specs(specs):
        try:
            parse_watch_spec(tok)
        except ValueError as exc:
            out.append(str(exc))
    return out


def rejected_proc_specs(specs) -> list:
    """``parse_watch_proc_name``'s rejects for the watched-process field —
    the same live-cue contract as :func:`rejected_port_specs`."""
    out = []
    for tok in (str(t).strip() for t in (specs or [])):
        if not tok:
            continue
        try:
            parse_watch_proc_name(tok)
        except ValueError as exc:
            out.append(str(exc))
    return out


def parse_watch_spec(spec) -> tuple:
    """Parse one watched-port spec exactly like the producer's parse_watch_port:
    'LABEL:PORT' → (label, port); a bare 'PORT' → (str(port), port); whitespace
    trimmed. Raises ValueError on a malformed spec or a port outside 1-65535.

    Mirrored here (Qt-free) so the ad-hoc "Watch a port…" input can validate
    BEFORE the spec is written — the producer skips a malformed config entry
    silently (fail-open), which for typed input would read as a watch that
    never appears.

    Two app-side tightenings over the producer (both reject-only, so nothing
    the app writes can ever be something the producer refuses): ``,`` is
    rejected because the persisted QSettings form comma-joins the specs — a
    comma label would silently split into phantom specs on the next launch —
    and the port must be ASCII digits (``.isdigit()`` alone admits e.g. ``²``,
    which then fails ``int()`` with an unhelpful message)."""
    text = str(spec or "")
    if "," in text:
        raise ValueError(f"spec must not contain ',': {spec!r}")
    label, sep, port_s = text.rpartition(":")
    if not sep:
        label, port_s = "", text
    port_s = port_s.strip()
    if not (port_s.isascii() and port_s.isdigit()):
        raise ValueError(f"port must be an integer: {spec!r}")
    port = int(port_s)
    if not 0 < port < 65536:
        raise ValueError(f"port out of range 1-65535: {port}")
    return label.strip() or str(port), port


def spec_port(spec):
    """The port a stored spec names, or None when malformed — a lenient
    companion to parse_watch_spec for set arithmetic over persisted specs."""
    try:
        return parse_watch_spec(spec)[1]
    except ValueError:
        return None


def add_watch_spec(specs, new_spec) -> list:
    """The spec list with ``new_spec`` appended in canonical form — unless its
    port is already watched: the producer dedups by port (first label wins), so
    a second spec for the same port would silently do nothing. Validates via
    parse_watch_spec (raises ValueError on a malformed spec). Returns a NEW
    list; the input is never mutated."""
    label, port = parse_watch_spec(new_spec)
    out = _clean_port_specs(specs)
    if any(spec_port(s) == port for s in out):
        return out
    out.append(str(port) if label == str(port) else f"{label}:{port}")
    return out


def remove_watch_port(specs, port) -> list:
    """The spec list with every spec naming ``port`` removed (a NEW list).
    Specs that don't parse are kept — a remove must only ever remove the port
    it names, never silently repair the rest of the stored state."""
    port = int(port)
    return [s for s in _clean_port_specs(specs) if spec_port(s) != port]


def pending_watch_ports(specs, reported_ports) -> list:
    """Desired watch specs whose port has no reported row yet, as (label, port)
    pairs — the honest "watching, but the producer hasn't reported it" set the
    Ports panel renders while an ad-hoc add propagates (the producer re-reads
    its config within a few seconds, then reports every watched port, idle
    included). Malformed specs are skipped (the producer would skip them too);
    duplicate ports collapse to the first spec's label."""
    have = set(reported_ports or ())
    out, seen = [], set()
    for s in specs or ():
        try:
            label, port = parse_watch_spec(s)
        except ValueError:
            continue
        if port in have or port in seen:
            continue
        seen.add(port)
        out.append((label, port))
    return out


def load_watch_ports(path=None) -> list:
    """The persisted watched-port specs (['label:port', …]); [] when unset."""
    s = _qsettings(path)
    raw = s.value(KEY_WATCH_PORTS, "")
    return [t.strip() for t in str(raw or "").split(",") if t.strip()]


def save_watch_ports(specs, path=None) -> None:
    """Persist the watched-port specs (an iterable of 'label:port' strings)."""
    s = _qsettings(path)
    s.setValue(KEY_WATCH_PORTS, ",".join(_clean_port_specs(specs)))
    s.sync()


def _pid_like(name) -> bool:
    """A bare (possibly signed) integer is a PID, never a name pattern —
    rejected in BOTH validation layers so a pid typo (wsl:-5) can't become
    a silently-persisted never-matching watch, and so proc_target_key's
    pid/name spaces can't collide where the data is normalized, not only
    where it is typed."""
    text = str(name).strip()
    if text[:1] in ("+", "-"):
        text = text[1:]
    return bool(text) and text.isascii() and text.isdigit()


def _valid_proc_name(name) -> bool:
    """Whether a string is an acceptable name-target pattern: non-empty after
    strip, ≤ PROC_NAME_SPEC_MAX, no control characters (rejected by the
    producer), no comma (it would corrupt the
    comma-joined QSettings form into a DIFFERENT valid watch on reload),
    and not pid-like."""
    return (isinstance(name, str) and bool(name.strip())
            and len(name.strip()) <= PROC_NAME_SPEC_MAX
            and not any(ord(ch) < 32 for ch in name)
            and "," not in name
            and not _pid_like(name))


def clean_proc_targets(targets) -> list:
    """Normalise process→ports targets to the entries the producer's
    watch_procs parser accepts, dropping anything malformed:

    - pid targets {side, pid, name?} — side wsl|windows, positive int pid,
      optional name; deduped on (side, pid). SESSION-ONLY by design
      (deliberately NOT persisted): a forgotten pid target would keep the
      producer's Windows PowerShell section riding every host sweep forever
      (footprint rule), and a persisted pid could silently follow a reused
      pid to a different process.
    - NAME targets {side, name} (pid None) — a case-insensitive substring
      watch; deduped on (side, lowered name). These PERSIST
      (save/load_watch_proc_names): a name re-matches live pids every sweep,
      so pid reuse can't lie, and the Settings field is their standing
      removal affordance — a persisted watch is never unremovable.

    A present-but-invalid pid is dropped, never demoted to a name watch
    (the producer's policy, mirrored)."""
    out, seen = [], set()
    for t in targets or ():
        if not isinstance(t, dict) or t.get("side") not in ("wsl", "windows"):
            continue
        name = t.get("name")
        name = str(name).strip() if isinstance(name, str) else None
        pid = t.get("pid")
        if pid is None:
            if not _valid_proc_name(name):
                continue
            key = (t["side"], "name", name.lower())
            if key in seen:
                continue
            seen.add(key)
            out.append({"side": t["side"], "pid": None, "name": name})
            continue
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid <= 0 or (t["side"], pid) in seen:
            continue
        seen.add((t["side"], pid))
        out.append({"side": t["side"], "pid": pid, "name": name or None})
    return out


def parse_watch_proc_name(spec) -> dict:
    """Parse one typed process-watch NAME spec — ``side:name`` with side
    ``wsl``|``windows`` (case-insensitive) — into the {side, pid: None, name}
    entry the config channel carries. Raises ValueError with a usable message
    on bad input (mirroring parse_watch_spec: validate BEFORE writing, because
    the producer skips malformed entries silently).

    Reject-only tightenings over the producer: ``,`` (the persisted QSettings
    form comma-joins specs), an all-digits name (that's a PID — pid targets
    are session-only and never live in the persisted field; use right-click ▸
    Show ports or "+ Watch a process…" with side:PID), and a wsl name longer
    than the 15-char kernel comm (the producer would answer it, but only ever
    with "can never match")."""
    text = str(spec or "").strip()
    if "," in text:
        raise ValueError(f"spec must not contain ',': {spec!r}")
    side, sep, name = text.partition(":")
    side = side.strip().lower()
    name = name.strip()
    if not sep or side not in ("wsl", "windows"):
        raise ValueError(
            f"spec must be side:name with side wsl or windows "
            f"(the two pid namespaces are separate): {spec!r}")
    if not name:
        raise ValueError(f"empty process name: {spec!r}")
    if _pid_like(name):
        raise ValueError(
            f"{name} looks like a PID — a persisted watch is by NAME; "
            f"target a pid via right-click ▸ Show ports of…, or type "
            f"{side}:{name.lstrip('+-')} in \"+ Watch a process…\" "
            f"(session-only)")
    if len(name) > PROC_NAME_SPEC_MAX:
        raise ValueError(f"name longer than {PROC_NAME_SPEC_MAX} chars: {spec!r}")
    if any(ord(ch) < 32 for ch in name):
        raise ValueError(f"name must not contain control characters: {spec!r}")
    if side == "wsl" and len(name) > WSL_COMM_MAX:
        raise ValueError(
            f"a wsl process name (comm) is at most {WSL_COMM_MAX} chars — "
            f"{name!r} can never match; shorten the pattern")
    return {"side": side, "pid": None, "name": name}


def load_watch_proc_names(path=None) -> list:
    """The persisted process-watch NAME entries ([{side, pid: None, name}]);
    [] when unset. Malformed persisted tokens are skipped (fail-open) —
    only name entries live here, pid targets are session-only by design."""
    s = _qsettings(path)
    raw = s.value(KEY_WATCH_PROC_NAMES, "")
    out = []
    for token in str(raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(parse_watch_proc_name(token))
        except ValueError:
            continue
    return clean_proc_targets(out)


def save_watch_proc_names(targets, path=None) -> None:
    """Persist the NAME entries of a target list as comma-joined ``side:name``
    tokens; pid entries are filtered out here too (session-only), so callers
    may hand the full mixed list."""
    tokens = [f"{t['side']}:{t['name']}"
              for t in clean_proc_targets(targets) if t.get("pid") is None]
    s = _qsettings(path)
    s.setValue(KEY_WATCH_PROC_NAMES, ",".join(tokens))
    s.sync()


def write_monitor_config(config_path, gpu_interval_s, host_refresh_s,
                         watch_ports=None, watch_procs=None,
                         session_ports=True, hosted_bursts=None) -> None:
    """Write the monitor-config:v1 JSON the producer reads — the app→producer
    channel. Cadences are clamped to the shared safe ranges; watch_ports is the
    normalised 'LABEL:PORT' spec list the producer parses (the app writes the FULL
    desired state each time, so an empty list means "watch nothing"); watch_procs
    is the process→ports target list (see clean_proc_targets — pid targets are
    session-only, name targets persist; an empty list means "target nothing").
    Atomic (temp + replace),
    Qt-free. Raising is the caller's to handle (the app treats a write failure
    as non-fatal, like its other view-state persistence)."""
    import json
    import os
    import tempfile
    from pathlib import Path

    path = Path(config_path)
    payload = {
        "schema": MONITOR_CONFIG_SCHEMA,
        "gpu_interval_s": _clamp(gpu_interval_s, DEFAULT_GPU_INTERVAL_S,
                                 GPU_INTERVAL_MIN_S, GPU_INTERVAL_MAX_S),
        "host_refresh_s": _clamp(host_refresh_s, DEFAULT_HOST_REFRESH_S,
                                 HOST_REFRESH_MIN_S, HOST_REFRESH_MAX_S),
        "watch_ports": _clean_port_specs(watch_ports),
        "watch_procs": clean_proc_targets(watch_procs),
        # Session-ports census toggle: the producer treats an
        # absent key as ON, so writing it explicitly just makes the user's
        # choice durable in the channel; non-bool junk fails open producer-side.
        "session_ports": bool(session_ports),
        # Hosted burst-run watch (OPT-IN): the full desired repo
        # list every write — an explicit [] is a real OFF (the producer's
        # present-key semantics), and a full-state writer that skipped the
        # field would fall back to the producer's CLI default instead of the
        # user's choice.
        "hosted_bursts": clean_hosted_repos(hosted_bursts),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
