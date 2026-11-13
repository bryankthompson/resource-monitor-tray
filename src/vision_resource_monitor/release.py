"""Release a session's node processes — the app's FIRST host-mutating affordance.

A stale Claude session can leave node (MCP-server) children using WSL RAM after
the work finishes. The app lets a person review the session and, after
confirmation, signal its node children.

**The warn-and-suggest guardrail evolves here, not breaks.** The monitor still
NEVER acts on its own — no detector, alert, or contributor calls anything in
this module. The only path in is a human click followed by an explicit
confirmation dialog: that is the human executing the advisory, in-app instead
of in a terminal. The safeguards are:

* **Exact PIDs only, resolved at click time** from a fresh ``ps`` sweep — the
  candidates are the *descendants of the clicked session's root pid* whose
  comm is ``node``, pruned at nested session roots (a subagent / ``claude -p``
  child session's nodes are ITS OWN — never a parent's candidates). Never a
  name-pattern match (no pkill-alike, ever).
* **Re-verified at kill time** against a *second* fresh sweep: a pid is
  signalled only while it still exists, is still ``node``, and is still a
  descendant of that same root — a pid reused by an unrelated process between
  confirm and kill can never be hit. The root itself gone → abort, kill
  nothing (the session ended; its children reparented and are no longer
  provably ours to touch).
* **SIGTERM only** — the polite signal; never SIGKILL.
* The claude session process itself is **never signalled** — only its node
  children. Per-pid outcomes are reported back to the human.

**The WSL bridge — and why the re-verification stays in Python.** The native
app runs on WINDOWS while the session fleet lives inside WSL, so the sweeps and
the signal cross the boundary via ``wsl.exe -e`` (the #23/#62 shell-out
precedent); under the ``vrm --window`` WSLg dev path the same argv runs
directly. Both the ancestor walk and the node/root re-verification run in
PYTHON (the pure, unit-tested :func:`node_children`, the Mac's model) — the
only thing that crosses the boundary to mutate is a bare integer-argv
``kill -TERM <pid> <pid> …`` with NO shell and NO script: no quoting surface,
no variable-scoping footgun, nothing that can outlive a killed launcher.
Eligibility (still-alive / still-node / still-under-root) is decided from the
fresh kill-time sweep before any pid enters the ``kill`` argv. Qt-free (the UI
calls in; nothing here imports Qt) so the whole flow is unit-testable with
injected sweeps + a spied kill.

**A timed-out kill bridge is UNKNOWN, not failed**: at high host load the
wrapper can outlive its budget AFTER the signals landed —
declaring failure there is a false claim. On timeout the outcome is decided
by a fresh recheck sweep: a pid that is gone was released; a still-present
node is reported honestly ``unconfirmed``, never ``kill failed``. Every
degraded branch leaves a log trace — ERROR whenever anything is unconfirmed
or did not run — so quiet log levels still record it.
"""
from __future__ import annotations

import logging
import subprocess
import sys

log = logging.getLogger(__name__)

# Bounded ancestor walk: a session's process chain is a handful of levels; the
# bound only guards a pathological or cyclic synthetic table.
_ANCESTOR_MAX_DEPTH = 32

RELEASE_COMM = "node"      # what "release" targets — the MCP-server fleet
SESSION_COMM = "claude"    # a session root — the ownership prune boundary:
                           # a node under a NESTED session (a subagent /
                           # `claude -p` child) belongs to that session, never
                           # to a parent's release

_SWEEP_TIMEOUT_S = 5.0     # one ps spawn across the interop
_KILL_TIMEOUT_S = 5.0      # a bare kill is instant; this only bounds a hang


def _bridge(argv):
    """The argv actually executed: through ``wsl.exe -e`` from the native
    Windows app, verbatim inside WSL/Linux (the dev-window path)."""
    return (["wsl.exe", "-e"] + argv) if sys.platform == "win32" else argv


