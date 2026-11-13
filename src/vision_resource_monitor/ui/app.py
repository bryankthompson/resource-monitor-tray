"""The Qt application: a QTimer polls the snapshot, updates the tray + window,
and fires toasts on run/connectivity edges. Thin glue over the pure modules.
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from .. import (
    build_version, default_activity_log_path, default_monitor_config_path,
    default_snapshot_path, rebuild_command_hint, wsl_rebuild_argv,
)
from ..askclaude import command_hint, launch_argvs, prepare_script
from .. import procexplorer
from .. import LIVE_ACTIONS_OFF_REASON, live_actions_enabled, procmonitor
from ..logging_setup import resolve_log_level, setup_logging
from ..reader import SnapshotReader
from ..settings import (
    Config, clean_hosted_repos, clean_proc_targets, load_active_style,
    load_busy_marker_pct, load_hosted_bursts, load_panel_hidden,
    load_panel_order, load_port_window_s, load_producer_cadence,
    load_session_ports, load_watch_proc_names, load_watch_ports,
    panel_hidden_configured, parse_watch_proc_name, resolve_config,
    save_active_style, save_busy_marker_pct, save_hosted_bursts, save_panel_hidden,
    save_panel_order, save_port_window_s,
    save_producer_cadence, save_session_ports, save_start_minimized,
    save_stored, save_watch_proc_names, save_watch_ports,
    stored_truth_config, write_monitor_config,
)
from ..state import (
    ACTIVE, ACTIVE_PULSE, PROC_TARGET_CAP, ProducerConfigTracker,
    ToastTracker, icon_spec, overall_status, panel_populate_report, tooltip,
)
from .theme import apply_theme
from .trayicon import TrayIcon
from .window import MonitorWindow

log = logging.getLogger(__name__)

_TOAST_ICON = {"info": QSystemTrayIcon.Information,
               "success": QSystemTrayIcon.Information,
               "warning": QSystemTrayIcon.Warning}


class MonitorApp:
    def __init__(self, qapp: QApplication, snapshot_path, *, interval_ms=2000,
                 stale_after_s=8.0, log_path=None, use_tray=True,
                 show_host=True, show_mem=True, config=None, settings_path=None,
                 diag_log_file=None, start_minimized=False, cli=None):
        self.qapp = qapp
        # The session's CLI layer ({KEY_*: value}, None = flag not given).
        # Kept so Settings can (a) pre-fill from STORED truth while cueing the
        # flag-shadowed knobs, and (b) live-apply edits with the session flags
        # overlaid (flag > pref for THIS session; the edit persists for the
        # next launch) — without it, opening Settings under a flag silently
        # promoted the flag into the persisted settings.
        self._cli = {k: v for k, v in dict(cli or {}).items() if v is not None}
        self.reader = SnapshotReader(snapshot_path, stale_after_s=stale_after_s)
        self.toasts = ToastTracker()
        self._notified_log_missing = False
        self._last_status = None
        self._last_error = None
        self._snapshot_path = Path(snapshot_path)
        self._settings_path = settings_path
        self._diag_log_file = diag_log_file
        self._start_minimized = bool(start_minimized)
        # The producer config the user chose (persisted): sample cadence + watched
        # ports. Pre-fills the Settings dialog; applying it writes the
        # monitor-config:v1 file the producer reads (--config) — the one place this
        # renderer writes.
        self._producer_cadence = load_producer_cadence(settings_path)
        self._watch_ports = load_watch_ports(settings_path)
        # Session-ports census toggle rides every monitor-config
        # write; the producer treats an absent key as ON, so the stored choice
        # only ever narrows.
        self._session_ports = load_session_ports(settings_path)
        # Hosted burst-run watch repos (producer, OPT-IN): rides every
        # monitor-config write (full-state writer — an omitted field would
        # fall back to the producer's CLI default, the earlier release lesson).
        self._hosted_bursts = load_hosted_bursts(settings_path)
        # The open Settings dialog, if any — a modal-re-entrancy guard: the
        # tray menu can fire open_settings again while exec() blocks (the
        # earlier release mechanics fold).
        self._settings_dlg = None
        # process→ports targets, two persistence classes in ONE list (the
        # config channel carries them together): pid targets are SESSION-ONLY
        # by design (never persisted — a forgotten pid target would keep the
        # producer's Windows sweep enriched forever against the footprint
        # rule, and a reused pid would silently follow the wrong process);
        # NAME targets PERSIST — a name re-matches live pids every
        # sweep so pid reuse can't lie, and the Settings field is their
        # standing removal affordance. Set from the window's right-click ▸
        # Show ports / "+ Watch a process…" / Settings ▸ Watched processes.
        self._watch_procs: list = load_watch_proc_names(settings_path)
        # The config file the last apply wrote — when the snapshot path moves
        # (Settings), the OLD file's session targets are cleared so the old
        # producer can't keep sweeping targets nobody can remove (review L3).
        self._last_cfg_path = default_monitor_config_path(self._snapshot_path)
        # The panel order the user chose (view-state); pre-fills the Settings Panels
        # list and drives the window's stack. Applied live on Settings-accept.
        self._panel_order = load_panel_order(settings_path)
        # Active-indicator style (view-state): how ACTIVE announces itself. The
        # pulse timer only runs while the status IS active and the user chose the
        # pulse style, so the default steady styles cost nothing per-frame.
        self._active_style = load_active_style(settings_path)
        # win cpu busy marker (view-state): pre-fills Settings, applied live.
        self._busy_marker_pct = load_busy_marker_pct(settings_path)
        # The Ports-graph window (view-state): the window's PortHistory loads it
        # itself at construction; the app keeps it only to pre-fill Settings.
        self._port_window_s = load_port_window_s(settings_path)
        self._pulse_on = False
        self._last_result = None
        # In-flight Process-Explorer launches: (QProcess, QTimer) pairs kept
        # referenced until their marker (or timeout) arrives — an unreferenced
        # PySide object is torn down under its own signals.
        self._px_running: set = set()
        self._pulse_timer = QTimer()
        self._pulse_timer.setInterval(600)
        self._pulse_timer.timeout.connect(self._pulse_tick)
        # Stale-build detection: this frozen build's stamped git SHA (None when
        # running from source). Compared each tick to the producer's live
        # source_version; the one-time nudge is edge-tracked by this flag.
        self._build_version = build_version()
        self._notified_new_version = False
        self._source_version = None   # latest producer source SHA (for About/banner)
        # Stale-producer detection: once a monitor-config file exists, a producer
        # running without --config silently ignores it — but the snapshot shows it
        # (configured watched ports never appear; the cadence never changes). The
        # tracker turns that sustained contradiction into a user-facing hint.
        self._producer_config_watch = ProducerConfigTracker()
        self._producer_hint = None

        # Source of truth for the Settings dialog. main() passes the resolved
        # Config; when absent (e.g. the smoke harness) synthesise one from the
        # discrete args so the dialog still pre-fills sensibly.
        if config is None:
            lvl = logging.getLogger("vision_resource_monitor").level
            config = Config(
                snapshot_path=str(snapshot_path), interval=interval_ms / 1000.0,
                stale_after=stale_after_s, theme="dark", show_host=show_host,
                show_mem=show_mem, mute_notifications=False, log_file="",
                log_level=logging.getLevelName(lvl) if lvl else "INFO")
        self._config = config

        # Unified panel visibility (Settings ▸ Panels checkboxes). One-time migration:
        # if it's never been set, seed it from the legacy show_host/show_mem so an
        # existing preference carries over; thereafter the panel-hidden set is the
        # single source of truth (the old checkboxes are gone).
        if not panel_hidden_configured(settings_path):
            seed = set()
            if not self._config.show_host:
                seed.add("host")
            if not self._config.show_mem:
                seed.add("memory")
            save_panel_hidden(seed, path=settings_path)
        self._panel_hidden = load_panel_hidden(settings_path)

        # Activity-log path the "Open activity log" item opens. An explicit
        # arg wins (tests); else the persisted/CLI setting; else the file the
        # producer writes next to the snapshot — so it works with zero config.
        if log_path is not None:
            self.log_path = Path(log_path)
        elif self._config.log_file:
            self.log_path = Path(self._config.log_file).expanduser()
        else:
            self.log_path = default_activity_log_path(self._snapshot_path)

        # Live actions (rebuild, ask-Claude, ProcMon) are unwired, not just
        # guarded, when off: the window hides an affordance whose callback is None.
        live = live_actions_enabled()
        self.window = MonitorWindow(show_host=show_host, show_mem=show_mem,
                                    on_settings=self.open_settings,
                                    on_open_log=self.open_log,
                                    on_about=self.open_about,
                                    on_rebuild=self.rebuild_now if live else None,
                                    on_ask_claude=self.ask_claude if live else None,
                                    on_open_procexp=self.open_in_procexp if live else None,
                                    on_open_procmon=self.open_in_procmon if live else None,
                                    on_watch_ports_changed=self._on_watch_ports_edited,
                                    on_watch_procs_changed=self._on_watch_procs_edited,
                                    settings_path=settings_path)
        # Seed the window with the desired watched-port set (it renders honest
        # "waiting for the producer" rows for any port the reports lack), and
        # clear any proc targets a crashed session left in the config file.
        self.window.set_desired_watch_ports(self._watch_ports)
        self._reconcile_stray_proc_targets()
        # Seed the window's target view UNCONDITIONALLY: on a clean restart
        # the reconcile no-ops (file already matches), and a window left
        # empty would both hide the standing name watches AND hand a stale
        # empty list back on the first add — silently erasing every
        # persisted watch (review B1, both lenses).
        self.window.set_proc_targets(self._watch_procs)
        # The no-tray exit (window close with quitOnLastWindowClosed) never
        # reaches quit() — aboutToQuit covers it (idempotent with quit()).
        self.qapp.aboutToQuit.connect(self._clear_proc_targets_on_exit)
        self.tray = None
        if use_tray:
            self.tray = TrayIcon(
                on_show=self.show_window,
                on_open_log=self.open_log,
                on_settings=self.open_settings,
                on_quit=self.quit,
                on_about=self.open_about,
                on_rebuild=self.rebuild_now if live else None,
            )
            self.tray.show()
            # Show the window on launch by default; the "start minimized to tray"
            # preference keeps it hidden until the user opens it from the tray.
            if not self._start_minimized:
                self.window.show()
                self.window.raise_()
                self.window.activateWindow()
        else:
            # No tray (e.g. WSLg, or --window): the window IS the app. Closing it
            # quits — the caller sets quitOnLastWindowClosed accordingly. Under
            # WSLg/XWayland a plain show() can map the window at 1x1, so pin an
            # explicit geometry only when the map came up degenerate — otherwise
            # keep whatever geometry was restored / defaulted.
            self.window.show()
            if self.window.width() < 100 or self.window.height() < 100:
                self.window.setGeometry(120, 120, 460, 640)
            self.window.raise_()
            self.window.activateWindow()

        self.timer = QTimer()
        self.timer.setInterval(interval_ms)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.tick()  # paint the first frame immediately

    # ── loop ────────────────────────────────────────────────────────────────
    def tick(self):
        result = self.reader.read()

        # Status / connectivity transitions (INFO); parse/read errors (WARNING);
        # the per-panel populate decisions (DEBUG) that explain a blank panel.
        status = overall_status(result)
        if status != self._last_status:
            age = f"{result.age_s:.1f}s" if result.age_s is not None else "?"
            log.info("status %s -> %s (age=%s)", self._last_status or "start",
                     status, age)
            self._last_status = status
        if result.error != self._last_error:
            if result.error:
                log.warning("read error: %s", result.error)
            else:
                log.info("read recovered (no error)")
            self._last_error = result.error
        if log.isEnabledFor(logging.DEBUG):
            for line in panel_populate_report(result.snapshot):
                log.debug("panel | %s", line)

        self._last_result = result
        want_pulse = (self.tray is not None and status == ACTIVE
                      and self._active_style == ACTIVE_PULSE)
        if want_pulse and not self._pulse_timer.isActive():
            self._pulse_timer.start()
        elif not want_pulse and self._pulse_timer.isActive():
            self._pulse_timer.stop()
            self._pulse_on = False
        if self.tray is not None:
            self.tray.apply(icon_spec(result, style=self._active_style,
                                      pulse_on=self._pulse_on), tooltip(result))
        if self.window.isVisible():
            self.window.update_from(result)

        # update() maintains edge state, so call it every tick regardless of tray.
        for toast in self.toasts.update(result):
            log.info("toast [%s] %s — %s", toast.kind, toast.title, toast.body)
            if self.tray is None:
                continue
            if self._config.mute_notifications:
                log.debug("notification muted (not shown): %s", toast.title)
                continue
            self.tray.showMessage(
                toast.title, toast.body,
                _TOAST_ICON.get(toast.kind, QSystemTrayIcon.Information), 5000)

        self._maybe_notify_new_version(result.snapshot)
        self._check_producer_config(result)

    def _pulse_tick(self):
        """One pulse phase: flip the accent between hot and dim and repaint the
        tray icon from the last-read frame. Runs only while ACTIVE with the pulse
        style selected (tick() starts/stops the timer on those edges)."""
        if self.tray is None or self._last_result is None:
            return
        self._pulse_on = not self._pulse_on
        self.tray.apply(icon_spec(self._last_result, style=self._active_style,
                                  pulse_on=self._pulse_on),
                        tooltip(self._last_result))

    def _check_producer_config(self, result) -> None:
        """Surface (and clear) the stale-producer hint: the app wrote a
        monitor-config the producer isn't applying — likely a service installed
        before the configuration channel. The detection is
        pure (ProducerConfigTracker); this glue feeds it the tick and pushes edge
        changes to the window banner + the log."""
        try:
            engaged = default_monitor_config_path(self._snapshot_path).exists()
        except OSError:
            engaged = False
        hint = self._producer_config_watch.update(
            result, engaged=engaged, watch_ports=self._watch_ports,
            gpu_interval_s=self._producer_cadence[0])
        if hint == self._producer_hint:
            return
        if hint is not None:
            log.warning("producer config not applied: %s", hint.text)
        else:
            log.info("producer config hint cleared — producer matches settings")
        self._producer_hint = hint
        self.window.set_producer_hint(hint)

    def _maybe_notify_new_version(self, snap) -> None:
        """Surface a newer build when available: this frozen exe carries the git
        SHA it was built from; the producer relays the live source SHA. A mismatch
        means `main` moved past this build — reveal a persistent in-app banner +
        tray 'Rebuild now' item (once), and fire a one-time tray toast. No-op when
        either SHA is unknown (running from source, or an older producer)."""
        if snap is not None:
            self._source_version = snap.source_version   # keep About current
        if self._notified_new_version or snap is None or not live_actions_enabled():
            return
        build, source = self._build_version, snap.source_version
        if not build or not source or build == source:
            return
        self._notified_new_version = True
        log.info("new version available: build=%s source=%s — run `vrm rebuild`",
                 build, source)
        # Persistent in-app surfaces (stay until rebuilt), independent of mute.
        self.window.show_update_banner(build, source)
        if self.tray is not None:
            self.tray.set_update_available(True)
        # The transient toast is a notification -> respect mute (still logged above).
        if self.tray is None or self._config.mute_notifications:
            return
        self.tray.showMessage(
            "New version available",
            f"A newer build is ready (source {source}, this build {build}). "
            "Run `vrm rebuild` to update.",
            QSystemTrayIcon.Information, 8000)

    # ── actions ──────────────────────────────────────────────────────────────
    def open_about(self):
        from .about_dialog import AboutDialog
        AboutDialog(parent=self.window, build=self._build_version,
                    source=self._source_version).exec()

    def rebuild_now(self):
        """Trigger a native rebuild from inside the app: shell `vrm rebuild
        --relaunch` inside WSL, DETACHED so it survives this exe being stopped
        mid-rebuild (the rebuild stops → reinstalls → relaunches the app). Falls
        back to showing the copyable command if the launch can't start."""
        if not live_actions_enabled():
            log.warning("rebuild refused: %s", LIVE_ACTIONS_OFF_REASON)
            return
        from PySide6.QtCore import QProcess
        argv = wsl_rebuild_argv()
        log.info("rebuild requested: %s", " ".join(argv))
        # The rebuild force-terminates this exe (`Stop-Process -Force`, to
        # release the .exe lock), so our closeEvent/quit never fire — persist the
        # window position NOW so the relaunched build restores it instead of
        # opening centered. Only when visible, so a rebuild triggered from the tray
        # with the window hidden doesn't overwrite a good saved geometry.
        if self.window.isVisible():
            self.window.save_geometry()
        try:
            # PySide6's static startDetached returns (ok, pid); be tolerant of a
            # bare-bool return too. A missing wsl.exe returns ok=False (no raise).
            result = QProcess.startDetached(argv[0], argv[1:])
            ok = result[0] if isinstance(result, tuple) else bool(result)
        except Exception:  # noqa: BLE001 — any launch error → fall back to the command
            ok = False
        if not ok:
            log.warning("rebuild launch failed (wsl.exe unavailable?) — showing command")
            self._show_rebuild_command()
            return
        log.info("rebuild launched (detached); the app will close + reopen shortly")
        if self.tray is not None:
            self.tray.showMessage(
                "Rebuilding…",
                "Building the new version in WSL (~1 minute). The window will "
                "close and reopen automatically when it's ready.",
                QSystemTrayIcon.Information, 10000)

    def _show_rebuild_command(self):
        """Fallback for rebuild_now(): show the exact command to run by hand."""
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Update — Vision Resource Monitor")
        box.setText("Couldn't start the rebuild automatically.\n\n"
                    "Run this in a WSL terminal to update:")
        box.setInformativeText(rebuild_command_hint())
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.exec()

    def ask_claude(self, contributor):
        """Hand one ranked contributor to an interactive Claude session in a
        WSL terminal (the Forecast panel's "ask claude" button). The launch
        ladder mirrors rebuild_now: wt.exe when present, bare wsl.exe (its own
        detached console) otherwise, the copyable command as the last resort.
        Warn-and-suggest stays locked — the click writes a temp launch script
        and (on first use) seeds a read-only-permissions worker folder in WSL,
        but the app never runs remediation: the session is pre-approved only
        for read-only diagnostics and denied every destructive command."""
        if not live_actions_enabled():
            log.warning("ask-claude refused: %s", LIVE_ACTIONS_OFF_REASON)
            return
        from PySide6.QtCore import QProcess
        path = prepare_script(contributor, feed_path=self._snapshot_path)
        if path is None:
            # Fail-open kept the tray up, but "nothing happened" would leave the
            # user guessing — state the cause (the repo's never-a-blank ethos).
            log.warning("ask-claude: could not prepare the hand-off script")
            box = QMessageBox(self.window)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Ask Claude — Vision Resource Monitor")
            box.setText("Couldn't prepare the hand-off session.")
            box.setInformativeText(
                "Writing the temporary launch script failed (temp folder full "
                "or unwritable?). See the log for details.")
            box.exec()
            return
        try:
            argvs = launch_argvs(path)
        except ValueError:
            # A temp path that can't cross the boundary cleanly (untranslatable,
            # or an apostrophe in %TEMP% that would break the wsl.exe re-parse).
            # The direct-paste command IS correct for a single shell.
            log.warning("ask-claude: path not launch-safe — showing command")
            self._show_askclaude_command(path)
            return
        for argv in argvs:
            try:
                # (ok, pid) from PySide6's static startDetached; tolerate a
                # bare bool. A missing wt.exe/wsl.exe returns ok=False.
                result = QProcess.startDetached(argv[0], argv[1:])
                ok = result[0] if isinstance(result, tuple) else bool(result)
            except Exception:  # noqa: BLE001 — any launch error → next rung
                ok = False
            if ok:
                log.info("ask-claude launched: %s", " ".join(argv))
                return
        log.warning("ask-claude launch failed (wt.exe/wsl.exe unavailable?) — "
                    "showing command")
        self._show_askclaude_command(path)

    def _show_askclaude_command(self, script_path):
        """Fallback for ask_claude(): show the exact command to run by hand."""
        box = QMessageBox(self.window)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Ask Claude — Vision Resource Monitor")
        box.setText("Couldn't open the terminal automatically.\n\n"
                    "Run this in a WSL terminal to start the session:")
        box.setInformativeText(command_hint(script_path))
        box.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.exec()

    def open_in_procexp(self, pid, name):
        """Open a windows-side process row in Process Explorer (#61): one
        PowerShell call resolves procexp from the WINDOWS PATH, picks the
        target pid (a pid row is existence-checked; an aggregated name row
        resolves to its largest-working-set instance) and launches
        ``procexp /s:<pid>``. The QProcess is ASYNC — a ~1–2 s PS spawn on
        the UI thread would freeze every panel repaint — and its VRMPX:
        marker is classified into a stated-cause dialog; silence past the
        timeout is stated too. Event-driven only (the footprint rule)."""
        if not live_actions_enabled():
            log.warning("procexp open refused: %s", LIVE_ACTIONS_OFF_REASON)
            return
        from PySide6.QtCore import QProcess
        who = procexplorer.subject(pid=pid, name=name)
        argv = procexplorer.launch_argv(pid=pid, name=name)
        log.info("procexp open requested for %s", who)
        proc = QProcess()
        # PS noise (profile warnings, errors) rides stderr; merge so the
        # classifier and the log both see one stream.
        proc.setProcessChannelMode(QProcess.MergedChannels)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.setInterval(procexplorer.LAUNCH_TIMEOUT_MS)
        entry = (proc, timer)
        self._px_running.add(entry)

        def finish(kind_override=None):
            # First signal wins: finished + errorOccurred can both fire for
            # one run (a crash), and the timeout kill re-fires them.
            if entry not in self._px_running:
                return
            self._px_running.discard(entry)
            timer.stop()
            out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
            if kind_override is not None:
                kind, detail = kind_override, ""
            else:
                kind, detail = procexplorer.classify_output(out)
            proc.deleteLater()
            timer.deleteLater()
            if kind == "ok":
                # detail = "<pid> closed=N survived=M" (#63), not just the pid.
                log.info("procexp opened (%s) for %s", detail or "?", who)
                return
            log.warning("procexp open for %s answered %r; output=%r",
                        who, kind, out[-500:])
            headline, info = procexplorer.explain(
                kind, pid=pid, name=name, detail=detail)
            box = QMessageBox(self.window)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Process Explorer — Vision Resource Monitor")
            box.setText(headline)
            box.setInformativeText(info)
            box.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box.exec()

        def on_timeout():
            proc.kill()
            finish("timeout")

        proc.finished.connect(lambda *_a: finish())
        proc.errorOccurred.connect(lambda *_a: finish())
        timer.timeout.connect(on_timeout)
        timer.start()
        proc.start(argv[0], argv[1:])

    def open_in_procmon(self, pid, name):
        """Open a windows-side process row in Process Monitor, FILTERED to it
        (#87): a procmon-parser PMC (built here, in-process) rides one
        PowerShell call that checks the target, elevates ONCE (UAC — ProcMon
        needs admin for its capture driver), /Terminate-replaces any running
        instance (ProcMon single-instances the capture) and launches
        ``/LoadConfig``. Same async QProcess + marker + stated-cause plumbing
        as :meth:`open_in_procexp`; a missing procmon-parser is stated
        BEFORE any spawn. Event-driven only (the footprint rule)."""
        if not live_actions_enabled():
            log.warning("procmon open refused: %s", LIVE_ACTIONS_OFF_REASON)
            return
        from PySide6.QtCore import QProcess
        who = procmonitor.subject(pid=pid, name=name)
        log.info("procmon open requested for %s", who)
        try:
            argv = procmonitor.launch_argv(pid=pid, name=name)
        except procmonitor.PmcUnavailableError:
            log.warning("procmon open for %s: procmon-parser missing", who)
            headline, info = procmonitor.explain("nolib", pid=pid, name=name)
            box = QMessageBox(self.window)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Process Monitor — Vision Resource Monitor")
            box.setText(headline)
            box.setInformativeText(info)
            box.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box.exec()
            return
        proc = QProcess()
        proc.setProcessChannelMode(QProcess.MergedChannels)
        timer = QTimer()
        timer.setSingleShot(True)
        timer.setInterval(procmonitor.LAUNCH_TIMEOUT_MS)
        entry = (proc, timer)
        self._px_running.add(entry)

        def finish(kind_override=None):
            if entry not in self._px_running:
                return
            self._px_running.discard(entry)
            timer.stop()
            out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
            if kind_override is not None:
                kind, detail = kind_override, ""
            else:
                kind, detail = procmonitor.classify_output(out)
            proc.deleteLater()
            timer.deleteLater()
            if kind == "ok":
                # detail = "<subject> closed=N" from the elevated stage.
                log.info("procmon opened (%s) for %s", detail or "?", who)
                return
            log.warning("procmon open for %s answered %r; output=%r",
                        who, kind, out[-500:])
            headline, info = procmonitor.explain(
                kind, pid=pid, name=name, detail=detail)
            box = QMessageBox(self.window)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Process Monitor — Vision Resource Monitor")
            box.setText(headline)
            box.setInformativeText(info)
            box.setTextInteractionFlags(Qt.TextSelectableByMouse)
            box.exec()

        def on_timeout():
            proc.kill()
            finish("timeout")

        proc.finished.connect(lambda *_a: finish())
        proc.errorOccurred.connect(lambda *_a: finish())
        timer.timeout.connect(on_timeout)
        timer.start()
        proc.start(argv[0], argv[1:])

    def show_window(self):
        self.window.update_from(self.reader.read())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def open_log(self):
        if not self.log_path:
            QMessageBox.information(self.window, "Activity log",
                                   "No activity-log path is configured.")
            return
        path = Path(self.log_path).expanduser()
        if not path.exists():
            QMessageBox.information(self.window, "Activity log",
                                   f"Activity log not found:\n{path}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_settings(self):
        from .settings_dialog import SettingsDialog
        # Re-entrancy guard: exec() blocks THIS call, but a tray-menu
        # activation can re-enter while the dialog is up — surface the open
        # dialog instead of stacking a second one over shared state.
        if self._settings_dlg is not None:
            self._settings_dlg.raise_()
            self._settings_dlg.activateWindow()
            return
        gpu, host = self._producer_cadence
        # Pre-fill from STORED truth, never the flag-merged effective config —
        # OK persists every field, so an effective-config pre-fill would
        # silently promote a session CLI flag into the store (earlier release).
        # The flagged set cues each shadowed knob in the dialog instead.
        dlg = SettingsDialog(stored_truth_config(self._settings_path),
                             parent=self.window,
                             start_minimized=self._start_minimized,
                             gpu_interval=gpu, host_refresh=host,
                             watch_ports=self._watch_ports,
                             watch_procs=[f"{t['side']}:{t['name']}"
                                          for t in self._watch_procs
                                          if t.get("pid") is None],
                             panel_order=self._panel_order,
                             panel_hidden=self._panel_hidden,
                             active_style=self._active_style,
                             port_window_s=self._port_window_s,
                             session_ports=self._session_ports,
                             hosted_bursts=self._hosted_bursts,
                             busy_marker_pct=self._busy_marker_pct,
                             flagged=frozenset(self._cli))
        # Every dialog read happens INSIDE the try, before the finally queues
        # deleteLater — Qt's loop-level rule would protect later reads anyway
        # (a DeferredDelete posted here can't fire in a deeper nested loop),
        # but this ordering makes the safety local instead of derived
        # (review fold).
        self._settings_dlg = dlg
        try:
            accepted = dlg.exec()
            if accepted:
                self._settings_accepted(dlg)
        finally:
            self._settings_dlg = None
            dlg.deleteLater()

    def _settings_accepted(self, dlg) -> None:
        """Apply an accepted Settings dialog (split from open_settings so the
        dialog's lifetime bracket stays a five-line try/finally)."""
        # Style first: apply_settings ends with a tick(), which repaints the
        # tray + window with whatever style is current by then.
        self._apply_active_style(dlg.active_style())
        self._apply_busy_marker(dlg.busy_marker_pct())
        self.apply_settings(dlg.values())
        self._apply_start_minimized(dlg.start_minimized())
        # The Settings field owns the NAME watches; session pid targets
        # ride along untouched.
        merged, dropped = self._merge_watch_proc_names(dlg.watched_procs())
        self._watch_procs = merged
        if dropped:
            # A dropped token must be SAID, not just logged: the field
            # won't show it on the next open, and past the producer's
            # cap a target would degrade to a wrong "producer doesn't
            # answer" diagnosis (review H1/L2).
            QMessageBox.warning(
                self.window, "Watched processes",
                "not applied:\n" + "\n".join(dropped))
        # Toggle before the config write below carries it to the producer.
        self._apply_session_ports(dlg.session_ports())
        self._apply_hosted_bursts(dlg.hosted_burst_repos())
        self._apply_producer_config(*dlg.producer_cadence(), dlg.watched_ports())
        self._apply_panels(dlg.panel_order(), dlg.panels_hidden())
        self._apply_port_window(dlg.port_window_s())

    def _apply_hosted_bursts(self, repos) -> None:
        """Persist the hosted-burst repo watch; the subsequent
        _apply_producer_config write carries it to the producer (which
        treats an explicit [] as a real OFF)."""
        repos = clean_hosted_repos(repos)
        if repos == self._hosted_bursts:
            return
        self._hosted_bursts = repos
        try:
            save_hosted_bursts(repos, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must not crash the app
            log.exception("failed to persist hosted-burst repos")

    def _apply_session_ports(self, on) -> None:
        """Persist the session-ports census toggle; it rides the next
        monitor-config write (the producer re-reads within seconds)."""
        on = bool(on)
        if on == self._session_ports:
            return
        self._session_ports = on
        try:
            save_session_ports(on, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must not crash the app
            log.exception("failed to persist session-ports toggle")
        log.info("session-ports census %s", "on" if on else "off")

    def _apply_active_style(self, style) -> None:
        """Persist + live-apply the Active-indicator style (view-state): the
        window adopts it for its next repaint; the tray follows on the next
        tick, where the pulse timer also starts/stops on the ACTIVE edge."""
        self._active_style = style
        try:
            save_active_style(style, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must not crash the app
            log.exception("failed to persist active style")
        self.window.set_active_style(style)
        log.info("active indicator style set to %s", style)

    def _apply_busy_marker(self, pct) -> None:
        """Persist + live-apply the win cpu busy marker (view-state)."""
        if pct == self._busy_marker_pct:
            return
        self._busy_marker_pct = pct
        try:
            save_busy_marker_pct(pct, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must not crash the app
            log.exception("failed to persist busy marker")
        self.window.set_busy_marker_pct(pct)
        log.info("win cpu busy marker set to %.0f%%", pct)

    def _apply_panels(self, order, hidden) -> None:
        """Persist the panel order + hidden set (view-state) AND apply both to the
        window live, so reorder + show/hide are visible immediately (no restart).
        Best-effort: a write failure is logged, never fatal."""
        self._panel_order = list(order)
        self._panel_hidden = set(hidden)
        try:
            save_panel_order(order, path=self._settings_path)
            save_panel_hidden(hidden, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must not crash the app
            log.exception("failed to persist panel layout")
        self.window.set_panel_order(self._panel_order)
        self.window.set_panel_hidden(self._panel_hidden)
        log.info("panels applied live: order=%s hidden=%s",
                 self._panel_order, sorted(self._panel_hidden))

    def _apply_producer_config(self, gpu_interval_s, host_refresh_s,
                               watch_ports) -> None:
        """Persist the producer config (sample cadence + watched ports) and write the
        monitor-config:v1 file the producer reads (its --config), next to the
        (possibly just-changed) snapshot. View-state (no CLI precedence); the one
        place the app writes rather than renders. The producer picks the change up
        within a few seconds — nothing to live-apply in the app. The FULL desired
        state is written each time (so clearing the ports field means "watch none").
        Best-effort: a write failure is logged, never fatal."""
        self._producer_cadence = (gpu_interval_s, host_refresh_s)
        self._watch_ports = list(watch_ports or [])
        # The desired state changed: restart the stale-producer evidence clock so
        # the producer gets its re-read window before we call a contradiction.
        self._producer_config_watch.reset()
        try:
            save_producer_cadence(gpu_interval_s, host_refresh_s,
                                  path=self._settings_path)
            save_watch_ports(self._watch_ports, path=self._settings_path)
            # Every apply path persists the NAME watches (the helper filters
            # out session pid targets itself) — a full-state writer that
            # skipped a watch field would actively clear it on the next
            # launch.
            save_watch_proc_names(self._watch_procs, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must not crash the app
            log.exception("failed to persist producer config")
        cfg_path = default_monitor_config_path(self._snapshot_path)
        if self._last_cfg_path is not None and self._last_cfg_path != cfg_path:
            self._clear_watch_procs_at(self._last_cfg_path)
        self._last_cfg_path = cfg_path
        try:
            write_monitor_config(cfg_path, gpu_interval_s, host_refresh_s,
                                 watch_ports=self._watch_ports,
                                 watch_procs=self._watch_procs,
                                 session_ports=self._session_ports,
                                 hosted_bursts=self._hosted_bursts)
            log.info("producer config set: gpu=%.1fs host=%.0fs ports=%s "
                     "procs=%s session_ports=%s hosted=%s -> %s (producer "
                     "re-reads within a few seconds)",
                     gpu_interval_s, host_refresh_s, self._watch_ports,
                     self._watch_procs, self._session_ports,
                     self._hosted_bursts, cfg_path)
        except Exception:  # noqa: BLE001 — the config write is best-effort
            log.exception("failed to write producer config %s", cfg_path)
        # Keep the window's desired-set views in step (it renders "waiting for
        # the producer" rows for desired ports/targets the reports lack yet).
        self.window.set_desired_watch_ports(self._watch_ports)
        self.window.set_proc_targets(self._watch_procs)

    def _on_watch_ports_edited(self, specs) -> None:
        """The window's ad-hoc watch/unwatch affordances hand the FULL desired
        spec list here; reusing the Settings apply path (persist + the
        monitor-config write) makes an ad-hoc watch exactly a Settings watch —
        one state, two entry points."""
        gpu_interval_s, host_refresh_s = self._producer_cadence
        self._apply_producer_config(gpu_interval_s, host_refresh_s, specs)

    def _on_watch_procs_edited(self, targets) -> None:
        """The window's watch/stop affordances hand the FULL process→ports
        target list here (session pid targets + persisted name watches, one
        list); the config write rides the same apply path as everything else
        on the channel, which also persists the name entries."""
        self._watch_procs = clean_proc_targets(targets)
        gpu_interval_s, host_refresh_s = self._producer_cadence
        self._apply_producer_config(gpu_interval_s, host_refresh_s,
                                    self._watch_ports)

    def _merge_watch_proc_names(self, tokens) -> tuple:
        """Fold the Settings field's NAME tokens into the target list —
        (merged, dropped_descriptions). Malformed tokens and everything past
        the producer's combined PROC_TARGET_CAP are DROPPED AND NAMED (the
        field won't show a dropped token on its next open, and an over-cap
        target would never answer — its grace-ladder row would then blame a
        healthy producer; review H1/L2). Names lead the merge (the persisted
        intent outranks session pins) so an over-cap drop hits pid targets
        first — deliberate, and stated in the returned description."""
        names, dropped = [], []
        for token in tokens:
            try:
                names.append(parse_watch_proc_name(token))
            except ValueError as exc:
                log.warning("dropping watched-process entry: %s", exc)
                dropped.append(f"{token} — {exc}")
        merged = clean_proc_targets(
            names + [t for t in self._watch_procs
                     if t.get("pid") is not None])
        if len(merged) > PROC_TARGET_CAP:
            for t in merged[PROC_TARGET_CAP:]:
                what = (f"{t['side']}:{t['name']}" if t.get("pid") is None
                        else f"pid {t['pid']} ({t['side']}, session)")
                dropped.append(f"{what} — over the producer's "
                               f"{PROC_TARGET_CAP}-target cap")
                log.warning("dropping over-cap watch target: %s", what)
            merged = merged[:PROC_TARGET_CAP]
        return merged, dropped

    def _reconcile_stray_proc_targets(self) -> None:
        """Reconcile the config FILE's watch_procs against this app's desired
        set once at startup. Pid targets are session-only but the file
        outlives the session — a crashed/killed app leaves them behind (the
        clean exits clear them — _clear_proc_targets_on_exit), silently
        keeping the producer's sweeps enriched forever (footprint rule);
        persisted NAME targets are the opposite — a fresh app must RE-ASSERT
        them or the producer keeps running without the watches the user
        chose (the earlier release lesson: a startup writer that omits a watch
        field actively clears it). One comparison covers both: rewrite only
        when the file's normalized set differs from the desired one, so an
        untouched setup stays untouched and a clean restart is a no-op.
        A missing/unreadable file is rewritten only when names are desired
        (never create the file just to write emptiness). Multi-instance is
        out of scope: two live apps sharing one config file already clobber
        each other's watch_ports by design (full-state writes); this
        reconcile inherits that limitation."""
        import json
        cfg_path = default_monitor_config_path(self._snapshot_path)
        desired = clean_proc_targets(self._watch_procs)
        try:
            stray = json.loads(cfg_path.read_text(encoding="utf-8")).get(
                "watch_procs")
        except (OSError, ValueError, AttributeError):
            stray = None
        file_targets = clean_proc_targets(stray) if isinstance(stray, list) else []
        if file_targets != desired or (stray and not file_targets):
            log.info("reconciling watch_procs in %s: file=%d desired=%d",
                     cfg_path, len(file_targets), len(desired))
            gpu_interval_s, host_refresh_s = self._producer_cadence
            self._apply_producer_config(gpu_interval_s, host_refresh_s,
                                        self._watch_ports)

    def _clear_watch_procs_at(self, cfg_path) -> None:
        """Best-effort: blank ALL watch_procs (session pids AND persisted
        name watches) in an OLD config file when the snapshot path moves —
        after the move no affordance points at that file, so anything left
        would keep that producer sweeping targets nobody can ever clear
        (review L3; the footprint rule outranks name-watch persistence,
        which follows the APP to the new path via the same apply). The
        file's own cadence/ports are preserved: they belong to that
        producer's app state and are persistent by design. Caveat, accepted:
        an out-of-band path switch (relaunch with a different --snapshot)
        bypasses this — the same pre-existing class as stray crash pids."""
        import json
        try:
            old = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, AttributeError):
            return
        if not isinstance(old, dict) or not old.get("watch_procs"):
            return
        try:
            # A full-state writer must carry EVERY field forward — the old file's session_ports + hosted_bursts
            # choices included.
            old_sp = old.get("session_ports")
            write_monitor_config(cfg_path, old.get("gpu_interval_s"),
                                 old.get("host_refresh_s"),
                                 watch_ports=old.get("watch_ports"),
                                 watch_procs=[],
                                 session_ports=(old_sp if isinstance(old_sp, bool)
                                                else True),
                                 hosted_bursts=old.get("hosted_bursts"))
            log.info("cleared watch_procs left in old config %s", cfg_path)
        except Exception:  # noqa: BLE001 — best-effort, never fatal
            log.exception("failed to clear old config %s", cfg_path)

    def _clear_proc_targets_on_exit(self) -> None:
        """Session-only means SESSION-only: drop the pid targets from the
        written state on the way out, so a clean exit never leaves the
        producer's sweeps enriched for a session that no longer exists (the
        startup reconcile then only covers the crash/kill path). Persisted
        NAME targets deliberately SURVIVE — the user chose a standing watch
        and the producer keeps serving it while the app is away (removal
        lives in Settings ▸ Watched processes). Best-effort and idempotent —
        wired to BOTH quit() and qapp.aboutToQuit (the no-tray window-close
        exit never reaches quit()); a failed write must not block shutdown."""
        if not any(t.get("pid") is not None for t in self._watch_procs):
            return
        self._watch_procs = [t for t in self._watch_procs
                             if t.get("pid") is None]
        try:
            gpu_interval_s, host_refresh_s = self._producer_cadence
            write_monitor_config(
                default_monitor_config_path(self._snapshot_path),
                gpu_interval_s, host_refresh_s,
                watch_ports=self._watch_ports,
                watch_procs=self._watch_procs,
                session_ports=self._session_ports,
                hosted_bursts=self._hosted_bursts)
            log.info("cleared session pid targets on exit (%d name watch(es) "
                     "kept)", len(self._watch_procs))
        except Exception:  # noqa: BLE001 — shutdown must never block on this
            log.exception("failed to clear proc targets on exit")

    def _apply_port_window(self, window_s) -> None:
        """Persist + live-apply the Ports-graph window (view-state): the window's
        rolling port history re-prunes to the new span and re-renders — shrinking
        shows immediately, growing refills as new frames arrive."""
        self._port_window_s = float(window_s)
        try:
            save_port_window_s(window_s, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must not crash the app
            log.exception("failed to persist ports-graph window")
        self.window.set_port_window_s(window_s)
        log.info("ports graph window set to %.0fs", self._port_window_s)

    def _apply_start_minimized(self, value: bool) -> None:
        """Persist the start-minimized preference. It is view-state (no CLI
        precedence, kept out of save_stored/resolve_config) and only governs the
        next launch, so there is nothing to live-apply — just remember it."""
        self._start_minimized = bool(value)
        try:
            save_start_minimized(self._start_minimized, path=self._settings_path)
        except Exception:  # noqa: BLE001 — view-state persistence must not crash the app
            log.exception("failed to persist start-minimized")
        log.info("start-minimized set to %s (effective next launch)",
                 self._start_minimized)

    def apply_settings(self, new: dict) -> None:
        """Persist the edited settings and live-apply the safe subset (log level,
        theme, interval, stale-after, snapshot path, mute). Panel visibility
        persists but takes effect on restart.

        Live-apply overlays the session's CLI flags (flag > pref, the earlier release semantics): the persisted edit takes effect on the next unflagged
        launch, while an explicit launch flag keeps governing THIS session —
        the dialog cues exactly those shadowed knobs."""
        try:
            save_stored(new, path=self._settings_path)
        except Exception:  # noqa: BLE001 — persistence failure must not crash the app
            log.exception("failed to persist settings")

        prev = self._config
        cfg = resolve_config(self._cli, new)
        self._config = cfg

        # Log level.
        setup_logging(resolve_log_level(cfg.log_level), log_file=self._diag_log_file)
        # Poll interval + stale-after.
        self.timer.setInterval(int(cfg.interval * 1000))
        self.reader.stale_after_s = cfg.stale_after
        # Theme (re-applied to the running QApplication).
        apply_theme(self.qapp, dark=cfg.dark)
        # Snapshot path (rebuild the reader only if it changed).
        new_path = (Path(cfg.snapshot_path).expanduser()
                    if cfg.snapshot_path else default_snapshot_path())
        if new_path != self._snapshot_path:
            self._snapshot_path = new_path
            self.reader = SnapshotReader(new_path, stale_after_s=cfg.stale_after)
            self._last_status = self._last_error = None
            # A different snapshot means a different producer — old frames are not
            # evidence against it.
            self._producer_config_watch.reset()
        # Activity-log path follows cfg.log_file, else the producer's file next
        # to the (possibly new) snapshot.
        self.log_path = (Path(cfg.log_file).expanduser() if cfg.log_file
                         else default_activity_log_path(self._snapshot_path))
        # (Panel visibility is no longer a Config setting — it's the unified
        # panel-hidden set, applied live via _apply_panels / set_panel_hidden.)

        log.info("settings applied: level=%s theme=%s interval=%.1fs "
                 "stale_after=%.1fs mute=%s snapshot=%s logfile=%s", cfg.log_level,
                 cfg.theme, cfg.interval, cfg.stale_after, cfg.mute_notifications,
                 new_path, self.log_path)
        self.tick()  # repaint immediately with the new cadence/theme

    def quit(self):
        log.info("quit requested")
        # Remember the window placement before teardown. Only when it is visible:
        # a never-shown (start-minimized) window would otherwise overwrite a good
        # saved geometry with its unshown default. closeEvent covers the case
        # where the user closed the window to tray earlier in the session.
        if self.window.isVisible():
            self.window.save_geometry()
        self._clear_proc_targets_on_exit()
        self.timer.stop()
        self._pulse_timer.stop()
        if self.tray is not None:
            self.tray.hide()
        self.qapp.quit()
