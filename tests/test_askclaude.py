"""The ask-claude hand-off: prompt content, the generated WSL-side script
(seeding, quoting, marker), path translation, launch-ladder shape.

All Qt-free — the window only wires a button to the app's launcher. The
end-to-end tests EXECUTE the generated script under real bash with a fake
``claude`` on PATH (the box test run is WSL Linux — the same environment the
real script runs in), which proves the quoting/seeding round-trip stronger
than any static parse.
"""
import base64
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys

import pytest

from vision_resource_monitor.askclaude import (
    WORKER_DIR_ENV, _SETTINGS_SEED, build_wsl_script, claude_md_seed,
    command_hint, contributor_prompt, launch_argvs, prepare_script,
    win_to_wsl_path, write_script,
)
from vision_resource_monitor.snapshot import Contributor

_bash = shutil.which("bash")
needs_bash = pytest.mark.skipif(
    sys.platform == "win32" or not _bash,
    reason="executes the generated script under real bash (box/WSL run)")


def _stale_high_cost():
    return Contributor(
        kind="stale_high_cost", rank=2, label="stale high-cost process",
        summary="node [PID 4242] · 3.1G rss · 2h cpu · up 9h",
        action="restart or close node [PID 4242] to reclaim ~3.1G",
        metric={"top_pid": 4242, "top_name": "node", "rss_mib": 3174.0,
                "absent": None})


# ── prompt ───────────────────────────────────────────────────────────────────────

def test_contributor_prompt_carries_context_pid_and_guardrails():
    p = contributor_prompt(_stale_high_cost())
    assert "stale high-cost process (rank 2)" in p
    assert "3.1G rss" in p                          # summary
    assert "advisory action: restart or close" in p
    assert "named process: PID 4242 (WSL-side)" in p  # metric top_pid, explicit
    assert "rss_mib=3174.0" in p and "top_name=node" in p  # metric detail
    assert "absent=" not in p                       # None values drop out
    # The locked discipline travels with the prompt: human executes, exact
    # PIDs only, no name-pattern kills.
    assert "do not execute anything destructive" in p
    assert "exact PID" in p and "pkill" in p


def test_contributor_prompt_tolerates_sparse_contributors():
    p = contributor_prompt(Contributor(kind="x", label="session-fleet growth"))
    assert "session-fleet growth" in p
    assert "(rank" not in p and "summary:" not in p
    assert "named process" not in p and "metric detail" not in p
    assert "diagnose" in p                          # guidance always present
    # Even a bare object without the dataclass fields formats something sane.
    assert "- contributor: ?" in contributor_prompt(object())


# ── seeds ────────────────────────────────────────────────────────────────────────

def test_settings_seed_is_readonly_diagnostics_with_no_contradiction():
    settings = json.loads(_SETTINGS_SEED)          # strict JSON, no trailing junk
    allow = settings["permissions"]["allow"]
    deny = settings["permissions"]["deny"]
    # Auto-permission but READ-ONLY: Linux diagnostics allowed, destruction denied.
    assert "Bash(ps:*)" in allow and "Bash(ss:*)" in allow and "Read" in allow
    assert "Bash(free:*)" in allow and "Bash(nvidia-smi:*)" in allow
    assert "Bash(/usr/lib/wsl/lib/nvidia-smi:*)" in allow   # WSL's off-PATH home
    assert "Bash(docker ps:*)" in allow and "Bash(docker stats:*)" in allow
    assert "Bash(pkill:*)" in deny and "Bash(kill:*)" in deny
    assert "Bash(sudo:*)" in deny and "Bash(rm:*)" in deny
    assert "Bash(docker stop:*)" in deny and "Bash(docker kill:*)" in deny
    assert "Bash(systemctl stop:*)" in deny
    assert "Bash(systemctl --user stop:*)" in deny
    assert not any(a in deny for a in allow)        # no self-contradiction


