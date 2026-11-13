"""Hand a ranked contributor to an interactive Claude session in a WSL terminal.

The Forecast panel's **"ask claude"** affordance (cross-port of the Mac
counterpart's): one click opens a terminal running ``claude`` inside WSL,
pre-loaded with everything the row knows — label, summary, the monitor's
advisory action, and the exact PID(s) when the metric names one — so the human
investigates with full context instead of retyping it.

**The session runs out of a dedicated worker folder** (``~/vrm-worker`` in the
WSL home, overridable via ``$VRM_WORKER_DIR`` *in the WSL environment*),
created and seeded on first use. A stable folder is what makes the sessions
low-friction: Claude Code keys project identity and permission grants off the
working directory, so a throwaway cwd would demand interactive approval for
every tool call, every time. The seeds — written only when absent, the
operator's edits are never overwritten — are:

- ``.claude/settings.json`` — auto-permission, **read-only**: the standard
  Linux diagnostics pre-approved (ps/ss/free/vmstat/df/docker ps|stats/
  nvidia-smi/…), the kill/stop/prune/shutdown families (kill/pkill,
  docker stop|rm|rmi|compose down|prune, systemctl stop|reboot,
  shutdown/reboot, rm/sudo) explicitly denied. Deny wins in Claude Code, so
  even an allowed prefix can't be parlayed into a kill. (Log readers like
  ``journalctl``/``dmesg`` are deliberately NOT pre-approved — their prefixes
  are write-capable, e.g. ``journalctl --vacuum-size`` — so they fall to a
  one-time interactive approval rather than a blanket allow.)
- ``CLAUDE.md`` — the worker's standing role: warn-and-suggest, exact PIDs
  only, the WSL-vs-Windows pid-namespace split, where the live snapshot feed
  lives.

**Warn-and-suggest stays locked.** This module never acts on the host beyond
composing a script that opens a terminal: the launched session is plain
interactive Claude in the user's own terminal, the prompt instructs it to
diagnose and propose commands for the human to run, and the seeded settings
deny the destructive commands outright.

Launch mechanics — this app is a native *Windows* process while the worker
folder, ``claude`` CLI, and the contributor's process all live inside WSL, so
the hand-off crosses the boundary the same way "Rebuild now" (#23) does:
``QProcess.startDetached`` of ``wsl.exe bash -lc …`` (with a ``wt.exe``
first-preference so Windows Terminal hosts the session when present, and the
app falling back to a copyable command when neither launches). The boundary
crossing is quote-hostile — wsl.exe hands its raw command-line tail to the
default shell for *re-parsing* — so **no contributor-derived text ever rides
in argv**: the full bash script (worker seeding via base64, cd, the
shell-quoted prompt) is written to a Windows temp file by Python, and the argv
references only that controlled path. Everything here is Qt-free and pure
except :func:`write_script`; the window only wires a button to the app's
launcher.
"""
from __future__ import annotations

import base64
import glob
import logging
import os
import re
import shlex
import stat
import tempfile
import time

log = logging.getLogger(__name__)

_SCRIPT_PREFIX = "vrm-ask-claude-"
_SCRIPT_MAX_AGE_S = 3600   # WSL reads the file after our detached launch; prune
                           # only scripts old enough that no launch is still using
                           # them, so the temp dir can't accumulate cleartext
                           # prompts (which embed host process cmdlines).

# Read by the generated script INSIDE WSL (this Windows-side process's own
# environment is irrelevant to where the worker folder lands).
WORKER_DIR_ENV = "VRM_WORKER_DIR"

# The standing instruction appended to every prompt — the human executes any
# remediation, and processes are only ever named by exact PID (a name-pattern
# kill can take out unrelated sessions).
_GUIDANCE = (
    "Please diagnose what is going on and whether any action is needed. "
    "Suggest specific commands for me to run, but do not execute anything "
    "destructive yourself — I execute the remediation. If a process should "
    "be stopped, name it by the exact PID given above; never suggest a "
    "name-pattern kill (pkill/killall).")

# ── the worker folder and its seeds ──────────────────────────────────────────────

