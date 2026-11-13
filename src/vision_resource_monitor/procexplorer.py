"""Open a windows-side process row in Sysinternals Process Explorer.

The right-click **"Open … in Process Explorer"** affordance (epic #9 /
issue #61 — the maintainer works in Process Explorer, not Task Manager): one
click opens procexp with the row's process selected via its documented
``/s:<PID>`` switch. Qt-free and pure by design — this module only COMPOSES
the PowerShell invocation and interprets its output; the app runs it (an
async QProcess, so the UI never blocks on the ~1–2 s spawn) and shows the
stated-cause dialogs.

Probed live on the box before the design was locked (2026-07-22, #61):

- procexp 17.06 resolves from the **Windows** PATH (``Get-Command``); the
  Windows PATH does NOT propagate into this box's WSL, so discovery happens
  INSIDE the PowerShell call, never in the app's own environment — one
  mechanism for the frozen native app and the WSLg dev window alike (the
  interop launches Windows GUI exes onto the Windows desktop either way).
- ``/s:<pid>`` selects the process on open; with an instance already
  running, a NEW instance opens carrying the selection (live-tested — no
  forwarding to the running window). There is no name-filter switch, so
  PID selection is the mechanism.
- The windows-tier Memory rows are name-aggregated with no PID
  (``{"name": "svchost", "mem_mib": …}``), so a name row resolves to its
  **largest-working-set instance** at click time (``Get-Process`` — the same
  namespace the producer's sweep names come from).

**Replace, don't stack** (#63, maintainer-directed): ``/s:`` cannot hand a
selection to a running instance, so before launching, the script CLOSES every
existing procexp — ``CloseMainWindow()`` first (graceful: procexp saves its
view settings), ``Stop-Process -Force`` for stragglers, each re-checked to
still be named ``procexp*`` before the kill (pid-reuse honesty, the #52
lesson) — and the click always yields THE procexp window. The close list is
the two constant image names, never row-derived text; it runs only AFTER
target resolution, so a GONE/NOPROC click closes nothing. An ELEVATED
instance survives both close paths (UIPI blocks WM_CLOSE, TerminateProcess
is denied) and the selection then opens beside it — the OK marker's
``closed=/survived=`` counts put that in the log. The TARGET pid is excluded
from the close set (review L1: a procexp row targeting procexp itself would
otherwise be killed and then "selected" dead — the one case where replace
yields to a correct selection beside the survivor). Warn-and-suggest stays
locked: this closes the user's own viewer at the user's explicit click,
never a monitored workload. A close that is actually needed adds up to ~5 s
of bounded waits before the fresh window appears (review L2 — the bare
spawn stays ~1 s warm).

One PowerShell spawn per click does discovery + resolution + launch —
event-driven only, zero per-tick cost (the standing footprint rule). The
whole script crosses argv as ``-EncodedCommand`` (base64 of UTF-16LE —
immune to every quoting layer, Windows and interop alike), and the process
NAME additionally travels base64 *within* the script:
smart-quote homoglyphs are PowerShell string delimiters, so quote-doubling
cannot make row-derived text safe) and is wildcard-escaped before
``Get-Process -Name``. ``/accepteula`` is deliberately NOT passed —
first-run EULA consent stays the user's.
"""
from __future__ import annotations

import base64
import shutil
import sys

# Every script answer is one "VRMPX:<KIND> [detail]" line; classify_output()
# keys on the prefix. The last marker wins so profile/host noise can't spoof
# an earlier line into the verdict.
_MARK = "VRMPX:"

# The canonical interop path for dev/WSLg runs — this box's WSL PATH carries
# no Windows dirs (verified 2026-07-22), so which() alone can't be trusted.
_WSL_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# How long the app waits for the marker before giving up (the PS spawn is
# ~1–2 s warm; a #63 close phase adds up to ~5 s of bounded waits when an
# instance actually needs closing; 20 s only trips when the host itself is
# in real trouble).
LAUNCH_TIMEOUT_MS = 20_000


