import pytest

from vision_resource_monitor.reader import SnapshotReader
from vision_resource_monitor.snapshot import Snapshot, SnapshotError


class FakeStat:
    def __init__(self, mtime):
        self.st_mtime = mtime


def make_reader(*, mtime, now, loader, stale_after_s=8.0):
    clock = {"t": now}
    stat_state = {"mtime": mtime, "raise": None}

    def _stat(_path):
        if stat_state["raise"] is not None:
            raise stat_state["raise"]
        return FakeStat(stat_state["mtime"])

    reader = SnapshotReader(
        "ignored", stale_after_s=stale_after_s,
        now=lambda: clock["t"], stat=_stat, loader=loader,
    )
    return reader, clock, stat_state


def test_fresh_read_is_not_stale():
    snap = Snapshot(host="A")
    reader, _, _ = make_reader(mtime=100.0, now=101.0, loader=lambda p: snap)
    r = reader.read()
    assert r.snapshot is snap
    assert not r.stale
    assert r.error is None
    assert r.age_s == pytest.approx(1.0)


def test_old_mtime_is_stale():
    snap = Snapshot(host="A")
    reader, _, _ = make_reader(mtime=100.0, now=200.0, loader=lambda p: snap)
    r = reader.read()
    assert r.snapshot is snap
    assert r.stale
    assert r.age_s == pytest.approx(100.0)


def test_missing_file_returns_last_good_and_missing_flag():
    snap = Snapshot(host="A")
    reader, clock, stat_state = make_reader(mtime=100.0, now=101.0, loader=lambda p: snap)
    assert reader.read().snapshot is snap        # prime last-good

    stat_state["raise"] = FileNotFoundError()
    r = reader.read()
    assert r.missing is True
    assert r.stale is True
    assert r.snapshot is snap                     # last good retained
    assert r.error


def test_unparseable_keeps_last_good():
    good = Snapshot(host="good")
    calls = {"n": 0}

    def loader(_path):
        calls["n"] += 1
        if calls["n"] == 1:
            return good
        raise SnapshotError("partial write")

    reader, clock, stat_state = make_reader(mtime=100.0, now=101.0, loader=loader)
    assert reader.read().snapshot is good

    stat_state["mtime"] = 102.0    # file advanced -> triggers a re-parse
    clock["t"] = 103.0
    r = reader.read()
    assert r.snapshot is good        # last good retained
    assert r.stale is True
    assert "partial" in r.error


def test_unchanged_mtime_does_not_reparse():
    snap = Snapshot(host="A")
    calls = {"n": 0}

    def loader(_path):
        calls["n"] += 1
        return snap

    reader, clock, _ = make_reader(mtime=100.0, now=101.0, loader=loader)
    reader.read()
    clock["t"] = 102.0
    reader.read()      # same mtime -> cached, loader not called again
    assert calls["n"] == 1
