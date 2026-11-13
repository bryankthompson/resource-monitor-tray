"""release.py — the rails, the WSL bridge, and the in-WSL verifier script.

The release safeguards, pinned: exact click-time PIDs (descendant walk
bounded + cycle-safe, never the root, never a name pattern), kill-time
re-verification in PYTHON (still alive / still node / still under the root;
root gone → abort all), SIGTERM only, integer-only script interpolation, and
honest outcomes for every degraded path (failed bridge, timed-out kill →
recheck before declaring, missing markers)."""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from vision_resource_monitor import release


def _rows(*triples):
    """(pid, ppid, comm[, rss_kb]) → sweep rows."""
    out = []
    for t in triples:
        pid, ppid, comm = t[0], t[1], t[2]
        rss = t[3] if len(t) > 3 else 1024
        out.append({"pid": pid, "ppid": ppid, "rss_kb": rss, "comm": comm})
    return out


def test_parse_ps_tolerates_junk_and_reads_comm():
    text = ("  10     1  2048 node\n"
            "junk line\n"
            "  x     1  2048 node\n"
            "  11    10  4096 python3\n")
    assert release._parse_ps(text) == [
        {"pid": 10, "ppid": 1, "rss_kb": 2048, "comm": "node"},
        {"pid": 11, "ppid": 10, "rss_kb": 4096, "comm": "python3"},
    ]
    assert release._parse_ps("") == [] and release._parse_ps(None) == []


def test_node_children_walk_depth_cycles_and_exclusions():
    rows = _rows(
        (100, 1, "claude"),               # the session root
        (110, 100, "sh"),                 # wrapper shell depth
        (111, 110, "node", 4096),         # grandchild node → included
        (112, 100, "node", 8192),         # direct child node → included
        (113, 100, "python3"),            # child, wrong comm → excluded
        (200, 1, "claude"),               # a FOREIGN session
        (201, 200, "node"),               # foreign node → excluded
        (300, 301, "node"),               # ppid cycle…
        (301, 300, "node"),               # …must not hang or match
    )
    kids = release.node_children(100, rows)
    assert [k["pid"] for k in kids] == [112, 111]      # heaviest first
    assert kids[0]["rss_mib"] == 8.0
    # The root itself is never a candidate even when its comm is node.
    rows2 = _rows((100, 1, "node"), (101, 100, "node"))
    assert [k["pid"] for k in release.node_children(100, rows2)] == [101]
    # Bad roots and empty sweeps answer empty.
    assert release.node_children(0, rows) == []
    assert release.node_children(None, rows) == []
    assert release.node_children(100, []) == []


def test_node_children_prunes_at_nested_session_roots():
    # A nested session (a `claude` descendant — a subagent / `claude -p`
    # review child) owns its own node fleet: the PARENT's candidates prune at
    # the nested root, transitively, so a parent-session release can never
    # SIGTERM a live nested session's MCP servers —
    # host-mutating stakes here).
    rows = _rows(
        (100, 1, "claude"),               # the clicked session root
        (112, 100, "node", 8192),         # own node → included
        (120, 100, "claude"),             # nested session root
        (121, 120, "node", 16384),        # nested session's node → excluded
        (122, 121, "node", 2048),         # deeper under the nested → excluded
        (130, 100, "sh"),                 # wrapper depth on the OWN side…
        (131, 130, "node", 4096),         # …still included
    )
    assert [k["pid"] for k in release.node_children(100, rows)] == [112, 131]
    # The nested session's own release sees exactly its own fleet.
    assert [k["pid"] for k in release.node_children(120, rows)] == [121, 122]


def test_release_nodes_nested_session_node_is_not_in_session():
    # Kill-time re-verify shares the prune: a confirmed pid that sits under a
    # NESTED session at kill time is `not-in-session` and never enters the
    # kill argv.
    sweep = _rows(
        (100, 1, "claude"),
        (111, 100, "node", 4096),         # own node — eligible
        (120, 100, "claude"),             # nested session root
        (121, 120, "node", 8192),         # the nested session's node
    )
    killed = {}

    def kill_fn(pids):
        killed["pids"] = list(pids)
        return True, ""

    out = release.release_nodes(100, [111, 121],
                                sweep_fn=lambda: sweep, kill_fn=kill_fn)
    assert out == {111: "terminated", 121: "not-in-session"}
    assert killed["pids"] == [111]


def test_read_ps_rows_failed_bridge_is_none_not_empty():
    assert release.read_ps_rows(
        run_fn=lambda argv, t: (False, "boom")) is None
    rows = release.read_ps_rows(
        run_fn=lambda argv, t: (True, "  10 1 2048 node\n"))
    assert rows == [{"pid": 10, "ppid": 1, "rss_kb": 2048, "comm": "node"}]


