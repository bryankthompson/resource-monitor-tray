"""The filtered Process-Monitor hand-off (#87): PMC generation, two-stage
script composition (honest checks → ONE elevated wrapper → result-file
markers), the -EncodedCommand argv, output classification, and the
stated-cause dialog texts.

All Qt-free. The e2e tests EXECUTE the outer script under the real Windows
PowerShell over the WSL interop (box runs only) with never-matching targets:
GONE/NOPROC answer BEFORE the ``Start-Process -Verb RunAs`` wrapper, so the
script provably parses and round-trips its markers without ever raising a
UAC prompt or touching a running ProcMon. The elevated INNER stage cannot be
reached that way, so a separate e2e parses it via ``[ScriptBlock]::Create``
— syntax proven, nothing executed (the #51 lesson both times: never trust
composed code that has never crossed a real parser).
"""
import base64
import os
import subprocess
import sys

import pytest

from vision_resource_monitor import procmonitor
from vision_resource_monitor.procmonitor import (
    LAUNCH_TIMEOUT_MS, PmcUnavailableError, build_pmc, classify_output,
    explain, launch_argv, manual_hint, powershell_path, ps_script, subject,
)

_ps = powershell_path()
needs_powershell = pytest.mark.skipif(
    sys.platform != "win32" and not os.path.exists(_ps),
    reason="executes the script under real Windows PowerShell (box/WSL run)")

procmon_parser = pytest.importorskip(
    "procmon_parser",
    reason="the [gui] extra's procmon-parser generates the PMC")


# ── PMC generation ─────────────────────────────────────────────────────────────

def test_build_pmc_mode_guard():
    with pytest.raises(ValueError):
        build_pmc()
    with pytest.raises(ValueError):
        build_pmc(name="")


def test_build_pmc_pid_rule_and_destructive_filter():
    import io
    cfg = procmon_parser.load_configuration(io.BytesIO(build_pmc(pid=4242)))
    rules = cfg["FilterRules"]
    assert len(rules) == 1
    assert rules[0].column.name == "PID"
    assert rules[0].value == "4242"
    assert rules[0].action.name == "INCLUDE"
    # The 5M-events probe datum: non-matching events are DROPPED at capture,
    # never stored-and-hidden — a resource monitor must not eat the host's
    # RAM through its own affordance.
    assert cfg["DestructiveFilter"] == 1


def test_build_pmc_pid_coerces_to_int():
    import io
    cfg = procmon_parser.load_configuration(io.BytesIO(build_pmc(pid="313")))
    assert cfg["FilterRules"][0].value == "313"
    with pytest.raises(ValueError):
        build_pmc(pid="313; Stop-Computer")     # int() is the injection gate


def test_build_pmc_name_ors_exe_and_bare_forms():
    import io
    cfg = procmon_parser.load_configuration(
        io.BytesIO(build_pmc(name="Memory Compression")))
    values = [r.value for r in cfg["FilterRules"]]
    # Ordinary processes carry the image name (name.exe) in ProcMon's
    # Process Name column; kernel-side ones ("System", "Memory Compression")
    # are bare — same-column includes OR, so both forms travel.
    assert values == ["Memory Compression.exe", "Memory Compression"]
    cfg2 = procmon_parser.load_configuration(
        io.BytesIO(build_pmc(name="vivaldi.exe")))
    assert [r.value for r in cfg2["FilterRules"]] == ["vivaldi.exe"]


def test_build_pmc_states_cause_when_library_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "procmon_parser", None)
    with pytest.raises(PmcUnavailableError):
        build_pmc(pid=1)


# ── script composition ─────────────────────────────────────────────────────────

def test_ps_script_mode_guard():
    with pytest.raises(ValueError):
        ps_script()
    with pytest.raises(ValueError):
        ps_script(name="")
    # A name[pid] row hands BOTH (name is display garnish): pid wins and the
    # name is IGNORED in pid mode (no subject rides the marker channel any
    # more, HIGH-1) — the procexplorer review-B1 regression class. Fixed
    # token so the comparison isolates the pid-vs-name behavior, not the
    # per-click uuid.
    assert ps_script(pid=4242, name="svchost", token="t") == \
        ps_script(pid=4242, token="t")