# Auto-permission, read-only: the diagnostics an investigation actually uses,
# pre-approved so the session starts working instead of asking; everything
# destructive denied outright (deny beats allow). The operator owns the file
# after first write — edits are never clobbered.
_SETTINGS_SEED = """\
{
  "permissions": {
    "allow": [
      "Read", "Glob", "Grep",
      "Bash(ps:*)", "Bash(pgrep:*)", "Bash(top:*)", "Bash(ss:*)",
      "Bash(netstat:*)", "Bash(free:*)", "Bash(vmstat:*)",
      "Bash(uptime:*)", "Bash(df:*)", "Bash(du:*)", "Bash(uname:*)",
      "Bash(nvidia-smi:*)", "Bash(/usr/lib/wsl/lib/nvidia-smi:*)",
      "Bash(docker ps:*)", "Bash(docker stats:*)", "Bash(docker inspect:*)",
      "Bash(systemctl status:*)", "Bash(systemctl --user status:*)",
      "Bash(ls:*)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)",
      "Bash(wc:*)", "Bash(stat:*)", "Bash(file:*)"
    ],
    "deny": [
      "Bash(kill:*)", "Bash(pkill:*)", "Bash(killall:*)",
      "Bash(docker kill:*)", "Bash(docker stop:*)", "Bash(docker rm:*)",
      "Bash(docker rmi:*)", "Bash(docker restart:*)",
      "Bash(docker compose stop:*)", "Bash(docker compose down:*)",
      "Bash(docker compose rm:*)", "Bash(docker-compose stop:*)",
      "Bash(docker-compose down:*)", "Bash(docker system prune:*)",
      "Bash(docker container prune:*)", "Bash(docker image prune:*)",
      "Bash(docker volume prune:*)",
      "Bash(systemctl stop:*)", "Bash(systemctl restart:*)",
      "Bash(systemctl kill:*)", "Bash(systemctl start:*)",
      "Bash(systemctl disable:*)", "Bash(systemctl mask:*)",
      "Bash(systemctl reboot:*)", "Bash(systemctl poweroff:*)",
      "Bash(systemctl halt:*)",
      "Bash(systemctl --user stop:*)", "Bash(systemctl --user restart:*)",
      "Bash(systemctl --user kill:*)", "Bash(systemctl --user start:*)",
      "Bash(shutdown:*)", "Bash(reboot:*)", "Bash(poweroff:*)", "Bash(halt:*)",
      "Bash(rm:*)", "Bash(sudo:*)"
    ]
  }
}
"""

_FEED_FALLBACK = ("the snapshot the tray renders (default "
                  "<state-dir>/vision-resource-monitor/status.json)")


def claude_md_seed(feed_path: str | None = None) -> str:
    """The worker's standing role file. ``feed_path`` is the WSL-visible path
    of the live snapshot feed when the caller knows it (the app passes its
    actual snapshot path, translated); a generic pointer otherwise."""
    feed = feed_path or _FEED_FALLBACK
    return f"""\
# vrm-worker — vision-resource-monitor investigation sessions

Sessions in this folder are opened by the Vision Resource Monitor tray's
**"ask claude"** button (the native Windows renderer of the WSL-side resource
monitor), each seeded with one ranked contention contributor (label, summary,
the monitor's advisory action, and the exact PID where one is named).

## Ground rules (locked — warn-and-suggest)

- **Diagnose, then advise.** Suggest specific commands; the operator executes
  anything destructive or state-changing.
- **Exact PIDs only.** Never suggest a name-pattern kill (`pkill`/`killall`)
  — pattern kills have taken out unrelated sessions before.
- **Verify before advising on a PID**: confirm it still belongs to the
  expected process (`ps -p <pid> -o pid,comm,args=`) — PIDs recycle.
- **Two pid namespaces.** Contributor PIDs are WSL-side (this session runs in
  the same WSL distro, so `ps`/`ss` see them). Windows-host processes (the
  Memory panel's windows tier, `gpu_users`) carry Windows PIDs that WSL tools
  cannot see — say so rather than guessing.

## Context

- The live monitor feed (JSON, atomically replaced every ~2 s):
  `{feed}` — read it for the current forecast / contributors / sessions /
  ports state. (This path was recorded when this worker folder was first
  seeded; if the monitor's snapshot path has since changed — a Settings edit,
  or a dev `vrm` window vs the native app — update it here.)
- `nvidia-smi` lives at `/usr/lib/wsl/lib/nvidia-smi` (often not on PATH).
- Read-only diagnostics are pre-approved in `.claude/settings.json`
  (ps/ss/free/vmstat/docker ps|stats/…); destructive commands are denied
  there — recommend them for the operator to run instead. Log readers
  (`journalctl`, `dmesg`) aren't pre-approved and will ask once — that's
  expected.
"""