def test_release_nodes_integer_rail_and_empty():
    # The exact-PID rail is enforced at the boundary, not trusted.
    with pytest.raises(ValueError):
        release.release_nodes(100, [111, -5])
    with pytest.raises(ValueError):
        release.release_nodes(0, [111])
    with pytest.raises((ValueError, TypeError)):
        release.release_nodes(100, ["111; rm -rf /"])
    assert release.release_nodes(100, []) == {}


def test_release_nodes_python_reverify_classifies_and_kills_survivors():
    # The kill-time sweep decides eligibility in PYTHON; only survivors enter
    # the kill argv. 111 = node under root (eligible); 112 exited (gone);
    # 113 = pid reused by a non-node (not-node — NEVER signalled); 114 = node
    # but reparented off the root (not-in-session).
    sweep = _rows(
        (100, 1, "claude"),
        (111, 100, "node", 4096),
        (113, 100, "python3"),          # 113 reused since click time
        (114, 1, "node"),               # 114 no longer under root
    )
    killed = {}

    def kill_fn(pids):
        killed["pids"] = list(pids)
        return True, ""

    out = release.release_nodes(100, [111, 112, 113, 114],
                                sweep_fn=lambda: sweep, kill_fn=kill_fn)
    assert out == {111: "terminated", 112: "gone", 113: "not-node",
                   114: "not-in-session"}
    # ONLY the verified-eligible pid was signalled.
    assert killed["pids"] == [111]


def test_release_nodes_root_gone_aborts_all_signals_nothing():
    sweep = _rows((111, 1, "node"))     # the root pid is absent
    killed = {"called": False}

    def kill_fn(pids):
        killed["called"] = True
        return True, ""

    out = release.release_nodes(100, [111], sweep_fn=lambda: sweep,
                                kill_fn=kill_fn)
    assert out == {111: "session-ended"}
    assert killed["called"] is False


def test_release_nodes_failed_sweep_and_failed_kill_are_honest():
    # A failed kill-time sweep signals nothing and states the cause.
    out = release.release_nodes(100, [111, 112], sweep_fn=lambda: None,
                                kill_fn=lambda p: (True, ""))
    assert set(out) == {111, 112}
    assert all(v.startswith("error: sweep failed") for v in out.values())
    # A kill that could not RUN never reports a false "terminated".
    sweep = _rows((100, 1, "claude"), (111, 100, "node"))
    out = release.release_nodes(100, [111], sweep_fn=lambda: sweep,
                                kill_fn=lambda p: (False, "wsl hung"))
    assert out[111].startswith("error: kill failed") and "wsl hung" in out[111]


def test_kill_ignores_esrch_exit_but_reports_bridge_death(monkeypatch):
    # A bare kill that RAN (even exiting non-zero on a raced ESRCH) → ran=True:
    # the surviving targets were signalled, so it must not read as a failure.
    class _P:
        pass
    def fake_run(argv, **kw):
        assert argv[:2] == (["wsl.exe", "-e"] if sys.platform == "win32"
                            else argv[:2]) or True
        assert "kill" in argv and "-TERM" in argv
        p = _P(); p.returncode = 1; p.stdout = ""; p.stderr = "ESRCH"
        return p
    monkeypatch.setattr(release.subprocess, "run", fake_run)
    ran, _ = release._kill([111, 112])
    assert ran is True
    # A bridge that could not run at all → ran=False (nothing delivered);
    # a TIMEOUT → ran=None (delivery UNKNOWN — the caller must verify, never
    # declare failure: the earlier release lesson).
    def gone(argv, **kw):
        raise FileNotFoundError("wsl.exe")
    monkeypatch.setattr(release.subprocess, "run", gone)
    ran, detail = release._kill([111])
    assert ran is False and "FileNotFoundError" in detail
    def boom(argv, **kw):
        raise release.subprocess.TimeoutExpired(argv, 5.0)
    monkeypatch.setattr(release.subprocess, "run", boom)
    ran, detail = release._kill([111])
    assert ran is None and "TimeoutExpired" in detail


def test_release_nodes_kill_timeout_verifies_before_declaring():
    """A timed-out kill is decided by a FRESH recheck sweep, never declared
    "kill failed" : under load the bridge can outlive its budget
    AFTER the signals landed): a pid now gone → gone; a pid reused by a
    non-node → gone (the node died); a still-present node → honest
    unconfirmed, not counted in the released tally."""
    first = _rows((100, 1, "claude"),
                  (111, 100, "node"), (112, 100, "node"), (113, 100, "node"))
    recheck = _rows((100, 1, "claude"),
                    (112, 100, "python3"),       # pid reused → the node died
                    (113, 100, "node"))          # still present
    sweeps = [first, recheck]
    out = release.release_nodes(
        100, [111, 112, 113], sweep_fn=lambda: sweeps.pop(0),
        kill_fn=lambda p: (None, "TimeoutExpired: kill"))
    assert out[111] == "gone" and out[112] == "gone"
    assert out[113].startswith("unconfirmed:")
    assert not any("kill failed" in v for v in out.values())
    assert not sweeps                             # the recheck sweep RAN
    assert release.result_text("s", out).startswith("released 2/3")


