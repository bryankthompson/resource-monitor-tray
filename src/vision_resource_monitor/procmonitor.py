"""Open a windows-side process row in Sysinternals Process Monitor, FILTERED.

The right-click **"Open … in Process Monitor (filtered)"** affordance
(epic #9 / issue #87): Process Explorer cannot filter at all (probed
2026-07-26 — no CLI switch, no GUI/registry filter concept), but Process
Monitor can, and its ``/LoadConfig`` switch accepts a PMC configuration
built programmatically by the ``procmon-parser`` library (MIT). One click
opens ProcMon capturing ONLY the row's process events. Qt-free and pure by
design — this module COMPOSES the PowerShell invocation and the PMC bytes
and interprets the run's output; the app runs it (async QProcess) and shows
the stated-cause dialogs.

Probed live on the box before the design was locked (2026-07-26, #87):

- ProcMon is NOT on the Windows PATH here; the maintainer chose
  ``C:\\tools`` as its install home — discovery is Windows-PATH first
  (``Get-Command``), then the ``C:\\tools`` convention, all INSIDE the
  PowerShell call (the WSL PATH carries no Windows dirs; the procexplorer
  lesson).
- A minimal procmon-parser PMC (one ``PID is <pid> → include`` rule) loads
  cleanly via ``/LoadConfig`` and the window opens filtered to exactly that
  process ("only shows explorer, and all its operations").
- The live run captured ~5M events in minutes: the generated config sets
  ``DestructiveFilter=1`` so non-matching events are DROPPED at capture,
  never stored-and-hidden (a long-open session must not eat the host's RAM
  from inside a resource monitor).
- ProcMon **requires elevation** (capture driver) and **single-instances
  the capture**: a second launch errors "already monitoring the system",
  and a non-elevated close of the running (elevated) instance is DENIED
  (CloseMainWindow posted=False / no effect, Stop-Process access denied).
  So the #63 replace-instance semantics ride ProcMon's own documented
  ``/Terminate`` switch, and the WHOLE windows-side stage (write PMC →
  terminate old → launch new) runs inside ONE elevated PowerShell wrapper:
  a single UAC prompt per click, the launched ProcMon inheriting the
  elevation. The elevated inner stage cannot share stdout with the app, so
  it answers through a result file in the app's per-user data directory
  that the outer (non-elevated) stage re-emits as markers.

Two row modes, mirroring procexplorer:

- a pid row filters ``PID is <pid>`` — existence-checked in the OUTER stage
  first, so an honest GONE never costs a UAC prompt;
- a pid-less windows-tier name row (name-aggregated by the producer)
  filters ``Process Name is <name>.exe OR <name>`` (same-column includes OR
  in ProcMon) — ALL instances of the name, exactly what the row aggregates;
  presence-checked in the outer stage (NOPROC without a prompt).

Warn-and-suggest stays locked: the terminate closes the user's own viewer
at the user's explicit click, never a monitored workload.
"""
from __future__ import annotations

import base64
import shutil
import sys
import uuid

from . import LEGACY_DATA_DIR, PUBLIC_DATA_DIR

# Every answer is one "VRMPM:<KIND> [detail]" line; classify_output() keys
# on the prefix; the last marker wins (profile/host noise cannot spoof).
_MARK = "VRMPM:"

_WSL_POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

# The maintainer-chosen install home probed 2026-07-26 (NOT on PATH).
_FALLBACK_DIR = "C:\\tools"

# Where the elevated inner stage writes the PMC and its result file — the
# app's own windows-side data dir (created if missing). Both filenames are
# per-click TOKEN-stamped (review HIGH-2): a single shared name lets two
# near-simultaneous clicks race — run B's PMC/result clobbers run A's, so A
# filters B's target or reads B's outcome. The token is a uuid hex string
# (safe in a single-quoted PS literal), stamped identically into the outer
# and inner stages of one click and swept when stale.
_PMC_PREFIX = "procmon-filter."
_PMC_SUFFIX = ".pmc"
_RESULT_PREFIX = "procmon-launch."
_RESULT_SUFFIX = ".out"

# The wait covers a HUMAN answering the UAC prompt plus a bounded terminate
# of the old instance plus the launch itself; the dialog for a timeout says
# the prompt may still be open.
LAUNCH_TIMEOUT_MS = 60_000


def _data_subdir() -> str:
    # Import the flag at call time so tests and the export can flip it.
    from . import PUBLIC_BUILD
    return PUBLIC_DATA_DIR if PUBLIC_BUILD else LEGACY_DATA_DIR


class PmcUnavailableError(RuntimeError):
    """procmon-parser is not importable in this environment — the app shows
    the stated cause WITHOUT spawning anything."""