# ── prompt ───────────────────────────────────────────────────────────────────────

def contributor_prompt(c) -> str:
    """The interactive-session prompt for one ranked contributor (a
    ``snapshot.Contributor``): its label/summary/advisory action, the named
    PID when the metric carries one, and the remaining metric numbers as
    context. Pure and tolerant — missing fields simply drop out."""
    lines = ["The vision-resource-monitor tray flagged this resource-contention "
             "contributor on this box (measured by the WSL-side monitor):"]
    label = getattr(c, "label", "") or "?"
    rank = getattr(c, "rank", 0)
    lines.append(f"- contributor: {label}" + (f" (rank {rank})" if rank else ""))
    if getattr(c, "summary", ""):
        lines.append(f"- summary: {c.summary}")
    if getattr(c, "action", ""):
        lines.append(f"- the monitor's advisory action: {c.action}")
    metric = getattr(c, "metric", None)
    metric = metric if isinstance(metric, dict) else {}
    pid = metric.get("top_pid")
    if pid is not None:
        lines.append(f"- named process: PID {pid} (WSL-side)")
    extras = {k: v for k, v in metric.items()
              if k != "top_pid" and v is not None}
    if extras:
        lines.append("- metric detail: " + ", ".join(
            f"{k}={v}" for k, v in sorted(extras.items())))
    return "\n".join(lines) + "\n\n" + _GUIDANCE


# ── the generated WSL-side script ───────────────────────────────────────────────

def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def build_wsl_script(prompt: str, feed_path=None) -> str:
    """The bash script the WSL side executes. It carries EVERYTHING that must
    not cross the Windows→WSL argv boundary: the worker-folder seeding (seed
    bodies travel base64 — immune to quoting), the cd, the session marker,
    the claude-missing hint, and the shell-quoted prompt. A login shell runs
    it (``bash -l`` in the argv), so the user's own PATH finds ``claude``."""
    feed_wsl = win_to_wsl_path(feed_path) if feed_path is not None else None
    return (
        "#!/bin/bash\n"
        "# vision-resource-monitor -> ask claude (auto-generated; safe to delete)\n"
        'worker="${' + WORKER_DIR_ENV + ':-$HOME/vrm-worker}"\n'
        'if ! mkdir -p "$worker/.claude"; then\n'
        '  echo "cannot create worker folder: $worker"\n'
        "  read -rsn1 -p 'press any key to close'\n"
        "  exit 1\n"
        "fi\n"
        "# seeds are absent-only: the operator's edits survive every later launch\n"
        "seed() { [ -e \"$1\" ] || printf '%s' \"$2\" | base64 -d > \"$1\"; }\n"
        f"seed \"$worker/.claude/settings.json\" '{_b64(_SETTINGS_SEED)}'\n"
        f"seed \"$worker/CLAUDE.md\" '{_b64(claude_md_seed(feed_wsl))}'\n"
        'cd "$worker" || exit 1\n'
        "export VRM_ASK_CLAUDE=1\n"
        "if ! command -v claude >/dev/null 2>&1; then\n"
        "  echo 'claude CLI not found on PATH inside WSL -- install it (or fix"
        " PATH in ~/.profile), then retry.'\n"
        "  read -rsn1 -p 'press any key to close'\n"
        "  exit 127\n"
        "fi\n"
        f"exec claude {shlex.quote(prompt)}\n")


def prune_old_scripts(max_age_s: float = _SCRIPT_MAX_AGE_S) -> None:
    """Best-effort delete of stale hand-off scripts left in the temp dir by
    earlier launches — their bodies embed the prompt (host process cmdlines),
    so they must not accumulate. Only files older than ``max_age_s`` go, so a
    script a just-launched detached session is still reading is never pulled
    from under it. Never raises."""
    try:
        cutoff = time.time() - max_age_s
        for p in glob.glob(os.path.join(tempfile.gettempdir(),
                                         _SCRIPT_PREFIX + "*.sh")):
            try:
                if os.path.getmtime(p) < cutoff:
                    os.unlink(p)
            except OSError:
                pass
    except Exception as exc:  # noqa: BLE001 — hygiene, never fatal
        log.debug("ask-claude prune skipped: %r", exc)


