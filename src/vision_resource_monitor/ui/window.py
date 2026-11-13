"""The pop-up monitor window: a status header over a scrollable stack of panels
(GPU, Activity, Runs, Host, Memory). Each ``update_from`` repaints from a fresh
ReadResult; the snapshot already carries every field these panels render —
except the Ports sparklines' series and each port's recent-clients memory,
which the window accumulates itself (``state.PortHistory`` /
``state.PortClients``).
"""
from __future__ import annotations

import html
import math
import time
from collections import deque
from datetime import datetime

from PySide6.QtCore import QCoreApplication, QEvent, Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QTextDocument
from PySide6.QtWidgets import (
    QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMenu, QMenuBar, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .. import LIVE_ACTIONS_OFF_REASON, live_actions_enabled, release
from ..reader import ReadResult
from ..settings import (
    WATCH_PANEL_IDS, add_watch_spec, load_active_style, load_busy_marker_pct,
    load_mem_collapsed, load_mem_min_mib, load_panel_collapsed, load_panel_hidden,
    load_panel_order, load_port_window_s, load_runners_expanded,
    load_window_geometry,
    parse_watch_proc_name, pending_watch_ports, remove_watch_port,
    save_mem_collapsed, save_mem_min_mib, save_panel_collapsed,
    save_runners_expanded, save_window_geometry, spec_port,
)
from ..state import (
    ACTIVE, IDLE, MEM_OK, MEM_UNAVAILABLE, NODATA, PROC_TARGET_CAP,
    PROC_TARGET_GRACE_S, RED, STALE, YELLOW, PortClients, PortHistory,
    active_color, bucket_means, busy_color, clients_note, find_pids, format_gpu_users,
    format_port_holds, format_proc_name_ports, format_proc_ports,
    gpu_users_gap_hint, idle_stats, memory_tiers, overall_status,
    panel_summaries, ports_held_by, proc_name_for_pid, proc_target_key,
    runner_groups,
    scale_sparkline, security_stack_headline, selfcost_note, SelfCpuWindow,
    spawn_headline, split_process_rows, stuck_flags, tooltip,
)
from .widgets import GaugeBar, Sparkline

# ACTIVE's colour is not fixed — it follows the user's Active-indicator style
# (state.active_color); the other states keep their fixed hues.
_STATE_COLORS = {IDLE: "#8b949e", STALE: "#d29922", NODATA: "#f85149"}
_RUN_COLORS = {"ok": "#2ea043", "error": "#f85149", "died": "#f85149",
               "stalled": "#d29922", "running": "#58a6ff"}
_DIM = "#8b949e"
_CLOUD = "#bc8cff"  # violet: a run on the cloud (hosted watch) — set apart from the local blue/grey
_LLM = "#d29922"
_WARN = "#d29922"  # amber: a tier the producer couldn't collect (not an error)
_OK_GREEN = "#2ea043"  # forecast "clear" — memory pressure normal
_SPARK_ACCENT = "#58a6ff"  # neutral activity blue for peak-normalized sparklines
# The port sparkline's sample budget: the widget is 84 px wide, so ~2 px per
# bucket. Longer windows carry more samples than pixels; bucket_means() folds
# them so a burst reads as area, not a sub-pixel spike.
_SPARK_BUCKETS = 42
_MAX_SESSION_ROWS = 8  # cap the per-session list; a "+N more" line notes the rest


class _ClickRow(QWidget):
    """A row widget that emits ``clicked`` on press — used for the collapsible
    Memory tier headers and the "N more" expander so the whole row is a hit
    target (QLabel has no clicked signal)."""
    clicked = Signal()

    def mousePressEvent(self, event):  # noqa: N802 (Qt override)
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _NoteLabel(QLabel):
    """A QLabel for OPTIONAL note lines: empty text hides the label. An
    emptied-but-visible label keeps its layout slot and reads as a trapped
    blank line inside the section card — the #60 Scanners first-line lesson,
    generalized after the card frames made every such row visible
    (maintainer report 2026-07-23: Host and Sessions carried one each).
    Every setText path syncs visibility, so early-return update branches
    can't leave a stale blank row."""

    def __init__(self, text=""):
        super().__init__(text)
        self.setVisible(bool(text))

    def setText(self, text):  # noqa: N802 (Qt override)
        super().setText(text)
        self.setVisible(bool(text))


class _CollapsibleSection(QWidget):
    """A panel wrapped with a clickable chevron header: clicking the header hides or
    shows the panel body (exposed as ``.body``) and reports the new collapsed state
    to ``on_toggle`` (the window persists it). The body's own QGroupBox title is
    cleared — the header owns the title — so the section reads as one titled group."""

    def __init__(self, panel_id, title, body, *, collapsed, on_toggle):
        super().__init__()
        self.panel_id = panel_id
        self.body = body
        self._on_toggle = on_toggle
        self._collapsed = collapsed      # tracked explicitly — isVisible() is
                                         # unreliable before the window is mapped
        if isinstance(body, QGroupBox):
            body.setTitle("")            # the header owns the title now
        # The card presentation (the Mac port's panel look, maintainer ask
        # 2026-07-22): the section owns ONE rounded card frame — header row on
        # the card, body inside it — and the body QGroupBox goes frameless so
        # the card never double-borders. Palette-driven (base over window, mid
        # border) so it follows dark and --light alike.
        self.setObjectName("vrm_card")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(
            "QWidget#vrm_card { background-color: palette(base);"
            " border: 1px solid palette(mid); border-radius: 6px; }"
            " QWidget#vrm_card QGroupBox { border: none;"
            " background: transparent; }")
        # 1 px side insets only: the body QGroupBox keeps its own inner
        # margins for the visual padding, and the row widgets are designed
        # tight against the 440 px minimum width — fatter card insets clip
        # the port rows' value column (smoke §[21] pins it).
        v = QVBoxLayout(self)
        v.setContentsMargins(1, 4, 1, 6)
        v.setSpacing(2)
        header = _ClickRow(); header.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(header); h.setContentsMargins(2, 2, 2, 0)
        self._chev = QLabel("▸" if collapsed else "▾")
        self._chev.setStyleSheet(f"color:{_DIM}; font-weight:bold;")
        self._chev.setFixedWidth(14)
        name = QLabel(title); name.setStyleSheet("font-weight:bold;")
        # The collapsed-header summary (maintainer ask 2026-07-21): while the
        # body is hidden the header carries one glanceable line (state.py's
        # panel_summaries). Stored every frame, SHOWN only while collapsed —
        # so a collapse shows the current frame's line instantly, no staleness
        # wait. Ignored h-policy: the summary clips before it can push the
        # title around; the full text rides its tooltip.
        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color:{_DIM};")
        self._summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._summary.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._summary.setVisible(False)
        h.addWidget(self._chev); h.addWidget(name)
        h.addWidget(self._summary, 1)
        v.addWidget(header)
        v.addWidget(body)
        body.setVisible(not collapsed)
        header.clicked.connect(self._toggle)

    def set_summary(self, text):
        """Store this frame's one-line summary (None/"" = the panel has nothing
        trustworthy to say — the header stays a plain title, never a blank or a
        fabricated line). Visibility follows the collapsed state."""
        text = text or ""
        if text != self._summary.text():
            self._summary.setText(text)
            # Summaries can carry producer names; armor the tooltip (the
            # label itself is PlainText via the update_from label pass).
            self._summary.setToolTip(_tip(text))
        self._apply_summary_visibility()

    def _apply_summary_visibility(self):
        self._summary.setVisible(self._collapsed and bool(self._summary.text()))

    def _toggle(self):
        self._collapsed = not self._collapsed
        self.body.setVisible(not self._collapsed)
        self._chev.setText("▸" if self._collapsed else "▾")
        self._apply_summary_visibility()
        self._on_toggle(self.panel_id, self._collapsed)


def _fmt_duration(seconds: float) -> str:
    # A non-finite producer value must render, not raise: round(inf/nan) throws,
    # and an exception here aborts update_from mid-loop — every panel after the
    # raising one silently stales and the tick's toasts are swallowed (the core's
    # tolerant parse deliberately passes "inf"/"nan" strings through as floats).
    if not math.isfinite(seconds):
        return "?"
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _clock(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return "?"


def _fmt_mem(mib: float) -> str:
    return f"{mib / 1024:.1f}G" if mib >= 1024 else f"{mib:.0f}M"


def _wincpu_note(wc) -> str:
    """The win cpu row's note: the cores-equivalent footprint
    (`time_pct/100 × cores` — "≈8.4/28 logical cores") and Task Manager's
    frequency-scaled utility ("· 41% of base clock"). Each part is dropped
    when its input is missing or junk (no/non-positive cores, non-finite
    time_pct, utility None) rather than render "≈inf cores"; with no busy
    figure the note says so."""
    parts = []
    if wc.time_pct is None:
        parts.append("no busy figure")
    elif wc.cores and wc.cores > 0 and math.isfinite(wc.time_pct):
        parts.append(f"≈{wc.time_pct / 100.0 * wc.cores:.1f}/{wc.cores} logical cores")
    if wc.utility_pct is not None:
        parts.append(f"{wc.utility_pct:.0f}% of base clock")
    return " · ".join(parts)


def _fmt_rate(bps) -> str:
    """A byte/s throughput as a compact human rate; '—' when unknown (None)."""
    if bps is None:
        return "—"
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f}M/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f}K/s"
    return f"{bps:.0f}B/s"


def _label_plain_text(label) -> str:
    """A QLabel's rendered text as plain text (rich-text markup stripped).
    A PlainText label DISPLAYS markup-shaped text literally, so its text is
    already exactly what the user sees — stripping it through the HTML parser
    would make Copy line copy something other than the rendered row."""
    text = label.text()
    if label.textFormat() == Qt.PlainText:
        return text
    if "<" in text and ">" in text:
        doc = QTextDocument()
        doc.setHtml(text)
        text = doc.toPlainText()
    return text


def _esc(value) -> str:
    """HTML-escape a producer-derived value for interpolation into one of the
    few RichText sinks (the colour-coded status header, forecast head, and the
    ``<br>``-joined amber alert/flag lines — the labels that carry our OWN
    markup). Everything the snapshot reports — process names (``comm`` is a
    process's own argv[0] basename), session labels, hosts, reasons, alerts —
    is controllable by any local process on either side of the boundary, and
    unescaped it would inject rich text into the advisory surface (earlier release,
    ported)."""
    return html.escape(str(value))


def _tip(text) -> str:
    """Armor a producer-derived string for ``setToolTip``. Qt renders a tooltip
    as rich text whenever the string LOOKS like markup (mightBeRichText), and
    there is no per-tooltip PlainText switch — so escape the content and pin
    the rich-text interpretation with a ``<qt>`` wrapper. Escaping alone is
    wrong: an entity in otherwise plain-looking text would then show literally
    ("a &amp;amp; b"). Empty stays empty — "<qt></qt>" would show a phantom
    empty tooltip box where setToolTip("") disables it."""
    s = str(text)
    if not s:
        return ""
    body = html.escape(s).replace("\n", "<br/>")
    return f"<qt>{body}</qt>"


def _build_copy_menu(text) -> QMenu:
    """The right-click copy menu for one rendered line: a "Copy PID N" action per
    PID the line mentions, then "Copy line". PARENTLESS by design — the panels
    rebuild on every advancing frame (~2 s), so the label under an open menu can
    be torn down mid-use; the actions capture plain strings, never the label."""
    menu = QMenu()
    for pid in find_pids(text):
        act = menu.addAction(f"Copy PID {pid}")
        act.triggered.connect(
            lambda _=False, s=str(pid): QGuiApplication.clipboard().setText(s))
    line = " ".join(text.split())
    if line:
        act = menu.addAction("Copy line")
        act.triggered.connect(
            lambda _=False, s=line: QGuiApplication.clipboard().setText(s))
    return menu


def _fmt_bytes(n) -> str:
    """A cumulative byte total as a compact human size; '—' when unknown (None)."""
    if n is None:
        return "—"
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f}G"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return f"{n:.0f}B"


def _fmt_ms(ms) -> str:
    """A sampler wall-time as a compact duration ('284ms', '2.3s')."""
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    return f"{ms:.0f}ms"


