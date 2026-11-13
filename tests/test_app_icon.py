"""The window/title-bar icon: the shortcut's vrm.ico must load, not fall back to the OS default."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from vision_resource_monitor.ui import trayicon


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_app_icon_loads_from_source_tree(app):
    assert trayicon.app_icon_path().is_file()
    assert not trayicon.load_app_icon().isNull()


def test_missing_icon_is_logged_not_fatal(app, monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(trayicon, "app_icon_path", lambda: tmp_path / "nope.ico")
    with caplog.at_level("WARNING"):
        assert trayicon.load_app_icon().isNull()
    assert "window icon not loaded" in caplog.text