def write_script(script: str) -> str:
    """Write the script to a Windows-side temp file (WSL reads it over
    /mnt/c) and return its path. Written with LF newlines — a CRLF script is
    unrunnable by bash (``$'\\r'`` on every line), and Python's text mode would
    otherwise translate to CRLF on Windows. The confidentiality of the file
    (its body embeds the prompt) rests on the per-user ``%TEMP%`` ACL on
    Windows; the ``chmod`` sets owner-only POSIX bits, which matter on the
    dev/WSL path but are a near-no-op against NTFS ACLs."""
    fd, path = tempfile.mkstemp(prefix=_SCRIPT_PREFIX, suffix=".sh")
    with os.fdopen(fd, "w", newline="\n") as f:   # bash needs LF, even from Windows
        f.write(script)
    os.chmod(path, stat.S_IRWXU)
    return path


def prepare_script(contributor, feed_path=None) -> str | None:
    """Compose + write the hand-off script for one contributor. Returns the
    (Windows-side) script path, or None on any failure — best-effort by
    design; a broken hand-off must never take the tray down."""
    try:
        prune_old_scripts()
        return write_script(
            build_wsl_script(contributor_prompt(contributor), feed_path=feed_path))
    except Exception as exc:  # noqa: BLE001 — fail-open, log the why
        log.warning("ask-claude script preparation failed: %r", exc)
        return None


# ── crossing the boundary ────────────────────────────────────────────────────────

def win_to_wsl_path(p) -> str | None:
    """A Windows path as WSL sees it under the default drive mount
    (``C:\\x`` → ``/mnt/c/x``). Already-POSIX paths pass through unchanged
    (dev runs and tests execute on the Linux side); UNC/relative paths return
    None — there is no reliable translation."""
    s = str(p)
    if s.startswith("/"):
        return s
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", s)
    if not m:
        return None
    return f"/mnt/{m.group(1).lower()}/{m.group(2).replace(chr(92), '/')}"


def _wsl_cmd(script_path) -> str:
    """The single ``bash -lc`` argument that runs the script: a fixed template
    plus one shell-quoted controlled path — no double quotes, no semicolons
    (wt.exe splits panes on ``;``), and never any contributor-derived text.
    The wsl.exe relay re-parses this through the default shell; the inner
    single quotes survive that parse and do their quoting job at the inner
    bash level.

    The path is not attacker-controlled (fixed prefix + a-z0-9 random +
    ``.sh``), but its ``%TEMP%`` parent can carry the Windows account name —
    and an apostrophe there makes ``shlex.quote`` emit the ``'"'"'`` idiom,
    injecting the very double quotes the wsl.exe re-parse would mangle. So we
    hard-enforce the invariant: an untranslatable OR quote-bearing path raises,
    and the caller falls back to the copyable command hint (correct for a
    single direct-paste shell, where shlex.quote's output IS right)."""
    wsl_path = win_to_wsl_path(script_path)
    if wsl_path is None:
        raise ValueError(f"untranslatable script path: {script_path!r}")
    if "'" in wsl_path:
        raise ValueError(f"script path needs double-quote escaping "
                         f"(would break the wsl.exe re-parse): {wsl_path!r}")
    return "exec bash -l " + shlex.quote(wsl_path)


def launch_argvs(script_path) -> list[list[str]]:
    """The launch ladder for the app's ``QProcess.startDetached`` (the #23
    precedent): Windows Terminal first when present, bare ``wsl.exe`` (its
    detached console window) otherwise. The app falls back to showing
    :func:`command_hint` when neither starts."""
    tail = ["wsl.exe", "bash", "-lc", _wsl_cmd(script_path)]
    return [["wt.exe"] + tail, tail]


def command_hint(script_path) -> str:
    """The command to run by hand in a WSL terminal — the fallback shown when
    neither wt.exe nor wsl.exe could be launched."""
    wsl_path = win_to_wsl_path(script_path)
    return f"bash {shlex.quote(wsl_path)}" if wsl_path else \
        f"bash <path of {script_path}>"