def test_pid_script_checks_existence_before_the_elevation_wrapper():
    s = ps_script(pid=4242, pmc_b64="QUJD", token="t")
    assert "Get-Process -Id 4242" in s
    assert "VRMPM:GONE" in s
    # An honest GONE must never cost a UAC prompt: the check precedes the
    # RunAs wrapper strictly.
    assert s.index("VRMPM:GONE") < s.index("-Verb RunAs")


def test_name_script_base64_channel_and_wildcard_escape():
    s = ps_script(name="NVIDIA Overlay", pmc_b64="QUJD", token="t")
    b64 = base64.b64encode("NVIDIA Overlay".encode()).decode()
    assert b64 in s                             # the name rides base64…
    assert "NVIDIA Overlay" not in s            # …never as raw script text
    assert "WildcardPattern" in s
    assert "VRMPM:NOPROC" in s
    assert s.index("VRMPM:NOPROC") < s.index("-Verb RunAs")


def test_name_script_smart_quotes_never_ride_raw():
    nasty = "we’ird “name”"                     # the Unicode delimiters
    s = ps_script(name=nasty, pmc_b64="QUJD", token="t")
    for ch in "‘’“”":
        assert ch not in s
    assert base64.b64encode(nasty.encode()).decode() in s


def test_outer_wrapper_answers_every_elevation_outcome():
    s = ps_script(pid=1, pmc_b64="QUJD", token="t")
    # ONE elevated stage per click; a declined prompt is stated, a missing
    # result file is stated, and a stale result can never be re-read.
    assert s.count("-Verb RunAs") == 1
    assert "VRMPM:CANCELLED" in s
    assert "VRMPM:NOANSWER" in s
    assert s.index("Remove-Item") < s.index("-Verb RunAs")
    # The inner stage's file lines are re-emitted under the marker prefix.
    assert "'VRMPM:' + $_" in s


def test_ps_script_token_stamps_both_files_and_defaults_unique():
    # HIGH-2: the token appears in BOTH stages' result path (and the inner's
    # PMC path), isolating a concurrent click's files.
    s = ps_script(pid=1, pmc_b64="QUJD", token="TOK123")
    assert "procmon-launch.TOK123.out" in s          # outer result path
    inner = procmonitor._inner_script("QUJD", "TOK123")
    assert "procmon-launch.TOK123.out" in inner      # inner result path
    assert "procmon-filter.TOK123.pmc" in inner      # inner PMC path
    # Two default (token-less) launches never share a filename.
    import re
    toks = [re.search(r"procmon-launch\.([0-9a-f]+)\.out", launch_argv_decoded)
            for launch_argv_decoded in (
                base64.b64decode(launch_argv(pid=1)[4]).decode("utf-16-le"),
                base64.b64decode(launch_argv(pid=1)[4]).decode("utf-16-le"))]
    assert toks[0] and toks[1] and toks[0].group(1) != toks[1].group(1)


@pytest.mark.parametrize("public_build", [False, True])
def test_result_scripts_use_build_data_directory(monkeypatch, public_build):
    import vision_resource_monitor as package
    monkeypatch.setattr(package, "PUBLIC_BUILD", public_build)
    expected = (package.PUBLIC_DATA_DIR if public_build
                else package.LEGACY_DATA_DIR)
    inner = procmonitor._inner_script("QUJD", "t")
    outer = ps_script(pid=1, pmc_b64="QUJD", token="t")
    assert f"Join-Path $env:LOCALAPPDATA '{expected}'" in inner
    assert f"Join-Path $env:LOCALAPPDATA '{expected}'" in outer


