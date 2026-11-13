"""Dev-only synthetic snapshot producer.

Writes ``monitor-snapshot:v1`` files on a loop with a scripted idle→active→idle
cycle, so the tray app can be developed and demoed without the real WSL-side
recorder (Phase 0). Pure stdlib; atomic writes via a temp file + ``os.replace``,
exactly as the real producer will. Not shipped in the app's run path.

    python -m vision_resource_monitor.fake_producer /path/to/status.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from .snapshot import SNAPSHOT_SCHEMA


def _build(now: float, start: float, host: str) -> dict:
    elapsed = now - start
    # A slow triangle wave drives a repeating idle→active→idle cycle (~40s).
    phase = (elapsed % 40.0) / 40.0
    util = max(0.0, math.sin(phase * math.pi) * 100.0 - 8.0)
    active = util >= 10.0
    vram_total = 16303.0
    vram_used = 1200.0 + util / 100.0 * 9000.0

    # Synthetic memory-pressure forecast (drives the Forecast panel). Reclaimable
    # headroom dips to ~1G at the cycle peak, so the panel cycles ok → watch →
    # warn (and back); the real producer derives this from vitals history. At warn
    # a worst-first `contributors` list appears, each with a recommended SAFE
    # action — mirroring the producer's own rank_contributors / _contributor_action
    # text (advisory only; nothing acts).
    headroom_mib = (8.0 - 7.0 * math.sin(phase * math.pi)) * 1024.0
    comp_slope = max(0.0, math.sin(phase * math.pi)) * 3.0        # MiB/s
    if headroom_mib < 2048.0:
        fc_level = "warn"
    elif headroom_mib < 4096.0:
        fc_level = "watch"
    else:
        fc_level = "ok"
    mib_per_min = comp_slope * 60.0
    eta_min = (round(headroom_mib / mib_per_min, 1)
               if fc_level != "ok" and mib_per_min > 0 else None)
    fc_reason = {
        "warn": "reclaimable headroom nearly exhausted and shrinking",
        "watch": "reclaimable headroom shrinking",
        "ok": "",
    }[fc_level]
    contributors = []
    if fc_level == "warn":
        eta_txt = f"~{eta_min:.0f} min" if eta_min is not None else "soon"
        when = f" (~{eta_min:.0f} min of headroom left)" if eta_min is not None else ""
        contributors = [
            {"kind": "memory_overcommit", "rank": 1, "label": "memory overcommit",
             "summary": f"reclaimable headroom {headroom_mib/1024:.1f}G, exhausted {eta_txt}",
             "action": (f"reclaim RAM now{when} — reduce the contributors below "
                        f"before the compressor/swap engage"),
             "metric": {"eta_min": eta_min, "headroom_mib": round(headroom_mib, 1)}},
            {"kind": "stale_high_cost", "rank": 2, "label": "stale high-cost process",
             "summary": "1 stale high-cost process (Code Helper (Renderer))",
             "action": ("restart or close Code Helper (Renderer) [PID 8123] to "
                        "reclaim ~2.6G — it's been idle-heavy for hours"),
             "metric": {"count": 1, "top_comm": "Code Helper (Renderer)",
                        "top_pid": 8123, "top_rss_mib": 2662.0}},
            {"kind": "docker_idle_burn", "rank": 3, "label": "Docker idle-burn",
             "summary": "Docker VM burning 47% CPU while 2 container(s) idle",
             "action": ("quit Docker Desktop, or cap its VM (Settings → Resources) "
                        "— the VM is burning CPU while its containers idle"),
             "metric": {"vm_cpu_pct": 47.0, "container_count": 2}},
        ]

    snap = {
        "schema": SNAPSHOT_SCHEMA,
        "t": now,
        "ts": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "host": host,
        "interval": 2.0,
        "uptime_s": elapsed,
        "gpus": [{
            "name": "NVIDIA GeForce RTX 5080 (fake)",
            "util": util,
            "mem_used_mib": vram_used,
            "mem_total_mib": vram_total,
            "temp_c": 45.0 + util / 100.0 * 30.0,
            "power_w": 60.0 + util / 100.0 * 280.0,
            "power_limit_w": 360.0,
        }],
        "gpu_util_history": [
            max(0.0, math.sin(((elapsed - i * 2.0) % 40.0) / 40.0 * math.pi) * 100.0 - 8.0)
            for i in range(24, 0, -1)
        ],
        "host_cpu": {"pct": 15.0 + util / 5.0, "cores_active": 6,
                     "cores_total": 28, "speed_mhz": 4700.0,
 # user-heavy fake load, sys floor
                     "user_pct": round(10.0 + util / 6.0, 1),
                     "sys_pct": 4.0},
        # The whole-box Windows strain (WCPU): utility runs above the busy
        # fraction, as a turbo box really reads.
        "windows_cpu": {"utility_pct": round(20.0 + util / 4.0, 1),
                        "time_pct": round(14.0 + util / 6.0, 1)},
        "cpu_history": [15.0 + i % 12 for i in range(24)],
        "host_ram": {"used_mib": 16000.0, "total_mib": 31900.0, "cached_mib": 9000.0},
        "processes": {
            "node_total": 96, "claude_total": 15,
            "sessions": [
                # child_pids/child_total: the child-pid index (the
                # Sessions filter matches these; 5001 is deliberately
                # truncated — total 14 > 12 listed — so the capped-index
                # honesty note is exercisable on the fake feed too).
                {"pid": 5001, "label": "build-pipeline · task-0", "node_count": 12,
                 "rss_mib": 3200.0, "age_s": 5400.0, "stale": False,
                 "child_pids": [7002, 7001, 7003, 7004, 7005, 7006, 7007,
                                7008, 7009, 7010, 7011, 7012],
                 "child_total": 14,
                 # ports/port_total: the session-ports census;
                 # 5001 exercises the +N note (4 total, 3 listed), 5002 the
                 # ran-but-owns-nothing []; 5003/5004 predate the fields
                 # (None) — all three states on the fake feed.
                 "ports": [5173, 8080, 9000, 9001], "port_total": 4},
                {"pid": 5002, "label": "interactive · demo-app",
                 "node_count": 9, "rss_mib": 2100.0, "age_s": 46800.0, "stale": True,
                 "child_pids": [7101, 7102], "child_total": 2,
                 "ports": [], "port_total": 0},
                {"pid": 5003, "label": "triage-issues", "node_count": 6,
                 "rss_mib": 1400.0, "age_s": 1200.0, "stale": False},
                {"pid": 5004, "label": "interactive", "node_count": 3,
                 "rss_mib": 640.0, "age_s": 300.0, "stale": False},
                {"pid": 5005, "label": "home", "node_count": 2, "rss_mib": 500.0,
                 "age_s": 240.0, "cockpit": "demo-cockpit", "role": "cockpit",
                 "lane": None, "state": "idle", "name": "demo-cockpit-cockpit",
                 "domain": "lead"},
                {"pid": 5006, "label": "home", "node_count": 2, "rss_mib": 450.0,
                 "age_s": 200.0, "cockpit": "demo-cockpit", "lane": "frontend",
                 "role": "lane", "state": "busy", "name": "frontend",
                 "domain": "dev", "child_pids": [7101, 7102],
                 "ports": [5173, 8080, 9000, 9001], "port_total": 4},
                {"pid": 5015, "label": "home", "node_count": 1, "rss_mib": 20.0,
                 "age_s": 3120.0, "cockpit": "demo-cockpit", "lane": "frontend",
                 "role": "lane", "state": "idle", "name": "frontend", "domain": "dev",
                 "last_event_age_s": 3120.0, "swap_mib": 24.5, "idle": True},
                {"pid": 5007, "label": "home", "node_count": 1, "rss_mib": 300.0,
                 "age_s": 100.0, "cockpit": "demo-cockpit-two", "lane": "dev",
                 "role": "lane", "state": "waiting", "name": "dev", "domain": "dev"},
                {"pid": 5008, "label": "home", "node_count": 1, "rss_mib": 200.0,
                 "age_s": 90.0, "cockpit": "other-cockpit", "lane": "solo",
                 "role": "lane", "state": "busy", "name": "solo", "domain": "dev"},
                {"pid": 5009, "label": "home", "node_count": 1, "rss_mib": 100.0,
                 "age_s": 70.0, "cockpit": "demo-cockpit", "role": "advisor",
                 "state": "idle", "name": "demo-cockpit-advisor", "domain": "advisor",
                 "stale": True},
                {"pid": 5010, "label": "home", "node_count": 1, "rss_mib": 90.0,
                 "age_s": 65.0, "cockpit": "demo-cockpit", "role": "drc",
                 "state": "waiting", "name": "demo-cockpit-drc", "domain": "review"},
                {"pid": 5011, "label": "home", "node_count": 1, "rss_mib": 80.0,
                 "age_s": 60.0, "cockpit": "demo-cockpit", "role": "router",
                 "state": "shell", "name": "demo-cockpit-router", "domain": "router"},
                {"pid": 5012, "label": "free session", "node_count": 0, "rss_mib": 40.0,
                 "age_s": 30.0, "cockpit": None, "lane": None, "role": None,
                 "state": None, "name": None, "domain": None},
                {"pid": 5013, "label": "free session two", "node_count": 0, "rss_mib": 35.0,
                 "age_s": 25.0},
                {"pid": 5014, "label": "free session three", "node_count": 0, "rss_mib": 30.0,
                 "age_s": 20.0}],
            "high_cost": [
                {"pid": 8123, "comm": "Code Helper (Renderer)", "rss_mib": 2662.0,
                 "cpu_s": 5400.0, "age_s": 46800.0}],
            "load": [4.2, 3.1, 2.4], "ncpu": 28,
            "swap": {"used_mib": 400.0, "total_mib": 8192.0},
            "pressure": "normal",
            "alerts": ["1 session(s) older than 12h"],
        },
        "forecast": {
            "level": fc_level, "eta_min": eta_min, "signal": "reclaimable headroom",
            "reason": fc_reason, "headroom_mib": round(headroom_mib, 1),
            "compressed_slope_mib_s": round(comp_slope, 2),
        },
        "contributors": contributors,
        # Exec-pressure spawn census: ok sides so the fake
        # sessions exercise the Scanners headline's live render, not only its
        # stated causes.
        "spawns": {
            "wsl": {"alloc_per_min": round(420.0 + 300.0 * util / 100.0, 1),
                    "visible_per_min": 40.0 + (8.0 if active else 0.0),
                    "self_per_min": 160.0,
                    "top": [{"label": "systemd-udevd", "count": 41,
                             "pid": None},
                            {"label": "python", "count": 5, "pid": 4242},
                            {"label": "bash", "count": 2, "pid": None}],
                    "status": "ok", "window_s": 60.0},
            "win": {"visible_per_min": 6.0,
                    "top": [{"label": "conhost", "count": 1}],
                    "status": "ok", "window_s": 10.0}},
        # The producer's own bill: the
        # windows line dominates on the real box, so the fake mirrors that.
        "self_cost": {"window_s": 60.0,
                      "total_pct": round(14.2 + 2.0 * util / 100.0, 2),
                      "python_pct": 0.3, "spawns_pct": 1.4,
                      "windows_pct": round(12.5 + 2.0 * util / 100.0, 2)},
        # Its 1/min trend — a gentle idle→active drift like the real box's.
        "self_cost_history": [round(9.0 + (i % 7) * 0.4 + (2.0 if i > 20 else 0.0), 2)
                              for i in range(30)],
        "ports": [
            {"label": "web-app", "port": 8080, "conns": 3 if active else 0,
             "listening": True, "owner": "node",
             "bytes_in_rate": 4200.0 * util / 100.0 if active else 0.0,
             "bytes_out_rate": 15000.0 * util / 100.0 if active else 0.0,
             "active": bool(active), "active_s": (elapsed % 40.0) if active else 0.0,
             "sustained": False,
             # Per-connection detail: 2 detail rows against conns=3
             # demos the "+1 more" remainder; the ownerless row demos a far end
             # the producer couldn't attribute (other user / remote).
             "connections": [
                 {"peer": "127.0.0.1:55868", "process": "MainThread", "pid": 4711,
                  "bytes_in": 44_191_737, "bytes_out": 870_596,
                  "state": "established"},
                 {"peer": "127.0.0.1:55882", "process": None, "pid": None,
                  "bytes_in": 57_181_251, "bytes_out": 19_709_681,
                  "state": "established"},
             ] if active else []},
            {"label": "tunnel-ssh", "port": 2222, "conns": 1, "listening": True,
             "owner": "sshd", "bytes_in_rate": 120.0, "bytes_out_rate": 340.0,
             "active": False, "active_s": 0.0, "sustained": False,
             "connections": [
                 {"peer": "127.0.0.1:50504", "process": "tunnel", "pid": 313,
                  "bytes_in": 2_823_778, "bytes_out": 5_037_332,
                  "state": "established"},
             ]},
        ],
        "memory": {
            "windows": {"used_mib": 30500.0, "total_mib": 65200.0, "processes": [
                {"name": "vmmemWSL", "mem_mib": 8900.0, "is_vmmem": True},
                {"name": "Memory Compression", "mem_mib": 2900.0},
                {"name": "llm-server", "mem_mib": 412.0, "llm": True},
                {"name": "chrome", "mem_mib": 5600.0}]},
            "wsl": [{"name": "MainThread", "mem_mib": 6400.0},
                    {"name": "python", "mem_mib": 950.0}],
            "containers": [
                {"name": "vector-db-tool", "mem_mib": 3500.0, "resident_mib": 3500.0},
                {"name": "vector-db", "mem_mib": 2500.0, "resident_mib": 178.0}],
            "disk": [
                {"type": "Images", "size_mib": 56000.0, "reclaimable_mib": 900.0},
                {"type": "Build Cache", "size_mib": 38000.0, "reclaimable_mib": 38000.0}],
        },
        "history": [{
            "start_t": now - 900, "end_t": now - 540, "duration_s": 360,
            "peak_util": 92, "peak_vram_mib": 10900, "vram_total_mib": vram_total,
            "workloads": ["batch-job"],
        }],
        "runs": [{
            "label": "job-output.json", "state": "running" if active else "ok",
            "percent": int(util) if active else None,
            "message": "Processing samples" if active else "",
            "last_t": now, "started_t": start, "pid": os.getpid(),
            "end_message": "",
        }],
        "log_path": "gpu-activity.jsonl",
        "log_status": "fake-producer",
    }
    if active:
        snap["activity"] = {
            "start_t": now - (elapsed % 40.0),
            "peak_util": util, "peak_vram_mib": vram_used,
            "vram_total_mib": vram_total,
            "workloads": ["batch-job", "llm:7b-model"],
        }
    return snap


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Write synthetic monitor snapshots on a loop.")
    p.add_argument("path", type=Path, help="Snapshot file to write")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--once", action="store_true", help="Write one frame and exit")
    args = p.parse_args(argv)

    # time.time() is fine here — this is a dev tool, not the resume-sensitive engine.
    start = time.time()
    host = "demo-host"   # never the real hostname: the feed is shown in screenshots
    try:
        while True:
            now = time.time()
            write_atomic(args.path, _build(now, start, host))
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
