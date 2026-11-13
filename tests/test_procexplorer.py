"""The Process-Explorer hand-off (#61): script composition (markers, the
base64 name channel, wildcard escape), the -EncodedCommand argv, output
classification, and the stated-cause dialog texts.

All Qt-free — the window only wires menu actions to the app's launcher. The
end-to-end test EXECUTES the generated script under the real Windows
PowerShell over the WSL interop (box runs only), targeting a name no process
has — which proves the script actually PARSES and the marker round-trips
(the #51 lesson: never trust composed code that has never been executed)
without ever launching Process Explorer, since NOPROC exits before
Start-Process.
"""
import base64
import os
import subprocess
import sys

import pytest

from vision_resource_monitor.procexplorer import (
    LAUNCH_TIMEOUT_MS, classify_output, explain, launch_argv, manual_hint,
    powershell_path, ps_script, subject,
)

_ps = powershell_path()
needs_powershell = pytest.mark.skipif(
    sys.platform != "win32" and not os.path.exists(_ps),
    reason="executes the script under real Windows PowerShell (box/WSL run)")


# ── script composition ─────────────────────────────────────────────────────────

def test_ps_script_mode_guard():
    with pytest.raises(ValueError):
        ps_script()
    with pytest.raises(ValueError):
        ps_script(name="")                      # empty ≠ a usable name
    # A name[pid] row hands BOTH (name is display garnish): pid — the
    # stronger identity — wins. Review B1: an exactly-one guard here made
    # every pid-row click raise out of the menu's triggered slot.
    assert ps_script(pid=4242, name="svchost") == ps_script(pid=4242)


def test_pid_script_existence_checks_and_selects():
    s = ps_script(pid=4242)
    assert "Get-Process -Id 4242" in s          # existence check, pid-exact
    assert "VRMPX:GONE" in s                    # honest "it exited" answer
    assert "$sel = 4242" in s
    assert '"/s:$sel"' in s                     # the documented select switch
    assert "VRMPX:NOEXE" in s and "VRMPX:OK" in s and "VRMPX:ERR" in s
    # Discovery prefers the 64-bit exe and asks the WINDOWS side (Get-Command
    # + the WindowsApps alias) — the app's own PATH may carry no Windows dirs.
    assert s.index("procexp64.exe") < s.index("procexp.exe")
    assert "WindowsApps" in s


def test_pid_script_coerces_pid_to_int():
    assert "Get-Process -Id 313" in ps_script(pid="313")
    with pytest.raises(ValueError):
        ps_script(pid="313; Stop-Computer")     # int() is the injection gate


def test_name_script_base64_channel_and_wildcard_escape():
    s = ps_script(name="NVIDIA Overlay")
    b64 = base64.b64encode("NVIDIA Overlay".encode()).decode()
    assert b64 in s                             # the name rides base64…
    assert "NVIDIA Overlay" not in s            # …never as raw script text
    assert "WildcardPattern" in s               # literal match, no globbing
    assert "WorkingSet64 -Descending" in s      # largest instance wins
    assert "VRMPX:NOPROC" in s


def test_name_script_smart_quotes_never_ride_raw():
    # U+2018/U+2019/U+201C/U+201D are PowerShell string
    # delimiters — no quoting can make them safe, only an encoding channel.
    nasty = "we’ird “name”"
    s = ps_script(name=nasty)
    for ch in "‘’“”":
        assert ch not in s
    assert base64.b64encode(nasty.encode()).decode() in s


def test_replace_instance_close_phase():
    # #63: a fresh /s: launch REPLACES the running instance (close-then-
    # launch) — /s: cannot hand a selection to a running window.
    s = ps_script(pid=4242)
    close_at = s.index("CloseMainWindow")
    # The close phase runs strictly AFTER the early exits: a GONE/NOPROC
    # click must close nothing (this is also what keeps the e2e tests below
    # from closing the operator's real procexp).
    assert s.index("VRMPX:GONE") < close_at
    n = ps_script(name="svchost")
    assert n.index("VRMPX:NOPROC") < n.index("CloseMainWindow")
    # The kill list is the two constant image names — never row-derived text.
    assert "Get-Process -Name 'procexp','procexp64'" in s
    # Graceful close precedes the force rung, and the force rung re-checks
    # the pid is STILL a procexp before killing (#52 pid-reuse honesty).
    assert close_at < s.index("Stop-Process")
    assert "$chk.Name -like 'procexp*'" in s
    assert s.index("-like 'procexp*'") < s.index("Stop-Process")
    # The OK marker carries the counts so an elevated survivor is in the log,
    # and the arithmetic is floored (review L3: a concurrent launch could
    # otherwise log a negative closed count).
    assert "closed=$closed survived=$survived" in s
    assert "[Math]::Max(0, $old.Count - $survived)" in s
    # The TARGET pid never joins the close set (review L1: a procexp row
    # targeting procexp itself would be killed, then "selected" dead).
    assert s.count("Where-Object { $_.Id -ne $sel }") == 2


