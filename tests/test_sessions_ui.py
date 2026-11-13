"""Offscreen checks for cockpit grouping in the Sessions panel."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from vision_resource_monitor import fake_producer
from vision_resource_monitor.snapshot import Snapshot
from vision_resource_monitor.snapshot import Session
from vision_resource_monitor.ui.window import MonitorWindow


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_cockpit_rows_group_exact_names_and_filter_expands(app, tmp_path):
    window = MonitorWindow(settings_path=str(tmp_path / "settings.json"))
    snap = Snapshot.from_dict(fake_producer._build(now=1010.0, start=1000.0, host="T"))
    window._last_snap = snap
    window._update_sessions(snap)
    labels = [label.text() for label in window._sections["sessions"].findChildren(QLabel)]
    assert any("demo-cockpit (2 lanes)" in text for text in labels)
    assert any("demo-cockpit-two (1 lane)" in text for text in labels)
    assert not any("frontend" in text for text in labels)  # collapsed
    assert all(label.property("vrm_copy_menu")
               for label in window._sections["sessions"].findChildren(QLabel))
    header = next(widget for widget in window._sections["sessions"].findChildren(QWidget)
        if type(widget).__name__ == "_ClickRow" and "demo-cockpit (2 lanes)" in
        " ".join(label.text() for label in widget.findChildren(QLabel)))
    header_label = " ".join(label.text() for label in header.findChildren(QLabel))
    assert "1.2G" in header_label and "52m" in header_label and "1 busy" in header_label
    assert any(label.text() == "stale" for label in header.findChildren(QLabel))
    header.clicked.emit()
    assert not window._sessions_expanded
    assert "demo-cockpit" in window._cockpit_expanded
    child_text = [" ".join(label.text() for label in row.findChildren(QLabel))
                  for row in window._sections["sessions"].findChildren(QWidget)
                  if row.findChildren(QLabel)]
    direct_labels = [row.layout().itemAt(0).widget().text()
                     for row in (window._sess_layout.itemAt(i).widget()
                                 for i in range(window._sess_layout.count()))
                     if row and row.layout() and row.layout().count()
                     and type(row).__name__ == "QWidget"]
    role_order = [next(i for i, text in enumerate(direct_labels) if token in text)
                  for token in ("cockpit · idle", "advisor · idle", "drc · waiting",
                                "router · shell", "frontend · busy")]
    assert role_order == sorted(role_order)
    assert sum("frontend · " in text for text in direct_labels) == 2
    assert any("ports: 5173, 8080, 9000 +1" in text for text in child_text)
    window._toggle_cockpit("demo-cockpit")

    window._sess_filter.setText("frontend")
    labels = [label.text() for label in window._sections["sessions"].findChildren(QLabel)]
    assert any("demo-cockpit" in text for text in labels)
    assert any("frontend · busy" in text for text in labels)
    window._sess_filter.clear()
    assert "demo-cockpit" not in window._cockpit_expanded
    window._sess_filter.setText("7102")
    labels = [label.text() for label in window._sections["sessions"].findChildren(QLabel)]
    assert any("via child 7102" in text for text in labels)
    window._sess_filter.setText("free session")
    labels = [label.text() for label in window._sections["sessions"].findChildren(QLabel)]
    assert any(text == "free session (0 node)" for text in labels)
    assert any("Code Helper (Renderer)" in text for text in labels)
    window.close()


def test_cockpit_group_counts_as_one_top_level_row(app, tmp_path):
    window = MonitorWindow(settings_path=str(tmp_path / "settings.json"))
    snap = Snapshot.from_dict(fake_producer._build(now=1010.0, start=1000.0, host="T"))
    window._update_sessions(snap)
    top_text = [" ".join(label.text() for label in window._sess_layout.itemAt(i).widget().findChildren(QLabel))
                for i in range(window._sess_layout.count())
                if window._sess_layout.itemAt(i).widget()]
    assert sum(("lane)" in text or "lanes)" in text) for text in top_text) == 3
    assert any("+" in text and "more session(s)" in text for text in top_text)
    window.close()


def test_idle_lane_shows_age_swap_highlight_and_tooltip(app, tmp_path):
    window = MonitorWindow(settings_path=str(tmp_path / "settings.json"))
    snap = Snapshot.from_dict(fake_producer._build(now=1010.0, start=1000.0, host="T"))
    window._sessions_expanded = True
    window._cockpit_expanded.add("demo-cockpit")
    window._update_sessions(snap)
    idle_row = next(row for row in window._sections["sessions"].findChildren(QWidget)
                    if row.property("vrm_idle") is True)
    texts = " ".join(label.text() for label in idle_row.findChildren(QLabel))
    assert "idle 52m" in texts and "swap 24M" in texts
    name_tip = idle_row.findChildren(QLabel)[0].toolTip()
    assert "last event age: 3120.0s" in name_tip
    assert "swap: 24.5 MiB" in name_tip
    assert "idle: True" in name_tip
    assert "<qt>" in name_tip and "&lt;qt&gt;" not in name_tip
    assert "background-color" in idle_row.styleSheet()

    # Ungrouped rows still render producer fields, while an explicit false
    # remains unhighlighted and an old snapshot stays visually unchanged.
    ungrouped = Session.from_dict({"pid": 909, "label": "solo", "role": "lane",
                                   "state": "idle", "idle": False,
                                   "last_event_age_s": 3600, "swap_mib": 8})
    row = window._session_row("solo", "  ·  ".join(window._session_metrics(ungrouped)),
                              False, pid=909, session=ungrouped)
    assert row.property("vrm_idle") is False and not row.styleSheet()
    assert "idle 1h 00m" in " ".join(label.text() for label in row.findChildren(QLabel))
    assert "swap 8M" in " ".join(label.text() for label in row.findChildren(QLabel))
    old = Session.from_dict({"pid": 910, "label": "old", "state": "idle"})
    old_row = window._session_row("old", "  ·  ".join(window._session_metrics(old)),
                                  False, pid=910, session=old)
    assert old_row.property("vrm_idle") is False and not old_row.styleSheet()
    assert "idle " not in " ".join(label.text() for label in old_row.findChildren(QLabel))
    window.close()
