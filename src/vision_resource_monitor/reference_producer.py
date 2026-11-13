"""Small Linux reference producer for ``monitor-snapshot:v1``.

Emits the envelope, NVIDIA GPU readings when available, aggregate Linux CPU
and RAM readings, and bounded success-only CPU/GPU utilization histories.
GPU history is cleared after a missing sample and omitted during the gap.
``uptime_s`` is Linux boot uptime, taken from /proc/uptime. It deliberately
omits ``memory``: the consumer's MemoryPanel describes per-process attribution,
containers, and disk usage, which these aggregate proc files cannot provide.
Also omitted: activity, run history, open_run_t, docker, ollama, Windows CPU,
Windows CPU history, processes/sessions, forecast/contributors, stuck,
GPU users, scanners, security stack, egress, runner fleet, hosted bursts,
spawns, self-cost fields, ports, proc_ports, and runs. This producer reports
missing sources by omitting their tiers; it never substitutes a measured zero.
``cores_active`` is rounded from aggregate busy core-equivalents, since /proc's
aggregate line cannot identify an exact count of individually busy cores.

    python -m vision_resource_monitor.reference_producer /tmp/status.json
"""
from __future__ import annotations

import argparse
import csv
import io
import math
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from .fake_producer import write_atomic
from .snapshot import SNAPSHOT_SCHEMA

GPU_QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
GPU_HISTORY_LIMIT = 60
CPU_HISTORY_LIMIT = 60


def parse_meminfo(text: str) -> dict[str, int]:
    """Return numeric proc meminfo values in KiB, ignoring malformed rows."""
    values = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0].rstrip(":")] = int(parts[1])
    return values


