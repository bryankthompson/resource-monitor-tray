"""Logging configuration for Vision Resource Monitor.

Qt-free and importable on the bare core interpreter (stdlib ``logging`` only), so
the read→render pipeline can emit breadcrumbs whether it runs under the GUI, the
smoke harness, or a headless test. Configures the ``vision_resource_monitor``
package logger with:

* a **rotating file** handler at
  ``%LOCALAPPDATA%\\vision-resource-monitor\\logs\\vrm.log`` (5 × ~1 MiB), so a
  long-running tray app never grows an unbounded log; and
* a **console** handler on stderr when attached to a terminal, so ``vrm`` from a
  shell shows the same lines live.

Level comes from (highest priority first) the ``--log-level`` CLI flag, the
``VRM_LOG_LEVEL`` env var, else INFO. No secrets/PII are logged — only file
paths, process names, and counts the snapshot already carries. The startup
record is written at every configured level.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import default_log_path

PACKAGE_LOGGER = "vision_resource_monitor"
LOG_LEVEL_ENV = "VRM_LOG_LEVEL"

_DEFAULT_LEVEL = logging.INFO
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 5
_VALID = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def resolve_log_level(cli_level=None) -> int:
    """Resolve the effective level: ``--log-level`` > ``$VRM_LOG_LEVEL`` > INFO.

    Accepts a level name (case-insensitive) or None at each layer; an
    unrecognized name is ignored (falls through to the next source, ultimately
    INFO) rather than crashing startup. Returns a ``logging`` level int.
    """
    for candidate in (cli_level, os.environ.get(LOG_LEVEL_ENV)):
        if not candidate:
            continue
        name = str(candidate).strip().upper()
        if name in _VALID:
            value = logging.getLevelName(name)
            if isinstance(value, int):
                return value
    return _DEFAULT_LEVEL


def setup_logging(level, *, log_file=None, to_console=None) -> Path | None:
    """Configure the package logger; return the active log-file Path (or None).

    ``level`` may be an int or a level name. ``log_file`` overrides the default
    location (mainly for tests). ``to_console`` forces the stderr handler on/off;
    when None it defaults to "on iff stderr is a TTY".

    Idempotent: previously-installed handlers are removed first, so repeated
    calls (tests, the smoke harness, a settings-driven re-init) never stack
    duplicate handlers. If the log file can't be opened (e.g. a read-only base
    dir) the app still starts, logging to console only.
    """
    if isinstance(level, str):
        level = resolve_log_level(level)

    logger = logging.getLogger(PACKAGE_LOGGER)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 — closing a handler must never fail setup
            pass

    fmt = logging.Formatter(_FORMAT)

    active_file: Path | None = None
    target = Path(log_file) if log_file is not None else default_log_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(target, maxBytes=_MAX_BYTES,
                                 backupCount=_BACKUP_COUNT, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        active_file = target
    except OSError:
        active_file = None

    if to_console is None:
        to_console = bool(getattr(sys.stderr, "isatty", lambda: False)())
    if to_console or active_file is None:
        # Always keep at least one sink so records aren't silently dropped.
        ch = logging.StreamHandler(stream=sys.stderr)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    logger.debug("logging configured: level=%s file=%s console=%s",
                 logging.getLevelName(level), active_file, to_console)
    return active_file
