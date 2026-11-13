import logging

import pytest

from vision_resource_monitor import default_log_path
from vision_resource_monitor import __main__ as entrypoint
from vision_resource_monitor.logging_setup import (
    PACKAGE_LOGGER, resolve_log_level, setup_logging,
)
from vision_resource_monitor.settings import KEY_LOG_LEVEL


@pytest.fixture(autouse=True)
def _restore_package_logger():
    """Snapshot and restore the package logger so these tests never leak handlers
    or a raised level into the rest of the suite."""
    logger = logging.getLogger(PACKAGE_LOGGER)
    saved = (logger.level, logger.propagate, list(logger.handlers))
    yield
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass
    logger.setLevel(saved[0])
    logger.propagate = saved[1]
    for h in saved[2]:
        logger.addHandler(h)


def test_resolve_precedence(monkeypatch):
    monkeypatch.delenv("VRM_LOG_LEVEL", raising=False)
    assert resolve_log_level(None) == logging.INFO          # default
    assert resolve_log_level("DEBUG") == logging.DEBUG
    assert resolve_log_level("debug") == logging.DEBUG      # case-insensitive
    monkeypatch.setenv("VRM_LOG_LEVEL", "WARNING")
    assert resolve_log_level(None) == logging.WARNING       # env used
    assert resolve_log_level("ERROR") == logging.ERROR      # CLI beats env
    monkeypatch.setenv("VRM_LOG_LEVEL", "bogus")
    assert resolve_log_level(None) == logging.INFO          # invalid env ignored


def test_default_log_path_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert default_log_path() == tmp_path / "vision-resource-monitor" / "logs" / "vrm.log"


def test_setup_creates_rotating_file_and_writes(tmp_path):
    log_file = tmp_path / "logs" / "vrm.log"
    active = setup_logging(logging.DEBUG, log_file=log_file, to_console=False)
    assert active == log_file
    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.info("hello-marker")
    for h in logger.handlers:
        h.flush()
    assert log_file.exists()
    assert "hello-marker" in log_file.read_text(encoding="utf-8")


def test_setup_is_idempotent_no_duplicate_handlers(tmp_path):
    log_file = tmp_path / "vrm.log"
    setup_logging(logging.INFO, log_file=log_file, to_console=False)
    n1 = len(logging.getLogger(PACKAGE_LOGGER).handlers)
    setup_logging(logging.INFO, log_file=log_file, to_console=False)
    n2 = len(logging.getLogger(PACKAGE_LOGGER).handlers)
    assert n1 == n2 == 1


def test_setup_falls_back_to_console_when_file_unwritable(tmp_path):
    # Parent is a regular file, so mkdir(parents=True) raises -> no file handler,
    # but a stderr sink is still installed so records aren't dropped.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    active = setup_logging(logging.INFO, log_file=blocker / "logs" / "vrm.log",
                           to_console=False)
    assert active is None
    assert len(logging.getLogger(PACKAGE_LOGGER).handlers) == 1


def test_setup_accepts_level_name(tmp_path):
    setup_logging("DEBUG", log_file=tmp_path / "vrm.log", to_console=False)
    assert logging.getLogger(PACKAGE_LOGGER).level == logging.DEBUG


def test_startup_is_visible_at_error_level(monkeypatch, tmp_path):
    log_file = tmp_path / "vrm.log"
    monkeypatch.setattr(entrypoint, "load_stored", lambda: {KEY_LOG_LEVEL: "ERROR"})
    monkeypatch.setattr(entrypoint, "load_start_minimized", lambda: False)
    monkeypatch.setattr(
        entrypoint, "setup_logging",
        lambda level: setup_logging(level, log_file=log_file, to_console=False),
    )
    monkeypatch.setattr(entrypoint, "default_lock_path", lambda: tmp_path / "instance.lock")
    monkeypatch.setattr(
        entrypoint, "SingleInstance",
        lambda _path: type("Lock", (), {"acquire": lambda self: False})(),
    )

    assert entrypoint.main([]) == 0

    logger = logging.getLogger(PACKAGE_LOGGER)
    assert logger.level == logging.ERROR
    logger.info("ordinary-info-marker")
    logger.error("ordinary-error-marker")
    for handler in logger.handlers:
        handler.flush()

    lines = log_file.read_text(encoding="utf-8").splitlines()
    startup = [line for line in lines if " starting — " in line]
    assert len(startup) == 1
    assert f"vision-resource-monitor {entrypoint.__version__} starting" in startup[0]
    assert " INFO " in startup[0]
    assert "level=ERROR" in startup[0]
    assert f"log_file={log_file}" in startup[0]
    assert "ordinary-info-marker" not in "\n".join(lines)
    assert "ordinary-error-marker" in "\n".join(lines)