def parse_cpu_stat(text: str) -> dict[str, int]:
    """Parse aggregate /proc/stat CPU counters (jiffies)."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            try:
                values = [int(value) for value in parts[1:]]
            except ValueError:
                return {}
            if len(values) < 4:
                return {}
            keys = ("user", "nice", "system", "idle", "iowait", "irq",
                    "softirq", "steal", "guest", "guest_nice")
            return dict(zip(keys, values))
    return {}


def cpu_reading(previous: dict[str, int], current: dict[str, int],
                cores_total: int) -> dict[str, float | int] | None:
    """Compute interval CPU fractions; None means counters were unusable."""
    keys = set(previous) & set(current)
    if not {"user", "nice", "system", "idle"}.issubset(keys):
        return None
    delta = {key: current[key] - previous[key] for key in keys}
    if any(value < 0 for value in delta.values()):
        return None
    total = sum(delta[key] for key in
                ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
                if key in delta)
    # Linux's consumer-side contract includes iowait in pct: only idle is
    # subtracted, so an I/O-waiting CPU remains busy from the host's view.
    idle = delta["idle"]
    if total <= 0 or idle > total:
        return None
    busy = total - idle
    user = delta["user"] + delta["nice"]
    system = delta["system"] + delta.get("irq", 0) + delta.get("softirq", 0)
    pct = busy * 100.0 / total
    return {
        "pct": pct,
        # Rounded active core-equivalents inferred from aggregate busy time.
        "cores_active": round(pct * cores_total / 100.0),
        "cores_total": cores_total,
        "user_pct": user * 100.0 / total,
        "sys_pct": system * 100.0 / total,
    }


def parse_nvidia_smi(text: str) -> list[dict]:
    """Parse the selected nvidia-smi CSV columns into schema GPU objects."""
    rows = []
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        if len(row) < 5:
            continue
        try:
            util, used, total, temp = (float(row[i].strip()) for i in range(1, 5))
        except ValueError:
            continue
        if (not all(map(lambda n: math.isfinite(n) and n >= 0,
                        (util, used, total, temp))) or util > 100 or used > total):
            continue
        gpu = {"name": row[0].strip(), "util": util,
               "mem_used_mib": used, "mem_total_mib": total, "temp_c": temp}
        # Power may read "[N/A]"; omit the key then rather than invent a 0 W.
        for key, i in (("power_w", 5), ("power_limit_w", 6)):
            try:
                watts = float(row[i].strip())
            except (IndexError, ValueError):
                continue
            if math.isfinite(watts) and watts >= 0:
                gpu[key] = watts
        rows.append(gpu)
    return rows


def read_gpu(timeout: float = 3.0) -> list[dict]:
    """Read GPU state; missing tool and command errors are reported to caller."""
    binary = shutil.which("nvidia-smi")
    if not binary:
        raise FileNotFoundError("nvidia-smi is not installed")
    result = subprocess.run(
        [binary, f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=timeout, check=True)
    rows = parse_nvidia_smi(result.stdout)
    wanted = sum(1 for line in result.stdout.splitlines() if line.strip())
    if not rows or len(rows) != wanted:
        # A dropped row would silently change which GPU is primary.
        raise RuntimeError("nvidia-smi returned an unreadable GPU row")
    return rows


def read_proc(proc_root: Path = Path("/proc")) -> tuple[dict, dict, float]:
    """Read Linux CPU, memory and uptime sources; raise if required data absent."""
    try:
        stat_text = (proc_root / "stat").read_text(encoding="ascii")
        mem_text = (proc_root / "meminfo").read_text(encoding="ascii")
        uptime_text = (proc_root / "uptime").read_text(encoding="ascii")
    except OSError as exc:
        raise RuntimeError(f"Linux /proc data unavailable: {exc}") from exc
    stats = parse_cpu_stat(stat_text)
    mem = parse_meminfo(mem_text)
    try:
        uptime = float(uptime_text.split()[0])
    except (ValueError, IndexError):
        raise RuntimeError("Linux /proc/uptime has no valid uptime value")
    if (not stats or "MemTotal" not in mem or "MemAvailable" not in mem
            or not math.isfinite(uptime) or uptime < 0):
        raise RuntimeError("Linux /proc is missing required CPU or memory fields")
    return stats, mem, uptime


def ram_reading(mem: dict[str, int]) -> dict[str, float] | None:
    total = mem.get("MemTotal")
    available = mem.get("MemAvailable")
    if (total is None or available is None or total <= 0 or available < 0
            or available > total):
        return None
    result = {"used_mib": (total - available) / 1024.0,
              "total_mib": total / 1024.0}
    cached = mem.get("Cached")
    if cached is not None:
        result["cached_mib"] = cached / 1024.0
    return result


def build_frame(*, now: float, started: float, interval: float, host: str,
                cpu: dict | None, ram: dict | None, gpus: list[dict] | None,
                cpu_history: list[float], gpu_history: list[float] | None) -> dict:
    frame = {
        "schema": SNAPSHOT_SCHEMA,
        "t": now,
        "ts": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        "host": host,
        "interval": interval,
        "uptime_s": max(0.0, now - started),
        "cpu_history": cpu_history[-CPU_HISTORY_LIMIT:],
        **({"gpu_util_history": gpu_history[-GPU_HISTORY_LIMIT:]}
           if gpu_history is not None else {}),
    }
    if cpu is not None:
        frame["host_cpu"] = cpu
    if ram is not None:
        frame["host_ram"] = ram
    if gpus is not None:
        frame["gpus"] = gpus
    return frame


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Write Linux resource snapshots.")
    parser.add_argument("path", type=Path, help="Snapshot JSON output path")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true", help="Write one frame and exit")
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval must be greater than zero")

    # SIGTERM -> SystemExit so write_atomic removes its temp file and
    # subprocess.run kills a hung nvidia-smi instead of orphaning it.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        previous, _, _ = read_proc()
        return _run(args, previous)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


def _run(args, previous: dict[str, int]) -> int:
    cpu_values: deque[float] = deque(maxlen=CPU_HISTORY_LIMIT)
    gpu_values: deque[float] = deque(maxlen=GPU_HISTORY_LIMIT)
    gpu_missing = None
    while True:
        if args.once:
            time.sleep(0.1)
        else:
            time.sleep(args.interval)
        try:
            current, mem, uptime = read_proc()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        reading = cpu_reading(previous, current, os.cpu_count() or 1)
        previous = current
        cpu = reading
        ram = ram_reading(mem)
        if cpu is not None:
            cpu_values.append(cpu["pct"])
        try:
            gpus = read_gpu()
            if gpu_missing is True:
                print("GPU data available again", file=sys.stderr)
            gpu_missing = False
            gpu_values.append(gpus[0]["util"])
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            gpus = None
            gpu_values.clear()
            if gpu_missing is not True:
                print(f"warning: GPU data unavailable ({exc})", file=sys.stderr)
                gpu_missing = True
        now = time.time()
        frame = build_frame(now=now, started=now - uptime, interval=args.interval,
                            host=os.uname().nodename, cpu=cpu, ram=ram, gpus=gpus,
                            cpu_history=list(cpu_values),
                            gpu_history=list(gpu_values) if gpus is not None else None)
        try:
            write_atomic(args.path, frame)
        except OSError as exc:
            print(f"could not write snapshot: {exc}", file=sys.stderr)
            return 1
        if args.once:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
