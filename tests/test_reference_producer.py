"""Small contract tests for the stdlib reference producer."""
import json

from vision_resource_monitor import reference_producer as producer
from vision_resource_monitor import snapshot


def test_meminfo_parser_and_ram_units():
    mem = producer.parse_meminfo(
        "MemTotal:       8192000 kB\nMemAvailable:   2048000 kB\nCached: 1024000 kB\n")
    assert mem == {"MemTotal": 8192000, "MemAvailable": 2048000, "Cached": 1024000}
    ram = producer.ram_reading(mem)
    assert ram == {"used_mib": 6000.0, "total_mib": 8000.0, "cached_mib": 1000.0}


def test_stat_parser_computes_interval_percentages():
    before = producer.parse_cpu_stat("cpu 100 10 20 300 5 2 3 0 0 0\n")
    after = producer.parse_cpu_stat("cpu 120 10 30 330 5 2 3 0 0 0\n")
    result = producer.cpu_reading(before, after, 4)
    assert result["pct"] == 50.0
    assert result["cores_active"] == 2
    assert result["user_pct"] == 33.333333333333336
    assert result["sys_pct"] == 16.666666666666668


def test_nvidia_csv_parser_maps_schema_fields():
    rows = producer.parse_nvidia_smi(
        '"Example GPU", 25, 1024, 8192, 50\n"Other GPU", 0, 0, 4096, 38\n')
    assert rows == [
        {"name": "Example GPU", "util": 25.0, "mem_used_mib": 1024.0,
         "mem_total_mib": 8192.0, "temp_c": 50.0},
        {"name": "Other GPU", "util": 0.0, "mem_used_mib": 0.0,
         "mem_total_mib": 4096.0, "temp_c": 38.0},
    ]


def test_frame_round_trips_and_omits_unavailable_tiers():
    frame = producer.build_frame(now=10, started=5, interval=2, host="test-host",
                                 cpu=None, ram=None, gpus=None,
                                 cpu_history=[], gpu_history=[])
    parsed = snapshot.parse(json.dumps(frame))
    assert parsed.schema == snapshot.SNAPSHOT_SCHEMA
    assert parsed.host_cpu is None and parsed.host_ram is None
    assert parsed.gpus == []
    assert "memory" not in frame


