"""The 'About' dialog: which version this build is, and whether an update is
available (the frozen build SHA vs the producer's live source SHA).

Thin over the Qt-free ``update_status_line`` so it stays screenshot-testable.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from .. import __version__, update_status_line

_DIM = "#8b949e"


class AboutDialog(QDialog):
    def __init__(self, parent=None, *, build=None, source=None):
        super().__init__(parent)
        self.setWindowTitle("About — Vision Resource Monitor")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        title = QLabel("Vision Resource Monitor")
        title.setStyleSheet("font-weight:bold; font-size:15px;")
        layout.addWidget(title)
        layout.addWidget(QLabel(f"Version {__version__}"))

        build_lbl = QLabel(f"Build: {build or 'source (unstamped)'}")
        build_lbl.setStyleSheet(f"color:{_DIM};")
        layout.addWidget(build_lbl)

        source_lbl = QLabel(f"Live source: {source or 'unknown'}")
        source_lbl.setStyleSheet(f"color:{_DIM};")
        layout.addWidget(source_lbl)

        status = QLabel(update_status_line(build, source))
        status.setWordWrap(True)
        status.setStyleSheet("margin-top:6px;")
        layout.addWidget(status)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        # Feed-derived build/source must render LITERALLY at FIRST paint: the
        # dialog is created on demand, so the window's update_from PlainText
        # pass (#73) cannot reach these labels before they show — and default
        # AutoText flips to rich text on a markup-shaped source_version.
        for lbl in self.findChildren(QLabel):
            lbl.setTextFormat(Qt.PlainText)
