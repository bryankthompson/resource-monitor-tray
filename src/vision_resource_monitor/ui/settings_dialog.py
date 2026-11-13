"""The tray *Settings…* dialog.

A thin QDialog over the persisted settings. It only reads current values in
and returns the edited values out (``values()``); the app is responsible for
persisting (``settings.save_stored``) and live-applying the safe subset. Kept
decoupled so it can be constructed and screenshotted headlessly.

Robustness posture : the app pre-fills this dialog from
STORED truth, never the flag-merged effective config — OK persists every
field, so an effective-config pre-fill would silently promote a session CLI
flag into the store. ``flagged`` names the KEY_* knobs a launch flag shadows;
each gets an explicit cue (the flag governs this session, the edit is saved
for the next launch). The watched-ports/processes fields validate LIVE — the
producer drops a malformed config entry silently, so a reject must be named
while it is still editable.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from ..settings import (
    DEFAULT_BUSY_MARKER_PCT, DEFAULT_GPU_INTERVAL_S, DEFAULT_HOST_REFRESH_S,
    GPU_INTERVAL_MAX_S, GPU_INTERVAL_MIN_S, HOST_REFRESH_MAX_S,
    HOST_REFRESH_MIN_S, KEY_INTERVAL,
    KEY_LOG_FILE, KEY_LOG_LEVEL, KEY_MUTE, KEY_SNAPSHOT, KEY_STALE_AFTER,
    KEY_THEME, PANEL_DEFS, Config, rejected_hosted_repo_specs,
    rejected_port_specs, rejected_proc_specs,
)
from ..state import (
    ACTIVE_HOT, ACTIVE_PULSE, ACTIVE_SUBTLE, DEFAULT_ACTIVE_STYLE,
    DEFAULT_PORT_WINDOW_S, PORT_WINDOW_MAX_S, PORT_WINDOW_MIN_S,
)

_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
_THEMES = ["dark", "light"]
# The Active-indicator styles as (value, label), in display order.
_ACTIVE_STYLES = [(ACTIVE_HOT, "Hot accent"),
                  (ACTIVE_PULSE, "Hot accent + tray pulse"),
                  (ACTIVE_SUBTLE, "Subtle green (legacy)")]
_DIM = "#8b949e"
_WARN = "#e5534b"

# The launch flag that shadows each Config knob (for the per-knob cue). Keys
# absent here (view-state knobs) have no CLI flag and never cue.
_FLAG_NAMES = {
    KEY_LOG_LEVEL: "--log-level",
    KEY_THEME: "--theme",
    KEY_MUTE: "--mute",
    KEY_INTERVAL: "--interval",
    KEY_STALE_AFTER: "--stale-after",
    KEY_SNAPSHOT: "--snapshot",
    KEY_LOG_FILE: "--log-file",
}


class _PanelsEditor(QWidget):
    """The Settings "Panels" list: one row per panel — a visibility checkbox, the
    title, and ▲/▼ reorder. ``order()`` / ``hidden()`` return the result; the app
    applies both live. The checkbox state is backed by the hidden set so it survives
    the re-render a move triggers."""

    def __init__(self, ordered, hidden, parent=None):
        super().__init__(parent)
        self._order = list(ordered)          # [(id, title), …]
        self._hidden = set(hidden)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._render()

    def _render(self):
        while self._layout.count():
            item = self._layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None); w.deleteLater()
        for i, (pid, title) in enumerate(self._order):
            row = QWidget()
            h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
            cb = QCheckBox(title)
            cb.setChecked(pid not in self._hidden)
            cb.setToolTip("Show this panel")
            cb.toggled.connect(lambda on, p=pid: self._set_visible(p, on))
            h.addWidget(cb, 1)
            up = QPushButton("▲"); up.setFixedWidth(30); up.setEnabled(i > 0)
            down = QPushButton("▼"); down.setFixedWidth(30)
            down.setEnabled(i < len(self._order) - 1)
            up.clicked.connect(lambda _=False, idx=i: self._move(idx, -1))
            down.clicked.connect(lambda _=False, idx=i: self._move(idx, +1))
            h.addWidget(up); h.addWidget(down)
            self._layout.addWidget(row)

    def _set_visible(self, pid, visible):
        if visible:
            self._hidden.discard(pid)
        else:
            self._hidden.add(pid)

    def _move(self, idx, delta):
        j = idx + delta
        if 0 <= j < len(self._order):
            self._order[idx], self._order[j] = self._order[j], self._order[idx]
            self._render()

    def order(self) -> list:
        return [pid for pid, _ in self._order]

    def hidden(self) -> set:
        return set(self._hidden)


class SettingsDialog(QDialog):
    def __init__(self, config: Config, parent=None, *, start_minimized=False,
                 gpu_interval=DEFAULT_GPU_INTERVAL_S,
                 host_refresh=DEFAULT_HOST_REFRESH_S, watch_ports=(),
                 watch_procs=(), panel_order=(), panel_hidden=(),
                 active_style=DEFAULT_ACTIVE_STYLE,
                 port_window_s=DEFAULT_PORT_WINDOW_S,
                 session_ports=True, hosted_bursts=(),
                 busy_marker_pct=DEFAULT_BUSY_MARKER_PCT, flagged=frozenset()):
        super().__init__(parent)
        self.setWindowTitle("Settings — Vision Resource Monitor")
        self.setMinimumWidth(520)
        self._flagged = frozenset(flagged)

        # ── Application ────────────────────────────────────────────────────
        app_form = QFormLayout()

        self._level = QComboBox()
        self._level.addItems(_LEVELS)
        lvl = str(config.log_level or "INFO").upper()
        self._level.setCurrentText(lvl if lvl in _LEVELS else "INFO")
        app_form.addRow("Log level", self._level)
        self._add_flag_cue(app_form, KEY_LOG_LEVEL)

        self._theme = QComboBox()
        self._theme.addItems(_THEMES)
        self._theme.setCurrentText(
            "light" if str(config.theme).lower() == "light" else "dark")
        app_form.addRow("Theme", self._theme)
        self._add_flag_cue(app_form, KEY_THEME)

        # How ● ACTIVE announces itself (header + Activity line + tray icon).
        # View-state like start-minimized — read out via active_style().
        self._active_style = QComboBox()
        for value, label in _ACTIVE_STYLES:
            self._active_style.addItem(label, value)
        idx = self._active_style.findData(active_style)
        self._active_style.setCurrentIndex(idx if idx >= 0 else 0)
        self._active_style.setToolTip(
            "How the ACTIVE state is shown: a hot accent that pops (optionally "
            "pulsing the tray icon), or the legacy subtle green. High-utilisation "
            "yellow/red always keep their meaning.")
        app_form.addRow("Active indicator", self._active_style)

        # The win cpu row's busy marker (view-state): the bar and trend turn
        # red from it. Read out via busy_marker_pct().
        self._busy_marker = QDoubleSpinBox()
        self._busy_marker.setRange(1.0, 100.0)
        self._busy_marker.setDecimals(0)
        self._busy_marker.setSuffix(" %")
        self._busy_marker.setValue(busy_marker_pct)
        self._busy_marker.setToolTip(
            "Where the Host panel's win cpu bar draws its marker line. The "
            "bar and trend turn red at or above it.")
        app_form.addRow("Win cpu busy marker", self._busy_marker)

        self._mute = QCheckBox("Mute notifications (tray pop-ups)")
        self._mute.setChecked(bool(config.mute_notifications))
        app_form.addRow("", self._mute)
        self._add_flag_cue(app_form, KEY_MUTE)

        # View-state, not part of Config's CLI precedence — persisted separately by
        # the app (settings.save_start_minimized) and read out via start_minimized().
        self._start_min = QCheckBox("Start minimized to tray (no window on launch)")
        self._start_min.setChecked(bool(start_minimized))
        app_form.addRow("", self._start_min)

        self._interval = QDoubleSpinBox()
        self._interval.setRange(0.2, 60.0)
        self._interval.setSingleStep(0.5)
        self._interval.setDecimals(1)
        self._interval.setSuffix(" s")
        self._interval.setKeyboardTracking(False)
        self._interval.setValue(float(config.interval or 2.0))
        app_form.addRow("Poll interval", self._interval)
        self._add_flag_cue(app_form, KEY_INTERVAL)

        self._stale = QDoubleSpinBox()
        self._stale.setRange(1.0, 600.0)
        self._stale.setSingleStep(1.0)
        self._stale.setDecimals(0)
        self._stale.setSuffix(" s")
        self._stale.setKeyboardTracking(False)
        self._stale.setValue(float(config.stale_after or 8.0))
        app_form.addRow("Stale after", self._stale)
        self._add_flag_cue(app_form, KEY_STALE_AFTER)

        # The Ports panel's sparkline window — how much recent activity each
        # per-port graph spans. View-state (read out via port_window_s()); the
        # app applies it to the rolling history live.
        self._port_window = QDoubleSpinBox()
        self._port_window.setRange(PORT_WINDOW_MIN_S, PORT_WINDOW_MAX_S)
        self._port_window.setSingleStep(5.0)
        self._port_window.setDecimals(0)
        self._port_window.setSuffix(" s")
        self._port_window.setKeyboardTracking(False)
        self._port_window.setToolTip(
            "How much recent activity the Ports sparklines span (5 s – 10 min). "
            "Longer = more context; samples are averaged into the graph's width, "
            "so bursts read as area rather than one-tick spikes.")
        self._port_window.setValue(float(port_window_s or DEFAULT_PORT_WINDOW_S))
        app_form.addRow("Ports graph window", self._port_window)

        app_box = self._section(
            "Application", app_form,
            "Applies immediately — except start-minimized, which takes effect "
            "on the next launch.")

        # ── Producer ───────────────────────────────────────────────────────
        prod_form = QFormLayout()

        # Producer write rates (the monitor-config:v1 channel). Distinct from the
        # app-side "Poll interval" above (how often the app READS): these tune how
        # often the PRODUCER writes, since the app can't render fresher than that.
        # Ranges match the producer's clamps.
        self._prod_gpu = QDoubleSpinBox()
        self._prod_gpu.setRange(GPU_INTERVAL_MIN_S, GPU_INTERVAL_MAX_S)
        self._prod_gpu.setSingleStep(0.5)
        self._prod_gpu.setDecimals(1)
        self._prod_gpu.setSuffix(" s")
        self._prod_gpu.setKeyboardTracking(False)
        self._prod_gpu.setToolTip(
            "How often the producer samples the GPU + writes a snapshot. Lower = "
            "fresher, more CPU.")
        self._prod_gpu.setValue(float(gpu_interval or DEFAULT_GPU_INTERVAL_S))
        prod_form.addRow("GPU write rate", self._prod_gpu)

        self._prod_host = QDoubleSpinBox()
        self._prod_host.setRange(HOST_REFRESH_MIN_S, HOST_REFRESH_MAX_S)
        self._prod_host.setSingleStep(1.0)
        self._prod_host.setDecimals(0)
        self._prod_host.setSuffix(" s")
        self._prod_host.setKeyboardTracking(False)
        self._prod_host.setToolTip(
            "How often the producer refreshes the slow host/docker collectors "
            "(memory panel + docker idle-burn).")
        self._prod_host.setValue(float(host_refresh or DEFAULT_HOST_REFRESH_S))
        prod_form.addRow("Host/docker refresh", self._prod_host)

        # Watched ports — the producer monitors each for connection count + byte
        # throughput (the Ports panel). Comma-separated LABEL:PORT specs; sent to
        # the producer via monitor-config:v1 (live-reconfigurable). Empty = none.
        self._watch_ports = QLineEdit(", ".join(watch_ports or []))
        self._watch_ports.setPlaceholderText(
            "label:port, label:port  (e.g. vscode:8080, 22)")
        self._watch_ports.setToolTip(
            "Ports the producer watches (connection count + throughput + owner). "
            "Comma-separated LABEL:PORT (a bare PORT labels itself). Empty = none.")
        prod_form.addRow("Watched ports", self._watch_ports)
        # Live reject cue: the producer skips a malformed config entry
        # SILENTLY (fail-open), so an unnamed reject reads as a watch that
        # never appears — name it while it is still editable.
        self._ports_reject = self._reject_cue(prod_form)
        self._watch_ports.textChanged.connect(self._update_ports_reject)
        # Cue a PRE-EXISTING malformed spec at open — textChanged alone would
        # leave it silent until the first keystroke (review fold).
        self._update_ports_reject()

        # Watched processes — persisted NAME watches (producer): each entry's
        # ports render in the Ports panel's targeted block; the producer
        # re-matches live pids every sweep. Session-only PID targets never
        # live here (right-click ▸ Show ports / "+ Watch a process…").
        self._watch_procs = QLineEdit(", ".join(watch_procs or []))
        self._watch_procs.setPlaceholderText(
            "side:name, side:name  (e.g. wsl:node, windows:python)")
        self._watch_procs.setToolTip(
            "Processes whose ports the producer reports (listeners + "
            "connections, grouped per matching process). side is wsl or "
            "windows — the two pid namespaces are separate. The name matches "
            "case-insensitively as a substring, so one entry follows a whole "
            "fleet; on windows it matches the extension-less process name "
            "(python, not python.exe). Persists until removed here. "
            "Empty = none.")
        prod_form.addRow("Watched processes", self._watch_procs)
        self._procs_reject = self._reject_cue(prod_form)
        self._watch_procs.textChanged.connect(self._update_procs_reject)
        self._update_procs_reject()

        # Hosted burst-run watch (producer, OPT-IN): the dispatcher's hosted
        # lanes have zero local footprint — the producer polls the Actions
        # API for these repos only when the field is non-empty.
        self._hosted_bursts = QLineEdit(", ".join(hosted_bursts or []))
        self._hosted_bursts.setPlaceholderText(
            "owner/repo, owner/repo  (empty = off — no API calls)")
        self._hosted_bursts.setToolTip(
            "Repos whose queued/in-progress Actions runs the producer polls "
            "(gh api, ~2 min cadence) and classifies against the local "
            "runner fleet — the only way to see the dispatcher's HOSTED "
            "bursts, which never touch this box. Empty = off: the producer "
            "makes zero GitHub API calls.")
        prod_form.addRow("Hosted burst repos", self._hosted_bursts)
        self._hosted_reject = self._reject_cue(prod_form)
        self._hosted_bursts.textChanged.connect(self._update_hosted_reject)
        self._update_hosted_reject()

        # Session-ports census toggle (producer): per-session listen ports on
        # the Sessions rows. Rides the producer's existing socket sample.
        self._session_ports = QCheckBox(
            "Session ports (listen ports on Sessions rows)")
        self._session_ports.setChecked(bool(session_ports))
        self._session_ports.setToolTip(
            "Each Sessions row names the listening ports its WSL-side process "
            "tree holds (a nested session's ports stay its own). The census "
            "rides the producer's existing per-tick socket sample.")
        prod_form.addRow("", self._session_ports)

        prod_box = self._section(
            "Producer", prod_form,
            "Written to the producer's config file — picked up within a few "
            "seconds.")

        # ── Panels ─────────────────────────────────────────────────────────
        # Show/hide (checkbox) + reorder (▲/▼) for every panel; the single
        # place for panel visibility + order. Lists the canonical panels in the
        # current order (titles from PANEL_DEFS). Applied live on accept
        # (persisted for next launch).
        panels_form = QFormLayout()
        titles = dict(PANEL_DEFS)
        ids = list(panel_order) or [pid for pid, _ in PANEL_DEFS]
        self._panels = _PanelsEditor(
            [(pid, titles.get(pid, pid)) for pid in ids], set(panel_hidden))
        panels_form.addRow(self._panels)
        panels_box = self._section(
            "Panels", panels_form,
            "Order and visibility apply live and persist.")

        # ── Paths ──────────────────────────────────────────────────────────
        paths_form = QFormLayout()

        self._snapshot = QLineEdit(str(config.snapshot_path or ""))
        self._snapshot.setPlaceholderText(
            "(default: $VRM_SNAPSHOT_PATH / LOCALAPPDATA)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self._snapshot, 1)
        row.addWidget(browse)
        snap_w = QWidget()
        snap_w.setLayout(row)
        paths_form.addRow("Snapshot path", snap_w)
        self._add_flag_cue(paths_form, KEY_SNAPSHOT)

        self._logfile = QLineEdit(str(config.log_file or ""))
        self._logfile.setPlaceholderText(
            "(default: gpu-activity.jsonl next to the snapshot)")
        browse_log = QPushButton("Browse…")
        browse_log.clicked.connect(self._browse_log)
        row_l = QHBoxLayout()
        row_l.setContentsMargins(0, 0, 0, 0)
        row_l.addWidget(self._logfile, 1)
        row_l.addWidget(browse_log)
        logf_w = QWidget()
        logf_w.setLayout(row_l)
        paths_form.addRow("Activity log path", logf_w)
        self._add_flag_cue(paths_form, KEY_LOG_FILE)

        paths_box = self._section(
            "Paths", paths_form,
            "Apply immediately — the reader follows a new snapshot path on "
            "the next tick.")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        outer = QVBoxLayout(self)
        outer.addWidget(app_box)
        outer.addWidget(prod_box)
        outer.addWidget(panels_box)
        outer.addWidget(paths_box)
        outer.addWidget(buttons)

    # ── section / cue helpers ─────────────────────────────────────────────
    def _section(self, title, form, cue_text) -> QGroupBox:
        """A titled section: its form plus one dim apply-semantics cue line
        (replacing the old single dense end-note paragraph)."""
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        v.addLayout(form)
        cue = QLabel(cue_text)
        cue.setWordWrap(True)
        cue.setStyleSheet(f"color:{_DIM};")
        v.addWidget(cue)
        return box

    def _add_flag_cue(self, form, key) -> None:
        """Under a knob a launch flag shadows: say WHO wins, explicitly — the
        flag governs this session; the edit is saved for the next launch."""
        if key not in self._flagged:
            return
        flag = _FLAG_NAMES.get(key, key)
        # One short line on purpose: a wrapped cue clips inside its form row,
        # and the field column starts indented — the tooltip carries the
        # full sentence.
        cue = QLabel(f"set by {flag} for this session — "
                     f"edit applies next launch")
        cue.setStyleSheet(f"color:{_DIM}; font-style: italic;")
        cue.setToolTip(
            f"This session was launched with {flag}, which takes precedence "
            f"over the stored preference. Your edit is persisted and takes "
            f"effect on the next launch without the flag.")
        form.addRow("", cue)

    def _reject_cue(self, form) -> QLabel:
        """The (hidden-until-needed) live-validation label under a spec field."""
        cue = QLabel("")
        cue.setWordWrap(True)
        cue.setStyleSheet(f"color:{_WARN};")
        cue.setVisible(False)
        form.addRow("", cue)
        return cue

    def _update_ports_reject(self) -> None:
        msgs = rejected_port_specs(self.watched_ports())
        self._ports_reject.setText(
            "not applied: " + "; ".join(msgs) if msgs else "")
        self._ports_reject.setVisible(bool(msgs))

    def _update_procs_reject(self) -> None:
        msgs = rejected_proc_specs(self.watched_procs())
        self._procs_reject.setText(
            "not applied: " + "; ".join(msgs) if msgs else "")
        self._procs_reject.setVisible(bool(msgs))

    def _update_hosted_reject(self) -> None:
        msgs = rejected_hosted_repo_specs(self.hosted_burst_repos())
        self._hosted_reject.setText(
            "not applied: " + "; ".join(msgs) if msgs else "")
        self._hosted_reject.setVisible(bool(msgs))

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select snapshot file", self._snapshot.text() or "",
            "JSON (*.json);;All files (*)")
        if path:
            self._snapshot.setText(path)

    def _browse_log(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select activity-log file", self._logfile.text() or "",
            "JSONL (*.jsonl);;All files (*)")
        if path:
            self._logfile.setText(path)

    def values(self) -> dict:
        """The edited **Config** settings as a ``{KEY_*: value}`` dict (all fields
        present). The start-minimized toggle is view-state, not Config — read it
        via ``start_minimized()`` and persist it separately."""
        return {
            KEY_LOG_LEVEL: self._level.currentText(),
            KEY_THEME: self._theme.currentText(),
            KEY_MUTE: self._mute.isChecked(),
            KEY_INTERVAL: float(self._interval.value()),
            KEY_STALE_AFTER: float(self._stale.value()),
            KEY_SNAPSHOT: self._snapshot.text().strip(),
            KEY_LOG_FILE: self._logfile.text().strip(),
        }

    def start_minimized(self) -> bool:
        """The start-minimized preference (view-state, persisted separately from
        the Config ``values()`` above)."""
        return self._start_min.isChecked()

    def active_style(self) -> str:
        """The chosen Active-indicator style (a state.ACTIVE_STYLES value);
        view-state, persisted separately and live-applied by the app."""
        return self._active_style.currentData()

    def busy_marker_pct(self) -> float:
        """The chosen win cpu busy-marker percent; view-state, persisted
        separately and live-applied by the app."""
        return self._busy_marker.value()

    def producer_cadence(self) -> tuple[float, float]:
        """The edited (gpu_interval_s, host_refresh_s) for the producer. View-state
        (not Config ``values()``) — the app persists it and writes the
        monitor-config:v1 file the producer reads."""
        return float(self._prod_gpu.value()), float(self._prod_host.value())

    def watched_ports(self) -> list:
        """The edited watched-port specs (['label:port', …]); view-state, written
        into the monitor-config:v1 file. Blank entries are dropped."""
        return [t.strip() for t in self._watch_ports.text().split(",") if t.strip()]

    def watched_procs(self) -> list:
        """The edited watched-process NAME tokens (['side:name', …]); the app
        validates each (parse_watch_proc_name) and writes the survivors into
        the monitor-config:v1 file. Blank entries are dropped."""
        return [t.strip() for t in self._watch_procs.text().split(",") if t.strip()]

    def session_ports(self) -> bool:
        """The session-ports census toggle (view-state; the app persists it and
        the value rides every monitor-config write)."""
        return self._session_ports.isChecked()

    def hosted_burst_repos(self) -> list:
        """The edited hosted-burst repo tokens (['owner/repo', …]); the app
        validates them (clean_hosted_repos) and the survivors ride every
        monitor-config write. Blank entries are dropped; empty = off."""
        return [t.strip() for t in self._hosted_bursts.text().split(",")
                if t.strip()]

    def port_window_s(self) -> float:
        """The edited Ports-graph window in seconds; view-state, persisted
        separately and live-applied to the rolling port history by the app."""
        return float(self._port_window.value())

    def panel_order(self) -> list:
        """The edited panel order (a list of ids); view-state, applied live by the
        app (and persisted for next launch)."""
        return self._panels.order()

    def panels_hidden(self) -> set:
        """The set of panel ids the user unchecked (hidden); view-state, applied live
        by the app (and persisted for next launch)."""
        return self._panels.hidden()
