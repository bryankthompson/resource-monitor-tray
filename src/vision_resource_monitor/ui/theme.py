"""Application theme. Defaults to a dark palette (the tool is a GPU/system
monitor — dark is the sensible default and matches the tray icon colours);
``--light`` opts out. Uses the Fusion style so the palette applies consistently
across platforms (the native WSLg/Windows styles otherwise force light).
"""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette


# GitHub-dark-ish palette; the accent colours used across the UI (green/yellow/
# red/#8b949e grey) were chosen to read against this.
_DARK = {
    "window": "#0d1117",
    "base": "#161b22",
    "alt": "#21262d",
    "text": "#c9d1d9",
    "dim": "#8b949e",
    "button": "#21262d",
    "highlight": "#1f6feb",
    # The card/frame border (the sections' `palette(mid)` in window.py) —
    # GitHub-dark's border grey. Without an explicit Mid the QPalette()
    # default (light Fusion) leaks through and the border only matches by
    # coincidence.
    "mid": "#30363d",
}


def apply_theme(app, dark: bool = True) -> None:
    app.setStyle("Fusion")
    if not dark:
        app.setPalette(app.style().standardPalette())
        return

    c = {k: QColor(v) for k, v in _DARK.items()}
    p = QPalette()
    p.setColor(QPalette.Window, c["window"])
    p.setColor(QPalette.WindowText, c["text"])
    p.setColor(QPalette.Base, c["base"])
    p.setColor(QPalette.AlternateBase, c["alt"])
    p.setColor(QPalette.Mid, c["mid"])
    p.setColor(QPalette.Text, c["text"])
    p.setColor(QPalette.Button, c["button"])
    p.setColor(QPalette.ButtonText, c["text"])
    p.setColor(QPalette.ToolTipBase, c["base"])
    p.setColor(QPalette.ToolTipText, c["text"])
    p.setColor(QPalette.PlaceholderText, c["dim"])
    p.setColor(QPalette.Highlight, c["highlight"])
    p.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    p.setColor(QPalette.Link, QColor("#58a6ff"))
    p.setColor(QPalette.Disabled, QPalette.Text, c["dim"])
    p.setColor(QPalette.Disabled, QPalette.WindowText, c["dim"])
    p.setColor(QPalette.Disabled, QPalette.ButtonText, c["dim"])
    app.setPalette(p)