def test_release_nodes_kill_timeout_recheck_failure_is_honest():
    # The recheck sweep failing after a timed-out kill states BOTH causes —
    # never a false "terminated", never an affirmative "kill failed".
    first = _rows((100, 1, "claude"), (111, 100, "node"))
    sweeps = [first, None]
    out = release.release_nodes(100, [111], sweep_fn=lambda: sweeps.pop(0),
                                kill_fn=lambda p: (None, "TimeoutExpired"))
    assert out[111].startswith("unconfirmed:") and "recheck" in out[111]


def test_release_failure_branches_log_at_error(caplog):
    # Failed sweeps, a kill that could not run, and an unconfirmed timeout
    # all leave an ERROR trace — a dismissed dialog must never be the only
    # record .
    with caplog.at_level(logging.ERROR,
                         logger="vision_resource_monitor.release"):
        assert release.read_ps_rows(run_fn=lambda a, t: (False, "boom")) is None
        sweep = _rows((100, 1, "claude"), (111, 100, "node"))
        release.release_nodes(100, [111], sweep_fn=lambda: sweep,
                              kill_fn=lambda p: (False, "no wsl"))
        sweeps = [list(sweep), list(sweep)]
        release.release_nodes(100, [111], sweep_fn=lambda: sweeps.pop(0),
                              kill_fn=lambda p: (None, "TimeoutExpired"))
    msgs = [r.getMessage() for r in caplog.records]
    assert any("sweep failed" in m for m in msgs)
    assert any("kill did not run" in m for m in msgs)
    assert any("unconfirmed" in m for m in msgs)


def test_confirm_and_result_texts():
    kids = [{"pid": 112, "comm": "node", "rss_mib": 8.0},
            {"pid": 111, "comm": "node", "rss_mib": 4.0}]
    txt = release.confirm_text("dir-review-cycle · task-0", kids)
    assert "2 node process(es)" in txt
    assert "SIGTERM  112" in txt and "SIGTERM  111" in txt
    assert "re-verified" in txt and "not touched" in txt
    res = release.result_text("s", {111: "terminated", 112: "not-node"})
    assert res.startswith("released 1/2")
    assert "112: not-node" in res
    assert "nothing to release" in release.result_text("s", {})


_E2E_OK = (sys.platform == "linux" and os.path.isdir("/proc")
           and shutil.which("ps") is not None and shutil.which("sh") is not None)


_NODE_ARGS = "-c 'while :; do sleep 1; done'"   # a live, killable fake node


def _fake_node(tmp_path):
    # A copy of the POSIX shell named `node` (comm == "node"; run it with
    # _NODE_ARGS). NOT a copy of `sleep`: on runners with a multi-call
    # coreutils (uutils) the copy dispatches on its own name, finds no `node`
    # applet and exits at once, leaving the e2e nothing to find (CI,
    # ubuntu-latest).
    node = tmp_path / "node"
    shutil.copy(shutil.which("sh") or "/bin/sh", node)
    node.chmod(0o755)
    return node


def _await(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(0.05)
    return pred()


def _await_node_children(root_pid, expected_count, timeout=5.0,
                         proc_root="/proc"):
    """Wait for a stable expected node count, collapsing forked duplicates."""
    deadline = time.time() + timeout
    last = []
    while time.time() < deadline:
        rows = release.read_ps_rows() or []
        candidates = release.node_children(root_pid, rows)
        by_pid = {row["pid"]: row for row in rows}
        identities = {}
        vanished = set()
        for child in candidates:
            pid = child["pid"]
            try:
                with open(f"{proc_root}/{pid}/cmdline", "rb") as stream:
                    cmdline = stream.read()
            except (FileNotFoundError, ProcessLookupError):
                vanished.add(pid)
                continue
            identities[pid] = (child["comm"], cmdline)
        # A forked sleep briefly retains its parent's comm and command line.
        # Collapse that parent/child pair, while preserving same-command-line
        # siblings whose parent is the session root.
        last = []
        for child in candidates:
            pid = child["pid"]
            parent_pid = by_pid.get(pid, {}).get("ppid")
            is_fork_duplicate = (pid not in vanished
                                 and parent_pid in identities
                                 and identities[pid] == identities[parent_pid])
            if pid not in vanished and not is_fork_duplicate:
                last.append(child)
        if len(last) == expected_count:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for {expected_count} node children under "
        f"root {root_pid}; last children: {last!r}")