def test_marker_channel_carries_no_row_text(monkeypatch):
    # HIGH-1: no row-derived subject may ride a marker line — a newline in a
    # name would otherwise inject a spurious VRMPM: line through the
    # Set-Content→Get-Content→re-emit round-trip. The OK marker carries only
    # the integer count; ERR collapses newlines.
    inner = procmonitor._inner_script("QUJD", "t")
    assert "'OK closed=' + $closed" in inner
    assert "[Text.Encoding]::UTF8.GetString" not in inner   # no name decode
    assert "-replace '[\\r\\n]+',' '" in inner              # ERR single-line
    # A newline-bearing name never reaches either stage as a subject.
    nasty = "evil\nVRMPM:OK spoofed"
    s = ps_script(name=nasty, pmc_b64="QUJD", token="t")
    assert "VRMPM:OK spoofed" not in s
    inner2 = procmonitor._inner_script("QUJD", "t")
    assert "VRMPM:OK spoofed" not in inner2


def test_inner_script_discovery_terminate_and_launch():
    inner = procmonitor._inner_script("QUJD", "t")
    # Discovery: Windows PATH first (64-bit preferred), then the C:\tools
    # convention (the maintainer-chosen home, probed NOT on PATH 2026-07-26).
    assert inner.index("procmon64.exe") < inner.index("procmon.exe")
    assert "C:\\tools\\Procmon64.exe" in inner
    assert inner.index("Get-Command") < inner.index("C:\\tools\\Procmon64.exe")
    assert "NOEXE" in inner
    # Replace-instance rides ProcMon's OWN /Terminate (an elevated instance
    # ignores a non-elevated close — probed), only when instances exist, and
    # a survivor is stated as BUSY with counts, never silently raced.
    assert "'/Terminate'" in inner
    assert inner.index("$old.Count -gt 0") < inner.index("'/Terminate'")
    assert "BUSY closed=" in inner
    # The launch: /Quiet suppresses the filter-confirm dialog; the PMC path
    # is quoted (LOCALAPPDATA may carry spaces); consent stays the user's.
    assert "'/Quiet','/LoadConfig'" in inner
    assert "accepteula" not in inner.lower()
    # The PMC bytes land via base64 → WriteAllBytes, no quoting layer.
    assert "FromBase64String('QUJD')" in inner
    assert "WriteAllBytes" in inner


# ── argv ───────────────────────────────────────────────────────────────────────

def test_launch_argv_builds_pmc_and_rides_encoded_command():
    # A shared token isolates the per-click uuid so the composed scripts
    # compare equal (HIGH-2 threads the token through both entry points).
    argv = launch_argv(pid=7, token="t")
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    decoded = base64.b64decode(argv[4]).decode("utf-16-le")
    pmc_b64 = base64.b64encode(build_pmc(pid=7)).decode("ascii")
    assert decoded == ps_script(pid=7, pmc_b64=pmc_b64, token="t")


def test_launch_argv_states_cause_without_the_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "procmon_parser", None)
    with pytest.raises(PmcUnavailableError):
        launch_argv(pid=7)


def test_powershell_path_is_startable_form():
    p = powershell_path()
    assert p.endswith("powershell.exe")
    if sys.platform != "win32":
        assert p.startswith("/")


# ── output classification ──────────────────────────────────────────────────────

def test_classify_output_variants():
    assert classify_output("VRMPM:OK vivaldi[7248] closed=1") == \
        ("ok", "vivaldi[7248] closed=1")
    assert classify_output("VRMPM:NOEXE") == ("noexe", "")
    assert classify_output("noise\r\nVRMPM:GONE\r\n") == ("gone", "")
    assert classify_output("VRMPM:CANCELLED") == ("cancelled", "")
    assert classify_output("VRMPM:NOANSWER") == ("noanswer", "")
    assert classify_output("VRMPM:BUSY closed=0 survived=1") == \
        ("busy", "closed=0 survived=1")
    assert classify_output("VRMPM:ERR Access is denied.") == \
        ("err", "Access is denied.")
    assert classify_output("VRMPM:ERR x\nVRMPM:OK 9")[0] == "ok"
    assert classify_output("")[0] == "silent"
    assert classify_output(None)[0] == "silent"
    assert classify_output("garbage only")[0] == "silent"