def test_no_accepteula_is_passed():
    # First-run EULA consent stays the user's — pinned, not incidental.
    assert "accepteula" not in ps_script(pid=1).lower()
    assert "accepteula" not in ps_script(name="x").lower()


# ── argv ───────────────────────────────────────────────────────────────────────

def test_launch_argv_rides_encoded_command():
    argv = launch_argv(pid=7)
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    decoded = base64.b64decode(argv[4]).decode("utf-16-le")
    assert decoded == ps_script(pid=7)


def test_powershell_path_is_startable_form():
    p = powershell_path()
    assert p.endswith("powershell.exe")
    if sys.platform != "win32":
        assert p.startswith("/")                # which() hit or the interop path


# ── output classification ──────────────────────────────────────────────────────

def test_classify_output_variants():
    assert classify_output("VRMPX:OK 4242") == ("ok", "4242")
    # The shape the script actually emits since #63 (review L5).
    assert classify_output("VRMPX:OK 4242 closed=1 survived=0") == \
        ("ok", "4242 closed=1 survived=0")
    assert classify_output("VRMPX:NOEXE") == ("noexe", "")
    assert classify_output("noise\r\nVRMPX:GONE\r\n") == ("gone", "")
    assert classify_output("VRMPX:ERR Access is denied.") == \
        ("err", "Access is denied.")
    # The LAST marker wins — earlier lines can be profile/host noise.
    assert classify_output("VRMPX:ERR x\nVRMPX:OK 9")[0] == "ok"
    # No marker at all is its own fact: PowerShell never answered.
    assert classify_output("")[0] == "silent"
    assert classify_output(None)[0] == "silent"
    assert classify_output("garbage only")[0] == "silent"


# ── dialog texts ───────────────────────────────────────────────────────────────

def test_subject_forms():
    assert subject(pid=4242, name="svchost") == "svchost[4242]"
    assert subject(pid=4242) == "PID 4242"
    assert subject(name="svchost") == "svchost"
    assert subject() == "?"


def test_manual_hint_forms():
    assert manual_hint(pid=313) == "procexp64.exe /s:313"
    # The name variant mirrors the script exactly: base64 channel + wildcard
    # escape (review L1 — hand-quoting row text re-imported the #177 hazard).
    nasty = "it’s [odd]"
    h = manual_hint(name=nasty)
    assert base64.b64encode(nasty.encode()).decode() in h
    assert nasty not in h and "’" not in h      # never raw, never quoted
    assert "WildcardPattern" in h
    assert "/s:(Get-Process" in h and "WorkingSet64" in h


@pytest.mark.parametrize("kind", ["noexe", "gone", "noproc", "err",
                                  "timeout", "silent", "???"])
def test_explain_states_a_cause_for_every_kind(kind):
    headline, info = explain(kind, pid=99, name="svchost", detail="boom")
    assert headline and info                    # never a blank "failed"
    if kind in ("noexe", "err", "timeout", "silent", "???"):
        assert "procexp64.exe /s:" in info      # the by-hand escape hatch
    if kind == "timeout":
        assert str(LAUNCH_TIMEOUT_MS // 1000) in info
    if kind == "gone":
        assert "svchost[99]" in headline


def test_explain_noproc_names_the_name():
    headline, _ = explain("noproc", name="steamwebhelper")
    assert "steamwebhelper" in headline


# ── end-to-end under real PowerShell (box/WSL run) ─────────────────────────────

@needs_powershell
@pytest.mark.winps
def test_e2e_noproc_roundtrip_under_real_powershell():
    # A name no process has: the script must PARSE, resolve procexp (or not),
    # miss the name, and answer NOPROC — all before the #63 close phase AND
    # Start-Process, so nothing is closed or launched on the real host.
    # Proves syntax + the base64 name channel end to end.
    argv = launch_argv(name="vrm-px-test-no-such-proc-8f3a2")
    res = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    kind, _ = classify_output(res.stdout)
    # NOEXE only if the box lost procexp from PATH — still a valid parse.
    assert kind in ("noproc", "noexe"), (res.stdout, res.stderr)


@needs_powershell
@pytest.mark.winps
def test_e2e_gone_pid_roundtrip_under_real_powershell():
    # An existence-checked NEGATIVE: pid 2147483647 is never allocatable
    # (Windows pids are multiples of 4), so GONE answers before the #63
    # close phase and before Start-Process — the operator's real procexp is
    # neither closed nor joined by a new instance.
    argv = launch_argv(pid=2147483647)
    res = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    kind, _ = classify_output(res.stdout)
    assert kind in ("gone", "noexe"), (res.stdout, res.stderr)