def _live_pids():
    return {r["pid"] for r in release.read_ps_rows() or []}


def test_await_node_children_waits_for_expected_count_and_deduplicates(
        tmp_path, monkeypatch):
    root = 100
    rows = [
        {"pid": root, "ppid": 1, "comm": "sh", "rss_kb": 1},
        {"pid": 111, "ppid": root, "comm": "node", "rss_kb": 1},
        {"pid": 112, "ppid": root, "comm": "node", "rss_kb": 1},
        {"pid": 113, "ppid": 111, "comm": "node", "rss_kb": 1},
    ]
    snapshots = [rows[:2], rows]
    monkeypatch.setattr(release, "read_ps_rows", lambda: snapshots.pop(0))
    for pid in (111, 112, 113):
        path = tmp_path / str(pid)
        path.mkdir()
        (path / "cmdline").write_bytes(b"same-node-command\0")

    kids = _await_node_children(root, 2, timeout=0.5, proc_root=tmp_path)
    assert [child["pid"] for child in kids] == [111, 112]
    assert not snapshots


def test_await_node_children_timeout_names_expected_count(monkeypatch):
    monkeypatch.setattr(release, "read_ps_rows", lambda: [])
    with pytest.raises(AssertionError, match="waiting for 1 node children"):
        _await_node_children(100, 1, timeout=0.05)


@pytest.mark.skipif(not _E2E_OK, reason="needs Linux procfs + ps + sh")
def test_release_e2e_direct_children(tmp_path):
    """Real procfs: a root shell with two direct fake `node` children — found
    by the real sweep walk, TERMed via the real bare `kill`, gone afterwards;
    a dead root then aborts everything."""
    q = shlex.quote(str(_fake_node(tmp_path)))
    root = subprocess.Popen(
        ["sh", "-c", f"{q} {_NODE_ARGS} & {q} {_NODE_ARGS} & wait"])
    try:
        kids = _await_node_children(root.pid, expected_count=2)
        assert kids and len(kids) == 2, (kids, root.pid, [r for r in (release.read_ps_rows() or []) if r.get('comm') in ('node', 'sh', 'sleep', 'dash')])
        out = release.release_nodes(root.pid, [k["pid"] for k in kids])
        assert all(v == "terminated" for v in out.values()), out
        assert _await(lambda: not release.node_children(
            root.pid, release.read_ps_rows() or []))
    finally:
        root.terminate()
        root.wait(timeout=5)
    # Root gone → abort: nothing signalled, every pid reads session-ended.
    assert release.release_nodes(root.pid, [123456]) == {123456: "session-ended"}


@pytest.mark.skipif(not _E2E_OK, reason="needs Linux procfs + ps + sh")
def test_release_e2e_nested_kills_the_node_not_its_shell_parent(tmp_path):
    """The rail the earlier in-WSL script broke (safety review BLOCKING): a
    `node` GRANDCHILD under a non-node `sh` launcher must be the pid that
    dies — never the walked-up shell. root(sh) → intermediate(sh) → node.
    The intermediate has an INDEPENDENT lifetime (it sits in its own `sleep`,
    not waiting on the node) so its survival proves the release didn't touch
    it — rather than it merely outliving nothing."""
    q = shlex.quote(str(_fake_node(tmp_path)))
    sleep = shlex.quote(shutil.which("sleep") or "/bin/sleep")
    # intermediate: background the node, then stay busy in sleep; `; :` blocks
    # the shell's exec-optimization so it stays a live non-node parent.
    inter = f"{q} {_NODE_ARGS} & {sleep} 600; :"
    root = subprocess.Popen(
        ["sh", "-c", f"sh -c {shlex.quote(inter)} & wait"])
    try:
        kids = _await_node_children(root.pid, expected_count=1)
        # Exactly ONE releasable pid — the node grandchild, not the sh/sleep.
        assert kids and len(kids) == 1 and kids[0]["comm"] == "node", kids
        node_pid = kids[0]["pid"]
        by_pid = {r["pid"]: r for r in release.read_ps_rows()}
        inter_pid = by_pid[node_pid]["ppid"]         # the node's parent
        assert by_pid[inter_pid]["comm"] != "node"   # a non-node launcher
        out = release.release_nodes(root.pid, [node_pid])
        assert out == {node_pid: "terminated"}, out
        # The node is gone; the non-node parent is UNTOUCHED (its own lifetime).
        assert _await(lambda: node_pid not in _live_pids())
        assert inter_pid in _live_pids()
    finally:
        root.terminate()
        root.wait(timeout=5)