def _b64utf8(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def subject(pid=None, name=None) -> str:
    """``name[pid]`` / ``PID n`` / bare name — the panels' bracket form."""
    if pid is not None and name:
        return f"{name}[{pid}]"
    if pid is not None:
        return f"PID {pid}"
    return str(name or "?")


def build_pmc(pid=None, name=None) -> bytes:
    """The PMC configuration for one click, as bytes. Lazy-imports
    procmon-parser (an optional [gui] dependency): raises
    :class:`PmcUnavailableError` with the stated cause when absent.
    ``DestructiveFilter=1`` drops non-matching events at capture (the 5M-
    events probe datum). Name mode ORs the ``<name>.exe`` and bare forms —
    ProcMon's Process Name column carries the image name (``vivaldi.exe``)
    for ordinary processes but bare names for kernel-side ones ("System",
    "Memory Compression")."""
    if pid is None and not name:
        raise ValueError("a pid or a non-empty name is required")
    try:
        from procmon_parser import Rule, dump_configuration
    except ImportError as exc:
        raise PmcUnavailableError(
            "the procmon-parser package is not installed in this app's "
            "environment") from exc
    import io
    if pid is not None:
        rules = [Rule("PID", "is", str(int(pid)), "include")]
    else:
        n = str(name)
        forms = [n if n.lower().endswith(".exe") else n + ".exe"]
        if not n.lower().endswith(".exe"):
            forms.append(n)
        rules = [Rule("Process_Name", "is", f, "include") for f in forms]
    buf = io.BytesIO()
    dump_configuration({"FilterRules": rules, "DestructiveFilter": 1}, buf)
    return buf.getvalue()


def _inner_script(pmc_b64: str, token: str) -> str:
    """The ELEVATED stage: resolve ProcMon, write the PMC, /Terminate any
    running instance (bounded), launch filtered, answer via the result
    file. Runs under -Verb RunAs; its stdout is unreachable, so every
    outcome is a line in the token-stamped result file (the outer stage
    re-emits). NO row-derived text ever enters a marker line (review
    HIGH-1): the OK marker carries only the integer close count, ERR
    collapses any newline in Windows' own message — so a name with an
    embedded newline can never inject a spurious ``VRMPM:`` line through
    the Set-Content → Get-Content → re-emit round-trip."""
    data_subdir = _data_subdir()
    return (
        "$ProgressPreference = 'SilentlyContinue'\n"
        f"$dir = Join-Path $env:LOCALAPPDATA '{data_subdir}'\n"
        "$null = New-Item -ItemType Directory -Path $dir -Force\n"
        f"$res = Join-Path $dir '{_RESULT_PREFIX}{token}{_RESULT_SUFFIX}'\n"
        "$out = @()\n"
        "try {\n"
        "  $exe = $null\n"
        "  foreach ($n in 'procmon64.exe','procmon.exe') {\n"
        "    $c = Get-Command $n -ErrorAction SilentlyContinue\n"
        "    if ($c -and $c.Source) { $exe = $c.Source; break }\n"
        "  }\n"
        "  if (-not $exe) {\n"
        f"    foreach ($p in @('{_FALLBACK_DIR}\\Procmon64.exe',\n"
        f"                     '{_FALLBACK_DIR}\\Procmon.exe')) {{\n"
        "      if (Test-Path $p) { $exe = $p; break }\n"
        "    }\n"
        "  }\n"
        "  if (-not $exe) { $out += 'NOEXE'; throw 'done' }\n"
        # Bound the token-stamped litter: sweep our own old PMC/result files
        # (a crashed inner leaves its pair behind) older than a day. Never
        # touches a live run's freshly-stamped pair.
        f"  Get-ChildItem -Path $dir -Filter '{_PMC_PREFIX}*{_PMC_SUFFIX}' "
        "-ErrorAction SilentlyContinue | Where-Object "
        "{ $_.LastWriteTime -lt (Get-Date).AddDays(-1) } | "
        "Remove-Item -Force -ErrorAction SilentlyContinue\n"
        f"  $pmc = Join-Path $dir '{_PMC_PREFIX}{token}{_PMC_SUFFIX}'\n"
        f"  [IO.File]::WriteAllBytes($pmc, "
        f"[Convert]::FromBase64String('{pmc_b64}'))\n"
        # Replace-instance: ProcMon single-instances the capture, and only
        # its own elevated /Terminate can close the elevated instance.
        "  $old = @(Get-Process -Name 'Procmon','Procmon64','Procmon64a' "
        "-ErrorAction SilentlyContinue)\n"
        "  $closed = 0\n"
        "  if ($old.Count -gt 0) {\n"
        "    Start-Process -FilePath $exe -ArgumentList '/Terminate' -Wait\n"
        # An event-heavy instance tears down SLOWLY (probed: a 5M-event
        # capture outlived a 3 s window after /Terminate) — launching into
        # a live teardown errors "already monitoring", so the wait is
        # generous and the launch gated on a true zero count.
        "    Wait-Process -Id @($old | ForEach-Object -MemberName Id) "
        "-Timeout 15 -ErrorAction SilentlyContinue\n"
        "    $left = @(Get-Process -Name 'Procmon','Procmon64','Procmon64a' "
        "-ErrorAction SilentlyContinue)\n"
        "    $closed = [Math]::Max(0, $old.Count - $left.Count)\n"
        "    if ($left.Count -gt 0) {\n"
        "      $out += ('BUSY closed=' + $closed + ' survived=' + $left.Count)\n"
        "      throw 'done'\n"
        "    }\n"
        "  }\n"
        "  Start-Process -FilePath $exe -ArgumentList "
        "'/Quiet','/LoadConfig',('\"' + $pmc + '\"')\n"
        # OK carries ONLY the integer count — no row-derived subject in the
        # marker channel (review HIGH-1). The app already knows the target.
        "  $out += ('OK closed=' + $closed)\n"
        "} catch {\n"
        "  if ($_.ToString() -ne 'done') { $out += ('ERR ' + "
        "($_.Exception.Message -replace '[\\r\\n]+',' ')) }\n"
        "}\n"
        "Set-Content -Path $res -Value $out -Encoding UTF8\n"
        "exit 0\n")


def ps_script(pid=None, name=None, pmc_b64="", token=None) -> str:
    """The OUTER (non-elevated) stage for one click: honest target checks
    first (GONE/NOPROC never cost a UAC prompt), then the elevated wrapper
    (Start-Process -Verb RunAs — a declined prompt answers CANCELLED), then
    the inner stage's TOKEN-stamped result file re-emitted as markers. The
    inner script itself crosses as base64 -EncodedCommand, so no quoting
    layer touches it; row-derived NAME text travels
    base64 within each stage, and NO row text ever rides a marker line
    (review HIGH-1). ``token`` isolates this click's PMC/result files from a
    concurrent click's (review HIGH-2); a fresh uuid when unset."""
    if pid is None and not name:
        raise ValueError("a pid or a non-empty name is required")
    token = token or uuid.uuid4().hex
    head = "$ProgressPreference = 'SilentlyContinue'\n"
    if pid is not None:
        pid = int(pid)
        target = (
            f"$t = Get-Process -Id {pid} -ErrorAction SilentlyContinue\n"
            f"if (-not $t) {{ Write-Output '{_MARK}GONE'; exit 0 }}\n")
    else:
        target = (
            "$n = [Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64utf8(str(name))}'))\n"
            "$t = @(Get-Process -Name ([WildcardPattern]::Escape($n)) "
            "-ErrorAction SilentlyContinue)\n"
            f"if ($t.Count -eq 0) {{ Write-Output '{_MARK}NOPROC'; exit 0 }}\n")
    inner = _inner_script(pmc_b64, token)
    inner_enc = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    data_subdir = _data_subdir()
    tail = (
        f"$dir = Join-Path $env:LOCALAPPDATA '{data_subdir}'\n"
        "$null = New-Item -ItemType Directory -Path $dir -Force\n"
        f"$res = Join-Path $dir '{_RESULT_PREFIX}{token}{_RESULT_SUFFIX}'\n"
        "Remove-Item -Path $res -ErrorAction SilentlyContinue\n"
        "try {\n"
        "  $p = Start-Process -FilePath 'powershell.exe' -Verb RunAs "
        "-WindowStyle Hidden -PassThru -Wait -ArgumentList "
        f"'-NoProfile','-NonInteractive','-EncodedCommand','{inner_enc}'\n"
        "} catch {\n"
        f"  Write-Output '{_MARK}CANCELLED'; exit 0\n"
        "}\n"
        "if (Test-Path $res) {\n"
        "  Get-Content -Path $res | ForEach-Object { "
        f"Write-Output ('{_MARK}' + $_) }}\n"
        "  Remove-Item -Path $res -Force -ErrorAction SilentlyContinue\n"
        "} else {\n"
        f"  Write-Output '{_MARK}NOANSWER'\n"
        "}\n"
        "exit 0\n")
    return head + target + tail


def powershell_path() -> str:
    """powershell.exe as THIS process can start it (procexplorer's rule)."""
    if sys.platform == "win32":
        return "powershell.exe"
    return shutil.which("powershell.exe") or _WSL_POWERSHELL


def launch_argv(pid=None, name=None, token=None) -> list[str]:
    """The exact argv the app's QProcess runs — builds the PMC (raising
    :class:`PmcUnavailableError` before any spawn when procmon-parser is
    absent) and rides the whole outer script over -EncodedCommand. ``token``
    (a fresh uuid per call when unset) stamps this click's PMC/result files
    so concurrent clicks never collide (review HIGH-2)."""
    token = token or uuid.uuid4().hex
    pmc_b64 = base64.b64encode(build_pmc(pid=pid, name=name)).decode("ascii")
    enc = base64.b64encode(
        ps_script(pid=pid, name=name, pmc_b64=pmc_b64, token=token)
        .encode("utf-16-le")).decode("ascii")
    return [powershell_path(), "-NoProfile", "-NonInteractive",
            "-EncodedCommand", enc]


def classify_output(text) -> tuple[str, str]:
    """(kind, detail) from the run's merged output — ok/noexe/gone/noproc/
    busy/cancelled/noanswer/err, or ``silent`` when no marker arrived
    (PowerShell itself failed). Last marker wins."""
    kind, detail = "silent", ""
    for line in (text or "").splitlines():
        line = line.strip()
        if line.startswith(_MARK):
            k, _, d = line[len(_MARK):].partition(" ")
            kind, detail = k.strip().lower(), d.strip()
    return kind, detail


def manual_hint(pid=None, name=None) -> str:
    """The by-hand fallback for the dialogs: terminate + a GUI filter
    recipe — a hand-typed /LoadConfig has no PMC to load, so the honest
    manual path is ProcMon's own filter dialog."""
    if pid is not None:
        what = f"PID is {int(pid)}"
    else:
        what = f"Process Name is {name}"
    return (f"C:\\tools\\Procmon64.exe  (then Filter \u2192 Filter\u2026 \u2192 "
            f"add \u201c{what}, then Include\u201d)")


def explain(kind, *, pid=None, name=None, detail="") -> tuple[str, str]:
    """The stated-cause dialog for one non-OK outcome as (headline, body) —
    every kind says what happened and what to do next (the repo's
    stated-cause ethos)."""
    who = subject(pid=pid, name=name)
    hint = manual_hint(pid=pid, name=name)
    if kind == "nolib":
        return ("The filtered view needs the procmon-parser package.",
                "This build lacks the optional procmon-parser dependency, "
                "which generates ProcMon's filter configuration. Reinstall "
                "the app with its [gui] extra (pip install -e .[gui]) or "
                f"filter by hand:\n{hint}")
    if kind == "noexe":
        return ("Process Monitor was not found.",
                "Neither the Windows PATH nor C:\\tools has Procmon64.exe. "
                "Install Sysinternals Process Monitor (ProcessMonitor.zip) "
                "into C:\\tools, then retry.")
    if kind == "gone":
        return (f"{who} is no longer running.",
                "The PID came from the last ~2 s frame and the process has "
                "since exited; the row will update on the next repaint.")
    if kind == "noproc":
        return (f"No running process is named \u201c{name}\u201d.",
                "The name came from the last ~2 s frame (a windows-tier row "
                "aggregates every instance of the name); every instance has "
                "since exited.")
    if kind == "cancelled":
        return ("The elevation prompt was declined.",
                "Process Monitor needs administrator rights for its capture "
                "driver, so each launch asks once (UAC). Click the row again "
                "and accept the prompt, or filter by hand in an elevated "
                f"ProcMon:\n{hint}")
    if kind == "busy":
        return ("The running Process Monitor would not close.",
                f"ProcMon single-instances its capture and /Terminate did "
                f"not end the running instance ({detail}). Close it manually "
                "(File \u2192 Exit) and click again.")
    if kind == "noanswer":
        return ("The elevated launch stage gave no answer.",
                "The elevation prompt was accepted but the launcher wrote no "
                "result. Check whether a ProcMon window appeared; otherwise "
                f"filter by hand:\n{hint}")
    if kind == "err":
        body = f"Windows reported: {detail}\n\n" if detail else ""
        return ("Process Monitor could not be launched.",
                body + f"Run it by hand instead:\n{hint}")
    if kind == "timeout":
        return ("The Process Monitor launch timed out.",
                f"No answer within {LAUNCH_TIMEOUT_MS // 1000} s — the UAC "
                "prompt may still be waiting on another display, or the "
                f"host is under heavy load. Check for it, or run by hand:\n"
                f"{hint}")
    return ("The Process Monitor launch did not report back.",
            "PowerShell produced no answer (its output is in the log). "
            f"Run it by hand instead:\n{hint}")