def test_claude_md_seed_role_feed_path_and_namespace_note():
    md = claude_md_seed("/mnt/c/SampleData/Local/vision-resource-monitor/status.json")
    assert "warn-and-suggest" in md
    assert "/mnt/c/SampleData/Local/vision-resource-monitor/status.json" in md
    assert "pkill" in md                            # the pattern-kill rule
    assert "ps -p" in md                            # verify-before-advising
    assert "Windows PIDs" in md                     # the two-namespace nuance
    assert "/usr/lib/wsl/lib/nvidia-smi" in md
    # No feed path known → generic pointer, never a broken blank.
    assert "status.json" in claude_md_seed(None)


# ── path translation ─────────────────────────────────────────────────────────────

def test_win_to_wsl_path():
    assert win_to_wsl_path(r"C:\SampleData\Local\Temp\x.sh") == \
        "/mnt/c/SampleData/Local/Temp/x.sh"
    assert win_to_wsl_path("D:/data/f.json") == "/mnt/d/data/f.json"
    assert win_to_wsl_path("/tmp/already-posix.sh") == "/tmp/already-posix.sh"
    assert win_to_wsl_path(r"\\wsl$\Ubuntu\home\x") is None    # UNC: no mapping
    assert win_to_wsl_path("relative/path.sh") is None
    # Production passes a pathlib Path (a WindowsPath on the box), not a str —
    # pin the input type the app actually sends, on any-platform gates.
    from pathlib import PurePosixPath, PureWindowsPath
    assert win_to_wsl_path(
        PureWindowsPath(r"C:\SampleData\Local\vision-resource-monitor\status.json")
    ) == "/mnt/c/SampleData/Local/vision-resource-monitor/status.json"
    assert win_to_wsl_path(PurePosixPath("/var/tmp/sample-user/state/x.json")) == \
        "/var/tmp/sample-user/state/x.json"


# ── the generated script (static) ───────────────────────────────────────────────

HOSTILE = "it's $(rm -rf ~) `boom` \"quoted\" \\ text\nline two"


def test_script_seeds_cd_marker_and_hostile_prompt_roundtrip():
    script = build_wsl_script(HOSTILE, feed_path=r"C:\Sample Data\status.json")
    assert script.startswith("#!/bin/bash\n")
    # Worker folder: WSL-side env override, $HOME default.
    assert f'"${{{WORKER_DIR_ENV}:-$HOME/vrm-worker}}"' in script
    # Seeds travel base64 (immune to quoting) and are absent-only.
    assert '[ -e "$1" ]' in script
    payloads = re.findall(r"seed \"[^\"]+\" '([A-Za-z0-9+/=]+)'", script)
    assert len(payloads) == 2
    assert base64.b64decode(payloads[0]).decode() == _SETTINGS_SEED
    assert base64.b64decode(payloads[1]).decode() == \
        claude_md_seed("/mnt/c/Sample Data/status.json")
    # cd precedes the exec; the session marker exports before claude starts.
    assert script.index('cd "$worker"') < script.index("exec claude")
    assert "export VRM_ASK_CLAUDE=1" in script
    # The exec statement round-trips the prompt exactly through shell parsing
    # — no interpolation, no injection. (The quoted prompt spans lines, so
    # parse from the exec to the end of the script, not line-by-line.)
    exec_part = script[script.index("exec claude"):]
    assert shlex.split(exec_part) == ["exec", "claude", HOSTILE]
    # And a missing CLI reports instead of a silently-vanishing window.
    assert "command -v claude" in script and "exit 127" in script


# ── the generated script (executed — the real proof) ───────────────────────────

def _run_script(tmp_path, script_path, extra_env=None):
    """Execute the generated script under bash with an isolated HOME and a
    fake ``claude`` on PATH that records its argv/cwd/env. stdin is closed so
    an unexpected 'press any key' path exits instead of hanging."""
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    fake = fakebin / "claude"
    fake.write_text("#!/bin/sh\n"
                    'printf %s "$1" > "$OUT/prompt.txt"\n'
                    'printf %s "${VRM_ASK_CLAUDE:-}" > "$OUT/marker.txt"\n'
                    'pwd > "$OUT/cwd.txt"\n')
    fake.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home), "OUT": str(out),
           "PATH": f"{fakebin}:{os.environ.get('PATH', '')}"}
    env.update(extra_env or {})
    proc = subprocess.run([_bash, str(script_path)], env=env, timeout=30,
                          stdin=subprocess.DEVNULL, capture_output=True,
                          text=True)
    return proc, home, out


