"""Small custom-painted widgets: a fill gauge and a sparkline.

Kept as hand-painted QWidgets so the MVP depends only on core PySide6 (no
QtCharts add-on). Colours come from ``state`` so the whole app shares one code.
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QWidget

from ..state import GREY, busy_color, util_color


class GaugeBar(QWidget):
    """A horizontal fill bar, 0..1, coloured by an explicit percent.

    An optional ``shade_fill`` draws a second, translucent segment right after
    the solid fill — used for reclaimable RAM cache and reclaimable disk, so a
    cache-heavy moment does not read as pressure.

    An optional ``marker`` (a percent, None = none) draws a thin vertical line
    at that point of the track and turns the fill red from it — the win cpu
    row's busy threshold.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fill = 0.0
        self._shade = 0.0
        self._pct_for_color = 0.0
        self._marker: float | None = None
        self.setMinimumHeight(14)

    def set_marker(self, pct: float | None):
        self._marker = pct
        self.update()

    def set_value(self, fill: float, pct_for_color: float | None = None,
                  shade_fill: float = 0.0):
        self._fill = min(max(fill, 0.0), 1.0)
        self._shade = min(max(shade_fill, 0.0), 1.0 - self._fill)
        self._pct_for_color = self._fill * 100 if pct_for_color is None else pct_for_color
        self.update()

    def sizeHint(self):
        return QSize(180, 14)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = self.rect().adjusted(0, 2, -1, -2)
        radius = r.height() / 2

        track = QColor(GREY)
        track.setAlpha(60)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, radius, radius)

        color = QColor(busy_color(self._pct_for_color, self._marker))
        if self._shade > 0:
            # Solid + shade drawn as one rounded rect underneath, then the solid
            # part painted over it — keeps the rounded ends clean.
            sr = r.adjusted(0, 0, -int(r.width() * (1 - self._fill - self._shade)), 0)
            shade = QColor(color)
            shade.setAlpha(90)
            p.setBrush(shade)
            p.drawRoundedRect(sr, radius, radius)
        if self._fill > 0:
            fr = r.adjusted(0, 0, -int(r.width() * (1 - self._fill)), 0)
            p.setBrush(color)
            p.drawRoundedRect(fr, radius, radius)
        if self._marker is not None:
            # Palette text colour so the line shows on both themes.
            line = self.palette().color(self.foregroundRole())
            p.setPen(QPen(line, 1.5))
            x = r.left() + r.width() * min(max(self._marker, 0.0), 100.0) / 100.0
            p.drawLine(QPointF(x, self.rect().top() + 1),
                       QPointF(x, self.rect().bottom() - 1))
        p.end()


class Sparkline(QWidget):
    """A polyline of 0..100 samples. The line colour follows util_color(last)
    unless a fixed ``color`` is given — a series normalized to its own peak
    always ends near 100, which util_color would misread as red-alert."""

    def __init__(self, parent=None, color=None):
        super().__init__(parent)
        self._values: list[float] = []
        self._color = color
        self.setMinimumHeight(28)

    def set_color(self, color):
        self._color = color

    def set_values(self, values):
        self._values = [min(max(float(v), 0.0), 100.0) for v in (values or [])]
        self.update()

    def sizeHint(self):
        return QSize(180, 28)

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        vals = self._values
        if len(vals) < 2:
            return
        r = self.rect().adjusted(1, 2, -1, -2)
        n = len(vals)
        dx = r.width() / (n - 1)
        pts = QPolygonF()
        for i, v in enumerate(vals):
            x = r.left() + i * dx
            y = r.bottom() - (v / 100.0) * r.height()
            pts.append(QPointF(x, y))
        last = vals[-1]
        p.setPen(QPen(QColor(self._color or util_color(last)), 1.6))
        p.drawPolyline(pts)
        p.end()