def test_missing_gpu_tool_keeps_frame_valid_without_gpu(monkeypatch, tmp_path):
    monkeypatch.setattr(producer, "read_proc", lambda: ({"user": 1}, {}, 100.0))
    monkeypatch.setattr(producer, "cpu_reading", lambda *args: None)
    monkeypatch.setattr(producer, "ram_reading", lambda mem: None)
    monkeypatch.setattr(producer, "read_gpu",
                        lambda: (_ for _ in ()).throw(FileNotFoundError("missing")))
    monkeypatch.setattr(producer.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(producer.time, "time", lambda: 100.0)
    monkeypatch.setattr(producer.os, "uname", lambda: type("Uname", (), {"nodename": "test"})())
    path = tmp_path / "status.json"
    assert producer.main([str(path), "--once"]) == 0
    parsed = snapshot.load(path)
    assert parsed.gpus == []
    assert parsed.host_cpu is None
    assert parsed.host_ram is None


def test_iowait_counts_as_busy_and_negative_counter_delta_is_rejected():
    before = producer.parse_cpu_stat("cpu 10 0 10 80 0 0 0 0\n")
    after = producer.parse_cpu_stat("cpu 10 0 10 80 80 0 0 0\n")
    result = producer.cpu_reading(before, after, 2)
    assert result["pct"] == 100.0
    assert producer.cpu_reading(after, before, 2) is None


def test_rejects_invalid_gpu_numbers_and_impossible_memory():
    assert producer.parse_nvidia_smi('"GPU", nan, 1, 2, 30\n') == []
    assert producer.parse_nvidia_smi('"GPU", 5, 3, 2, 30\n') == []
    assert producer.ram_reading({"MemTotal": 100, "MemAvailable": 101}) is None


def test_read_gpu_missing_tool_and_timeout_are_reported(monkeypatch):
    monkeypatch.setattr(producer.shutil, "which", lambda name: None)
    try:
        producer.read_gpu()
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing tool must be reported")
    monkeypatch.setattr(producer.shutil, "which", lambda name: "/fake/nvidia-smi")
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 3.0
        raise producer.subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(producer.subprocess, "run", timeout)
    try:
        producer.read_gpu()
    except producer.subprocess.TimeoutExpired:
        pass
    else:
        raise AssertionError("timeout must be reported")


def test_once_emits_valid_cpu_ram_gpu_tiers(monkeypatch, tmp_path):
    frames = iter([
        ({"user": 10, "nice": 0, "system": 10, "idle": 80}, {}, 100.0),
        ({"user": 20, "nice": 0, "system": 20, "idle": 160},
         {"MemTotal": 8192000, "MemAvailable": 2048000, "Cached": 1024000}, 100.1),
    ])
    monkeypatch.setattr(producer, "read_proc", lambda: next(frames))
    monkeypatch.setattr(producer, "read_gpu", lambda: [{"name": "GPU", "util": 25,
                        "mem_used_mib": 10, "mem_total_mib": 100, "temp_c": 40}])
    monkeypatch.setattr(producer.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(producer.time, "time", lambda: 100.1)
    monkeypatch.setattr(producer.os, "uname", lambda: type("Uname", (), {"nodename": "test"})())
    path = tmp_path / "status.json"
    assert producer.main([str(path), "--once"]) == 0
    parsed = snapshot.load(path)
    assert parsed.host_cpu.pct == 20.0
    assert parsed.host_ram.total_mib == 8000.0
    assert parsed.gpu.util == 25.0


def test_loop_warns_once_recovers_clears_gpu_history_and_handles_ctrl_c(
        monkeypatch, tmp_path, capsys):
    frame = ({"user": 1, "nice": 0, "system": 1, "idle": 8},
             {"MemTotal": 1000, "MemAvailable": 500}, 10.0)
    monkeypatch.setattr(producer, "read_proc", lambda: frame)
    monkeypatch.setattr(producer, "cpu_reading", lambda *args: {"pct": 10})
    outcomes = iter([FileNotFoundError("gone"), FileNotFoundError("gone"),
                     [{"name": "GPU", "util": 25, "mem_used_mib": 1,
                       "mem_total_mib": 2, "temp_c": 30}]])
    def gpu():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
    monkeypatch.setattr(producer, "read_gpu", gpu)
    sleeps = iter([None, None, None, KeyboardInterrupt()])
    def sleep(_seconds):
        outcome = next(sleeps)
        if isinstance(outcome, BaseException):
            raise outcome
    monkeypatch.setattr(producer.time, "sleep", sleep)
    monkeypatch.setattr(producer.time, "time", lambda: 10.0)
    monkeypatch.setattr(producer.os, "uname", lambda: type("Uname", (), {"nodename": "test"})())
    path = tmp_path / "status.json"
    assert producer.main([str(path), "--interval", "0.1"]) == 0
    captured = capsys.readouterr()
    assert captured.err.count("warning: GPU data unavailable") == 1
    assert captured.err.count("GPU data available again") == 1
    frame = json.loads(path.read_text())
    assert frame["gpu_util_history"] == [25]


def test_missing_proc_fails_loudly(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(producer, "read_proc",
                        lambda: (_ for _ in ()).throw(RuntimeError("Linux /proc unavailable")))
    assert producer.main([str(tmp_path / "status.json"), "--once"]) == 1
    assert "Linux /proc unavailable" in capsys.readouterr().err


def test_interval_must_be_finite(monkeypatch):
    for interval in ("nan", "inf"):
        try:
            producer.main(["unused.json", "--interval", interval])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("nonfinite interval must be rejected")


def test_nvidia_power_parsed_and_na_omitted():
    rows = producer.parse_nvidia_smi('"A", 1, 2, 3, 4, 120.5, 300\n"B", 1, 2, 3, 4, [N/A], [N/A]\n')
    assert rows[0]["power_w"] == 120.5 and rows[0]["power_limit_w"] == 300.0
    assert "power_w" not in rows[1] and "power_limit_w" not in rows[1]


def test_read_gpu_rejects_a_dropped_row(monkeypatch):
    """One unreadable device row must fail the whole sample, not promote the next GPU."""
    monkeypatch.setattr(producer.shutil, "which", lambda name: "/fake/nvidia-smi")
    text = '"A", [N/A], 2, 3, 4, 10, 20\n"B", 1, 2, 3, 4, 10, 20\n'
    monkeypatch.setattr(producer.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": text})())
    try:
        producer.read_gpu()
    except RuntimeError:
        pass
    else:
        raise AssertionError("a dropped GPU row must reject the whole sample")