def _b64utf8(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def subject(pid=None, name=None) -> str:
    """The row's process as the dialogs/log name it: ``name[pid]`` /
    ``PID n`` / bare name — the same bracket form the panels render."""
    if pid is not None and name:
        return f"{name}[{pid}]"
    if pid is not None:
        return f"PID {pid}"
    return str(name or "?")


def ps_script(pid=None, name=None) -> str:
    """The PowerShell program for one click. A pid picks pid mode — it is
    existence-checked first (an honest GONE beats procexp opening
    mysteriously unselected — rows come from ~2 s frames, so the process can
    exit under the open menu) — else a non-empty name resolves to its
    largest-working-set instance (NOPROC when none). A row that carries BOTH
    (the menu's ``name[pid]`` form hands both, the name as display garnish)
    launches by the pid — the stronger identity (review B1: an exactly-one
    guard here turned every pid-row click into a ValueError out of the
    triggered slot). procexp is resolved from the Windows PATH (procexp64
    preferred), with the Microsoft-Store WindowsApps alias as the fallback;
    NOEXE states the miss. After resolution — never before it — every
    running procexp is closed (gracefully, then force; constants-only kill
    list) so the fresh ``/s:`` launch REPLACES the window instead of
    stacking a second one (#63)."""
    if pid is None and not name:
        raise ValueError("a pid or a non-empty name is required")
    head = (
        "$ProgressPreference = 'SilentlyContinue'\n"
        "$exe = $null\n"
        "foreach ($n in 'procexp64.exe','procexp.exe') {\n"
        "  $c = Get-Command $n -ErrorAction SilentlyContinue\n"
        "  if ($c -and $c.Source) { $exe = $c.Source; break }\n"
        "}\n"
        "if (-not $exe) {\n"
        "  foreach ($p in @(\"$env:LOCALAPPDATA\\Microsoft\\WindowsApps\\procexp64.exe\",\n"
        "                   \"$env:LOCALAPPDATA\\Microsoft\\WindowsApps\\procexp.exe\")) {\n"
        "    if (Test-Path $p) { $exe = $p; break }\n"
        "  }\n"
        "}\n"
        f"if (-not $exe) {{ Write-Output '{_MARK}NOEXE'; exit 0 }}\n")
    if pid is not None:
        pid = int(pid)
        target = (
            f"$t = Get-Process -Id {pid} -ErrorAction SilentlyContinue\n"
            f"if (-not $t) {{ Write-Output '{_MARK}GONE'; exit 0 }}\n"
            f"$sel = {pid}\n")
    else:
        target = (
            "$n = [Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64utf8(str(name))}'))\n"
            "$t = @(Get-Process -Name ([WildcardPattern]::Escape($n)) "
            "-ErrorAction SilentlyContinue | "
            "Sort-Object WorkingSet64 -Descending)\n"
            f"if ($t.Count -eq 0) {{ Write-Output '{_MARK}NOPROC'; exit 0 }}\n"
            "$sel = $t[0].Id\n")
    tail = (
        # Replace-instance (#63): runs strictly AFTER the target resolved —
        # a click that will launch nothing must close nothing. Constant
        # image names only; the Stop-Process rung re-checks each pid is
        # STILL a procexp before killing (pid-reuse honesty, #52 lesson).
        "$old = @(Get-Process -Name 'procexp','procexp64' "
        "-ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $sel })\n"
        "$survived = 0\n"
        "if ($old.Count -gt 0) {\n"
        "  foreach ($o in $old) { try { $null = $o.CloseMainWindow() } "
        "catch {} }\n"
        "  Wait-Process -Id @($old | ForEach-Object -MemberName Id) "
        "-Timeout 3 -ErrorAction SilentlyContinue\n"
        "  foreach ($o in $old) {\n"
        "    $chk = Get-Process -Id $o.Id -ErrorAction SilentlyContinue\n"
        "    if ($chk -and $chk.Name -like 'procexp*') "
        "{ Stop-Process -Id $o.Id -Force -ErrorAction SilentlyContinue }\n"
        "  }\n"
        "  Wait-Process -Id @($old | ForEach-Object -MemberName Id) "
        "-Timeout 2 -ErrorAction SilentlyContinue\n"
        "  $survived = @(Get-Process -Name 'procexp','procexp64' "
        "-ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $sel }).Count\n"
        "}\n"
        "$closed = [Math]::Max(0, $old.Count - $survived)\n"
        "try {\n"
        "  Start-Process -FilePath $exe -ArgumentList \"/s:$sel\"\n"
        f"  Write-Output \"{_MARK}OK $sel closed=$closed survived=$survived\"\n"
        "} catch {\n"
        f"  Write-Output (\"{_MARK}ERR \" + $_.Exception.Message)\n"
        "}\n"
        "exit 0\n")
    return head + target + tail


def powershell_path() -> str:
    """powershell.exe as THIS process can start it: the bare name on Windows
    (System32 is always on PATH there); which() then the canonical /mnt/c
    interop path on the dev/WSLg side."""
    if sys.platform == "win32":
        return "powershell.exe"
    return shutil.which("powershell.exe") or _WSL_POWERSHELL


def launch_argv(pid=None, name=None) -> list[str]:
    """The exact argv the app's QProcess runs. The script rides
    ``-EncodedCommand`` (base64 UTF-16LE), so no argv/interop quoting layer
    can touch it."""
    enc = base64.b64encode(
        ps_script(pid=pid, name=name).encode("utf-16-le")).decode("ascii")
    return [powershell_path(), "-NoProfile", "-NonInteractive",
            "-EncodedCommand", enc]


def classify_output(text) -> tuple[str, str]:
    """(kind, detail) from the run's merged output. Kinds mirror the script's
    markers lower-cased (ok/noexe/gone/noproc/err); no marker at all is
    ``silent`` — PowerShell itself failed to run or parse, a different fact
    from every in-script answer."""
    kind, detail = "silent", ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(_MARK):
            k, _, d = line[len(_MARK):].partition(" ")
            kind, detail = k.strip().lower(), d.strip()
    return kind, detail


def manual_hint(pid=None, name=None) -> str:
    """The command to run by hand in a Windows terminal when the automatic
    launch can't (the #23/ask-claude fallback ethos). The name variant
    reproduces the script's pick EXACTLY — same base64 channel, same
    wildcard escape, same largest-instance sort (review L1: a hand-quoted
    name here re-imported the very #177 smart-quote hazard the script path
    exists to avoid, and globbed on wildcard-bearing names)."""
    if pid is not None:
        return f"procexp64.exe /s:{int(pid)}"
    return ("procexp64.exe /s:(Get-Process -Name ([WildcardPattern]::Escape("
            "[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("
            f"'{_b64utf8(str(name or ''))}')))) | "
            "Sort-Object WorkingSet64 -Descending | "
            "Select-Object -First 1).Id")


def explain(kind, *, pid=None, name=None, detail="") -> tuple[str, str]:
    """The stated-cause dialog for one non-OK outcome as (headline, body).
    Every kind says what actually happened and what to do next — never a
    bare "failed" (the repo's stated-cause ethos)."""
    who = subject(pid=pid, name=name)
    hint = manual_hint(pid=pid, name=name)
    if kind == "noexe":
        return ("Process Explorer was not found on the Windows PATH.",
                "Install Sysinternals Process Explorer and add its folder to "
                "the Windows PATH (or drop procexp64.exe into "
                "%LOCALAPPDATA%\\Microsoft\\WindowsApps), then retry.\n\n"
                f"Manual command, once installed:\n{hint}")
    if kind == "gone":
        return (f"{who} is no longer running.",
                "The PID came from the last ~2 s frame and the process has "
                "since exited; the row will update on the next repaint.")
    if kind == "noproc":
        return (f"No running process is named “{name}”.",
                "The name came from the last ~2 s frame (a windows-tier row "
                "aggregates every instance of the name); every instance has "
                "since exited.")
    if kind == "err":
        body = f"Windows reported: {detail}\n\n" if detail else ""
        return ("Process Explorer could not be launched.",
                body + f"Run it by hand instead:\n{hint}")
    if kind == "timeout":
        return ("The Process Explorer launch timed out.",
                f"PowerShell gave no answer within "
                f"{LAUNCH_TIMEOUT_MS // 1000} s (host under heavy load?). "
                f"Run it by hand instead:\n{hint}")
    # "silent" and anything unrecognised: PowerShell never answered.
    return ("The Process Explorer launch did not report back.",
            "PowerShell produced no answer (its output is in the log). "
            f"Run it by hand instead:\n{hint}")