# ── dialog texts ───────────────────────────────────────────────────────────────

def test_subject_forms():
    assert subject(pid=4242, name="svchost") == "svchost[4242]"
    assert subject(pid=4242) == "PID 4242"
    assert subject(name="svchost") == "svchost"
    assert subject() == "?"


def test_manual_hint_is_a_gui_recipe():
    # A hand-typed /LoadConfig has no PMC to load: the honest by-hand path
    # is ProcMon's own filter dialog, spelled per mode.
    assert "PID is 313" in manual_hint(pid=313)
    assert "Process Name is chrome" in manual_hint(name="chrome")
    assert "Procmon64.exe" in manual_hint(pid=313)


@pytest.mark.parametrize("kind", ["nolib", "noexe", "gone", "noproc",
                                  "cancelled", "busy", "noanswer", "err",
                                  "timeout", "silent", "???"])
def test_explain_states_a_cause_for_every_kind(kind):
    headline, info = explain(kind, pid=99, name="svchost", detail="boom")
    assert headline and info                    # never a blank "failed"
    if kind == "timeout":
        assert str(LAUNCH_TIMEOUT_MS // 1000) in info
        assert "UAC" in info                    # the prompt may be waiting
    if kind == "cancelled":
        assert "administrator" in info or "UAC" in info
    if kind == "gone":
        assert "svchost[99]" in headline
    if kind == "nolib":
        assert "procmon-parser" in info


def test_explain_noproc_names_the_name():
    headline, _ = explain("noproc", name="steamwebhelper")
    assert "steamwebhelper" in headline


# ── end-to-end under real PowerShell (box/WSL run) ─────────────────────────────
# The noproc/gone round trips run a script to its exit path and flake with a WSL
# vsock error ("accept4 failed 110") when the box is loaded: winps, off by
# default. The parse check below stays on.

@needs_powershell
@pytest.mark.winps
def test_e2e_noproc_roundtrip_under_real_powershell():
    # A name no process has: parses, misses, answers NOPROC — strictly
    # before the RunAs wrapper, so no UAC prompt and no ProcMon touched.
    argv = launch_argv(name="vrm-pm-test-no-such-proc-8f3a2")
    res = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    kind, _ = classify_output(res.stdout)
    assert kind == "noproc", (res.stdout, res.stderr)


@needs_powershell
@pytest.mark.winps
def test_e2e_gone_pid_roundtrip_under_real_powershell():
    # Windows pids are multiples of 4: 2147483647 can never exist, so GONE
    # answers before the wrapper — nothing elevated, nothing launched.
    argv = launch_argv(pid=2147483647)
    res = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    kind, _ = classify_output(res.stdout)
    assert kind == "gone", (res.stdout, res.stderr)


@needs_powershell
def test_e2e_inner_stage_parses_under_real_powershell():
    # The elevated inner stage is unreachable without a UAC prompt, so its
    # syntax is proven by PARSING it for real: ScriptBlock::Create throws on
    # any syntax error and executes nothing.
    inner = procmonitor._inner_script(
        base64.b64encode(build_pmc(pid=7)).decode("ascii"), "probetoken")
    b64 = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
    wrapper = (
        "$t = [Text.Encoding]::Unicode.GetString("
        f"[Convert]::FromBase64String('{b64}'))\n"
        "$null = [ScriptBlock]::Create($t)\n"
        "Write-Output 'VRMPM:PARSED'\n")
    enc = base64.b64encode(wrapper.encode("utf-16-le")).decode("ascii")
    res = subprocess.run(
        [powershell_path(), "-NoProfile", "-NonInteractive",
         "-EncodedCommand", enc],
        capture_output=True, text=True, timeout=90)
    assert "VRMPM:PARSED" in res.stdout, (res.stdout, res.stderr)
