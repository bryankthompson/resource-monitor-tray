"""The system-tray icon: a dynamic GPU gauge painted from an IconSpec.

Kept thin — all decisions (level, colour, fill, text) come pre-computed from
``state.icon_spec`` so this module only paints and wires Qt signals.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from ..state import IconSpec

log = logging.getLogger(__name__)

_ICON_PX = 64


def app_icon_path() -> Path:
    """assets/vrm.ico — the shortcut / taskbar icon. PyInstaller unpacks bundled
    data under ``sys._MEIPASS`` (the bundled app ships it as ``assets/``); from
    the source tree it is the repo's own assets folder."""
    root = getattr(sys, "_MEIPASS", None)
    base = Path(root) if root else Path(__file__).resolve().parents[3]
    return base / "assets" / "vrm.ico"


def load_app_icon() -> QIcon:
    """The static application icon for every window's title bar. A missing or
    unreadable file is logged and yields a null icon (the OS default), never a
    crash."""
    path = app_icon_path()
    icon = QIcon(str(path))
    if icon.isNull():
        log.warning("window icon not loaded (missing or unreadable): %s", path)
    return icon


def render_icon(spec: IconSpec) -> QIcon:
    """Paint the tray icon: a ring that fills with GPU utilisation, coloured by
    threshold; grey and hollow when stale / no data."""
    pm = QPixmap(_ICON_PX, _ICON_PX)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    color = QColor(spec.color)
    ring = 8
    rect = pm.rect().adjusted(ring, ring, -ring, -ring)

    # Track ring.
    track = QColor(spec.color)
    track.setAlpha(70)
    p.setPen(QPen(track, ring, Qt.SolidLine, Qt.RoundCap))
    p.drawArc(rect, 0, 360 * 16)

    # Filled arc (utilisation), starting at 12 o'clock, clockwise. The fill
    # amount + colour is the whole at-a-glance signal — idle reads as a nearly
    # empty green ring, a busy GPU as a full red one.
    if spec.fill > 0:
        p.setPen(QPen(color, ring, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 90 * 16, -int(360 * 16 * spec.fill))

    if spec.text:
        p.setPen(QPen(color))
        f = QFont()
        f.setPixelSize(int(_ICON_PX * 0.34))
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, spec.text)

    p.end()
    return QIcon(pm)


class TrayIcon(QSystemTrayIcon):
    """QSystemTrayIcon plus a small context menu. Signals are plain callables set
    by the app so this stays decoupled from the window/loop."""

    def __init__(self, on_show=None, on_open_log=None, on_settings=None,
                 on_quit=None, on_about=None, on_rebuild=None, parent=None):
        super().__init__(parent)
        self._menu = QMenu()
        self._act_show = QAction("Show monitor", self._menu)
        self._act_log = QAction("Open activity log", self._menu)
        self._act_settings = QAction("Settings…", self._menu)
        # Hidden until an update is detected (set_update_available). Placed up top
        # so it's the obvious action once it appears.
        self._act_rebuild = QAction("Rebuild now — update available", self._menu)
        self._act_rebuild.setVisible(False)
        self._act_about = QAction("About", self._menu)
        self._act_quit = QAction("Quit", self._menu)
        self._menu.addAction(self._act_rebuild)
        self._menu.addAction(self._act_show)
        self._menu.addAction(self._act_log)
        self._menu.addAction(self._act_settings)
        self._menu.addAction(self._act_about)
        self._menu.addSeparator()
        self._menu.addAction(self._act_quit)
        self.setContextMenu(self._menu)

        if on_show:
            self._act_show.triggered.connect(on_show)
            self.activated.connect(
                lambda reason: on_show() if reason == QSystemTrayIcon.Trigger else None
            )
        if on_open_log:
            self._act_log.triggered.connect(on_open_log)
        if on_settings:
            self._act_settings.triggered.connect(on_settings)
        if on_about:
            self._act_about.triggered.connect(on_about)
        if on_rebuild:
            self._act_rebuild.triggered.connect(on_rebuild)
        if on_quit:
            self._act_quit.triggered.connect(on_quit)

        self.setIcon(render_icon(IconSpec(level="nodata", color="#8b949e")))

    def set_update_available(self, available: bool) -> None:
        """Show/hide the 'Rebuild now' tray item (called when a newer build is
        detected)."""
        self._act_rebuild.setVisible(bool(available))

    def apply(self, spec: IconSpec, tooltip: str) -> None:
        self.setIcon(render_icon(spec))
        self.setToolTip(tooltip)