def _win_flags():
    """No console flash from the tray app (the shell-out convention)."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _run(argv, timeout):
    """Run one bridged read-only command, capturing stdout. Returns
    ``(ok, stdout)``; ``ok`` is False when the command couldn't run OR exited
    non-zero with no output. Never raises — a dead bridge is an outcome."""
    try:
        proc = subprocess.run(
            _bridge(argv), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, **_win_flags())
    except Exception as exc:                       # timeout, missing wsl.exe…
        return False, f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0 and not proc.stdout:
        return False, (proc.stderr or "").strip() or f"exit {proc.returncode}"
    return True, proc.stdout or ""


def _kill(pids):
    """SIGTERM ``pids`` via a bare bridged ``kill -TERM <int> …`` — no shell,
    integer argv only. Returns ``(ran, detail)`` with a TRI-STATE ``ran``:

    * ``True`` — the command EXECUTED (the signals were delivered to every
      still-existing target). A non-zero exit from ``kill`` (a pid that raced
      to exit → ESRCH) is NOT a failure — the surviving targets were still
      signalled — so the return code is deliberately ignored.
    * ``False`` — the bridge could not run at all (missing ``wsl.exe``, spawn
      error): nothing was delivered, and saying so is honest.
    * ``None`` — the bridge TIMED OUT: delivery is UNKNOWN. The signals may
      have landed before the wrapper outlived its budget, so the caller must
      verify the outcome before declaring anything."""
    argv = ["kill", "-TERM"] + [str(int(p)) for p in pids]
    try:
        subprocess.run(_bridge(argv), capture_output=True, text=True,
                       encoding="utf-8", errors="replace",
                       timeout=_KILL_TIMEOUT_S, **_win_flags())
    except subprocess.TimeoutExpired as exc:       # delivery UNKNOWN
        return None, f"{type(exc).__name__}: {exc}"
    except Exception as exc:                       # missing wsl.exe… — not run
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def _parse_ps(text):
    """Rows from ``ps -Ao pid=,ppid=,rss=,comm=`` into
    ``{pid, ppid, rss_kb, comm}``. comm is the kernel comm (15-char cap —
    ``node`` fits); tolerant of junk lines."""
    rows = []
    for line in (text or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        rows.append({"pid": pid, "ppid": ppid, "rss_kb": rss,
                     "comm": parts[3].strip()})
    return rows


def read_ps_rows(run_fn=_run):
    """A WSL process sweep, or None when the bridge failed — the caller states
    the cause rather than treating failure as an empty fleet (failed sweeps are
    never empty answers — the #52 lesson)."""
    ok, out = run_fn(["ps", "-Ao", "pid=,ppid=,rss=,comm="], _SWEEP_TIMEOUT_S)
    if not ok:
        log.error("release: WSL ps sweep failed: %s", out)
        return None
    return _parse_ps(out)


def _under_root(pid, root_pid, parents, session_pids=frozenset()):
    """True when ``pid``'s ppid chain reaches ``root_pid`` WITHOUT first
    crossing a pid in ``session_pids`` — the nearest-session prune: an
    ancestor that is itself a session root claims the whole subtree, so a
    nested session's processes are never a parent session's. Bounded +
    cycle-safe. Pure (its own local walk variable — no shared state)."""
    seen = set()
    cur = parents.get(pid)
    for _ in range(_ANCESTOR_MAX_DEPTH):
        if cur is None or cur in seen:
            return False
        if cur == root_pid:
            return True
        if cur in session_pids:
            return False               # a NEARER session owns this subtree
        seen.add(cur)
        cur = parents.get(cur)
    return False


def node_children(root_pid, rows):
    """The session's releasable node processes: every row whose comm is
    ``node`` and whose ppid chain reaches ``root_pid`` without crossing
    another session root — a nested session's (a subagent / ``claude -p``
    child's) node fleet belongs to that NEAREST session and is never a
    parent-session candidate. Heaviest first, ``[{pid, comm, rss_mib}]``.
    Never includes the root itself. Pure — the caller supplies the sweep
    (fresh at click time)."""
    if not root_pid or root_pid <= 0:
        return []
    parents = {r["pid"]: r.get("ppid") for r in rows or []
               if r.get("pid") is not None}
    nested = {r["pid"] for r in rows or []
              if r.get("pid") is not None and r.get("pid") != root_pid
              and r.get("comm") == SESSION_COMM}
    out = []
    for r in rows or []:
        pid = r.get("pid")
        if (pid is not None and pid != root_pid
                and r.get("comm") == RELEASE_COMM
                and _under_root(pid, root_pid, parents, nested)):
            out.append({"pid": pid, "comm": r.get("comm"),
                        "rss_mib": round((r.get("rss_kb") or 0) / 1024, 1)})
    return sorted(out, key=lambda k: k["rss_mib"], reverse=True)


def _recheck_after_timeout(pids, detail, sweep_fn):
    """The kill bridge timed out, so delivery is UNKNOWN — decide each pid's
    outcome from a FRESH sweep instead of declaring failure. A pid no longer
    present, or reused by a non-node, means the node is gone — mission done;
    a still-present node is honestly ``unconfirmed`` (it may still be shutting
    down, or the signal never landed). The recheck sweep itself failing is its
    own stated cause."""
    rows = sweep_fn()
    if rows is None:
        log.error("release: kill bridge timed out (%s) and the recheck sweep "
                  "did not answer for pids %s", detail, pids)
        return {p: "unconfirmed: the kill bridge timed out and the recheck "
                   "sweep did not answer" for p in pids}
    by_pid = {r["pid"]: r for r in rows if r.get("pid") is not None}
    out = {}
    for p in pids:
        row = by_pid.get(p)
        if row is None or row.get("comm") != RELEASE_COMM:
            out[p] = "gone"               # no longer present (or pid reused)
        else:
            out[p] = ("unconfirmed: the kill bridge timed out and the "
                      "process is still present — it may still be shutting "
                      "down, or the signal never landed; check the row on "
                      "the next frames")
    still = [p for p in pids if out[p].startswith("unconfirmed")]
    if still:
        log.error("release: kill bridge timed out (%s); delivery unconfirmed "
                  "for pids %s", detail, still)
    else:
        log.warning("release: kill bridge timed out (%s) but every target is "
                    "gone on recheck", detail)
    return out


def release_nodes(root_pid, pids, sweep_fn=read_ps_rows, kill_fn=_kill):
    """SIGTERM the confirmed ``pids`` — after re-deriving eligibility from a
    SECOND fresh sweep (the confirm dialog may have sat open for minutes;
    nothing is trusted from before it). Every check runs in Python against that
    sweep; only the eligible survivors enter the ``kill`` argv. Returns
    ``{pid: outcome}`` with outcomes ``terminated`` / ``gone`` / ``not-node`` /
    ``not-in-session`` / ``session-ended`` / ``unconfirmed: …`` / ``error: …``.

    Eligibility per requested pid: present in the sweep (else ``gone``), comm
    still ``node`` (else ``not-node`` — a reused pid is NEVER signalled), still
    a descendant of ``root_pid`` with no nearer session root in between (else
    ``not-in-session`` — a node that now sits under a nested session is that
    session's, not ours). The root missing
    from the sweep aborts the whole release (every pid ``session-ended``,
    nothing signalled). A ``kill`` that could not run reports ``error: …`` for
    the pids it would have signalled — never a false ``terminated``. A ``kill``
    that TIMED OUT is neither claimed nor blamed: a recheck sweep decides
    (``gone`` when the pid no longer exists, ``unconfirmed: …`` when a node
    still holds it — see :func:`_recheck_after_timeout`)."""
    kids = [int(p) for p in pids]
    root = int(root_pid)
    if root <= 0 or any(p <= 0 for p in kids):
        raise ValueError("pids must be positive integers")
    if not kids:
        return {}
    rows = sweep_fn()
    if rows is None:
        return {p: "error: sweep failed (the WSL bridge did not answer)"
                for p in kids}
    by_pid = {r["pid"]: r for r in rows if r.get("pid") is not None}
    if root not in by_pid:
        log.info("release: root %s absent from the kill-time sweep — "
                 "aborted, nothing signalled", root)
        return {p: "session-ended" for p in kids}
    eligible = {k["pid"] for k in node_children(root, rows)}
    outcomes, to_kill = {}, []
    for p in kids:
        row = by_pid.get(p)
        if row is None:
            outcomes[p] = "gone"              # already exited — mission done
        elif row.get("comm") != RELEASE_COMM:
            outcomes[p] = "not-node"          # pid reused — NEVER signalled
        elif p not in eligible:
            outcomes[p] = "not-in-session"    # reparented away — not ours
        else:
            to_kill.append(p)
    if to_kill:
        ran, detail = kill_fn(to_kill)
        if ran is None:                       # timed out — verify, don't claim
            outcomes.update(
                _recheck_after_timeout(to_kill, detail, sweep_fn))
        elif ran:
            for p in to_kill:
                outcomes[p] = "terminated"
        else:
            log.error("release: kill did not run (%s) for pids %s",
                      detail, to_kill)
            for p in to_kill:
                outcomes[p] = f"error: kill failed ({detail or 'bridge'})"
    log.info("release: root %s outcomes %s", root, outcomes)
    return outcomes


def confirm_text(label, kids):
    """The confirmation dialog's body — every pid spelled out before anything
    is signalled. Pure (unit-tested; the dialog only wraps it)."""
    lines = [f"Release {len(kids)} node process(es) of session "
             f"“{label}”?", ""]
    lines += [f"  SIGTERM  {k['pid']}  ({k['rss_mib']:.0f}M)" for k in kids]
    lines += ["", "Each PID is re-verified against a fresh WSL process sweep "
                  "at the moment of signalling; the session process itself is "
                  "not touched. If this session is still in use, its MCP "
                  "servers will drop."]
    return "\n".join(lines)


def result_text(label, outcomes):
    """The per-pid outcome summary shown after a release. Pure."""
    if not outcomes:
        return f"nothing to release for “{label}”"
    done = sum(1 for v in outcomes.values() if v in ("terminated", "gone"))
    lines = [f"released {done}/{len(outcomes)} node process(es) of "
             f"“{label}”"]
    lines += [f"  {pid}: {out}" for pid, out in sorted(outcomes.items())]
    return "\n".join(lines)