@needs_bash
def test_script_executes_seeds_worker_and_hands_prompt_byte_exact(tmp_path):
    hostile = Contributor(
        kind="stale_high_cost", rank=1, label=HOSTILE,
        summary='a "summary" with $(dollar) and back\\slash',
        action="restart node [PID 77]", metric={"top_pid": 77})
    prompt = contributor_prompt(hostile)
    script = tmp_path / "hand.sh"
    script.write_text(build_wsl_script(prompt, feed_path="/mnt/c/x/status.json"))
    proc, home, out = _run_script(tmp_path, script)
    assert proc.returncode == 0, proc.stderr
    # The prompt reached claude byte-exact through every quoting layer.
    assert (out / "prompt.txt").read_text() == prompt
    assert (out / "marker.txt").read_text() == "1"
    worker = home / "vrm-worker"
    assert os.path.realpath((out / "cwd.txt").read_text().strip()) == \
        os.path.realpath(str(worker))
    # The worker came up seeded: strict-JSON settings + the role file.
    settings = json.loads((worker / ".claude" / "settings.json").read_text())
    assert "Bash(pkill:*)" in settings["permissions"]["deny"]
    md = (worker / "CLAUDE.md").read_text()
    assert "warn-and-suggest" in md and "/mnt/c/x/status.json" in md
    # Seeds are absent-only: operator edits survive a second launch.
    (worker / "CLAUDE.md").write_text("operator-owned")
    (worker / ".claude" / "settings.json").write_text("operator-owned")
    proc2, _, _ = _run_script(tmp_path, script)
    assert proc2.returncode == 0, proc2.stderr
    assert (worker / "CLAUDE.md").read_text() == "operator-owned"
    assert (worker / ".claude" / "settings.json").read_text() == "operator-owned"


@needs_bash
def test_script_honors_worker_dir_env_override(tmp_path):
    script = tmp_path / "hand.sh"
    script.write_text(build_wsl_script("hello", feed_path=None))
    elsewhere = tmp_path / "elsewhere"
    proc, _, out = _run_script(
        tmp_path, script, extra_env={WORKER_DIR_ENV: str(elsewhere)})
    assert proc.returncode == 0, proc.stderr
    assert os.path.realpath((out / "cwd.txt").read_text().strip()) == \
        os.path.realpath(str(elsewhere))
    assert (elsewhere / "CLAUDE.md").exists()


@needs_bash
def test_script_reports_missing_claude_instead_of_vanishing(tmp_path):
    script = tmp_path / "hand.sh"
    script.write_text(build_wsl_script("hello", feed_path=None))
    home = tmp_path / "home"; home.mkdir()
    # No fake claude on PATH; closed stdin makes the hold-open read return.
    proc = subprocess.run(
        [_bash, str(script)], timeout=30, stdin=subprocess.DEVNULL,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True)
    assert proc.returncode == 127
    assert "claude CLI not found" in proc.stdout


# ── launch ladder + fallback hint ───────────────────────────────────────────────

def test_launch_argvs_ladder_wt_first_then_wsl():
    argvs = launch_argvs(r"C:\SampleData\Local\Temp\vrm-ask-claude-x.sh")
    assert [a[0] for a in argvs] == ["wt.exe", "wsl.exe"]
    assert argvs[0][1:] == argvs[1]                 # wt wraps the same tail
    assert argvs[1][:3] == ["wsl.exe", "bash", "-lc"]
    cmd = argvs[1][3]
    # A login inner bash (the user's PATH finds claude), on the translated path.
    assert cmd == ("exec bash -l "
                   "/mnt/c/SampleData/Local/Temp/vrm-ask-claude-x.sh")
    # The boundary constraint: nothing in the cmd for wt.exe to split on (';')
    # and no double quotes for the wsl.exe relay re-parse to mangle.
    assert ";" not in cmd and '"' not in cmd


