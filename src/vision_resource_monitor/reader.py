"""Poll the snapshot file and report freshness.

Freshness is judged from the file's **mtime on the consumer's own clock**, not
from the ``t`` inside the snapshot: the producer runs in WSL and the consumer on
the Windows host, and while they share the machine clock, keying staleness off a
local ``stat`` avoids any skew entirely and also catches a producer that died
mid-write (the file simply stops advancing).

The reader retains the last good snapshot: a transient unreadable/partial file
(e.g. read mid-rewrite) returns the previous frame flagged ``stale`` rather than
blanking the UI.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from .snapshot import Snapshot, SnapshotError, load

DEFAULT_STALE_AFTER_S = 8.0

log = logging.getLogger(__name__)


@dataclass
class ReadResult:
    snapshot: Snapshot | None      # newest good snapshot (or last good, if stale)
    stale: bool                    # True when no fresh frame is available
    age_s: float | None            # seconds since the file was last written
    error: str | None              # why this read produced nothing new
    missing: bool = False          # the snapshot file does not exist (yet)


class SnapshotReader:
    """Reads a snapshot file on demand, tracking freshness and last-good state.

    ``stat``/``now`` are injectable so freshness logic is unit-testable without
    touching a real filesystem or the wall clock.
    """

    def __init__(self, path, stale_after_s=DEFAULT_STALE_AFTER_S,
                 *, now=time.time, stat=None, loader=load):
        self.path = Path(path)
        self.stale_after_s = stale_after_s
        self._now = now
        self._stat = stat or (lambda p: os.stat(p))
        self._load = loader
        self._last_good: Snapshot | None = None
        self._last_mtime: float | None = None

    def read(self) -> ReadResult:
        result = self._read()
        if log.isEnabledFor(logging.DEBUG):
            age = f"{result.age_s:.1f}s" if result.age_s is not None else "?"
            kind = "missing" if result.missing else ("stale" if result.stale else "fresh")
            log.debug("read %s: %s age=%s error=%s", self.path, kind, age, result.error)
        return result

    def _read(self) -> ReadResult:
        now = self._now()
        try:
            st = self._stat(self.path)
        except FileNotFoundError:
            return ReadResult(self._last_good, stale=True, age_s=None,
                              error="snapshot file not found", missing=True)
        except OSError as exc:
            return ReadResult(self._last_good, stale=True, age_s=None,
                              error=f"cannot stat snapshot: {exc}")

        mtime = st.st_mtime
        age = max(0.0, now - mtime)

        # Only re-parse when the file actually advanced — cheap idle polling.
        if self._last_mtime is not None and mtime <= self._last_mtime and self._last_good:
            return ReadResult(self._last_good, stale=age > self.stale_after_s,
                              age_s=age, error=None)

        try:
            snap = self._load(self.path)
        except SnapshotError as exc:
            # Partial/garbled read — keep the last good frame, mark stale.
            return ReadResult(self._last_good, stale=True, age_s=age,
                              error=str(exc))
        except OSError as exc:
            return ReadResult(self._last_good, stale=True, age_s=age,
                              error=f"cannot read snapshot: {exc}")

        self._last_good = snap
        self._last_mtime = mtime
        return ReadResult(snap, stale=age > self.stale_after_s, age_s=age, error=None)