def _fmt_age(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m"
    return f"{seconds}s"


def _clear(layout) -> None:
    """Remove every widget from a layout immediately (see _update_runs note)."""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


class MonitorWindow(QWidget):
    def __init__(self, parent=None, *, show_host=True, show_mem=True,
                 show_sessions=True, on_settings=None, on_open_log=None,
                 on_about=None, on_rebuild=None, on_ask_claude=None,
                 on_open_procexp=None, on_open_procmon=None,
                 on_watch_ports_changed=None,
                 on_watch_procs_changed=None, settings_path=None):
        super().__init__(parent)
        self._show_host = show_host
        self._show_mem = show_mem
        self._show_sessions = show_sessions
        self._settings_path = settings_path
        # The ad-hoc watch/unwatch affordances hand the FULL desired spec list
        # to the app through this callback (which persists it + writes the
        # monitor-config file — the same path Settings ▸ Watched ports takes).
        # None (tests, bare constructions) hides the affordances.
        self._on_watch_ports_changed = on_watch_ports_changed
        # "ask claude" on each ranked contributor: the app hands the row to an
        # interactive Claude session in a WSL terminal. None (tests, bare
        # constructions) hides the buttons.
        self._on_ask_claude = on_ask_claude
        # "Open … in Process Explorer" on windows-side rows (#61): the app
        # hands (pid | None, name | None) to its async PowerShell launcher
        # (procexplorer.py). None (tests, bare constructions) hides the
        # actions — the app always wires it.
        self._on_open_procexp = on_open_procexp
        # "Open … in Process Monitor (filtered)" on the same windows-side
        # rows (#87): ProcMon CAN filter (procexp cannot), via a generated
        # /LoadConfig PMC. None (tests, bare constructions) hides it.
        self._on_open_procmon = on_open_procmon
        # What the app WROTE to the config (set_desired_watch_ports); snap.ports
        # is what the producer REPORTS — a desired port with no reported row
        # renders as an honest "waiting for the producer" pending row.
        self._desired_watch_ports: list = []
        # process→ports targets (right-click ▸ Show ports): the same shape —
        # the callback gets the FULL target list; set_proc_targets echoes the
        # app's session-only state back; snap.proc_ports carries the answers.
        self._on_watch_procs_changed = on_watch_procs_changed
        self._proc_targets: list = []
        # When each target was adopted (monotonic clock): the "querying…"
        # placeholder degrades to a stated cause past PROC_TARGET_GRACE_S — a
        # producer that predates proc_ports / runs without --config will
        # never answer, and a permanent "querying" would over-claim.
        self._proc_target_added: dict = {}
        # Presentation state for the Memory panel, re-read on every repaint since
        # _update_memory() rebuilds the layout each tick. Collapsed tiers and the
        # minimum row size persist (view state, via QSettings); the per-tier "show
        # small procs" expander and the name filter are session-only (a persisted
        # filter that hid most rows on next launch would just confuse). _last_snap
        # lets a toggle/filter re-render without waiting for a fresh frame.
        self._mem_collapsed = load_mem_collapsed(path=settings_path)
        self._mem_min_mib = load_mem_min_mib(path=settings_path)
        self._wsl_filter = ""   # the WSL section's tier filter (session-only)
        self._painting_frame = False   # update_from's rebuild-in-progress flag
        # Runners groups the user has EXPANDED (default collapsed — the
        # persisted set is the exceptions; a new repo group arrives folded).
        self._runners_expanded = load_runners_expanded(path=settings_path)
        # BURST-ONLY group expansions are SESSION-ONLY (never persisted): the
        # group itself is capped by the producer's ~10 min linger, so a disk
        # label would strand once the linger ends (that frame early-returns
        # past the prune — the CI review-gate finding) and pre-expand a
        # future same-initials group; kept in-memory instead, auto-dropped
        # when the group leaves the frame, and outage-safe (an empty frame
        # touches neither set).
        self._burst_expanded: set = set()
        self._burst_only_labels: set = set()
        self._active_style = load_active_style(path=settings_path)
        # The win cpu row's busy marker (view-state; Settings ▸ live-applied),
        # and the app-side busy trend: the feed's windows_cpu_history is
        # utility only, so busy % samples accumulate here, one per fresh
        # Windows sweep (a changed (utility, busy) pair).
        self._busy_marker_pct = load_busy_marker_pct(path=settings_path)
        self._wincpu_busy: deque = deque(maxlen=120)
        self._wincpu_last_pair = None
        self._mem_filter = ""
        self._mem_show_hidden: dict[str, bool] = {}
        # Expanded port rows (connection-detail dropdowns) — session-only like the
        # Memory expanders, keyed (label, port); pruned to the live watched set.
        self._ports_expanded: set = set()
        # Sessions list expanded past the top-N cap — session-only view state
        # (survives advancing frames like _ports_expanded). Collapsed by default
        # so a deep fleet only pays for the top N rows per repaint (#49).
        self._sessions_expanded = False
        self._cockpit_expanded = set()
        self._last_snap = None
        # The (frame clock, status) of the last panel repaint: update_from skips
        # the panel pass while both hold, so held/stale frames never tear down
        # and rebuild row widgets (see update_from).
        self._render_key = None
        # Rolling per-port activity for the Ports sparklines, accumulated
        # app-side from successive frames (deduped by the frame's own clock).
        # The time span it keeps is the persisted Ports-graph window setting.
        self._port_history = PortHistory(
            window_s=load_port_window_s(path=settings_path))
        # Rolling distinct-recent-clients memory per watched port — WHO hit
        # the port over the same window (a short-lived hitter keeps a
        # last-seen tag instead of vanishing between frames).
        self._port_clients = PortClients(
            window_s=load_port_window_s(path=settings_path))
        # The tray's own CPU (the app-side half of the producer's self_cost
        # bill — the feed is one-way, so the tray measures itself); fed in
        # update_from's hoisted data pass, rendered as the Host panel's
        # "monitor:" note. None while warming.
        self._selfcpu = SelfCpuWindow()
        self._tray_pct = None
        self.setWindowTitle("Vision Resource Monitor")
        # A concrete minimum size (not just width): some compositors — notably
        # WSLg's Weston via XWayland — otherwise map the window at 1x1.
        self.setMinimumSize(440, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # A menu bar so the window can reach Settings / the activity log / About on
        # its own — window-only mode (e.g. WSLg) has no tray menu.
        self._gear = None
        if on_settings or on_open_log or on_about:
            menubar = QMenuBar()
            menu = menubar.addMenu("Options")
            if on_settings:
                act = QAction("Settings…", self)
                act.triggered.connect(on_settings)
                menu.addAction(act)
            if on_open_log:
                act_log = QAction("Open activity log", self)
                act_log.triggered.connect(on_open_log)
                menu.addAction(act_log)
            if on_about:
                act_about = QAction("About", self)
                act_about.triggered.connect(on_about)
                menu.addAction(act_about)
            outer.setMenuBar(menubar)

        # An update banner above everything else — hidden until a newer build is
        # detected (show_update_banner). The button calls the app's rebuild action,
        # which shells `vrm rebuild` in WSL and falls back to showing the command.
        self._update_banner = QWidget()
        self._update_banner.setStyleSheet("background:#1f6feb;")
        bl = QHBoxLayout(self._update_banner)
        bl.setContentsMargins(12, 6, 12, 6)
        self._update_label = QLabel("New version available")
        self._update_label.setStyleSheet("color:white; font-weight:bold;")
        self._update_label.setWordWrap(True)
        # PlainText by construction: the reveal interpolates feed-derived SHAs,
        # and the update_from label pass (#73) need not have run yet.
        self._update_label.setTextFormat(Qt.PlainText)
        self._update_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        bl.addWidget(self._update_label, 1)
        self._update_btn = QPushButton("Rebuild now")
        if on_rebuild:
            self._update_btn.clicked.connect(on_rebuild)
        bl.addWidget(self._update_btn)
        self._update_banner.setVisible(False)
        outer.addWidget(self._update_banner)

        # A stale-producer notice below it (amber) — hidden until the app detects
        # a producer ignoring the written monitor-config (a service installed
        # before the configuration channel). Text-only: the hint is carried in
        # the message and selectable.
        self._producer_hint = None
        self._producer_banner = QWidget()
        self._producer_banner.setStyleSheet("background:#9e6a03;")
        hl = QHBoxLayout(self._producer_banner)
        hl.setContentsMargins(12, 6, 12, 6)
        self._producer_label = QLabel("")
        self._producer_label.setStyleSheet("color:white;")
        self._producer_label.setTextFormat(Qt.PlainText)
        self._producer_label.setWordWrap(True)
        self._producer_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._producer_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hl.addWidget(self._producer_label, 1)
        self._producer_banner.setVisible(False)
        outer.addWidget(self._producer_banner)

        self._status = QLabel("Connecting…")
        self._status.setTextFormat(Qt.RichText)
        self._status.setContentsMargins(14, 12, 14, 6)
        # The header row: the colour-coded status label plus an always-visible
        # ⚙ shortcut to Settings at the right edge (settings shortcut,
        # round-6 item 2) — one click, no Options menu hop. A plain layout
        # row, NOT a QMenuBar corner widget: the corner-widget path renders
        # blank offscreen (verified by screenshot 2026-07-26) and its layout
        # is style-dependent. The label keeps its own contents margins, so
        # its rendering is unchanged; the "None hides the affordance"
        # convention applies — no on_settings wiring, no dead button.
        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(0)
        hdr.addWidget(self._status, 1)
        if on_settings:
            self._gear = QToolButton()
            self._gear.setText("⚙")
            self._gear.setAutoRaise(True)
            self._gear.setCursor(Qt.PointingHandCursor)
            self._gear.setToolTip("Settings…")
            self._gear.setAccessibleName("Settings")
            self._gear.setStyleSheet(
                "QToolButton { margin: 8px 10px 0 0; font-size: 14px; }")
            self._gear.clicked.connect(on_settings)
            hdr.addWidget(self._gear, 0, Qt.AlignTop)
        outer.addLayout(hdr)

        # Panels live in a scroll area — the full stack is taller than the window.
        # It scrolls VERTICALLY only: the panels are a vertical stack, so a long
        # monospace line (history workloads, a run's error message) must never
        # widen the shared content past the viewport and clip every panel's right
        # column. Those variable-width labels get an Ignored h-policy (below) so
        # they clip their own text instead of forcing the content wide.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(14, 6, 14, 12)
        root.setSpacing(10)
        self._root_layout = root   # kept so set_panel_order() can restack live

        # The panel registry: (id, title, build, update). The window stack is built
        # from it, each panel wrapped in a collapsible section (chevron header),
        # so every panel can be collapsed to just its header — persisted per panel.
        # Host/Memory/Sessions are still gated by their show_* flags (Settings-driven
        # reordering + show/hide is a follow-up); the memory-pressure Forecast leads
        # the host-contention panels below it.
        panel_defs = {
            "gpu": ("GPU", self._build_gpu, self._update_gpu),
            "activity": ("Activity", self._build_activity, self._update_activity),
            "runs": ("Runs", self._build_runs, self._update_runs),
            "forecast": ("Forecast", self._build_forecast, self._update_forecast),
            # The 2026-07-29 host/WSL regroup (maintainer ask): each section
            # carries ALL the stats for its side of the box — id "host" is
            # the WSL section (wsl cpu/ram/processes + the monitor's own
            # bill), id "memory" the Windows-host section (win cpu + host
            # memory + docker containers/disk). The IDS deliberately keep
            # their old names so stored order/collapse/hidden state follows
            # the same physical panels through the retitle.
            "host": ("WSL", self._build_host, self._update_host),
            "memory": ("Host", self._build_memory, self._update_memory),
            "sessions": ("Sessions", self._build_sessions, self._update_sessions),
            "runners": ("Runners", self._build_runners, self._update_runners),
            "stuck": ("Stuck", self._build_stuck, self._update_stuck),
            "egress": ("Egress", self._build_egress, self._update_egress),
            "scanners": ("Scanners", self._build_scanners, self._update_scanners),
            "ports": ("Ports", self._build_ports, self._update_ports),
        }
        # Build ALL panels in the user's saved order (reconciled), each wrapped in a
        # collapsible section, then show/hide per the unified panel-hidden set
        # (Settings ▸ Panels checkboxes — the single source of truth for visibility;
        # the legacy show_* constructor flags are seeded into it once by the app).
        # Reorder + show/hide both also apply live (set_panel_order/set_panel_hidden).
        self._panel_collapsed = load_panel_collapsed(path=settings_path)
        self._panel_hidden = load_panel_hidden(path=settings_path)
        self._sections = {}       # panel id -> _CollapsibleSection (exposes .body)
        self._updaters = []       # (panel id, update fn) for every built panel
        # Panels whose body went stale because update_from skipped them while
        # hidden/collapsed — revealing one repaints it from the last frame.
        self._dirty_panels = set()
        # The watch-for zone caption: one dim separator line above the first
        # watch panel, shown only while the visible watch panels sit together
        # at the bottom of the stack (the default order). Placement recomputes
        # on reorder and show/hide (_place_watch_caption); an interleaved
        # custom order gets NO caption rather than a wrong one.
        self._watch_caption = QLabel("watch for")
        self._watch_caption.setStyleSheet(
            f"color:{_DIM}; font-weight:bold; letter-spacing:1px;")
        self._watch_caption.setContentsMargins(2, 4, 0, 0)
        self._watch_caption.setVisible(False)
        for pid in load_panel_order(path=settings_path):
            entry = panel_defs.get(pid)
            if entry is None:
                continue
            title, build, update = entry
            section = _CollapsibleSection(
                pid, title, build(), collapsed=pid in self._panel_collapsed,
                on_toggle=self._on_panel_toggle)
            self._sections[pid] = section
            self._updaters.append((pid, update))
            root.addWidget(section)
            section.setVisible(pid not in self._panel_hidden)
        root.addStretch(1)
        self._place_watch_caption()

        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        self.resize(460, 680)

        # Restore the last window position/size (view state) over the default
        # resize, when the user has one. restoreGeometry() must run before the
        # window is shown; a corrupt/foreign stored blob must never block startup.
        geo = load_window_geometry(path=settings_path)
        if geo is not None:
            try:
                self.restoreGeometry(geo)
            except Exception:  # noqa: BLE001 — a bad stored geometry must not crash startup
                pass

    # ── update banner ────────────────────────────────────────────────────────────
    def show_update_banner(self, build, source) -> None:
        """Reveal the in-app 'new version available' banner (build vs live source)."""
        self._update_label.setText(
            f"⬆ New version available — source {source or '?'} "
            f"(this build {build or '?'}). ")
        self._update_banner.setVisible(True)

    def hide_update_banner(self) -> None:
        self._update_banner.setVisible(False)

    # ── stale-producer notice ────────────────────────────────────────────────────
    def set_producer_hint(self, hint) -> None:
        """Show/hide the stale-producer notice (a state.ProducerConfigHint or
        None). The Ports panel's empty-state consults it too, so a configured-
        but-unreported port set no longer reads as "no ports watched"."""
        self._producer_hint = hint
        self._render_key = None   # the Ports empty-state renders it: repaint next tick
        if hint is None:
            self._producer_banner.setVisible(False)
            return
        self._producer_label.setText(hint.text)
        self._producer_banner.setVisible(True)

    # ── active-indicator style (view state) ─────────────────────────────────────
    def set_active_style(self, style: str) -> None:
        """Adopt the Active-indicator style (a state.ACTIVE_STYLES value) — used
        by the header + Activity line on their next repaint (the app ticks right
        after applying settings, so it is effectively immediate)."""
        self._active_style = style
        self._render_key = None   # the Activity line renders it: repaint next tick

    def _record_busy(self, snap) -> None:
        """Add a busy-trend sample per fresh Windows sweep: the producer holds
        one sweep between its 2 s ticks, so a repeated (utility, busy) pair is
        the same sweep. Runs per frame even while the Host panel is collapsed
        (like the port history) so the strip has no gaps when revealed."""
        wc = snap.windows_cpu if snap else None
        busy = wc.time_pct if wc is not None else None
        if busy is None or not math.isfinite(busy):
            return
        pair = (wc.utility_pct, busy)
        if pair != self._wincpu_last_pair:
            self._wincpu_last_pair = pair
            self._wincpu_busy.append(busy)

    # ── win cpu busy marker (view state) ────────────────────────────────────────
    def set_busy_marker_pct(self, pct: float) -> None:
        """Adopt the busy-marker percent live: the line, the red threshold and
        the label tooltip follow on the next repaint."""
        self._busy_marker_pct = pct
        self._apply_busy_marker()
        self._render_key = None

    def _apply_busy_marker(self) -> None:
        self._wincpu.set_marker(self._busy_marker_pct)
        self._wincpu_name.setToolTip(
            "Busy % (Windows Processor Time) across all logical processors. "
            f"The marker is at {self._busy_marker_pct:.0f}%; the bar and trend "
            "turn red from it. \"% of base clock\" in the note is Task "
            "Manager's frequency-scaled Utility figure and exceeds 100% "
            "under turbo. The value is read once per Windows sweep (every "
            "10-30 s), so a short spike can slip past the marker. The WSL "
            "section's cpu row sees the WSL VM only.")

    # ── window geometry (view state) ────────────────────────────────────────────
    def save_geometry(self) -> None:
        """Persist the current window position/size as view-state (best-effort)."""
        try:
            save_window_geometry(self.saveGeometry(), path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must never crash the UI
            pass

    def closeEvent(self, event):  # noqa: N802 (Qt override)
        # Closing the window (the X) hides it to tray in tray mode / quits in
        # window mode — either way, remember where the user left it.
        self.save_geometry()
        super().closeEvent(event)

    # ── panel construction ────────────────────────────────────────────────────
    def _build_gpu(self):
        box = QGroupBox("GPU")
        g = QGridLayout(box)
        self._gpu_name = QLabel("—"); self._gpu_name.setStyleSheet(f"color:{_DIM};")
        self._compute = GaugeBar(); self._compute_lbl = QLabel("—")
        self._vram = GaugeBar(); self._vram_lbl = QLabel("—")
        self._gpu_extra = _NoteLabel("—"); self._gpu_extra.setStyleSheet(f"color:{_DIM};")
        self._spark = Sparkline()
        g.addWidget(self._gpu_name, 0, 0, 1, 3)
        g.addWidget(QLabel("compute"), 1, 0)
        g.addWidget(self._compute, 1, 1); g.addWidget(self._compute_lbl, 1, 2)
        g.addWidget(QLabel("vram"), 2, 0)
        g.addWidget(self._vram, 2, 1); g.addWidget(self._vram_lbl, 2, 2)
        g.addWidget(self._gpu_extra, 3, 0, 1, 3)
        g.addWidget(self._spark, 4, 0, 1, 3)
        g.setColumnStretch(1, 1)
        return box

    def _build_activity(self):
        box = QGroupBox("Activity")
        a = QVBoxLayout(box)
        self._act_status = QLabel("—")
        # Carries our own <b> markup (● ACTIVE / ○ idle head): explicit
        # RichText, or the update_from label pass would render it literally.
        # Its interpolations are all app-formatted numbers/clocks, never
        # producer strings.
        self._act_status.setTextFormat(Qt.RichText)
        self._act_workloads = _NoteLabel(); self._act_workloads.setStyleSheet(f"color:{_DIM};")
        self._act_workloads.setWordWrap(True)
        # The "WHAT is loading into the VRAM" note (snap.gpu_users): Windows-side
        # dedicated-VRAM holders in the Copy-PID name[pid] form; while ACTIVE with
        # no workload names it degrades to an honest unattributed hint instead of
        # silence. Full list rides the tooltip (the line wraps/clips).
        self._act_vram = _NoteLabel(); self._act_vram.setStyleSheet(f"color:{_DIM};")
        # The vram holders are WINDOWS pids (GPU perf counters) — right-click
        # gains "Show ports of …" per holder in the windows namespace.
        self._act_vram.setProperty("vrm_proc_side", "windows")
        self._act_vram.setWordWrap(True)
        # Idle-only line: the GPU's at-rest baseline (temp/power/VRAM) so the panel
        # shows the card idling rather than going blank. Cleared while active.
        self._act_idle = _NoteLabel(); self._act_idle.setStyleSheet(f"color:{_DIM};")
        self._history = _NoteLabel(); self._history.setStyleSheet(f"color:{_DIM}; font-family: monospace;")
        # Long workload names would otherwise make this monospace table dictate a
        # ~900px min width and clip every panel's right column — let it clip its
        # own overflow to whatever width is available instead.
        self._history.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        a.addWidget(self._act_status)
        a.addWidget(self._act_workloads)
        a.addWidget(self._act_vram)
        a.addWidget(self._act_idle)
        a.addWidget(self._history)
        return box

    def _build_runs(self):
        box = QGroupBox("Runs")
        self._runs_layout = QVBoxLayout(box)
        return box

    def _build_forecast(self):
        """The memory-pressure Forecast panel: the producer's leading-indicator
        early-warning (level + ETA/headroom) over the worst-first ranked
        contributors, each with its recommended SAFE action. Advisory ONLY — the
        app never acts on the host (warn-and-suggest, docs § 8). Quiet by design:
        level `ok` with nothing firing reads as a single "clear" line, not a blank
        panel — the same informative-not-blank stance the other panels take."""
        box = QGroupBox("Forecast")
        v = QVBoxLayout(box)
        self._fc_status = QLabel("—")
        self._fc_status.setTextFormat(Qt.RichText)
        self._fc_status.setWordWrap(True)
        self._fc_detail = _NoteLabel(); self._fc_detail.setStyleSheet(f"color:{_DIM};")
        self._fc_detail.setWordWrap(True)
        self._fc_layout = QVBoxLayout()
        v.addWidget(self._fc_status)
        v.addWidget(self._fc_detail)
        v.addLayout(self._fc_layout)
        return box

    def _contributor_block(self, c):
        """A ranked-contributor entry: an amber '#rank label — summary' headline
        over an indented '↳ action' suggestion (the SAFE recommended step). Both
        wrap; the action line is omitted when the producer gave no action for the
        kind. When the app wired on_ask_claude, the headline row carries an
        "ask claude" button handing the whole contributor to an interactive
        session (the click captures the contributor object, never the widgets —
        this panel is torn down on every advancing frame, #46 lesson)."""
        row = QWidget()
        v = QVBoxLayout(row); v.setContentsMargins(0, 2, 0, 0); v.setSpacing(0)
        head = QLabel(f"{c.rank}. {c.label} — {c.summary}")
        head.setStyleSheet(f"color:{YELLOW};")
        head.setWordWrap(True)
        if self._on_ask_claude is None:
            v.addWidget(head)
        else:
            top = QWidget()
            h = QHBoxLayout(top); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(6)
            h.addWidget(head, 1)
            btn = QPushButton("ask claude")
            btn.setObjectName("ask_claude")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(
                "Open a WSL terminal running Claude, pre-loaded with this "
                "contributor's context (label, summary, advisory action, exact "
                "PID). Diagnose-and-advise only — the session is pre-approved "
                "for read-only diagnostics and denied every destructive "
                "command; it never changes the host itself. (First run seeds a "
                "read-only-permissions worker folder in WSL.)")
            btn.setStyleSheet(
                f"QPushButton {{ color:{_SPARK_ACCENT}; border:1px solid "
                f"{_SPARK_ACCENT}; border-radius:3px; padding:0px 8px; "
                "background:transparent; }"
                " QPushButton:hover { background:rgba(88,166,255,0.15); }")
            btn.clicked.connect(
                lambda _=False, cc=c: self._on_ask_claude(cc))
            h.addWidget(btn, 0, Qt.AlignTop)
            v.addWidget(top)
        if c.action:
            act = QLabel(f"    ↳ {c.action}")
            act.setStyleSheet(f"color:{_DIM};")
            act.setWordWrap(True)
            v.addWidget(act)
        return row

    def _build_host(self):
        """The WSL section (2026-07-29 regroup): everything WSL-side in one
        place — cpu gauge + split/load note + trend, VM ram, the wsl
        process-memory tier (moved in from the old Memory panel, keeping its
        tier key/collapse/expand state), and the monitor's own bill as the
        footer. The whole-box `win cpu` row moved OUT to the Windows-host
        section (_build_memory) where the rest of the Windows data lives."""
        box = QGroupBox("WSL")
        self._host_box = box
        g = QGridLayout(box)
        self._host_sub = QLabel(""); self._host_sub.setStyleSheet(f"color:{_DIM};")
        self._cpu = GaugeBar(); self._cpu_lbl = QLabel("—")
        self._cpu_extra = _NoteLabel(); self._cpu_extra.setStyleSheet(f"color:{_DIM};")
        # The note gained the #213 user/sys split + load/core — at narrow
        # widths the single line would force the scroll content wider than
        # the viewport (the horizontal-overflow rule); wrapping keeps every
        # segment readable instead of clipping the tail.
        self._cpu_extra.setWordWrap(True)
        self._cpu_trend = Sparkline()
        self._ram = GaugeBar(); self._ram_lbl = QLabel("—")
        self._ram_note = _NoteLabel(); self._ram_note.setStyleSheet(f"color:{_DIM};")
        self._host_note = _NoteLabel(); self._host_note.setStyleSheet(f"color:{_DIM};")
        # The wsl process-memory tier's own name filter (the moved tier must
        # not be steered by ANOTHER panel's filter box — action at a
        # distance); the size threshold stays the shared persisted
        # _mem_min_mib (one view preference, named in the "N more under X"
        # row itself).
        self._wsl_filter_edit = QLineEdit()
        self._wsl_filter_edit.setPlaceholderText("filter wsl processes by name…")
        self._wsl_filter_edit.setClearButtonEnabled(True)
        self._wsl_filter_edit.textChanged.connect(self._on_wsl_filter_changed)
        self._wsl_mem_layout = QVBoxLayout()
        # What the monitoring stack itself costs (producer self_cost + the
        # tray's own window) — the footprint ledger's standing readout.
        # Wrapped like _cpu_extra: the full form carries four segments.
        self._host_selfcost = _NoteLabel()
        self._host_selfcost.setStyleSheet(f"color:{_DIM};")
        self._host_selfcost.setWordWrap(True)
        # The ~2 h self-cost trend under the note (producer-downsampled
        # self_cost_history). Fixed DIM: the util ladder's colours mean
        # health-alert states, and the monitor's own footprint must not
        # read as one. Hidden until the feed carries ≥2 drawable samples
        # (older producer / warming — no dead row, the hide-don't-clear
        # rule).
        self._host_selfcost_trend = Sparkline(color=_DIM)
        self._host_selfcost_trend.setVisible(False)
        g.addWidget(self._host_sub, 0, 0, 1, 3)
        cpu_name = QLabel("cpu")
        cpu_name.setToolTip(
            "Virtual cores are the WSL VM's vCPUs. The first number is how "
            "many were at least 10% busy in the last sample window; the "
            "second is how many the VM has (processors= in .wslconfig). "
            "This row sees the WSL VM only.")
        g.addWidget(cpu_name, 1, 0)
        g.addWidget(self._cpu, 1, 1); g.addWidget(self._cpu_lbl, 1, 2)
        g.addWidget(self._cpu_extra, 2, 0, 1, 3)
        g.addWidget(self._cpu_trend, 3, 0, 1, 3)
        g.addWidget(QLabel("ram"), 4, 0)
        g.addWidget(self._ram, 4, 1); g.addWidget(self._ram_lbl, 4, 2)
        g.addWidget(self._ram_note, 5, 0, 1, 3)
        g.addWidget(self._host_note, 6, 0, 1, 3)
        g.addWidget(self._wsl_filter_edit, 7, 0, 1, 3)
        g.addLayout(self._wsl_mem_layout, 8, 0, 1, 3)
        g.addWidget(self._host_selfcost, 9, 0, 1, 3)
        g.addWidget(self._host_selfcost_trend, 10, 0, 1, 3)
        g.setColumnStretch(1, 1)
        return box

    def _build_memory(self):
        """The Windows-host section (2026-07-29 regroup): everything
        Windows-side in one place — the whole-box `win cpu` row (moved in
        from the old Host panel), the windows host-memory tier, and the
        docker containers + disk tiers (Docker Desktop runs Windows-side).
        The wsl process tier moved OUT to the WSL section (_build_host)."""
        box = QGroupBox("Host")
        outer = QVBoxLayout(box)
        # Whole physical box CPU (the epic-#9 maintainer ask): busy % (Windows
        # Processor Time) across all logical processors, with a marker at the
        # busy threshold; the frequency-scaled utility figure (may exceed 100
        # under turbo) rides in the note.
        wgrid = QGridLayout()
        self._wincpu = GaugeBar(); self._wincpu_lbl = QLabel("—")
        self._wincpu_extra = _NoteLabel()
        self._wincpu_extra.setStyleSheet(f"color:{_DIM};")
        self._wincpu_extra.setWordWrap(True)
        wlbl = QLabel("win cpu")
        self._wincpu_name = wlbl
        wgrid.addWidget(wlbl, 0, 0)
        wgrid.addWidget(self._wincpu, 0, 1); wgrid.addWidget(self._wincpu_lbl, 0, 2)
        wgrid.addWidget(self._wincpu_extra, 1, 0, 1, 3)
        # The busy trend (app-side _wincpu_busy, one sample per fresh host
        # sweep — 10-30 s cadence, NOT the 2 s tick like the WSL trend).
        # Hidden until ≥2 samples (warming / an older producer — the
        # hide-don't-clear rule).
        self._wincpu_trend = Sparkline()
        self._wincpu_trend.setVisible(False)
        self._wincpu_trend.setToolTip(
            "Busy %, one point per Windows sweep (every 10-30 s), since this "
            "window opened.")
        wgrid.addWidget(self._wincpu_trend, 2, 0, 1, 3)
        wgrid.setColumnStretch(1, 1)
        self._apply_busy_marker()
        outer.addLayout(wgrid)
        # A persistent filter row that _update_memory() must NOT rebuild — the
        # process/tier stack below is cleared every tick, so keeping these controls
        # out of _mem_layout preserves the QLineEdit's focus and text while typing.
        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        self._mem_filter_edit = QLineEdit()
        self._mem_filter_edit.setPlaceholderText("filter processes by name…")
        self._mem_filter_edit.setClearButtonEnabled(True)
        self._mem_min_spin = QDoubleSpinBox()
        self._mem_min_spin.setRange(0.0, 64.0)
        self._mem_min_spin.setSingleStep(0.5)
        self._mem_min_spin.setDecimals(1)
        self._mem_min_spin.setSuffix(" G")
        self._mem_min_spin.setToolTip(
            "Minimum size shown directly; smaller processes collapse into the "
            "'N more' group (0 = show every process)")
        self._mem_min_spin.setValue(self._mem_min_mib / 1024.0)
        # Connect AFTER setValue so the initial programmatic value doesn't fire a
        # spurious save/re-render before the first frame.
        self._mem_filter_edit.textChanged.connect(self._on_mem_filter_changed)
        self._mem_min_spin.valueChanged.connect(self._on_mem_min_changed)
        controls.addWidget(self._mem_filter_edit, 1)
        min_lbl = QLabel("min"); min_lbl.setStyleSheet(f"color:{_DIM};")
        controls.addWidget(min_lbl)
        controls.addWidget(self._mem_min_spin)
        outer.addLayout(controls)

        self._mem_layout = QVBoxLayout()
        outer.addLayout(self._mem_layout)
        return box

    def _build_sessions(self):
        """The Sessions panel: per-Claude/Code session attribution (label, node
        count, RSS, age, stale) + a node/session/load sub-line + the stale
        high-cost contributors. Answers "which session is eating the box," which
        the GPU panels can't. Ported from the Mac counterpart's Sessions panel."""
        box = QGroupBox("Sessions")
        v = QVBoxLayout(box)
        self._sess_sub = QLabel("—"); self._sess_sub.setStyleSheet(f"color:{_DIM};")
        self._sess_sub.setWordWrap(True)
        self._sess_alerts = _NoteLabel(); self._sess_alerts.setWordWrap(True)
        # Filter field (mac #49/#55 port, epic #9 round 3): with dozens of
        # sessions, search beats scrolling. Non-empty → EVERY match is shown
        # (the top-N collapse is a repaint-cost guard for the unfiltered
        # default; a filtered set is already small). Matches the label, the
        # session's root pid, OR any pid in the producer's child-pid index
        # (producer) — "which session owns 47112?" for a pid copied via Copy
        # PID (usually a node/MCP child of the root, e.g. off a Ports row).
        self._sess_filter = QLineEdit()
        self._sess_filter.setPlaceholderText("filter sessions (label, pid, cockpit, lane or name)…")
        self._sess_filter.setClearButtonEnabled(True)
        self._sess_filter.textChanged.connect(self._on_session_filter)
        self._sess_layout = QVBoxLayout()
        v.addWidget(self._sess_sub)
        v.addWidget(self._sess_alerts)
        v.addWidget(self._sess_filter)
        v.addLayout(self._sess_layout)
        return box

    def _session_row(self, label, metrics, stale, pid=None,
                     session_label=None, node_count=0, session=None):
        """A session row: the label on the left takes all available width, then a
        right-aligned compact ``stale`` tag + metrics group that always stays
        aligned. The label gets an Ignored h-policy so a long task label CLIPS to
        the available width instead of forcing the shared scroll-content past the
        viewport (the horizontal-overflow rule the Activity/Runs labels follow).
        ``pid`` (the session's WSL pid — the row text doesn't carry it) arms
        the right-click "Show ports of PID N"; the session LABEL is not a comm,
        so no requested name is seeded. Rows with a root pid and node children
        carry the *release* button — the human-initiated SIGTERM of the
        session's node (MCP) fleet, confirmation-gated (see ``release``'s
        rails; mac #49 port)."""
        row = QWidget()
        is_idle = bool(session and session.role == "lane" and session.idle is True)
        row.setProperty("vrm_idle", is_idle)
        detail_tip = "\n".join(
            [f"last event age: {session.last_event_age_s:.1f}s"
             if session and session.last_event_age_s is not None
             else "last event age: unknown",
             f"swap: {session.swap_mib:.1f} MiB"
             if session and session.swap_mib is not None else "swap: unknown",
             f"idle: {session.idle}" if session and session.idle is not None
             else "idle: unknown"])
        row.setToolTip(_tip(detail_tip))
        if is_idle:
            row.setStyleSheet("background-color: rgba(210, 153, 34, 24);")
        h = QHBoxLayout(row); h.setContentsMargins(12, 0, 0, 0)
        nm = QLabel(label)
        nm.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        # Ignored h-policy clips a long name — and the "(N node)" suffix clips
        # first — so carry the full text on the tooltip (the Ports name label
        # does the same) to keep the promoted node count recoverable on hover.
        nm.setToolTip(_tip(f"{label}\n{detail_tip}"))
        if pid:
            nm.setProperty("vrm_proc_side", "wsl")
            nm.setProperty("vrm_proc_pid", int(pid))
        h.addWidget(nm, 1)
        if pid and node_count:
            rel = QPushButton("release")
            rel.setCursor(Qt.PointingHandCursor)
            rel.setStyleSheet(
                f"QPushButton {{ color:{_DIM}; background:transparent;"
                f" border:1px solid {_DIM}; border-radius:3px;"
                f" padding:0px 6px; }}")
            rel.setToolTip(_tip(
                "SIGTERM this session's node (MCP) processes — lists the "
                "exact PIDs and asks first; each is re-verified inside WSL "
                "at the moment of signalling; the session process itself is "
                "not touched"))
            # Plain click-time data only (pid int + label str) — the flow
            # never touches this row again, so the ~2 s rebuild can't bite
            # (the mac #38 row-lifetime lesson, already this app's idiom).
            # The RAW session label (never the "(N node)"-decorated row text)
            # goes to the confirm dialog: session_label is passed on every
            # real path, so fall back to the decorated label only when it is
            # genuinely absent — an empty string is still the real label
            # (review L1).
            ll = session_label if session_label is not None else label
            if live_actions_enabled():
                rel.clicked.connect(
                    lambda _=False, p=int(pid), ll=ll:
                    self._release_session(p, ll))
            else:
                # Shown but disabled, with the stated reason (no silent no-op).
                rel.setEnabled(False)
                rel.setToolTip(_tip(f"release: {LIVE_ACTIONS_OFF_REASON}"))
            h.addWidget(rel)
        if stale:
            tag = QLabel("stale"); tag.setStyleSheet(f"color:{YELLOW};")
            h.addWidget(tag)
        val = QLabel(metrics); val.setStyleSheet(f"color:{_DIM};")
        val.setToolTip(_tip(detail_tip))
        h.addWidget(val)
        return row

    # ── session release (human-initiated; see the release module's rails) ────
    def _release_session(self, root_pid, label):
        """The release flow: fresh WSL sweep → candidates → confirmation
        listing every exact PID → Python-reverified TERM → per-pid outcome
        report. Holds only plain data (never row widgets); every dialog
        parents to the window; a failed sweep states its cause and touches
        nothing (failed sweeps are never empty answers).

        The two bridged calls (a ~1-2 s ``ps`` sweep, then an instant ``kill``)
        run synchronously behind a wait cursor. Unlike the casual right-click
        procexplorer launch (async QProcess), this is a DELIBERATE, confirmed,
        destructive action — a brief bounded wait on an explicit click is the
        expected desktop contract (cf. an "empty trash" pause), and a
        synchronous flow keeps the kill path simple and fully offscreen-
        verifiable. Timeouts bound a hung interop (~5 s each)."""
        if not live_actions_enabled():
            self._show_release_result(label, [], note=LIVE_ACTIONS_OFF_REASON)
            return
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            rows = release.read_ps_rows()
        finally:
            QGuiApplication.restoreOverrideCursor()
        if rows is None:
            self._show_release_result(
                label, {}, note="the WSL process sweep did not answer — "
                                "nothing was signalled; try again")
            return
        kids = release.node_children(root_pid, rows)
        if not kids:
            self._show_release_result(
                label, {}, note="no node processes found under this session "
                                "(they may have already exited, or "
                                "belong to a nested session)")
            return
        if not self._confirm_release(label, kids):
            return
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            outcomes = release.release_nodes(root_pid, [k["pid"] for k in kids])
        finally:
            QGuiApplication.restoreOverrideCursor()
        self._show_release_result(label, outcomes)

    def _confirm_release(self, label, kids):
        """Modal confirmation — the human sees every PID before anything is
        signalled. A seam the offscreen smoke replaces (no real modal).
        PlainText: the session label is producer-derived and may carry markup
        (the #73 rule) — AutoText would both inject and collapse the newlines
        of the per-PID list the human must read."""
        box = QMessageBox(self)
        box.setWindowTitle("Release node processes")
        box.setIcon(QMessageBox.Warning)
        box.setTextFormat(Qt.PlainText)
        box.setText(release.confirm_text(label, kids))
        box.setStandardButtons(QMessageBox.Cancel | QMessageBox.Ok)
        box.setDefaultButton(QMessageBox.Cancel)
        box.button(QMessageBox.Ok).setText("SIGTERM them")
        return box.exec() == QMessageBox.Ok

    def _show_release_result(self, label, outcomes, note=None):
        """Per-pid outcome report (a seam for the smoke, like the confirm).
        A constructed box, PlainText — the label and any bridge-stderr in an
        ``error: …`` outcome are producer/environment-derived (#73)."""
        box = QMessageBox(self)
        box.setWindowTitle("Release node processes")
        box.setIcon(QMessageBox.Information)
        box.setTextFormat(Qt.PlainText)
        box.setText(note or release.result_text(label, outcomes))
        box.exec()

    def _session_toggle(self, text):
        """The Sessions list expand/collapse control (the port-row interaction:
        whole-line hit target, pointing cursor; a right-click Copy line rides the
        shared copy-menu on the inner label). Left-click flips the expanded
        state and re-renders from the last frame."""
        row = _ClickRow(); row.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(row); h.setContentsMargins(12, 0, 0, 0)
        lbl = QLabel(text); lbl.setStyleSheet(f"color:{_DIM};")
        h.addWidget(lbl); h.addStretch(1)
        row.clicked.connect(self._toggle_sessions)
        return row

    def _toggle_sessions(self):
        """Flip the Sessions list expanded state (session-only), then re-render
        from the last frame — no waiting for a fresh one."""
        self._sessions_expanded = not self._sessions_expanded
        if self._last_snap is not None:
            self._update_sessions(self._last_snap)

    def _toggle_cockpit(self, cockpit):
        if cockpit in self._cockpit_expanded:
            self._cockpit_expanded.remove(cockpit)
        else:
            self._cockpit_expanded.add(cockpit)
        if self._last_snap is not None:
            self._update_sessions(self._last_snap)

    def _on_session_filter(self, _text):
        """Re-render the session list from the last frame as the filter types
        — no waiting for a fresh snapshot (the toggle's pattern; the field
        itself lives outside the rebuilt layout, so focus survives)."""
        if self._last_snap is not None:
            self._update_sessions(self._last_snap)

    # ── small row builders (memory panel) ──────────────────────────────────────
    @staticmethod
    def _dim_label(text):
        lbl = QLabel(text); lbl.setStyleSheet(f"color:{_DIM};")
        return lbl

    @staticmethod
    def _status_row(name, reason, *, warn):
        """A tier row for a blank tier: ``name`` then either an amber
        "⚠ unavailable" tag (``warn``) or nothing, then a dim reason. Keeps an
        unpopulated tier visible with a stated cause instead of vanishing."""
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(0, 4, 0, 0)
        nm = QLabel(name); nm.setStyleSheet(f"color:{_DIM}; font-weight:bold;")
        nm.setMinimumWidth(70)
        h.addWidget(nm)
        if warn:
            tag = QLabel("⚠ unavailable")
            tag.setStyleSheet(f"color:{_WARN}; font-weight:bold;")
            h.addWidget(tag)
        if reason:
            note = QLabel(reason); note.setStyleSheet(f"color:{_DIM};")
            note.setWordWrap(True)
            h.addWidget(note, 1)
        h.addStretch(0 if reason else 1)
        return row

    # ── collapsible tier headers ────────────────────────────────────────────────
    def _tier_header(self, key, text, *, used_mib=None, total_mib=None):
        """A clickable Memory-tier header: a chevron (▾ open / ▸ collapsed) + the
        tier name, plus the windows tier's used/total gauge. Clicking toggles the
        tier and persists the collapsed set."""
        collapsed = key in self._mem_collapsed
        row = _ClickRow(); row.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(row); h.setContentsMargins(0, 4, 0, 0)
        chev = QLabel("▸" if collapsed else "▾")
        chev.setStyleSheet(f"color:{_DIM}; font-weight:bold;"); chev.setFixedWidth(14)
        nm = QLabel(text); nm.setStyleSheet(f"color:{_DIM}; font-weight:bold;")
        h.addWidget(chev); h.addWidget(nm)
        if total_mib is not None:
            gauge = GaugeBar()
            pct = used_mib / total_mib * 100 if total_mib else 0
            gauge.set_value(used_mib / total_mib if total_mib else 0, pct)
            val = QLabel(f"{used_mib/1024:.1f}/{total_mib/1024:.1f}G")
            h.addWidget(gauge, 1); h.addWidget(val)
        else:
            h.addStretch(1)
        row.clicked.connect(lambda: self._toggle_tier(key))
        return row

    def _hidden_summary(self, key, hidden):
        """The collapsible 'N more under <threshold>' expander row for a tier."""
        expanded = self._mem_show_hidden.get(key, False)
        total = sum(p.mem_mib for p in hidden)
        row = _ClickRow(); row.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(row); h.setContentsMargins(12, 0, 0, 0)
        chev = QLabel("▾" if expanded else "▸"); chev.setStyleSheet(f"color:{_DIM};")
        chev.setFixedWidth(14)
        nm = QLabel(f"{len(hidden)} more under {_fmt_mem(self._mem_min_mib)}")
        nm.setStyleSheet(f"color:{_DIM};")
        val = QLabel(_fmt_mem(total)); val.setStyleSheet(f"color:{_DIM};")
        h.addWidget(chev); h.addWidget(nm); h.addStretch(1); h.addWidget(val)
        row.clicked.connect(lambda: self._toggle_hidden(key))
        return row

    def _mem_add_procs(self, key, procs, *, windows=False, layout=None,
                       query=None):
        """Render a tier's process rows: the ≥threshold/LLM rows always, then a
        collapsible 'N more' group for the sub-threshold remainder (expanded in
        place when the user has opened it). An active name filter shows every
        match directly and adds no 'N more' group — and, on the wsl tier, says
        which watched ports the match holds (the search↔ports tie-in). WSL tier
        ONLY: the producer's socket view runs inside WSL and can never see a
        Windows host process, so a same-named windows-tier row is a different
        process the note must not claim. Search-gated so the per-row port scan
        never runs on a full unfiltered repaint (the standing footprint rule).
        `layout`/`query` default to the Windows-host panel's stack + filter;
        the moved wsl tier (2026-07-29 regroup) passes its own — each panel's
        filter box steers only its own rows."""
        layout = layout if layout is not None else self._mem_layout
        query = query if query is not None else self._mem_filter
        filtering = bool(query.strip())
        shown, hidden = split_process_rows(
            procs, query=query, min_mib=self._mem_min_mib)
        if filtering and not shown:
            layout.addWidget(self._dim_label("   (no matches)"))
            return
        snap_ports = (self._last_snap.ports
                      if key == "wsl" and filtering and self._last_snap
                      else [])
        for p in shown:
            if windows and p.is_vmmem:
                note, col = "← WSL2 VM (all distros + Docker)", _DIM
            elif p.llm:
                note, col = "[LLM]", _LLM
            else:
                note, col = "", _DIM
            tie = (format_port_holds(ports_held_by(p.name, snap_ports))
                   if snap_ports else "")
            if tie:
                note = f"{note}  ·  {tie}" if note else tie
            # Windows-tier rows are name-aggregated (no pid in the snapshot);
            # tagging the namespace + name arms right-click ▸ Open in Process
            # Explorer (#61), which resolves the pid at click time.
            layout.addWidget(self._kv_row(
                p.name, _fmt_mem(p.mem_mib), note, col,
                note_tooltip=tie,
                proc_side="windows" if windows else None,
                proc_name=p.name if windows else None))
        if hidden:
            layout.addWidget(self._hidden_summary(key, hidden))
            if self._mem_show_hidden.get(key):
                for p in hidden:
                    note, col = ("[LLM]", _LLM) if p.llm else ("", _DIM)
                    layout.addWidget(self._kv_row(
                        p.name, _fmt_mem(p.mem_mib), note, col,
                        proc_side="windows" if windows else None,
                        proc_name=p.name if windows else None))

    def _toggle_tier(self, key):
        self._mem_collapsed.discard(key) if key in self._mem_collapsed \
            else self._mem_collapsed.add(key)
        try:
            save_mem_collapsed(self._mem_collapsed, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must never crash the UI
            pass
        # The wsl tier lives in the WSL section since the 2026-07-29 regroup
        # — repaint the panel that actually renders the toggled tier.
        if key == "wsl":
            self._update_host(self._last_snap)
        else:
            self._update_memory(self._last_snap)

    def _toggle_hidden(self, key):
        self._mem_show_hidden[key] = not self._mem_show_hidden.get(key, False)
        if key == "wsl":
            self._update_host(self._last_snap)
        else:
            self._update_memory(self._last_snap)

    # ── panel-level collapse (whole sections) ────────────────────────────────────
    def _on_panel_toggle(self, panel_id, collapsed):
        """A panel section was collapsed/expanded — track it and persist the set
        (view-state, per settings path). Distinct from the Memory tier collapse
        above (a tier within Memory); this is the whole-panel state."""
        if collapsed:
            self._panel_collapsed.add(panel_id)
        else:
            self._panel_collapsed.discard(panel_id)
        try:
            save_panel_collapsed(self._panel_collapsed, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must never crash the UI
            pass
        if not collapsed and panel_id not in self._panel_hidden:
            self._refresh_if_dirty(panel_id)

    def _refresh_if_dirty(self, pid):
        """Repaint one panel from the last frame if update_from skipped it while
        hidden/collapsed. The reveal paths (panel expand, Settings re-show) call
        this, so a skipped panel is never SEEN stale — it repaints in the same
        event before the reveal can paint."""
        if pid not in self._dirty_panels:
            return
        self._dirty_panels.discard(pid)
        for upd_pid, update in self._updaters:
            if upd_pid == pid:
                update(self._last_snap)
                break
        # This path bypasses update_from — attach copy menus to the fresh rows.
        self._refresh_copy_menus()

    def set_panel_order(self, order):
        """Restack the panel sections in place to match ``order`` (a list of panel
        ids) — applied LIVE, no restart. Existing section widgets are detached and
        re-added in the new order, so every panel keeps its state (Memory filter,
        sparklines, collapsed state). Unknown / not-shown ids are skipped; any shown
        section absent from ``order`` keeps its place at the end."""
        lay = self._root_layout
        # takeAt only detaches from the layout — it does NOT delete the section
        # widgets, so re-adding them below preserves their state.
        while lay.count():
            lay.takeAt(0)
        seen = set()
        for pid in order:
            sec = self._sections.get(pid)
            if sec is not None and pid not in seen:
                seen.add(pid)
                lay.addWidget(sec)
        for pid, sec in self._sections.items():   # safety net (order is reconciled)
            if pid not in seen:
                lay.addWidget(sec)
        lay.addStretch(1)
        self._place_watch_caption()

    def _stack_order(self) -> list:
        """The panel ids in their CURRENT display order, read straight off the
        root layout (the sections dict keeps build order, which a live restack
        leaves behind)."""
        lay = self._root_layout
        out = []
        for i in range(lay.count()):
            w = lay.itemAt(i).widget()
            if isinstance(w, _CollapsibleSection):
                out.append(w.panel_id)
        return out

    def _place_watch_caption(self):
        """(Re)position the watch-zone caption: directly above the first
        visible watch panel, shown only when the visible watch panels form the
        contiguous BOTTOM of the visible stack — the caption labels a zone,
        and a custom order that interleaves zones gets no caption rather than
        a lying one. Also hidden when the watch zone is the whole visible
        stack (nothing above it to separate from)."""
        lay = self._root_layout
        idx = lay.indexOf(self._watch_caption)
        if idx != -1:
            lay.takeAt(idx)
        visible = [pid for pid in self._stack_order()
                   if pid not in self._panel_hidden]
        watch = [pid for pid in visible if pid in WATCH_PANEL_IDS]
        contiguous = bool(watch) and visible[-len(watch):] == watch
        if not contiguous or watch[0] == visible[0]:
            self._watch_caption.setVisible(False)
            self._watch_caption.setParent(None)
            return
        sec = self._sections[watch[0]]
        lay.insertWidget(lay.indexOf(sec), self._watch_caption)
        self._watch_caption.setVisible(True)

    def set_panel_hidden(self, hidden):
        """Show/hide panels live to match ``hidden`` (a set of ids) — toggles each
        section's visibility (the whole section: header + body). No rebuild, so a
        re-shown panel keeps its state. Distinct from collapse, which hides only a
        shown panel's body."""
        self._panel_hidden = set(hidden)
        for pid, sec in self._sections.items():
            visible = pid not in self._panel_hidden
            sec.setVisible(visible)
            # A re-shown panel skipped while hidden repaints now; one that is
            # ALSO collapsed stays dirty — its body is still not visible, and
            # the expand toggle repaints it then.
            if visible and pid not in self._panel_collapsed:
                self._refresh_if_dirty(pid)
        self._place_watch_caption()

    # ── process filter + size threshold ─────────────────────────────────────────
    @property
    def _filtering(self) -> bool:
        return bool(self._mem_filter.strip())

    def _filter_named(self, items):
        """Filter items carrying a ``.name`` by the active case-insensitive query
        (returns them unchanged when no filter is set)."""
        q = self._mem_filter.strip().lower()
        return [x for x in items if q in x.name.lower()] if q else list(items)

    def _on_mem_filter_changed(self, text):
        # Session-only; just re-render the last frame in place (keeps edit focus).
        self._mem_filter = text
        self._update_memory(self._last_snap)

    def _on_wsl_filter_changed(self, text):
        # The WSL section's own tier filter (2026-07-29 regroup) — same
        # session-only, repaint-in-place contract as the Host filter above.
        self._wsl_filter = text
        self._update_host(self._last_snap)

    def _on_mem_min_changed(self, value_g):
        self._mem_min_mib = float(value_g) * 1024.0
        try:
            save_mem_min_mib(self._mem_min_mib, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must never crash the UI
            pass
        # The threshold is SHARED by both sections' tiers since the regroup —
        # repaint both, or the WSL tier's rows and its "N more under X" text
        # go stale against the new split (review H1).
        self._update_memory(self._last_snap)
        self._update_host(self._last_snap)

    @staticmethod
    def _kv_row(name, value_text, note="", note_color=_DIM, indent=True,
                note_tooltip="", proc_side=None, proc_pid=None, proc_name=None):
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(12 if indent else 0, 0, 0, 0)
        nm = QLabel(name); nm.setMinimumWidth(150)
        # A row whose subject is a PROCESS may name the pid namespace it lives
        # in (proc_side "wsl"|"windows") — the right-click menu then offers
        # "Show ports of …" (explicit proc_pid when the text doesn't carry the
        # pid; proc_name only when it is a real comm — the producer checks it
        # against the live one).
        if proc_side:
            nm.setProperty("vrm_proc_side", proc_side)
            if proc_pid is not None:
                nm.setProperty("vrm_proc_pid", int(proc_pid))
            if proc_name:
                nm.setProperty("vrm_proc_name", proc_name)
        val = QLabel(value_text)
        h.addWidget(nm)
        if note:
            note_lbl = QLabel(note); note_lbl.setStyleSheet(f"color:{note_color};")
            # The note (e.g. the long vmmem tag) is secondary — it takes the middle
            # and CLIPS rather than force the row wider than the panel and push the
            # value off-screen. (Ignored h-policy = clip to available width.) The
            # optional tooltip carries a clipped note's full text.
            note_lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            if note_tooltip:
                note_lbl.setToolTip(_tip(note_tooltip))
            h.addWidget(note_lbl, 1)
        else:
            h.addStretch(1)
        h.addWidget(val)
        return row

    def _disk_row(self, dtype, size_mib, kept_mib, reclaim_mib):
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(12, 0, 0, 0)
        nm = QLabel(dtype); nm.setMinimumWidth(88)
        gauge = GaugeBar()
        kept_frac = kept_mib / size_mib if size_mib else 0
        reclaim_frac = reclaim_mib / size_mib if size_mib else 0
        gauge.set_value(kept_frac, 40, shade_fill=reclaim_frac)  # low pct -> green kept
        val = QLabel(_fmt_mem(size_mib))
        note = QLabel(f"{_fmt_mem(reclaim_mib)} reclaimable")
        note.setStyleSheet(f"color:{_DIM};")
        h.addWidget(nm); h.addWidget(gauge, 1); h.addWidget(val); h.addWidget(note)
        return row

    # ── update ──────────────────────────────────────────────────────────────
    def update_from(self, result: ReadResult) -> None:
        status = overall_status(result)
        color = (active_color(0.0, self._active_style) if status == ACTIVE
                 else _STATE_COLORS.get(status, _DIM))
        snap = result.snapshot

        head = {ACTIVE: "● ACTIVE", IDLE: "○ idle",
                STALE: "⚠ stale", NODATA: "✗ no data"}.get(status, status)
        sub = ""
        if snap is not None and status in (ACTIVE, IDLE):
            sub = (f"  ·  {_esc(snap.host) if snap.host else 'host'}"
                   f"  ·  updated {_clock(snap.t)}")
        elif status == STALE and result.age_s is not None:
            sub = f"  ·  last update {result.age_s:.0f}s ago"
        elif status == NODATA and result.error:
            sub = f"  ·  {_esc(result.error)}"
        self._status.setText(
            f"<b><span style='color:{color}'>{head}</span></b>"
            f"<span style='color:{_DIM}'>{sub}</span>"
        )
        self.setWindowTitle(tooltip(result))

        # Nothing a panel renders can change while the frame AND status hold —
        # the header above stays live (its stale-age text follows the wall
        # clock), but a held frame (the app polling faster than the producer
        # writes, or a stale re-serve during a producer stall) must not tear
        # down and rebuild every row widget again. That per-tick rebuild was
        # both wasted work (the standing footprint rule) and the exposure
        # behind the 2026-07-19 freeze glitch: a starved paint captured the
        # Ports rows mid-relayout, half-drawn (obs #435).
        key = (snap.t if snap is not None else None, status)
        if key == self._render_key:
            return
        self._render_key = key

        # Data-side effects hoisted OUT of the per-panel render fns so panel
        # skipping below can never stall them: the last frame (toggle/filter
        # re-renders and the reveal repaint read it) and the rolling port
        # history + recent-clients memory (deduped by frame clock; the Ports
        # sparklines and the clients sub-lines must keep accumulating while
        # that panel is hidden or collapsed).
        self._last_snap = snap
        self._port_history.update(snap)
        self._port_clients.update(snap)
        self._record_busy(snap)
        self._tray_pct = self._selfcpu.update(time.time())

        # Collapsed-header summaries: one Qt-free pass per frame, pushed to
        # EVERY section (a guarded setText on one label — O(panels), no body
        # rebuild), so the #49 skip below stays intact while a collapsed
        # header still reads live.
        summaries = panel_summaries(snap)
        for pid, section in self._sections.items():
            section.set_summary(summaries.get(pid))

        # Repaint every VISIBLE panel via the registry. A hidden or collapsed
        # panel's body can't be seen, so its rebuild is skipped and the panel
        # marked dirty — revealing it (expand / re-show) repaints immediately
        # from the last frame (the standing footprint rule: no per-tick work
        # for surfaces nobody can see). Painting is suspended for the rebuild
        # and pending relayouts are drained before it resumes, so no paint —
        # however starved the event loop — can catch a row teardown/re-add in
        # flight.
        content = self._root_layout.parentWidget()
        content.setUpdatesEnabled(False)
        # While painting a frame, the per-panel update fns skip their own
        # tail _refresh_copy_menus (each is a full-window QLabel sweep the
        # direct toggle/filter call paths need, but tripling it per frame
        # violates the footprint rule — review L3): the single pass below
        # covers the whole rebuild.
        self._painting_frame = True
        try:
            for pid, update in self._updaters:
                if pid in self._panel_hidden or pid in self._panel_collapsed:
                    self._dirty_panels.add(pid)
                    continue
                self._dirty_panels.discard(pid)
                update(snap)
            QCoreApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        finally:
            self._painting_frame = False
            content.setUpdatesEnabled(True)
        # Labels created by this rebuild gain the right-click copy affordance.
        self._refresh_copy_menus()

    def _refresh_copy_menus(self):
        """Give every info QLabel a right-click "Copy PID N / Copy line" menu
        (property-guarded: new labels only — each panel rebuild creates fresh
        ones). A selection-free copy is the RELIABLE path to get a PID out of
        the app: panels rebuild on every advancing ~2 s frame, which would wipe
        an in-progress mouse selection, so selectable text was rejected
        (epic #9 maintainer request — look a PID up in Process Monitor)."""
        for lbl in self.findChildren(QLabel):
            if lbl.property("vrm_copy_menu"):
                continue
            lbl.setProperty("vrm_copy_menu", True)
            # Same new-labels-only pass, second duty: force literal PlainText
            # rendering. Most of what these labels paint comes straight from
            # the snapshot (comm names, session labels, peers, reasons), and
            # QLabel's default AutoText auto-detects HTML — a process named
            # with markup would inject rich text into the advisory surface
            # (spoof/hide a line, or load a local file via <img>). The few
            # labels that carry our OWN markup opt into RichText explicitly
            # (and _esc() their producer interpolations); everything still on
            # the default becomes PlainText before its first paint — this runs
            # at the end of the same update_from that created the label.
            if lbl.textFormat() == Qt.AutoText:
                lbl.setTextFormat(Qt.PlainText)
            lbl.setContextMenuPolicy(Qt.CustomContextMenu)
            lbl.customContextMenuRequested.connect(
                lambda pos, l=lbl: self._show_copy_menu(l, pos))

    def _label_menu(self, label) -> QMenu:
        """The right-click menu for one label (built, not shown — the exec
        lives in _show_copy_menu so this stays testable offscreen). The menu is
        parentless and its actions capture plain strings/ints, so a frame
        rebuild tearing the label down under the open menu cannot break them.
        A Ports-panel name label (vrm_port property) additionally offers
        Unwatch — the ad-hoc watch's remove path — but ONLY for a port the
        app's own desired set carries: a port reported because the producer
        was LAUNCHED with --watch-port isn't the app's to remove, and a silent
        no-op action would just gaslight."""
        menu = _build_copy_menu(_label_plain_text(label))
        port = label.property("vrm_port")
        if (port is not None and self._on_watch_ports_changed is not None
                and any(spec_port(s) == int(port)
                        for s in self._desired_watch_ports)):
            if menu.actions():
                menu.addSeparator()
            where = label.property("vrm_port_name") or f":{port}"
            act = menu.addAction(f"Unwatch {where}")
            act.triggered.connect(
                lambda _=False, p=int(port): self.unwatch_port(p))
        # process→ports entry points: a row tagged with the pid namespace its
        # PIDs live in (vrm_proc_side) offers "Show ports of …" per PID the
        # line names; a targeted-process row (vrm_proc_target) offers the
        # remove. Same plain-value capture rule as everything above. The pid
        # candidates are shared with the Process-Explorer block below, so
        # they're extracted whenever the row names its namespace.
        side = label.property("vrm_proc_side")
        cands = []
        if side:
            text = _label_plain_text(label)
            expid = label.property("vrm_proc_pid")
            if expid is not None:
                # Rows whose text doesn't carry the pid (session rows) tag it
                # explicitly; the tagged name is a real comm or absent.
                cands = [(int(expid), label.property("vrm_proc_name") or None)]
            else:
                cands = [(p, proc_name_for_pid(text, p)) for p in find_pids(text)]
        if side and self._on_watch_procs_changed is not None:
            targeted = {(t.get("side"), t.get("pid")) for t in self._proc_targets}
            sep_added = False
            for pid, nm in cands:
                if (side, pid) in targeted:
                    continue
                if not sep_added and menu.actions():
                    menu.addSeparator()
                    sep_added = True
                act = menu.addAction(
                    f"Show ports of {nm}[{pid}]" if nm
                    else f"Show ports of PID {pid}")
                act.triggered.connect(
                    lambda _=False, s=side, p=pid, n=nm:
                    self._menu_add_proc_target(s, p, n))
        # windows-side rows additionally open in Process Explorer (#61):
        # per-pid /s: selection when the row carries pids; a pid-less row
        # that names a process (the aggregated Memory windows tier) resolves
        # its largest instance at click time. WSL rows never offer it —
        # procexp cannot see the WSL pid namespace, and a same-named windows
        # process would be a DIFFERENT process (the #47 honesty rule).
        if side == "windows" and self._on_open_procexp is not None:
            px_cands = cands
            if not px_cands and label.property("vrm_proc_name"):
                px_cands = [(None, str(label.property("vrm_proc_name")))]
            sep_added = False
            for pid, nm in px_cands:
                if not sep_added and menu.actions():
                    menu.addSeparator()
                    sep_added = True
                if pid is None:
                    txt = f"Open {nm} in Process Explorer"
                else:
                    txt = (f"Open {nm}[{pid}] in Process Explorer" if nm
                           else f"Open PID {pid} in Process Explorer")
                act = menu.addAction(txt)
                act.triggered.connect(
                    lambda _=False, p=pid, n=nm: self._on_open_procexp(p, n))
        # …and in Process Monitor, FILTERED to the row (#87) — the view
        # procexp cannot give (no filter concept, probed 2026-07-26). Same
        # windows-only rule and candidate set as the procexp block; the
        # wording carries "(filtered)" so the two actions read distinctly.
        if side == "windows" and self._on_open_procmon is not None:
            pm_cands = cands
            if not pm_cands and label.property("vrm_proc_name"):
                pm_cands = [(None, str(label.property("vrm_proc_name")))]
            sep_added = False
            for pid, nm in pm_cands:
                if not sep_added and menu.actions():
                    menu.addSeparator()
                    sep_added = True
                if pid is None:
                    txt = f"Open {nm} in Process Monitor (filtered)"
                else:
                    txt = (f"Open {nm}[{pid}] in Process Monitor (filtered)"
                           if nm else
                           f"Open PID {pid} in Process Monitor (filtered)")
                act = menu.addAction(txt)
                act.triggered.connect(
                    lambda _=False, p=pid, n=nm: self._on_open_procmon(p, n))
        target = label.property("vrm_proc_target")
        if target and self._on_watch_procs_changed is not None:
            t_side, _, t_pid = str(target).partition(":")
            if t_pid.isdigit():
                if menu.actions():
                    menu.addSeparator()
                act = menu.addAction("Stop showing ports")
                act.triggered.connect(
                    lambda _=False, s=t_side, p=int(t_pid):
                    self.remove_proc_target(s, p))
        # A NAME watch's header row (vrm_proc_name_target) offers its remove —
        # which the app also persists, so a standing watch is never stuck.
        # Same plain-value capture rule as everything above.
        ntarget = label.property("vrm_proc_name_target")
        if ntarget and self._on_watch_procs_changed is not None:
            n_side, _, n_name = str(ntarget).partition(":")
            if n_name:
                if menu.actions():
                    menu.addSeparator()
                act = menu.addAction(f"Stop watching {n_name}")
                act.triggered.connect(
                    lambda _=False, s=n_side, n=n_name:
                    self.remove_proc_name_target(s, n))
        return menu

    def _show_copy_menu(self, label, pos):
        menu = self._label_menu(label)
        if menu.actions():
            menu.exec(label.mapToGlobal(pos))
        menu.deleteLater()

    def _update_gpu(self, snap):
        gpu = snap.gpu if snap else None
        if gpu is None:
            self._gpu_name.setText("no GPU data")
            self._compute.set_value(0); self._vram.set_value(0)
            self._compute_lbl.setText("—"); self._vram_lbl.setText("—")
            self._gpu_extra.setText(""); self._spark.set_values([])
            return
        self._gpu_name.setText(gpu.name)
        self._compute.set_value(gpu.util / 100.0, gpu.util)
        self._compute_lbl.setText(f"{gpu.util:.0f}%")
        vpct = gpu.mem_used_mib / gpu.mem_total_mib * 100 if gpu.mem_total_mib else 0
        self._vram.set_value(vpct / 100.0, vpct)
        self._vram_lbl.setText(f"{gpu.mem_used_mib/1024:.1f}/{gpu.mem_total_mib/1024:.1f}G")
        self._gpu_extra.setText(
            f"{gpu.temp_c:.0f}°C  ·  {gpu.power_w:.0f}/{gpu.power_limit_w:.0f}W"
        )
        self._spark.set_values(snap.gpu_util_history)

    def _update_activity(self, snap):
        if snap is None:
            self._act_status.setText("—")
            self._act_workloads.setText(""); self._act_workloads.setToolTip("")
            self._act_vram.setText(""); self._act_vram.setToolTip("")
            self._act_idle.setText(""); self._history.setText("")
            return
        if snap.is_active and snap.activity:
            act = snap.activity
            dur = _fmt_duration((snap.t - act.start_t)) if snap.t else "?"
            self._act_status.setText(
                f"<b style='color:{active_color(act.peak_util, self._active_style)}'>"
                f"● ACTIVE</b>"
                f" since {_clock(act.start_t)} · {dur} · peak {act.peak_util:.0f}%"
                f" · {act.peak_vram_mib/1024:.1f}G vram"
            )
            self._act_workloads.setText(", ".join(act.workloads))
            self._act_workloads.setToolTip("")
            self._act_idle.setText("")  # baseline is an idle-only line
        else:
            self._update_idle(snap)

        # The VRAM-holders note, active AND idle (an idle card with a 7.9G
        # holder is a story too). Only an ACTIVE frame with no workload names
        # earns the unattributed hint — idle quiet stays quiet.
        users = snap.gpu_users
        if users:
            line, tip = format_gpu_users(users)
            self._act_vram.setText(line)
            self._act_vram.setToolTip(_tip(tip))
        elif (snap.is_active and snap.activity
              and not any(snap.activity.workloads)):
            self._act_vram.setText(gpu_users_gap_hint(users))
            self._act_vram.setToolTip("")
        else:
            self._act_vram.setText(""); self._act_vram.setToolTip("")

        lines = []
        for r in reversed(snap.history[-3:]):
            wl = ", ".join(r.workloads)
            lines.append(
                f"{_clock(r.start_t):>8}  {_fmt_duration(r.duration_s):>7}  "
                f"{r.peak_util:3.0f}%  {r.peak_vram_mib/1024:4.1f}G  {wl}"
            )
        self._history.setText("\n".join(lines))

    def _update_idle(self, snap):
        """Paint the idle Activity view: how long idle + today's session stats +
        the GPU's at-rest baseline, so an idle panel is informative, not blank."""
        st = idle_stats(snap)

        # Line 1 — idle status + when the last run ended.
        if snap.history:
            last = snap.history[-1]
            idle_for = (_fmt_duration(st.idle_for_s)
                        if st and st.idle_for_s is not None else "?")
            self._act_status.setText(
                f"<b style='color:{_DIM}'>○ idle</b> {idle_for}"
                f" · last run ended {_clock(last.end_t)}"
            )
        else:
            self._act_status.setText(
                f"<b style='color:{_DIM}'>○ idle</b> · no GPU activity recorded yet"
            )

        # Line 2 — today's session totals (a lower bound, marked ≥, when the
        # bounded history reaches only into today so earlier runs may have aged out).
        if st and snap.history:
            approx = "" if st.today_complete else "≥"
            self._act_workloads.setText(
                f"today: {approx}{st.runs_today} run(s)"
                f" · {_fmt_duration(st.active_today_s)} GPU-active"
            )
            self._act_workloads.setToolTip(
                "" if st.today_complete else
                "≥ — today's totals are from the producer's recorded history, which "
                "reaches only into today, so earlier runs may not be counted."
            )
        else:
            self._act_workloads.setText(""); self._act_workloads.setToolTip("")

        # Line 3 — the GPU baseline at rest (temp / power / VRAM).
        gpu = snap.gpu
        if gpu is not None:
            self._act_idle.setText(
                f"idle baseline · {gpu.temp_c:.0f}°C"
                f" · {gpu.power_w:.0f}/{gpu.power_limit_w:.0f}W"
                f" · {gpu.mem_used_mib/1024:.1f}/{gpu.mem_total_mib/1024:.1f}G vram"
            )
        else:
            self._act_idle.setText("")

    def _update_runs(self, snap):
        _clear(self._runs_layout)
        runs = snap.runs if snap else []
        if not runs:
            self._runs_layout.addWidget(self._dim_label("no recent progress streams"))
            return
        for r in runs:
            row = QWidget()
            h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
            name = QLabel(r.label); name.setMinimumWidth(140)
            state = QLabel(r.state.upper())
            state.setStyleSheet(f"color:{_RUN_COLORS.get(r.state, _DIM)}; font-weight:bold;")
            detail = r.message or (f"{r.percent}%" if r.percent is not None else "")
            if r.state == "error" and r.end_message:
                detail = r.end_message
            info = QLabel(detail); info.setStyleSheet(f"color:{_DIM};")
            # Ignored (not Expanding) horizontally: a long message/end_message must
            # clip to the available width, never force the content past the viewport.
            info.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            h.addWidget(name); h.addWidget(state); h.addWidget(info, 1)
            self._runs_layout.addWidget(row)

    @staticmethod
    def _forecast_detail(fc):
        """The dim second line: ETA to headroom-exhaustion, the current reclaimable
        headroom, and the leading signal — each shown only when the producer gave
        it (all are optional on a warming / healthy tick)."""
        bits = []
        if fc.eta_min is not None:
            bits.append(f"~{fc.eta_min:.0f} min to exhaustion")
        if fc.headroom_mib is not None:
            bits.append(f"{_fmt_mem(fc.headroom_mib)} headroom")
        if fc.signal:
            bits.append(f"signal: {fc.signal}")
        return "  ·  ".join(bits)

    def _update_forecast(self, snap):
        """Paint the Forecast panel from the snapshot's `forecast` + `contributors`.
        The `warn`-level line also rides `processes.alerts` (Sessions panel); this
        panel is the structured surface — the pre-`warn` `watch` advisory, the
        ETA/headroom, and each contributor's recommended action, none of which the
        terse alerts tier carries."""
        _clear(self._fc_layout)
        fc = snap.forecast if snap else None
        contribs = sorted(snap.contributors, key=lambda c: c.rank) if snap else []

        # No structured forecast at all: a GPU-only feed, an older producer, or
        # --no-forecast. Stay visible with a stated cause (not a blank panel).
        if fc is None and not contribs:
            self._fc_status.setText(
                f"<span style='color:{_DIM}'>no forecast data</span>")
            self._fc_detail.setText("")
            return

        if fc is not None:
            glyph, col = {
                "warn": ("⚠ WARN", RED),
                "watch": ("◐ watch", _WARN),
            }.get(fc.level, ("○ clear", _OK_GREEN))
            reason = fc.reason or ("memory pressure normal"
                                   if fc.level == "ok" else "")
            self._fc_status.setText(
                f"<b><span style='color:{col}'>{glyph}</span></b>"
                + (f"<span style='color:{_DIM}'> · {_esc(reason)}</span>"
                   if reason else ""))
            self._fc_detail.setText(self._forecast_detail(fc))
        else:
            # Contributors firing without a forecast object (an unusual feed shape):
            # still surface them under a neutral header rather than dropping them.
            self._fc_status.setText(
                f"<b><span style='color:{_WARN}'>⚠ contributors active</span></b>")
            self._fc_detail.setText("")

        for c in contribs:
            self._fc_layout.addWidget(self._contributor_block(c))

    def _update_host(self, snap):
        cpu = snap.host_cpu if snap else None
        ram = snap.host_ram if snap else None
        mem = snap.memory if snap else None

        # The monitoring stack's own bill — rendered on EVERY branch
        # (including host-stats-unavailable below: the producer's self-bill
        # and the tray's own reading don't depend on the host tiers).
        self._host_selfcost.setText(
            selfcost_note(snap.self_cost if snap else None, self._tray_pct))
        # Peak-normalized (the ports-sparkline treatment): the signal here is
        # RELATIVE drift — a 9→15% creep is a +67% footprint regression that
        # would read as a flat floor line on the absolute 0-100 scale. The
        # fixed DIM colour already guards the widget's normalized-series-
        # reads-red caveat; the absolute figure lives in the note above.
        trend = snap.self_cost_history if snap else []
        self._host_selfcost_trend.set_values(scale_sparkline(trend))
        self._host_selfcost_trend.setVisible(len(trend) >= 2)

        # The wsl process-memory tier (the 2026-07-29 regroup) — rendered on
        # EVERY branch like the notes above: the tier's availability (its
        # own MEM_OK/unavailable honesty rows) is independent of the wsl
        # cpu/ram gauges below.
        self._render_wsl_mem_tier(snap)

        # "WSL VM · X cap of Y host" context, when both are known.
        host_total = mem.windows.total_mib if (mem and mem.windows) else 0
        if ram and ram.total_mib and host_total:
            self._host_sub.setText(
                f"WSL VM · {ram.total_mib/1024:.1f}G cap of {host_total/1024:.1f}G host"
            )
        else:
            self._host_sub.setText("WSL")

        if cpu is None and ram is None:
            self._host_note.setText("host stats unavailable")
            self._cpu.set_value(0); self._cpu_lbl.setText("—")
            self._cpu_extra.setText(""); self._cpu_trend.set_values([])
            self._ram.set_value(0); self._ram_lbl.setText("—"); self._ram_note.setText("")
            # Direct calls (tier/filter toggles) bypass update_from — attach
            # the copy/context affordances to the fresh tier rows here too
            # (update_from's own single pass covers the frame path).
            if not self._painting_frame:
                self._refresh_copy_menus()
            return
        self._host_note.setText("")

        if cpu:
            self._cpu.set_value(cpu.pct / 100.0, cpu.pct)
            self._cpu_lbl.setText(f"{cpu.pct:.0f}%")
            extra = []
            if cpu.cores_total:
                extra.append(f"{cpu.cores_active}/{cpu.cores_total} virtual cores")
            if cpu.speed_mhz:
                extra.append(f"{cpu.speed_mhz/1000:.2f} GHz")
            # The user/sys split (producer; mac #56's first-class CPU row —
            # here the gauge already existed, the split is what was missing).
            # Omitted when the feed doesn't carry it (an older producer) —
            # never guessed. load/core (the Stuck panel's format) rides the
            # processes block, which every split-capable feed also carries.
            if cpu.user_pct is not None and cpu.sys_pct is not None:
                extra.append(f"user {cpu.user_pct:.0f}%  ·  sys {cpu.sys_pct:.0f}%")
            procs = snap.processes    # snap is non-None whenever cpu is
            if procs and procs.load and procs.ncpu:
                extra.append(f"load {procs.load[0] / procs.ncpu:.1f}/core")
            self._cpu_extra.setText("  ·  ".join(extra))
        else:
            self._cpu.set_value(0); self._cpu_lbl.setText("—"); self._cpu_extra.setText("")
        self._cpu_trend.set_values(snap.cpu_history if snap else [])

        if ram and ram.total_mib:
            used_pct = ram.used_mib / ram.total_mib * 100
            self._ram.set_value(ram.used_mib / ram.total_mib, used_pct,
                                shade_fill=ram.cached_mib / ram.total_mib)
            self._ram_lbl.setText(f"{ram.used_mib/1024:.1f}/{ram.total_mib/1024:.1f}G")
            self._ram_note.setText(
                f"+{ram.cached_mib/1024:.1f}G cache" if ram.cached_mib else ""
            )
        else:
            self._ram.set_value(0); self._ram_lbl.setText("—"); self._ram_note.setText("")
        # Direct calls (tier/filter toggles) bypass update_from — attach the
        # copy/context affordances to the fresh tier rows here too (the
        # _update_memory convention; update_from's single pass covers frames).
        if not self._painting_frame:
            self._refresh_copy_menus()

    def _render_wsl_mem_tier(self, snap):
        """The wsl process-memory tier inside the WSL section (moved from the
        old Memory panel in the 2026-07-29 regroup; same tier key, so the
        persisted collapse/expand state carries over). Filtered by the WSL
        panel's OWN filter box — the search↔ports tie-in (#47) rides it."""
        _clear(self._wsl_mem_layout)
        tiers = memory_tiers(snap)
        if tiers is None:
            self._wsl_mem_layout.addWidget(
                self._dim_label("collecting… (host sampled every 10s)"))
            return
        tk = {t.key: t for t in tiers}["wsl"]
        if tk.state == MEM_OK:
            self._wsl_mem_layout.addWidget(self._tier_header("wsl", "wsl memory"))
            if "wsl" not in self._mem_collapsed:
                self._mem_add_procs("wsl", snap.memory.wsl,
                                    layout=self._wsl_mem_layout,
                                    query=self._wsl_filter)
        else:
            self._wsl_mem_layout.addWidget(
                self._status_row("wsl memory", tk.reason, warn=True))

    def _update_memory(self, snap):
        # Whole-box Windows CPU strain (the epic-#9 maintainer ask; moved
        # here in the 2026-07-29 regroup — the Windows-host section owns the
        # Windows readouts) — rendered on EVERY branch, before the tiers'
        # collecting/None handling: its source is the host sweep,
        # independent of the memory tiers. The label carries the raw
        # utility figure (may exceed 100 under turbo) — the gauge fill
        # saturates, the number never lies.
        wc = snap.windows_cpu if snap else None
        busy = wc.time_pct if wc is not None else None
        marker = self._busy_marker_pct
        if busy is not None:
            self._wincpu.set_value(busy / 100.0, busy)
            self._wincpu_lbl.setText(f"{busy:.0f}%")
            self._wincpu_extra.setText(_wincpu_note(wc))
        elif wc is not None and wc.utility_pct is not None:
            # Half-present reading (busy unusable, utility real — a feed
            # shape today's producer never emits, but the app must not lie
            # on it): dash the headline, say so, keep the real figure.
            self._wincpu.set_value(0)
            self._wincpu_lbl.setText("—")
            self._wincpu_extra.setText(_wincpu_note(wc))
        else:
            # Older producer / --no-mem / a failed counter section: stated,
            # never a fabricated 0-strain gauge.
            self._wincpu.set_value(0)
            self._wincpu_lbl.setText("—")
            self._wincpu_extra.setText(
                "no whole-box reading (producer predates it, or the "
                "Windows counters were unavailable)")
        # The busy trend, rendered on every branch like the row.
        trend = list(self._wincpu_busy)
        self._wincpu_trend.set_color(busy_color(trend[-1], marker) if trend else None)
        self._wincpu_trend.set_values(trend)
        self._wincpu_trend.setVisible(len(trend) >= 2)
        _clear(self._mem_layout)
        tiers = memory_tiers(snap)
        if tiers is None:
            self._mem_layout.addWidget(
                self._dim_label("collecting… (host/docker sampled every 10s)"))
            return
        mem = snap.memory
        by_key = {t.key: t for t in tiers}

        # Windows host — a collapsible tier with a used/total gauge in its header.
        tw = by_key["windows"]
        if tw.state == MEM_OK:
            self._mem_layout.addWidget(self._tier_header(
                "windows", "windows",
                used_mib=mem.windows.used_mib, total_mib=mem.windows.total_mib))
            if "windows" not in self._mem_collapsed:
                self._mem_add_procs("windows", mem.windows.processes, windows=True)
        else:
            self._mem_layout.addWidget(self._status_row("windows", tw.reason, warn=True))

        # (The wsl process tier lives in the WSL section since the
        # 2026-07-29 regroup — _render_wsl_mem_tier.)

        # Docker containers.
        td = by_key["docker"]
        if td.state == MEM_OK:
            self._mem_layout.addWidget(self._tier_header("docker", "docker"))
            if "docker" not in self._mem_collapsed:
                conts = self._filter_named(mem.containers)  # name filter, no size split
                if self._filtering and not conts:
                    self._mem_layout.addWidget(self._dim_label("   (no matches)"))
                for c in conts:
                    note = f"{_fmt_mem(c.resident_mib)} resident" if c.resident_mib is not None else ""
                    self._mem_layout.addWidget(self._kv_row(c.name, _fmt_mem(c.mem_mib), note))
        elif td.reason:
            self._mem_layout.addWidget(
                self._status_row("docker", td.reason, warn=td.state == MEM_UNAVAILABLE))

        # Docker disk.
        tdd = by_key["disk"]
        if tdd.state == MEM_OK:
            self._mem_layout.addWidget(
                self._tier_header("disk", "disk · docker, shared layers counted once"))
            if "disk" not in self._mem_collapsed:
                for d in mem.disk:
                    kept = max(0.0, d.size_mib - d.reclaimable_mib)
                    self._mem_layout.addWidget(
                        self._disk_row(d.type.lower(), d.size_mib, kept, d.reclaimable_mib))
        elif tdd.reason:
            self._mem_layout.addWidget(
                self._status_row("disk", tdd.reason, warn=tdd.state == MEM_UNAVAILABLE))
        # Direct calls (tier/filter toggles) bypass update_from — attach here
        # too (update_from's single pass covers the frame path).
        if not self._painting_frame:
            self._refresh_copy_menus()

    def _update_sessions(self, snap):
        _clear(self._sess_layout)
        procs = snap.processes if snap else None
        if procs is None:
            self._sess_sub.setText("no session data")
            self._sess_alerts.setText("")
            return

        parts = [f"{procs.claude_total} Claude session(s)",
                 f"{procs.node_total} node proc(s)"]
        if procs.load:
            parts.append("load " + "/".join(f"{v:.1f}" for v in procs.load[:3]))
        # Swap readout (the always-on number the panel was missing). The full
        # detail — top holders + the safe action — rides the swap_full contributor
        # in the Forecast panel once swap crosses the producer's threshold; here we
        # just state the level so it's visible before it becomes a contributor.
        sw = procs.swap
        if sw and sw.total_mib:
            pct = sw.pct if sw.pct is not None else sw.used_mib / sw.total_mib * 100
            parts.append(f"swap {pct:.0f}% ({sw.used_mib/1024:.1f}/{sw.total_mib/1024:.1f}G)")
        stale_n = sum(1 for s in procs.sessions if s.stale)
        if stale_n:
            parts.append(f"{stale_n} stale")
        self._sess_sub.setText("  ·  ".join(parts))

        if procs.alerts:
            self._sess_alerts.setText(
                "<br>".join(f"⚠ {_esc(a)}" for a in procs.alerts))
            self._sess_alerts.setStyleSheet(f"color:{YELLOW};")
            self._sess_alerts.setTextFormat(Qt.RichText)
        else:
            self._sess_alerts.setText("")

        # Heaviest sessions first. The node count moves onto the NAME ("(N node)")
        # — it was hiding in the metrics as a cryptic "Nn" — so the metrics keep
        # RSS + age with no double-statement. The list caps at the top N with a
        # click-to-expand toggle; COLLAPSED (the default) builds only the top N
        # rows per repaint, so a deep fleet isn't rebuilt every ~2 s unless asked.
        sessions = sorted(procs.sessions, key=lambda s: s.rss_mib, reverse=True)
        # A non-empty filter shows EVERY match (an already-small set) and
        # reads "no session matches" rather than a blank list; the top-N
        # collapse applies only to the unfiltered default (mac #49 port).
        # The needle matches the label, the root pid's digits, or any pid in
        # the producer's per-session child-pid index (producer) — so a pid
        # copied from a PORT row's connecting process, usually a node/MCP
        # CHILD, resolves to its owning session. A via-child match says so on
        # the row (the label/pid alone wouldn't explain why it appeared). On
        # a feed predating the index, child_pids is empty → root-only, the
        # pre-index behavior.
        needle = (self._sess_filter.text() or "").strip().lower()
        if needle:
            # kept pairs a session with the child pids the needle matched
            # (None = matched on label/root pid) — pairing, not a dict keyed
            # by root pid, so duplicate/defaulted pids in a malformed feed
            # can't cross-attribute another session's via-child note
            # (review L2).
            kept = []
            for s in sessions:
                if (needle in (s.label or "").lower() or needle in str(s.pid)
                        or any(needle in (getattr(s, key) or "").lower()
                               for key in ("cockpit", "lane", "name"))):
                    kept.append((s, None))
                    continue
                hits = [p for p in s.child_pids if needle in str(p)]
                if hits:
                    kept.append((s, hits))
            sessions = [s for s, _ in kept]
            shown = kept
            if not sessions:
                msg = "no session matches"
                # Honesty note for a digit needle (ascii-gated — unicode
                # digits can never match str(int)'s output): under the
                # contract a lookup MISS is conclusive only when every
                # session's child list is complete. Two inconclusive
                # states: a producer-truncated list (child_total >
                # len(child_pids); concrete, so stated first on a mixed
                # feed) and a feed predating the index entirely — no
                # children listed for sessions that demonstrably have them
                # (child_total None + node_count > 0 — review H1; a real
                # pre-index feed has no truncated lists, so the elif fires
                # there). Matched rows are true positives, so the hedge
                # scopes to the ZERO-match claim only — hedging every
                # partial match would be standing noise (decision recorded,
                # review L3).
                if needle.isascii() and needle.isdigit():
                    sess_all = procs.sessions
                    if any(s.child_total is not None
                           and s.child_total > len(s.child_pids)
                           for s in sess_all):
                        msg += (" (some sessions have more children than"
                                " the feed lists)")
                    elif any(s.child_total is None and s.node_count > 0
                             for s in sess_all):
                        msg += " (this feed does not list child pids)"
                self._sess_layout.addWidget(self._dim_label(msg))
        else:
            shown = [(s, None) for s in
                     (sessions if self._sessions_expanded
                      else sessions[:_MAX_SESSION_ROWS])]
        # Group only explicit cockpit metadata. Preserve every process row;
        # exact names key groups, so prefix-overlapping cockpits stay separate.
        groups = {}
        for s in procs.sessions:
            if s.cockpit:
                groups.setdefault(s.cockpit, []).append(s)
        if needle:
            top = []
            included = set()
            filter_groups = {s.cockpit for s, _ in shown if s.cockpit}
            for s, hits in shown:
                if s.cockpit:
                    if s.cockpit not in included:
                        included.add(s.cockpit)
                        top.append(("group", s.cockpit, groups[s.cockpit]))
                else:
                    top.append(("plain", s, hits))
            shown_rows = top
        else:
            top = [("group", key, members) for key, members in groups.items()]
            top += [("plain", s, None) for s in procs.sessions if not s.cockpit]
            top.sort(key=lambda row: (sum(x.rss_mib for x in row[2])
                                      if row[0] == "group" else row[1].rss_mib),
                     reverse=True)
            shown_rows = top if self._sessions_expanded else top[:_MAX_SESSION_ROWS]
        for item in shown_rows:
            kind = item[0]
            if kind == "group":
                _, cockpit, members = item
                order = {"cockpit": 0, "advisor": 1, "drc": 2, "router": 3, "lane": 4}
                members = sorted(members, key=lambda s: (order.get(s.role, 5), s.lane or "", s.pid))
                lane_count = sum(s.role == "lane" for s in members)
                total_rss = sum(s.rss_mib for s in members)
                ages = [s.age_s for s in members if s.age_s is not None]
                busy = sum(s.state == "busy" for s in members)
                bits = [_fmt_mem(total_rss)]
                if ages: bits.append(_fmt_age(max(ages)))
                bits.append(f"{busy} busy")
                expanded = cockpit in self._cockpit_expanded or (needle and cockpit in filter_groups)
                row = _ClickRow(); row.setCursor(Qt.PointingHandCursor)
                h = QHBoxLayout(row); h.setContentsMargins(12, 0, 0, 0)
                chevron = QLabel("▾" if expanded else "▸")
                chevron.setStyleSheet(f"color:{_DIM}; font-weight:bold;")
                chevron.setFixedWidth(14); h.addWidget(chevron)
                title = QLabel(f"{cockpit} ({lane_count} lane{'' if lane_count == 1 else 's'})")
                title.setStyleSheet(f"color:{_DIM}; font-weight:bold;")
                title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
                title.setToolTip(_tip(title.text()))
                h.addWidget(title, 1)
                if any(s.stale for s in members):
                    stale_tag = QLabel("stale"); stale_tag.setStyleSheet(f"color:{YELLOW};")
                    h.addWidget(stale_tag)
                metric = QLabel("  ·  ".join(bits)); metric.setStyleSheet(f"color:{_DIM};")
                h.addWidget(metric)
                row.clicked.connect(lambda key=cockpit: self._toggle_cockpit(key))
                self._sess_layout.addWidget(row)
                if expanded:
                    for s in members:
                        label = s.lane or s.role or "session"
                        if s.state: label += f" · {s.state}"
                        hits = next((hs for candidate, hs in shown if candidate is s), None)
                        cbits = self._session_metrics(s, hits)
                        self._sess_layout.addWidget(self._session_row(
                            f"{label} ({s.node_count} node)", "  ·  ".join(cbits), s.stale,
                            pid=s.pid, session_label=s.label, node_count=s.node_count,
                            session=s))
                        self._sess_layout.itemAt(self._sess_layout.count() - 1).widget().layout().setContentsMargins(28, 0, 0, 0)
                continue
            _, s, hits = item
            bits = self._session_metrics(s, hits)
            self._sess_layout.addWidget(
                self._session_row(f"{s.label} ({s.node_count} node)",
                                  "  ·  ".join(bits), s.stale, pid=s.pid,
                                  session_label=s.label,
                                  node_count=s.node_count, session=s))
        extra = (len(top) - _MAX_SESSION_ROWS) if not needle else 0
        if not needle and extra > 0:
            self._sess_layout.addWidget(self._session_toggle(
                f"▾ show top {_MAX_SESSION_ROWS} only" if self._sessions_expanded
                else f"▸ +{extra} more session(s)"))

        # Stale high-cost contributors — a first-class "what's heavy" callout below
        # the sessions (advisory; the app never acts on the host).
        for h in procs.high_cost:
            note = _fmt_age(h.age_s) if h.age_s is not None else ""
            self._sess_layout.addWidget(self._kv_row(
                f"⚠ {h.comm}", f"{_fmt_mem(h.rss_mib)}  ·  {h.cpu_s:.0f}s cpu",
                note, note_color=RED,
                proc_side="wsl", proc_pid=h.pid, proc_name=h.comm))
        self._refresh_copy_menus()

    @staticmethod
    def _session_metrics(s, hits=None):
        bits = [_fmt_mem(s.rss_mib)]
        if s.age_s is not None:
            bits.append(_fmt_age(s.age_s))
        if s.state == "idle" and s.last_event_age_s is not None:
            bits.append(f"idle {_fmt_age(s.last_event_age_s)}")
        if s.swap_mib is not None:
            bits.append(f"swap {_fmt_mem(s.swap_mib)}")
        if hits:
            word = "child" if len(hits) == 1 else "children"
            listed = ", ".join(str(p) for p in hits[:3])
            if len(hits) > 3:
                listed += f" +{len(hits) - 3}"
            bits.append(f"via {word} {listed}")
        if s.ports:
            p_listed = ", ".join(str(p) for p in s.ports[:3])
            p_total = (s.port_total if isinstance(s.port_total, int)
                       and s.port_total >= len(s.ports) else len(s.ports))
            if p_total > 3:
                p_listed += f" +{p_total - 3}"
            bits.append(f"ports: {p_listed}")
        return bits

    # ── stuck panel ────────────────────────────────────────────────────────────
    def _build_stuck(self):
        """The Stuck panel: the producer's stuck-process / run-queue probe (the
        freeze-forensics measurement, producer). The sub-line answers "is the
        CPU being hammered" (run-queue depth vs cores, blocked count, load per
        core); wedged rows name processes stuck in uninterruptible sleep past
        the producer's persistence gate; the amber lines are the producer's
        CALIBRATED flags — the app never re-derives thresholds from the raw
        numbers. Escalation already rides the alerts / contributors tiers; this
        panel is the structured surface. Advisory only (warn-and-suggest)."""
        box = QGroupBox("Stuck")
        v = QVBoxLayout(box)
        self._stuck_sub = QLabel("—")
        self._stuck_sub.setStyleSheet(f"color:{_DIM};")
        self._stuck_sub.setWordWrap(True)
        self._stuck_flags = QLabel("")
        self._stuck_flags.setWordWrap(True)
        self._stuck_layout = QVBoxLayout()
        self._stuck_samplers = QLabel("")
        self._stuck_samplers.setStyleSheet(f"color:{_DIM};")
        self._stuck_samplers.setWordWrap(True)
        v.addWidget(self._stuck_sub)
        v.addWidget(self._stuck_flags)
        v.addLayout(self._stuck_layout)
        v.addWidget(self._stuck_samplers)
        return box

    def _update_stuck(self, snap):
        _clear(self._stuck_layout)
        st = snap.stuck if snap else None
        if st is None:
            # An older producer / --no-stuck: stay visible with a stated cause
            # (informative-not-blank, like the Forecast panel's absent state).
            self._stuck_sub.setText("no stuck-probe data")
            self._stuck_flags.setText("")
            self._stuck_samplers.setText("")
            return
        bits = []
        if st.procs_running is not None:
            core = f"/{st.ncpu}" if st.ncpu else ""
            bits.append(f"queue {st.procs_running}{core} running")
        if st.procs_blocked is not None:
            bits.append(f"{st.procs_blocked} blocked")
        if st.d_count:
            # Instantaneous D-state count — a superset of the persisted wedged
            # census below, so only worth a word when non-zero.
            bits.append(f"{st.d_count} in D now")
        if st.load1_per_core is not None:
            bits.append(f"load {st.load1_per_core:.2f}/core")
        if st.cpu_pct is not None:
            # Busy% from the producer's saturation split (producer) — the
            # number the cpu_saturated flag below is judged on.
            bits.append(f"busy {st.cpu_pct:.0f}%")
        self._stuck_sub.setText("  ·  ".join(bits) or "probe reported no numbers")

        flags = stuck_flags(st)
        if flags:
            self._stuck_flags.setTextFormat(Qt.RichText)
            self._stuck_flags.setStyleSheet(f"color:{YELLOW};")
            self._stuck_flags.setText(
                "<br>".join(f"⚠ {_esc(f)}" for f in flags))
        else:
            self._stuck_flags.setText("")

        # Wedged census — the acute, named signal. The name[pid] form gives the
        # row the right-click Copy-PID affordance (the producer already bounds
        # the list to the top-N longest-wedged).
        for w in st.wedged:
            # Explicit pid + comm: the rendered text is decorated ("⚠ …") and
            # a text-derived name would seed "⚠ comm" into the producer's
            # pid-reuse check — a guaranteed false mismatch (review H1).
            self._stuck_layout.addWidget(self._kv_row(
                f"⚠ {w.comm}[{w.pid}]", _fmt_duration(w.for_s),
                "uninterruptible I/O", RED,
                proc_side="wsl", proc_pid=w.pid, proc_name=w.comm))

        if st.sampler_ms:
            parts = [f"{n} {_fmt_ms(ms)}" for n, ms in
                     sorted(st.sampler_ms.items(), key=lambda kv: -kv[1])]
            self._stuck_samplers.setText("samplers: " + " · ".join(parts))
        else:
            self._stuck_samplers.setText("")

    # ── runners panel ───────────────────────────────────────────────────────────
    def _build_runners(self):
        """The Runners panel: the box's self-hosted GitHub Actions runner
        fleet (producer ``runner_fleet``): which
        runners exist, which are busy, and what they cost. The fleet runs as
        docker containers — identity comes from the producer's once-per-
        lifetime inspect (RUNNER_NAME/REPO_URL env), cost from the shared
        docker-stats sweep; ``active`` is the producer's calibrated judgment
        (cpu, or tasks over the runner's idle floor) — never re-derived here.
        Observes only — no alerts line, no contributor."""
        box = QGroupBox("Runners")
        v = QVBoxLayout(box)
        self._runners_sub = QLabel("—")
        self._runners_sub.setStyleSheet(f"color:{_DIM};")
        self._runners_sub.setWordWrap(True)
        self._runners_sub.setToolTip(
            "Self-hosted GitHub Actions runners on this box (docker "
            "containers), grouped by the repo they serve — click a group "
            "to expand it. active = the producer judged a job likely "
            "running (cpu, or the container's task count over its idle "
            "floor). tasks = processes + threads inside the container — "
            "container-internal, so no host-side pid affordances apply. "
            "burst = one contiguous active span (a short-lived runner "
            "job); ended bursts linger ~10 min then drop, and ≥ marks an "
            "age whose start the producer never witnessed. All data comes "
            "from the local docker sweeps: no GitHub API calls.")
        self._runners_layout = QVBoxLayout()
        v.addWidget(self._runners_sub)
        v.addLayout(self._runners_layout)
        return box

    def _update_runners(self, snap):
        _clear(self._runners_layout)
        rf = snap.runner_fleet if snap else None
        hb = snap.hosted_bursts if snap else None
        if rf is None:
            # An older producer / --no-docker / docker unavailable (the
            # census rides the docker sweeps): a stated cause, never blank.
            self._runners_sub.setText(
                "no fleet census — producer older, --no-docker, or docker "
                "unavailable")
            self._runners_sub.setVisible(True)
            # The hosted watch polls the Actions API, not docker — its
            # reading survives a docker outage and must render through one.
            self._render_hosted_block(hb)
            return
        burst_running = sum(1 for b in rf.bursts if b.state == "running")
        if not rf.runners:
            if rf.unresolved:
                # Containers are running but their identity inspects haven't
                # answered — an UNKNOWN fleet, never claimed empty (the
                # producer's consumer rule).
                cause = (f"fleet unknown — {rf.unresolved} container(s) "
                         "awaiting identity (inspect pending or failing)")
            else:
                # Docker answered and no container carries runner identity —
                # a real empty fleet, distinct from "docker down" above.
                cause = "no runner containers on this box"
            if burst_running:
                cause += f"  ·  {burst_running} burst"
            self._runners_sub.setText(cause)
            self._runners_sub.setVisible(True)
            # Bursts in the frame still render (the linger OUTLIVES the
            # runner by design — burst evidence must not vanish with the
            # container; review H-empty-fleet). Only a burst-less frame
            # stops here.
            if not rf.bursts:
                self._render_hosted_block(hb)
                return
        else:
            # Non-finite floats (json Infinity/NaN from a corrupt feed) drop
            # their bit rather than render "inf%"/"nanM" — the egress rule.
            parts = [f"{rf.total} up"]
            if rf.active_count:
                parts.append(f"{rf.active_count} active")
            if rf.unresolved:
                parts.append(f"{rf.unresolved} unresolved")
            if burst_running:
                parts.append(f"{burst_running} burst")
            if (rf.cpu_pct_total is not None
                    and math.isfinite(rf.cpu_pct_total)):
                parts.append(f"cpu {rf.cpu_pct_total:.1f}%")
            if (rf.mem_mib_total is not None
                    and math.isfinite(rf.mem_mib_total)):
                parts.append(f"mem {_fmt_mem(rf.mem_mib_total)}")
            self._runners_sub.setText("  ·  ".join(parts))
            self._runners_sub.setVisible(True)
        # Runners grouped by served repo, expandable. Every group defaults to a
        # COLLAPSED summary header; the expanded exceptions persist (the
        # memory-tier collapse pattern, inverted). Within a group the
        # producer's active-first order holds; per-group rows are bounded
        # with a COUNTED tail (the Sessions "+N more" convention).
        groups = runner_groups(rf)
        # Burst-only labels this frame + the session-only expansion set's
        # auto-drop: a burst group that left the frame takes its expansion
        # with it (in-memory — never the persisted set).
        self._burst_only_labels = {g.label for g in groups if not g.runners}
        self._burst_expanded &= {g.label for g in groups}
        # Prune stored expansions for groups no longer in the fleet — a
        # stale label would pre-expand a FUTURE same-initials group (review
        # F3). Only a POPULATED fleet prunes: a docker outage, an empty
        # frame, or a bursts-only frame (the fleet story there is "no live
        # runner", not an authoritative roster) can't wipe the set.
        stale = (self._runners_expanded - {g.label for g in groups}
                 if rf.runners else set())
        if stale:
            self._runners_expanded -= stale
            try:
                save_runners_expanded(self._runners_expanded,
                                      path=self._settings_path)
            except Exception:  # noqa: BLE001 — view-state persistence must never crash the UI
                pass
        for g in groups:
            expanded = (g.label in self._runners_expanded
                        or g.label in self._burst_expanded)
            self._runners_layout.addWidget(
                self._runner_group_header(g, expanded))
            if not expanded:
                continue
            for r in g.runners[:16]:
                bits = []
                if r.cpu_pct is not None and math.isfinite(r.cpu_pct):
                    bits.append(f"cpu {r.cpu_pct:.1f}%")
                if r.mem_mib is not None and math.isfinite(r.mem_mib):
                    bits.append(_fmt_mem(r.mem_mib))
                if r.pids is not None:
                    bits.append(f"{r.pids} tasks")
                if r.up:
                    # The docker ps Status string VERBATIM ("Up 7 minutes") —
                    # restart churn beside 20-hour siblings, zero parsing.
                    bits.append(r.up)
                if r.active:
                    state = "active"
                elif r.active is False:
                    state = "quiet"
                else:
                    # active None = neither signal had data this sweep — say
                    # so rather than render a fabricated "quiet".
                    state = "no reading"
                value = (f"{state} · " + " · ".join(bits)) if bits else state
                # note falls back to the container name (then "?" — an empty
                # producer string must not drop the label AND its tooltip);
                # the tooltip leads with the note's full text so a clipped
                # repo keeps a hover path (the _kv_row clipped-note
                # convention).
                note = r.repo or r.container or "?"
                self._runners_layout.addWidget(self._kv_row(
                    r.name, value, note,
                    note_tooltip=(f"{note} — container {r.container or '?'}"
                                  + (f" · idle floor {r.pids_floor} tasks"
                                     if r.pids_floor is not None else ""))))
            extra = len(g.runners) - 16
            if extra > 0:
                more = QLabel(f"+{extra} more runner(s)")
                more.setStyleSheet(f"color:{_DIM};")
                more.setContentsMargins(24, 0, 0, 0)
                self._runners_layout.addWidget(more)
            # Burst-runner sessions (producer): the group's short-lived job
            # episodes, producer order (running oldest-first, then done
            # newest-first). The producer owns the lifecycle — rows exist
            # exactly while the frame carries them (running, or inside the
            # ~10 min done-linger), so appear-fast/disappear-fast and
            # no-stale-rows hold by construction. ≥ = the producer never
            # witnessed the start (ages are floors); missing numbers drop
            # their bit, never fabricate.
            for b in g.bursts[:8]:
                geq = "≥" if b.start_unseen else ""

                def _span(v):
                    # Non-finite AND negative junk both drop their bit —
                    # a corrupt -5 must not render a crisp "0s" claim.
                    return v is not None and math.isfinite(v) and v >= 0

                peak_bits = []
                if _span(b.peak_cpu_pct):
                    peak_bits.append(f"peak cpu {b.peak_cpu_pct:.0f}%")
                if _span(b.peak_mem_mib):
                    peak_bits.append(f"mem {_fmt_mem(b.peak_mem_mib)}")
                if b.state == "running":
                    txt = "bursting"
                    if _span(b.age_s):
                        txt += f" {geq}{_fmt_duration(b.age_s)}"
                    value = " · ".join([txt] + peak_bits)
                    self._runners_layout.addWidget(self._kv_row(
                        b.runner, value, "burst"))
                elif b.state == "done":
                    bits = ["burst over"]
                    if _span(b.dur_s):
                        bits.append(f"ran {geq}{_fmt_duration(b.dur_s)}")
                    if _span(b.ended_ago_s):
                        bits.append(
                            f"ended {_fmt_duration(b.ended_ago_s)} ago")
                    done = QLabel(
                        " · ".join([b.runner] + bits + peak_bits))
                    done.setStyleSheet(f"color:{_DIM};")
                    done.setContentsMargins(24, 0, 0, 0)
                    self._runners_layout.addWidget(done)
                else:
                    # An unknown state from a newer producer: name it
                    # verbatim rather than guess a lifecycle — but keep the
                    # data the frame carries (peaks drop nothing).
                    self._runners_layout.addWidget(self._kv_row(
                        b.runner,
                        " · ".join([f"burst · {b.state}"] + peak_bits),
                        "burst"))
            extra_b = len(g.bursts) - 8
            if extra_b > 0:
                more_b = QLabel(f"+{extra_b} more burst(s)")
                more_b.setStyleSheet(f"color:{_DIM};")
                more_b.setContentsMargins(24, 0, 0, 0)
                self._runners_layout.addWidget(more_b)
        self._render_hosted_block(hb)

    def _hosted_line(self, text, indent=0, color=_DIM):
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{color};")
        lbl.setContentsMargins(indent, 0, 0, 0)
        self._runners_layout.addWidget(lbl)

    def _render_hosted_block(self, hb):
        """The opt-in hosted burst-run watch (producer): runs the local
        sweeps cannot see, from the producer's Actions-API sampler. None =
        the watch is OFF (silent — Settings ▸ Hosted burst repos turns it
        on); ON always states something (warming / the API's own error /
        runs / a live 'no runs'), never a blank."""
        if hb is None:
            return
        if hb.ok is None:
            self._hosted_line("☁ hosted watch: checking (first sweep "
                              "pending)")
            return
        if hb.ok is False:
            self._hosted_line(f"☁ hosted watch: {hb.error or 'error'}")
            return
        stale = ""
        if (hb.checked_ago_s is not None and math.isfinite(hb.checked_ago_s)
                and hb.checked_ago_s >= 150):
            # Past the sampler's cadence: the sweep is stale — say so.
            stale = f" · checked {_fmt_duration(hb.checked_ago_s)} ago"
        if not hb.runs:
            self._hosted_line("☁ hosted watch: no queued/in-progress runs"
                              + stale)
            return
        self._hosted_line(f"☁ hosted watch ({len(hb.runs)} run(s)){stale}:",
                          color=_CLOUD)
        where_txt = {"this-box": "this box", "self-other-host":
                     "self-hosted elsewhere", "hosted": "hosted",
                     "pending": "pending"}
        for run in hb.runs[:8]:
            bits = [run.status, where_txt.get(run.where, run.where)]
            if run.runner:
                bits.append(run.runner)
            if (run.age_s is not None and math.isfinite(run.age_s)
                    and run.age_s >= 0):
                bits.append(_fmt_duration(run.age_s))
            repo = f" ({run.repo})" if run.repo else ""
            self._hosted_line(f"☁ {run.workflow}{repo} · "
                              + " · ".join(bits), indent=12, color=_CLOUD)
        if hb.unchecked:
            self._hosted_line(f"+{hb.unchecked} in-progress run(s) "
                              "unchecked (jobs budget)", indent=12)
        if hb.truncated:
            self._hosted_line(f"+{hb.truncated} more run(s) not shown",
                              indent=12)

    def _runner_group_header(self, g, expanded):
        """A clickable Runners group header: chevron + the repo group label
        and a glanceable summary (count · active · cpu · mem —
        bits omitted when the group had no reading, the honest-header rule).
        Clicking toggles the group and persists the expanded set."""
        row = _ClickRow(); row.setCursor(Qt.PointingHandCursor)
        h = QHBoxLayout(row); h.setContentsMargins(0, 4, 0, 0)
        chev = QLabel("▾" if expanded else "▸")
        chev.setStyleSheet(f"color:{_DIM}; font-weight:bold;")
        chev.setFixedWidth(14)
        nm = QLabel(f"{g.label}:")
        nm.setStyleSheet(f"color:{_DIM}; font-weight:bold;")
        if g.repo:
            nm.setToolTip(_tip(g.repo))
        if g.runners:
            bits = [f"{len(g.runners)} up"]
            if g.active_count:
                bits.append(f"{g.active_count} active")
            elif all(r.active is None for r in g.runners):
                # The fold must not hide the panel's stated cause: a group
                # with NO activity signal says so, never reading like a
                # measured all-quiet group (review F4).
                bits.append("no reading")
        else:
            # A burst-only group (its runner already left the fleet): no
            # live runner to read is not a reading failure — say which, and
            # count the lingering bursts so the group's sole content is
            # glanceable while collapsed.
            bits = ["no live runner"]
            done_n = len(g.bursts) - g.burst_running
            if done_n:
                bits.append(f"{done_n} recent burst(s)")
        if g.burst_running:
            # Groups default collapsed — a running burst must be glanceable
            # without expanding.
            bits.append(f"{g.burst_running} bursting")
        if g.cpu_pct_total is not None:
            bits.append(f"cpu {g.cpu_pct_total:.1f}%")
        if g.mem_mib_total is not None:
            bits.append(f"mem {_fmt_mem(g.mem_mib_total)}")
        summary = QLabel(" · ".join(bits))
        summary.setStyleSheet(f"color:{_DIM};")
        h.addWidget(chev); h.addWidget(nm); h.addWidget(summary)
        h.addStretch(1)
        row.clicked.connect(lambda: self._toggle_runner_group(g.label))
        return row

    def _toggle_runner_group(self, label):
        if label in self._burst_only_labels:
            # Session-only: a burst-only group's expansion never reaches
            # disk (see the __init__ note — the CI review-gate finding).
            (self._burst_expanded.discard(label)
             if label in self._burst_expanded
             else self._burst_expanded.add(label))
        else:
            # The label may have been expanded while the group was still
            # burst-only (runner since returned): fold that state in so one
            # click always inverts what the user SEES.
            was = (label in self._runners_expanded
                   or label in self._burst_expanded)
            self._burst_expanded.discard(label)
            if was:
                self._runners_expanded.discard(label)
            else:
                self._runners_expanded.add(label)
            try:
                save_runners_expanded(self._runners_expanded,
                                      path=self._settings_path)
            except Exception:  # noqa: BLE001 — view-state persistence must never crash the UI
                pass
        self._update_runners(self._last_snap)
        if not self._painting_frame:
            self._refresh_copy_menus()

    # ── egress panel ────────────────────────────────────────────────────────────
    def _build_egress(self):
        """The Egress panel: WHO is sending data off this box (producer — the
        box port of the Mac egress watch). Headline = the whole-box view
        (Windows NIC ↑/↓ + the up:down ratio — sustained out≫in is the
        context-re-send fingerprint) beside the TCP-attributed totals; rows =
        the producer's ranked talkers, worst ↑ first, with running Σ↑.
        Observes only — no alerts line, no contributor."""
        box = QGroupBox("Egress")
        v = QVBoxLayout(box)
        self._egress_sub = QLabel("—")
        self._egress_sub.setStyleSheet(f"color:{_DIM};")
        self._egress_sub.setWordWrap(True)
        self._egress_sub.setToolTip(
            "box = whole-box NIC rates (all traffic, both sides); tcp = the "
            "per-process attributed slice. Per-process rows are WSL-side "
            "only — a Windows process's traffic appears in the whole-box "
            "counters but never as a row (ss cannot see Windows sockets).")
        self._egress_layout = QVBoxLayout()
        v.addWidget(self._egress_sub)
        v.addLayout(self._egress_layout)
        return box

    def _update_egress(self, snap):
        _clear(self._egress_layout)
        eg = snap.egress if snap else None
        if eg is None:
            # An older producer / --no-egress / the watch still warming (it
            # needs ~10 s of samples): a stated cause, never a blank panel.
            self._egress_sub.setText(
                "no egress data — producer off, older, or warming")
            self._egress_sub.setVisible(True)
            return
        parts = []
        iface = eg.iface
        if iface is not None and iface.out_bps is not None:
            parts.append(f"box ↑{_fmt_rate(iface.out_bps)} "
                         f"↓{_fmt_rate(iface.in_bps)}")
            if (iface.out_in_ratio is not None
                    and math.isfinite(iface.out_in_ratio)):
                parts.append(f"up:down {iface.out_in_ratio:.1f}×")
        if eg.total_out_bps is not None:
            parts.append(f"tcp ↑{_fmt_rate(eg.total_out_bps)} "
                         f"↓{_fmt_rate(eg.total_in_bps)}")
        if eg.window_s is not None and math.isfinite(eg.window_s):
            parts.append(f"{eg.window_s:.0f}s window")
        # The headline stays VISIBLE here (unlike Scanners): it carries the
        # whole-box reading, not a placeholder for the rows.
        self._egress_sub.setText("  ·  ".join(parts) or "—")
        self._egress_sub.setVisible(True)
        if not eg.top:
            # The row asserts what the ranking KNOWS: it ranks WSL-side
            # sockets only, and a Windows upload keeps the box headline hot
            # while top stays [] — "no off-box traffic" here would contradict
            # the numbers right above it (review find, 2026-07-23).
            quiet = QLabel("no WSL-side talkers in the window")
            quiet.setStyleSheet(f"color:{_DIM};")
            quiet.setContentsMargins(12, 0, 0, 0)
            self._egress_layout.addWidget(quiet)
            return
        # The producer bounds top at 8 — the slice is pure defense so a
        # corrupt feed can't build unbounded widgets (the Sessions/Stuck
        # bounding convention).
        for t in eg.top[:8]:
            name = f"{t.name} [{t.pid}]" if t.pid is not None else t.name
            total = (f"Σ↑ {_fmt_mem(t.total_out_mib)}"
                     if t.total_out_mib is not None else "")
            # wsl proc_side: the #52 "Show ports of …" tie-in applies (and
            # windows-only affordances like procexp correctly do not).
            self._egress_layout.addWidget(self._kv_row(
                name,
                f"↑{_fmt_rate(t.out_bps)}  ↓{_fmt_rate(t.in_bps)}",
                total,
                proc_side="wsl" if t.pid is not None else None,
                proc_pid=t.pid))

    # ── scanners panel ──────────────────────────────────────────────────────────
    def _build_scanners(self):
        """The Scanners panel: the producer's Windows scanner-burn census
        (producer — the box port of the Mac scanner watch): the Defender family
        and the Search indexer reacting to WSL file churn. One compact row per
        group PRESENT on the host; the states are the producer's calibrated,
        hysteresis-gated judgments — the app never re-derives thresholds.
        Escalation (alerts line, ranked contributor) already rides the existing
        tiers; this panel is the structured surface. Advisory only."""
        box = QGroupBox("Scanners")
        v = QVBoxLayout(box)
        # Exec-pressure headline (producer, the mac #40 render): the spawn
        # census ABOVE the scanner rows — the event volume this fleet reacts
        # to. Independent lifecycle from the census rows below: spawns and
        # the scanner census fail independently, so neither may blank the
        # other (review finding 8). Wording lives in state.spawn_headline.
        self._spawn_head = QLabel("—")
        self._spawn_head.setStyleSheet(f"color:{_DIM};")
        self._spawn_head.setWordWrap(True)
        # Whole-stack tax headline (producer, the mac #48 render): ONE summed
        # "security stack: N.N cores" figure under the spawn line; warns only
        # on the producer's `taxing` judgment. Hidden while the producer
        # predates the field (the census stated-cause lines cover the family
        # — no fabricated line). Wording lives in state.security_stack_headline.
        self._stack_head = QLabel("")
        self._stack_head.setStyleSheet(f"color:{_DIM};")
        self._stack_head.setWordWrap(True)
        self._stack_head.setVisible(False)
        self._scan_sub = QLabel("—")
        self._scan_sub.setStyleSheet(f"color:{_DIM};")
        self._scan_sub.setWordWrap(True)
        self._scan_layout = QVBoxLayout()
        v.addWidget(self._spawn_head)
        v.addWidget(self._stack_head)
        v.addWidget(self._scan_sub)
        v.addLayout(self._scan_layout)
        return box

    def _update_scanners(self, snap):
        # The spawns headline FIRST, on its own stated-cause path — the
        # scanner early-returns below must never decide its visibility.
        sp = snap.spawns if snap else None
        if sp is None:
            self._spawn_head.setText("no spawn census from the producer")
            self._spawn_head.setToolTip("")
        else:
            text, tip = spawn_headline(sp)
            self._spawn_head.setText(text)
            self._spawn_head.setToolTip(_tip(tip))
        # The whole-stack tax line, on its own independent lifecycle too
        # (absent field → hidden, never a fabricated cause).
        head = security_stack_headline(snap.security_stack if snap else None)
        if head is None:
            self._stack_head.setVisible(False)
            self._stack_head.setText("")
            self._stack_head.setToolTip("")
        else:
            text, warn, tip = head
            self._stack_head.setText(text)
            self._stack_head.setToolTip(_tip(tip))
            self._stack_head.setStyleSheet(
                f"color:{YELLOW};" if warn else f"color:{_DIM};")
            self._stack_head.setVisible(True)
        _clear(self._scan_layout)
        sc = snap.scanners if snap else None
        if sc is None:
            # An older producer / --no-scanners / the counter section failed:
            # stay visible with a stated cause (informative-not-blank).
            self._scan_sub.setText("no scanner census from the producer")
            self._scan_sub.setVisible(True)
            return
        if not sc:
            # The census answered and none of the watched scanners runs here
            # (third-party AV / indexer disabled) — absent is not "idle".
            self._scan_sub.setText("no watched scanner runs on this host")
            self._scan_sub.setVisible(True)
            return
        # Rows carry the content — an emptied-but-visible QLabel would keep
        # its line and render as a blank first row (maintainer catch,
        # 2026-07-21 click-test).
        self._scan_sub.setText("")
        self._scan_sub.setVisible(False)
        # The producer emits one row per canonical group (2 today) — the slice
        # is pure defense so a corrupt feed can't build unbounded widgets
        # inside a frozen repaint (the Sessions/Stuck bounding convention).
        for r in sc[:8]:
            if r.state == "active":
                note = "⚡ active"
                if r.active_s is not None:
                    note += f" {_fmt_duration(r.active_s)}"
                color = YELLOW
            elif r.state == "burst":
                note = "burst"
                if r.ago_s is not None:
                    note += f" {_fmt_duration(r.ago_s)} ago"
                color = _DIM
            else:
                note = r.state or "idle"
                color = _DIM
            # `burning` is the producer's sustained-and-still-hot judgment (the
            # same flag firing the alert + contributor) — honor it in EVERY
            # state so the panel can never disagree with those tiers (e.g. the
            # hysteresis edge where a still-flagged row flips to burst).
            if r.burning:
                color = RED
            self._scan_layout.addWidget(self._kv_row(
                r.label, f"{r.cpu_pct:.0f}% · {r.proc_count}p · {_fmt_mem(r.rss_mib)}",
                note, color, note_tooltip=", ".join(r.procs[:16])))

    # ── ports panel ────────────────────────────────────────────────────────────
    def _build_ports(self):
        """The Ports panel: per-watched-port connection count + byte throughput +
        owner (producer's `ports` field, from --watch-port / the config channel).
        The set is configured live from Settings ▸ Watched ports. Quiet ("no ports
        watched") when none are configured, never a blank panel."""
        box = QGroupBox("Ports")
        v = QVBoxLayout(box)
        self._ports_sub = QLabel("—"); self._ports_sub.setStyleSheet(f"color:{_DIM};")
        self._ports_sub.setWordWrap(True)
        self._ports_layout = QVBoxLayout()
        v.addWidget(self._ports_sub)
        v.addLayout(self._ports_layout)
        # The ad-hoc "who hits this port?" entry point: watch a port without a
        # trip through Settings. A temporary watch is just a watch — the same
        # watch_ports state Settings edits, removed via right-click ▸ Unwatch.
        # Hidden when the window has no apply callback (bare constructions).
        self._ports_add = QPushButton("+ Watch a port…")
        self._ports_add.setFlat(True)
        self._ports_add.setCursor(Qt.PointingHandCursor)
        # Both segments f-strings: a continuation line without the f-prefix
        # keeps its }} literal and the whole sheet silently fails to parse
        # (#51 shipped that; caught via the smoke's stderr in §[29]).
        self._ports_add.setStyleSheet(
            f"QPushButton {{ color:{_DIM}; border:none; padding:0; "
            f"text-align:left; }}")
        self._ports_add.setToolTip(
            "Watch another port ad hoc — who connects (click the row's ▸ for "
            "per-connection detail), throughput, owner. Same watched set as "
            "Settings ▸ Watched ports; right-click a row ▸ Unwatch to remove.")
        self._ports_add.clicked.connect(self._prompt_watch_port)
        self._ports_add.setVisible(self._on_watch_ports_changed is not None)
        v.addWidget(self._ports_add)
        # The typed process-watch entry point (producer): a NAME follows every
        # matching process's ports and persists; a side:PID form is the
        # right-click Show-ports affordance for a process with no visible row
        # (session-only, like every pid target). Same styling rule as above —
        # both segments f-strings or the sheet dies silently (#51).
        self._procs_add = QPushButton("+ Watch a process…")
        self._procs_add.setFlat(True)
        self._procs_add.setCursor(Qt.PointingHandCursor)
        self._procs_add.setStyleSheet(
            f"QPushButton {{ color:{_DIM}; border:none; padding:0; "
            f"text-align:left; }}")
        self._procs_add.setToolTip(
            "Watch a process's ports — side:name matches case-insensitively "
            "as a substring (one entry follows a whole fleet) and persists "
            "until removed in Settings ▸ Watched processes; side:PID targets "
            "one exact pid for this session only. side is wsl or windows.")
        self._procs_add.clicked.connect(self._prompt_watch_process)
        self._procs_add.setVisible(self._on_watch_procs_changed is not None)
        v.addWidget(self._procs_add)
        return box

    def _port_row(self, p, series, stats, window_s, clients):
        """One watched-port row: a listening dot + label:port on the left (clips if
        long), a recent-activity sparkline (from the app-side rolling history — it
        disambiguates the point-in-time ``0``: a quiet port reads as a flat line,
        a broken one as activity that stopped), then conns · throughput · owner,
        with an amber 'sustained' advisory tag when the producer flags sustained
        activity. ``stats`` (PortRateStats or None) swaps the instantaneous ↓/↑
        readout for the window mean: the producer differences counters over its
        ~2 s tick, so bursty traffic reads a near-permanent 0 B/s live (epic
        #9's "rarely-populated rates") while the window mean stays legible. The
        peak and the live readout ride the tooltips (value + sparkline) and the
        owner the name tooltip — measured at the 440 px default width the value
        label gets ~170 px, which holds conns + the mean pair and nothing more.
        ``window_s`` is the history's time span (for the tooltip).

        ``clients`` (the app-side rolling PortClients memory) paints a dim
        ``clients:`` sub-line under the header — WHO hit the port over the
        graph window, without expanding: a hitter that connected and left
        between frames stays visible with a last-seen tag instead of
        vanishing with its connection.

        When the producer reports per-connection detail (producer), the row is
        clickable (chevron) and expands in place to the bounded connection list —
        WHAT connected: far end, its process when the producer's user could see
        that socket, cumulative bytes (service perspective). Session-only state,
        like the Memory expanders."""
        key = (p.label, p.port)
        expanded = key in self._ports_expanded and bool(p.connections)
        row = _ClickRow() if p.connections else QWidget()
        # The fixed-width chevron slot REPLACES the old 12 px left indent (margin
        # 0 + 12 px slot = the same name position), so the value label keeps the
        # measured width the #44 fit check pins — and names stay aligned whether
        # or not a row has detail to expand. Implicit spacing is zeroed and the
        # old default 6 px gaps re-inserted explicitly (addSpacing below): with
        # implicit spacing the chevron would add one MORE gap and steal exactly
        # that from the value label (caught by the §[21] fit check).
        h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(0)
        chev = QLabel("▾" if expanded else ("▸" if p.connections else ""))
        chev.setStyleSheet(f"color:{_DIM};")
        chev.setFixedWidth(12)
        h.addWidget(chev)
        if p.connections:
            row.clicked.connect(lambda: self._toggle_port(key))
            row.setCursor(Qt.PointingHandCursor)
        dot = "●" if p.listening else "○"
        name = QLabel(f"{dot} {p.label} :{p.port}")
        name.setStyleSheet(f"color:{_OK_GREEN if p.listening else _DIM};")
        # Ignored h-policy so a long label clips rather than widening the shared
        # content — but with a floor (and a full-identity tooltip), because the
        # fixed-width sparkline now competes for the same row width and the label
        # must stay recognisable.
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        name.setMinimumWidth(96)
        name.setToolTip(_tip(f"{p.label} :{p.port}"
                             + (f"  ·  {p.owner}" if p.owner else "")))
        # The right-click menu on this label gains an Unwatch action (the
        # ad-hoc watch's remove path) — see _show_copy_menu.
        name.setProperty("vrm_port", p.port)
        name.setProperty("vrm_port_name", f"{p.label} :{p.port}")
        h.addWidget(name, 1)
        if len(series.values) >= 2:      # the widget needs 2+ points to draw
            h.addSpacing(6)
            spark = Sparkline(color=_SPARK_ACCENT)
            spark.setFixedSize(84, 16)
            # Bucket-average down to the pixel budget FIRST, then normalize to
            # the bucketed peak — so the tallest bucket still touches the top.
            spark.set_values(scale_sparkline(
                bucket_means(series.values, _SPARK_BUCKETS)))
            unit = "throughput" if series.kind == "rate" else "connection count"
            pk = f" — peak {_fmt_rate(stats.peak)}" if stats is not None else ""
            spark.setToolTip(
                f"{unit} over the last {_fmt_duration(window_s)} (averaged), "
                f"scaled to its recent peak{pk} — window in Settings")
            h.addWidget(spark)
        if p.sustained:
            h.addSpacing(6)
            tag = QLabel("⚠ sustained"); tag.setStyleSheet(f"color:{_WARN};")
            h.addWidget(tag)
        bits = [f"{p.conns} conn"]
        if stats is not None:
            bits.append(f"↓{_fmt_rate(stats.avg_in)} ↑{_fmt_rate(stats.avg_out)}")
        elif p.bytes_in_rate is not None or p.bytes_out_rate is not None:
            # Fewer than two rated samples yet (fresh start): the live readout
            # is all there is.
            bits.append(f"↓{_fmt_rate(p.bytes_in_rate)} ↑{_fmt_rate(p.bytes_out_rate)}")
        val = QLabel("  ·  ".join(bits)); val.setStyleSheet(f"color:{_DIM};")
        if stats is not None:
            val.setToolTip(
                f"↓/↑ mean over the last {_fmt_duration(max(stats.span_s, 1.0))} "
                f"(window in Settings)  ·  peak {_fmt_rate(stats.peak)}"
                f"  ·  live now: ↓{_fmt_rate(p.bytes_in_rate)} "
                f"↑{_fmt_rate(p.bytes_out_rate)}")
        h.addSpacing(6)
        h.addWidget(val)
        note = clients_note(clients)
        if not expanded and not note:
            return row
        outer = QWidget()
        ov = QVBoxLayout(outer)
        ov.setContentsMargins(0, 0, 0, 0); ov.setSpacing(0)
        ov.addWidget(row)
        if note:
            # The recent-clients sub-line, indented to the port name (a
            # port-level summary, not a connection row).
            sub = QLabel(note)
            sub.setStyleSheet(f"color:{_DIM};")
            sub.setWordWrap(True)
            sub.setContentsMargins(12, 0, 0, 0)
            # The named processes are WSL pids from the same same-user ss
            # view as the detail rows — arm "Show ports of …" here too.
            sub.setProperty("vrm_proc_side", "wsl")
            sub.setToolTip(
                f"distinct clients seen over the last {_fmt_duration(window_s)} "
                "(window in Settings) — the connecting process where the "
                "producer could see its socket, else the far-end host; a "
                "departed client keeps a last-seen tag. From the producer's "
                "sampled top connections, so a floor — a hitter shorter than "
                "one sample can still be missed")
            ov.addWidget(sub)
        if not expanded:
            return outer
        # Expanded: stack the connection-detail rows under the header. The
        # producer bounds the list (top movers by cumulative bytes); ``conns``
        # keeps the true count, so the remainder reads as an explicit "+N more"
        # instead of silently truncating.
        for c in p.connections:
            bits = [c.peer]
            if c.process:
                bits.append(f"{c.process}[{c.pid}]" if c.pid is not None
                            else c.process)
            if c.bytes_in is not None or c.bytes_out is not None:
                bits.append(f"Σ ↓{_fmt_bytes(c.bytes_in)} ↑{_fmt_bytes(c.bytes_out)}")
            sub = QLabel("↳ " + "  ·  ".join(bits))
            sub.setStyleSheet(f"color:{_DIM};")
            sub.setContentsMargins(30, 0, 0, 0)
            # The connecting process is a WSL pid (same-user ss view) — arm
            # "Show ports of …" on the detail row.
            sub.setProperty("vrm_proc_side", "wsl")
            sub.setToolTip(
                f"{c.peer} — {c.process or 'process not visible to the producer'}"
                + (f" [pid {c.pid}]" if c.pid is not None else "")
                + (f"  ·  total ↓{_fmt_bytes(c.bytes_in)} ↑{_fmt_bytes(c.bytes_out)}"
                   f" since the connection opened (service perspective)"
                   if c.bytes_in is not None or c.bytes_out is not None else ""))
            ov.addWidget(sub)
        more = p.conns - len(p.connections)
        if more > 0:
            lbl = QLabel(f"↳ +{more} more (top {len(p.connections)} by bytes shown)")
            lbl.setStyleSheet(f"color:{_DIM};")
            lbl.setContentsMargins(30, 0, 0, 0)
            ov.addWidget(lbl)
        return outer

    def _toggle_port(self, key):
        """Expand/collapse a port row's connection detail (session-only), then
        re-render from the last frame — no waiting for a fresh one."""
        if key in self._ports_expanded:
            self._ports_expanded.discard(key)
        else:
            self._ports_expanded.add(key)
        if self._last_snap is not None:
            self._update_ports(self._last_snap)

    def set_desired_watch_ports(self, specs) -> None:
        """Adopt the app's desired watched-port specs (what it wrote to the
        monitor-config) and re-render — drives the ad-hoc add/unwatch
        affordances and the pending "waiting for the producer" rows (a desired
        port the reports don't carry yet). Renders even with NO frame yet
        (_update_ports handles snap=None): an add while the producer is down
        must show its pending row immediately — update_from's render-key skip
        would otherwise hold the stale "no ports watched" claim until the
        producer's first write."""
        self._desired_watch_ports = list(specs or [])
        self._update_ports(self._last_snap)

    def _prompt_watch_port(self):
        """The "+ Watch a port…" click: prompt for a spec and add it to the
        watched set. Validation errors surface immediately (the producer would
        skip a malformed config entry silently)."""
        text, ok = QInputDialog.getText(
            self, "Watch a port",
            "Port to watch — LABEL:PORT or a bare PORT\n"
            "(e.g. vscode:8080, or just 22):")
        if not ok or not text.strip():
            return
        err = self.add_watch_port(text.strip())
        if err:
            QMessageBox.warning(self, "Watch a port", err)

    def add_watch_port(self, spec) -> str:
        """Add one watched-port spec to the desired set and hand the FULL new
        list to the app (which persists it and writes the monitor-config file
        the producer re-reads within a few seconds). Returns an error message
        for bad input, "" on success; a port that is already watched is a
        no-op success (the producer dedups by port anyway)."""
        if self._on_watch_ports_changed is None:
            return "watched ports can't be changed from this window"
        try:
            new_specs = add_watch_spec(self._desired_watch_ports, spec)
        except ValueError as exc:
            return str(exc)
        if new_specs != self._desired_watch_ports:
            self._on_watch_ports_changed(new_specs)
        return ""

    def unwatch_port(self, port) -> None:
        """Remove every desired spec naming ``port`` (right-click ▸ Unwatch on
        a port row) and hand the new list to the app."""
        if self._on_watch_ports_changed is None:
            return
        new_specs = remove_watch_port(self._desired_watch_ports, port)
        if new_specs != self._desired_watch_ports:
            self._on_watch_ports_changed(new_specs)

    # ── process→ports targets (right-click ▸ Show ports) ─────────────────────────

    def set_proc_targets(self, targets) -> None:
        """Adopt the app's process→ports target list (session pid targets +
        persisted name watches, one list) and re-render — even with no frame
        yet, exactly like the watched-port desired set (a just-added target
        must show its querying row). Each target keeps its adoption time
        (new keys only) for the placeholder's grace window; removed keys
        drop theirs."""
        self._proc_targets = list(targets or [])
        now = time.monotonic()
        keys = {proc_target_key(t) for t in self._proc_targets}
        self._proc_target_added = {
            k: self._proc_target_added.get(k, now) for k in keys}
        self._update_ports(self._last_snap)

    def add_proc_target(self, side, pid, name=None) -> str:
        """Target one process (side "wsl"|"windows" — the two pid namespaces
        are separate) and hand the FULL new list to the app, which writes it
        through the config channel; the producer answers within a sweep or
        two. Returns an error message ("" on success); an already-targeted
        process is a no-op success. `name` seeds the producer's pid-reuse
        check when the row it came from carried one."""
        if self._on_watch_procs_changed is None:
            return "process targeting isn't available from this window"
        pid = int(pid)
        if any(t.get("pid") == pid and t.get("side") == side
               for t in self._proc_targets):
            return ""
        if len(self._proc_targets) >= PROC_TARGET_CAP:
            return (f"already showing ports for {PROC_TARGET_CAP} processes — "
                    "stop one first (right-click its row in the Ports panel, "
                    "or edit Settings ▸ Watched processes)")
        self._on_watch_procs_changed(
            self._proc_targets + [{"side": side, "pid": pid, "name": name}])
        return ""

    def remove_proc_target(self, side, pid) -> None:
        """Stop showing a target's ports (right-click ▸ Stop showing ports on
        its row) and hand the pruned list to the app."""
        if self._on_watch_procs_changed is None:
            return
        pid = int(pid)
        new = [t for t in self._proc_targets
               if not (t.get("pid") == pid and t.get("side") == side)]
        if new != self._proc_targets:
            self._on_watch_procs_changed(new)

    def _menu_add_proc_target(self, side, pid, name):
        err = self.add_proc_target(side, pid, name)
        if err:
            QMessageBox.warning(self, "Show ports", err)

    def _prompt_watch_process(self):
        text, ok = QInputDialog.getText(
            self, "Watch a process",
            "Process to watch — side:name or side:PID\n"
            "(e.g. wsl:node, windows:python, wsl:8021 — a name matches as a\n"
            "substring and persists; a PID is session-only):")
        if not ok or not text.strip():
            return
        err = self.add_watch_process(text.strip())
        if err:
            QMessageBox.warning(self, "Watch a process", err)

    def add_watch_process(self, text) -> str:
        """Add one typed process watch and hand the FULL new target list to
        the app. ``side:name`` becomes a persisted NAME watch (validated
        here — the producer skips malformed entries silently, which for
        typed input would read as a watch that never appears); ``side:PID``
        becomes a session-only pid target, exactly the right-click Show-ports
        affordance for a process with no visible row. Returns an error
        message ("" on success); an already-watched entry is a no-op
        success."""
        if self._on_watch_procs_changed is None:
            return "process watching isn't available from this window"
        side, sep, rest = str(text or "").partition(":")
        side_l, rest = side.strip().lower(), rest.strip()
        if sep and side_l in ("wsl", "windows") and rest.isascii() \
                and rest.isdigit():
            if int(rest) <= 0:
                return f"PID must be positive: {text!r}"
            return self.add_proc_target(side_l, int(rest), None)
        try:
            entry = parse_watch_proc_name(text)
        except ValueError as exc:
            return str(exc)
        if any(proc_target_key(t) == proc_target_key(entry)
               for t in self._proc_targets):
            return ""
        if len(self._proc_targets) >= PROC_TARGET_CAP:
            return (f"already watching {PROC_TARGET_CAP} processes — "
                    "stop one first (right-click its row in the Ports panel, "
                    "or edit Settings ▸ Watched processes)")
        self._on_watch_procs_changed(self._proc_targets + [entry])
        return ""

    def remove_proc_name_target(self, side, name) -> None:
        """Stop a NAME watch (right-click ▸ Stop watching on its header row)
        and hand the pruned list to the app — which also persists the
        removal (a persisted watch must never be unremovable)."""
        if self._on_watch_procs_changed is None:
            return
        key = ("name", side, str(name).strip().lower())
        new = [t for t in self._proc_targets if proc_target_key(t) != key]
        if new != self._proc_targets:
            self._on_watch_procs_changed(new)

    def _proc_target_row(self, t, rec):
        """One targeted process under the Ports panel's "targeted processes"
        block: the producer's answer when it has one (format_proc_ports —
        stated causes, never blank), a "querying…" placeholder while the
        config propagates. Right-click: Copy PID (the leading name[pid] form)
        + Stop showing ports."""
        if rec is None:
            who = f"{t.get('name') or '?'}[{t['pid']}] ({t['side']})"
            age = time.monotonic() - self._proc_target_added.get(
                proc_target_key(t), time.monotonic())
            if age > PROC_TARGET_GRACE_S:
                # Past the grace window nothing is coming — an old producer
                # (predates proc_ports) or one running without --config
                # (review H2: a permanent "querying…" would over-claim).
                line = (f"{who} — no answer from the producer — it may "
                        "predate process targeting or run without --config")
            else:
                line = f"{who} — querying… (the producer answers within a sweep)"
            tip = line
        else:
            line, tip = format_proc_ports(rec)
        lbl = QLabel("↳ " + line)
        lbl.setStyleSheet(f"color:{_DIM};")
        lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lbl.setContentsMargins(12, 0, 0, 0)
        lbl.setToolTip(tip)
        lbl.setProperty("vrm_proc_target", f"{t['side']}:{t['pid']}")
        return lbl

    def _proc_name_target_rows(self, t, rec) -> list:
        """The widgets for one NAME watch under the targeted block: a header
        (pattern · side · match count, or the stated cause / a querying
        placeholder on the pid-row grace ladder) plus one indented row per
        match — each leading with name[pid] so the #46 Copy-PID menu applies
        as-is. The header carries vrm_proc_name_target for right-click ▸
        Stop watching."""
        side, name = t.get("side"), t.get("name") or "?"
        rows = []
        if rec is None:
            age = time.monotonic() - self._proc_target_added.get(
                proc_target_key(t), time.monotonic())
            if age > PROC_TARGET_GRACE_S:
                line = (f"◎ {name} ({side}) — no answer from the producer — "
                        "it may predate name watches or run "
                        "without --config")
            else:
                line = (f"◎ {name} ({side}) — querying… "
                        "(the producer answers within a sweep)")
            header, tip, match_rows = line, line, []
        else:
            header, tip, match_rows = format_proc_name_ports(rec)
        lbl = QLabel(header)
        lbl.setStyleSheet(f"color:{_DIM};")
        lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lbl.setContentsMargins(12, 0, 0, 0)
        lbl.setToolTip(tip)
        lbl.setProperty("vrm_proc_name_target", f"{side}:{name}")
        rows.append(lbl)
        for mline, mtip in match_rows:
            m = QLabel("↳ " + mline)
            m.setStyleSheet(f"color:{_DIM};")
            m.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            m.setContentsMargins(24, 0, 0, 0)
            m.setToolTip(mtip)
            rows.append(m)
        return rows

    def _pending_port_row(self, label, port):
        """A desired-but-not-yet-reported watch (an ad-hoc add propagating):
        an honest placeholder, not a claim about the port. The producer
        normally reports every watched port — idle included — within a few
        seconds of a config write; until then this row only says "waiting".
        Right-click ▸ Unwatch works here too, so a mistyped add can be
        removed before it ever reports."""
        where = f":{port}" if label == str(port) else f"{label} :{port}"
        row = QWidget()
        h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(0)
        chev = QLabel(""); chev.setFixedWidth(12)
        h.addWidget(chev)
        name = QLabel(f"○ {where}")
        name.setStyleSheet(f"color:{_DIM};")
        name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        name.setMinimumWidth(96)
        name.setToolTip(
            f"{where} is in the watched set; the producer re-reads its config "
            "within a few seconds and will report it. If this never resolves, "
            "the producer may be running without --config — the amber notice "
            "will say so.")
        name.setProperty("vrm_port", port)
        name.setProperty("vrm_port_name", where)
        h.addWidget(name, 1)
        h.addSpacing(6)
        val = QLabel("watching — waiting for the producer")
        val.setStyleSheet(f"color:{_DIM};")
        h.addWidget(val)
        return row

    def _update_ports(self, snap):
        # The rolling series is accumulated in update_from (so it keeps growing
        # while this panel is hidden/collapsed and its render is skipped); this
        # fn only reads it. Direct callers (_toggle_port, set_port_window_s,
        # set_desired_watch_ports) re-render the already-accumulated last frame.
        _clear(self._ports_layout)
        ports = snap.ports if snap else []
        pending = pending_watch_ports(self._desired_watch_ports,
                                      (p.port for p in ports))
        if not ports:
            # Distinguish "nothing configured" from "configured but the producer
            # isn't applying the config" (the stale-producer notice is showing) —
            # the latter must not read as "no ports watched" (obs #343). The
            # pending rows' propagation promise is dropped with it (the banner
            # explains why it would be false); targeted-process rows still
            # render below — their placeholder degrades to a stated cause
            # past the grace window, so they can't promise forever either.
            if self._producer_hint is not None and self._producer_hint.ports_stale:
                self._ports_sub.setStyleSheet(f"color:{_WARN};")
                self._ports_sub.setText(
                    "⚠ watched ports are configured, but the producer isn't "
                    "reporting them — see the notice above")
                pending = []
                if not self._proc_targets:
                    return
            elif not pending and not self._proc_targets:
                self._ports_sub.setStyleSheet(f"color:{_DIM};")
                # Point only at controls that exist: the + button is hidden on
                # a window with no apply callback.
                self._ports_sub.setText(
                    "no ports watched — + Watch a port… below, or Settings ▸ "
                    "Watched ports" if self._on_watch_ports_changed is not None
                    else "no ports watched — add some in Settings ▸ Watched ports")
                return
            elif pending:
                # Only just-added watches so far: configured, not reported yet.
                self._ports_sub.setStyleSheet(f"color:{_DIM};")
                self._ports_sub.setText(
                    f"{len(pending)} watched  ·  waiting for the producer")
            else:
                # No watched ports at all — the panel is carrying only the
                # targeted-process block below.
                self._ports_sub.setStyleSheet(f"color:{_DIM};")
                self._ports_sub.setText("no ports watched")
        else:
            active_n = sum(1 for p in ports if p.active)
            sub = f"{len(ports)} watched  ·  {active_n} active"
            if pending:
                sub += f"  ·  {len(pending)} pending"
            self._ports_sub.setStyleSheet(f"color:{_DIM};")
            self._ports_sub.setText(sub)
        # Bound the expanded set to the live watched set (non-empty frames only,
        # matching PortHistory's prune semantics) so it can't grow across
        # Settings-driven port-set churn. The pending-only path renders with
        # ports == [] — that must NOT wipe the expansion state a producer
        # restart's empty frame would otherwise clear.
        if ports:
            self._ports_expanded &= {(p.label, p.port) for p in ports}
        for p in ports:
            self._ports_layout.addWidget(
                self._port_row(p, self._port_history.series(p.label, p.port),
                               self._port_history.rate_stats(p.label, p.port),
                               self._port_history.window_s,
                               self._port_clients.clients(p.label, p.port)))
        for label, port in pending:
            self._ports_layout.addWidget(self._pending_port_row(label, port))
        # Targeted processes (right-click ▸ Show ports): one row per session
        # target, matched to the snapshot's proc_ports answers — an answer the
        # snapshot doesn't carry yet renders a stated "querying…" placeholder,
        # and an answer for a REMOVED target is simply not rendered.
        if self._proc_targets:
            recs = (snap.proc_ports or []) if snap else []
            answers = {(r.side, r.pid): r for r in recs if r.kind != "name"}
            name_answers = {(r.side, (r.name or "").strip().lower()): r
                            for r in recs if r.kind == "name"}
            self._ports_layout.addWidget(self._dim_label("targeted processes:"))
            for t in self._proc_targets:
                if t.get("pid") is not None:
                    rec = answers.get((t["side"], t["pid"]))
                    self._ports_layout.addWidget(self._proc_target_row(t, rec))
                else:
                    rec = name_answers.get(
                        (t["side"], (t.get("name") or "").strip().lower()))
                    for w in self._proc_name_target_rows(t, rec):
                        self._ports_layout.addWidget(w)
        # Direct calls (the expander toggle, set_port_window_s) bypass
        # update_from — attach the copy menus to the fresh rows here too.
        self._refresh_copy_menus()

    def set_port_window_s(self, window_s) -> None:
        """Adopt a new Ports-graph window (Settings, live): the history and
        the recent-clients memory re-prune to it and the panel re-renders from
        the last frame — shrinking shows immediately; growing refills as new
        frames arrive."""
        self._port_history.set_window_s(window_s)
        self._port_clients.set_window_s(window_s)
        if self._last_snap is not None:
            self._update_ports(self._last_snap)