def test_launch_argvs_untranslatable_path_raises():
    with pytest.raises(ValueError):
        launch_argvs(r"\\wsl$\Ubuntu\tmp\x.sh")


def test_launch_argvs_apostrophe_path_raises_to_protect_the_invariant():
    # A sample directory with an apostrophe → shlex.quote
    # would emit '"'"' and inject the double quotes the wsl.exe re-parse
    # mangles. The ladder must refuse rather than emit a broken command; the
    # app falls back to the copyable hint (correct for a single shell).
    with pytest.raises(ValueError):
        launch_argvs(r"C:\Sample Data\O'Brien\Temp\vrm-ask-claude-x.sh")


def test_command_hint_names_the_wsl_path():
    hint = command_hint(r"C:\Temp\vrm-ask-claude-1.sh")
    assert hint == "bash /mnt/c/Temp/vrm-ask-claude-1.sh"


def test_command_hint_is_correct_for_a_single_shell_even_with_apostrophe():
    # The fallback path: shlex.quote's output is exactly right for ONE shell
    # (the human pasting it), so the hint round-trips through shlex.split.
    hint = command_hint(r"C:\Sample Data\O'Brien\Temp\vrm-ask-claude-1.sh")
    assert shlex.split(hint) == [
        "bash", "/mnt/c/Sample Data/O'Brien/Temp/vrm-ask-claude-1.sh"]


def test_prune_old_scripts_drops_stale_keeps_fresh(tmp_path, monkeypatch):
    import tempfile as _tf
    import time as _t
    from vision_resource_monitor import askclaude
    monkeypatch.setattr(_tf, "tempdir", str(tmp_path))
    stale = tmp_path / "vrm-ask-claude-old.sh"
    fresh = tmp_path / "vrm-ask-claude-new.sh"
    other = tmp_path / "someone-elses.sh"
    for p in (stale, fresh, other):
        p.write_text("x")
    old = _t.time() - 7200
    os.utime(stale, (old, old))
    askclaude.prune_old_scripts(max_age_s=3600)
    assert not stale.exists()          # older than the window → pruned
    assert fresh.exists()              # inside the window → kept (may be in use)
    assert other.exists()              # not ours → never touched
    # Never raises even when the temp dir is gone.
    monkeypatch.setattr(_tf, "tempdir", str(tmp_path / "nope"))
    askclaude.prune_old_scripts()


# ── prepare_script (the app's entry) ────────────────────────────────────────────

def test_prepare_script_writes_owner_only_and_carries_the_prompt(tmp_path, monkeypatch):
    import tempfile
    # tempfile caches its dir after first use — patch the cache, not the env.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    path = prepare_script(_stale_high_cost(), feed_path="/tmp/status.json")
    assert path is not None and path.endswith(".sh") and str(tmp_path) in path
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == stat.S_IRWXU
    # LF newlines are load-bearing: a CRLF script is unrunnable by bash. Read
    # BINARY — text mode's universal-newline decode would hide a CRLF even on
    # a Windows run, exactly where write_script's newline="\n" matters.
    raw = open(path, "rb").read()
    assert b"\r" not in raw
    text = raw.decode()
    assert "PID 4242" in text and text.startswith("#!/bin/bash")
    assert "/tmp/status.json" in base64.b64decode(
        re.findall(r"'([A-Za-z0-9+/=]+)'", text)[-1]).decode()


def test_prepare_script_fails_open(monkeypatch):
    from vision_resource_monitor import askclaude

    def boom(_script):
        raise OSError("disk full")
    monkeypatch.setattr(askclaude, "write_script", boom)
    assert prepare_script(_stale_high_cost()) is None
