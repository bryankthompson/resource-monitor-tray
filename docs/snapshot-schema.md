# `monitor-snapshot:v1`

This is the JSON input contract consumed by `vision_resource_monitor.snapshot.parse`.
The parser coerces individual bad or missing values to dataclass defaults; it
rejects invalid JSON, non-object JSON, and a non-empty unsupported `schema`.
Unknown keys are ignored. Producers should send honest measurements and omit a
tier they could not collect; do not encode unavailable data as a measured zero.

## Envelope

| Key | JSON type | Default if omitted | Meaning |
|---|---|---|---|
| `schema` | string | `monitor-snapshot:v1` | Format identifier. |
| `t` | number | `0` | Timestamp; unit and epoch are unspecified by the consumer. |
| `ts` | string | empty | Timestamp text; format unspecified. |
| `host` | string | empty | Host label; exact naming convention unspecified. |
| `interval` | number | `2` | Expected sample interval, seconds. |
| `uptime_s` | number | `0` | Seconds; what the uptime measures is unspecified by the consumer. |

These envelope values are useful to the renderer, but the parser does not
require any particular key: a JSON object with no keys parses as a default
snapshot. Producers should send all six envelope keys. There is no schema-level
minimum set that makes the renderer paint a particular panel; each panel needs
its own tier. Missing optional object tiers and `null` become `None` (no data).
List-valued tiers with a default empty list become `[]`, which means no rows or
history samples; where a list tier is optional, absent, `null`, or a non-list
becomes `None`. These distinctions are called out below where they matter.

## Top-level tiers

The dataclass defaults below describe parser output, not proof that a producer
measured a zero. For the full accepted model fields, see each tier table.

| Key | JSON shape | Default / absence convention | Meaning |
|---|---|---|---|
| `gpus` | array of GPU objects | `[]` | GPU readings; `[]` means no reported GPUs. |
| `gpu_util_history` | array of numbers | `[]` | GPU utilization samples, percent. |
| `activity` | object or null | `None` | Current GPU activity, if classified. |
| `history` | array of run objects | `[]` | Completed GPU runs. |
| `open_run_t` | number or null | `None` | Open-run timestamp; unit and epoch are unspecified by the consumer. |
| `docker` | object or null | `None` | Docker status. |
| `ollama` | array or null | `None` | `[]` means collection ran and found no models; null/absent means no data. |
| `host_cpu` | object or null | `None` | Host CPU reading. |
| `windows_cpu` | object or null | `None` | Whole physical host CPU reading. |
| `cpu_history` | array of numbers | `[]` | Host CPU utilization samples, percent. |
| `windows_cpu_history` | array of numbers | `[]` | Whole-host CPU utility samples, percent. |
| `host_ram` | object or null | `None` | Host RAM reading. |
| `memory` | object or null | `None` | Process and disk memory attribution. |
| `processes` | object or null | `None` | Sessions, process load, swap and alerts. |
| `forecast` | object or null | `None` | Memory pressure forecast. |
| `contributors` | array | `[]` | Ranked forecast contributors; empty means none supplied. |
| `stuck` | object or null | `None` | Run-queue and stuck-process reading. |
| `gpu_users` | array or null | `None` | Optional VRAM-holder census; `[]` means answered, none found. |
| `scanners` | array or null | `None` | Optional scanner census; `[]` means answered, none found. |
| `security_stack` | object or null | `None` | Security-related CPU/spawn summary. |
| `egress` | object or null | `None` | Network egress summary. |
| `runner_fleet` | object or null | `None` | Self-hosted runner census. |
| `hosted_bursts` | object or null | `None` | Optional hosted-run watch. |
| `spawns` | object or null | `None` | Process-spawn rate summary. |
| `self_cost` | object or null | `None` | Producer CPU cost summary. |
| `self_cost_history` | array of numbers | `[]` | Producer CPU cost samples, percent. |
| `ports` | array | `[]` | Watched port readings; empty means no rows supplied. |
| `proc_ports` | array or null | `None` | Per-process port answers; null means no target answer. |
| `runs` | array | `[]` | Progress records. |
| `source_version` | string or null | `None` | Optional source revision. |
| `log_path` | string | empty | Activity log path, if any. |
| `log_status` | string | empty | Activity log status label. |

## Tier fields

Types are JSON types; `number` maps to Python float and `integer` to int in the
consumer. A named record type (such as `Gpu` or `PortConn`) means a JSON object;
`array of T` means a JSON array whose elements have type T. `?` and empty-string
defaults are literal dataclass defaults. A `nullable` field accepts null and
parses to `None`. Meanings not specified by the consumer are marked
**unspecified**.

### GPU and run history

| Object | Fields (`name: type [unit; default; meaning]`) |
|---|---|
| `Gpu` | `name: string [—; "?"; device label]`; `util: number [percent; 0; utilization]`; `mem_used_mib: number [MiB; 0; used VRAM]`; `mem_total_mib: number [MiB; 0; total VRAM]`; `temp_c: number [°C; 0; temperature]`; `power_w: number [W; 0; power draw]`; `power_limit_w: number [W; 0; power limit]` |
| `Activity` | `start_t: number [timestamp unit/epoch unspecified; 0; activity start]`; `peak_util: number [percent; 0; peak utilization]`; `peak_vram_mib: number [MiB; 0; peak VRAM use]`; `vram_total_mib: number [MiB; 0; total VRAM]`; `workloads: array of strings [—; []; workload labels]` |
| `RunRecord` | `start_t`, `end_t: numbers [timestamp unit/epoch unspecified; 0; run boundaries]`; `duration_s: number [seconds; 0; duration]`; `peak_util: number [percent; 0; peak utilization]`; `peak_vram_mib`, `vram_total_mib: numbers [MiB; 0; peak/total VRAM]`; `workloads: array of strings [—; []; workload labels]` |
| `GpuUser` | `pid: integer [—; 0; process ID]`; `name: string [—; "?"; process label]`; `vram_mib: number [MiB; 0; attributed VRAM]` |

### Host CPU, RAM, and memory

| Object | Fields (`name: type [unit; default; meaning]`) |
|---|---|
| `CpuStat` | `pct: number [percent; 0; CPU busy, including iowait]`; `cores_active`, `cores_total: integers [cores; 0; active/total cores]`; `speed_mhz: nullable number [MHz; null; clock speed]`; `user_pct`, `sys_pct: nullable numbers [percent; null; user/system share]` |
| `WindowsCpu` | `utility_pct`, `time_pct: nullable number [percent; null; frequency-aware utility / busy time]`; `cores: nullable integer [cores; null; logical processor count]` |
| `RamStat` | `used_mib`, `total_mib`, `cached_mib: number [MiB; 0; used, total and cached RAM]` |
| `MemoryPanel` | `windows: nullable HostMemory [null; host-attributed memory]`; `wsl: array of ProcRow [ []; rows]`; `containers: array of ContainerMem [ []; rows]`; `disk: array of DiskRow [ []; rows]` |
| `HostMemory` | `used_mib`, `total_mib: number [MiB; 0; memory used/total]`; `processes: array of ProcRow [ []; process rows]` |
| `ProcRow` | `name: string [—; "?"; process label]`; `mem_mib: number [MiB; 0; memory]`; `llm`, `is_vmmem: boolean [—; false; category flags]` |
| `ContainerMem` | `name: string [—; "?"; container label]`; `mem_mib: number [MiB; 0; memory]`; `resident_mib: nullable number [MiB; null; resident memory]` |
| `DiskRow` | `type: string [—; "?"; disk category]`; `size_mib`, `reclaimable_mib: number [MiB; 0; total/reclaimable size]` |

### Processes, sessions, forecast, and stuck work

| Object | Fields (`name: type [unit; default; meaning]`) |
|---|---|
| `Processes` | `node_total`, `claude_total: integer [count; 0; process counts]`; `sessions: array of Session [ []; sessions]`; `high_cost: array of HighCost [ []; high-cost processes]`; `load: nullable array of numbers [unspecified; null; load values]`; `ncpu: nullable integer [cores; null; CPU count]`; `swap: nullable Swap [null; swap summary]`; `pressure: nullable string [—; null; pressure label]`; `alerts: array of strings [ []; messages]` |
| `Session` | `pid: integer [—; 0; process ID]`; `label: string [—; "?"; session label]`; `node_count: integer [count; 0; node count]`; `rss_mib: number [MiB; 0; resident memory]`; `age_s`, `last_event_age_s: nullable numbers [seconds; null; session/event age]`; `stale: boolean [—; false; stale flag]`; `swap_mib: nullable number [MiB; null; swap use]`; `idle: nullable boolean [—; null; idle state]`; `child_pids: array of integers [ []; listed descendants; documented producer-side cap 32]`; `child_total: nullable integer [count; null; true descendant count; greater than the listed count means truncation]`; `ports: nullable array of integers [port numbers; null means census unavailable; [] means census ran and found none; documented producer-side port cap 8]`; `port_total: nullable integer [count; null; true port count]`; `cockpit`, `lane`, `role`, `state`, `name`, `domain: nullable strings [—; null; producer labels; role/state accepted values are constrained by parser]` |
| `HighCost` | `pid: integer [—; 0; process ID]`; `comm: string [—; "?"; process name]`; `rss_mib: number [MiB; 0; resident memory]`; `cpu_s: number [seconds; 0; accumulated CPU time]`; `age_s: nullable number [seconds; null; process age]` |
| `Swap` | `used_mib`, `total_mib: number [MiB; 0; swap used/total]`; `pct: nullable number [percent; null; used/total]`; `top: array of SwapHolder [ []; largest holders]` |
| `SwapHolder` | `name: string [—; "?"; process label]`; `pid: integer [—; 0; process ID]`; `swap_mib: number [MiB; 0; private swap]` |
| `Forecast` | `level: string [—; "ok"; level]`; `eta_min: nullable number [minutes; null; projected time to no headroom]`; `signal: nullable string [—; null; leading signal]`; `reason: string [—; empty; explanation]`; `headroom_mib: nullable number [MiB; null; reclaimable headroom]`; `compressed_slope_mib_s: nullable number [MiB/s; null; compression slope]` |
| `Contributor` | `kind: string [—; "?"; cause kind]`; `rank: integer [1-based rank; 0; order]`; `label: string [—; "?"; display label]`; `summary`, `action: strings [—; empty; explanation and suggested action]`; `metric: object [—; {}; kind-specific metrics]` |
| `Stuck` | `procs_running`, `procs_blocked`, `d_count`, `ncpu: nullable integers [counts; null; queue/process counts]`; `wedged: array of StuckWedged [ []; processes]`; `load1_per_core: nullable number [load/core; null; one-minute load per core]`; `cpu_pct: nullable number [percent; null; CPU use]`; `sampler_ms: object [ms values; {}; sampler durations]`; `queue_saturated`, `wait_diverged`, `sampler_slow`, `cpu_saturated: booleans [—; false; condition flags]` |
| `StuckWedged` | `pid: integer [—; 0; process ID]`; `comm: string [—; "?"; process name]`; `for_s: number [seconds; 0; duration wedged]` |

### Docker and Ollama

| Object | Fields (`name: type [unit; default; meaning]`) |
|---|---|
| `DockerStatus` | `running_count`, `unknown_count: integers [count; 0; counts]`; `gpu_containers: array of DockerContainer [ []; GPU containers]` |
| `DockerContainer` | `name`, `image: strings [—; "?"; container name/image]`; `status`, `id: strings [—; empty; status and identifier]` |
| `OllamaModel` | `name: string [—; "?"; model label]`; `size_gb`, `vram_gb: numbers [GB; 0; model size and VRAM]` |

### Ports, egress, scanners, and security

| Object | Fields (`name: type [unit; default; meaning]`) |
|---|---|
| `Port` | `label: string [—; "?"; service label]`; `port: integer [port number; 0; watched port]`; `conns: integer [count; 0; connection count]`; `listening`, `active`, `sustained: booleans [—; false; state flags]`; `owner: nullable string [—; null; owner label]`; `bytes_in_rate`, `bytes_out_rate: nullable numbers [bytes/s; null; traffic rates]`; `active_s: number [seconds; 0; active duration]`; `connections: array of PortConn [ []; details]` |
| `PortConn` | `peer: string [—; "?"; peer endpoint]`; `process: nullable string [—; null; process label]`; `pid: nullable integer [—; null; process ID]`; `bytes_in`, `bytes_out: nullable integers [bytes; null; cumulative traffic]`; `state: string [—; established; connection state]` |
| `ProcPortRecord` | `side: string [—; "?"; source side]`; `pid: integer [—; 0; target process]`; `name`, `note`, `kind: nullable strings [—; null; target metadata]`; `found: nullable boolean [—; null; lookup result]`; `sockets: nullable array of ProcPortSocket [null; null means not measured]`; `sockets_total: nullable integer [count; null; true socket count]`; `matches: nullable array of ProcPortMatch [null; null means not measured]`; `match_total: nullable integer [count; null; true match count]` |
| `ProcPortSocket` | `proto: string [—; tcp; protocol]`; `state`, `local: strings [—; "?"; state/local endpoint]`; `peer: nullable string [—; null; remote endpoint]` |
| `ProcPortMatch` | `pid: integer [—; 0; matched process ID]`; `name: nullable string [—; null; process label]`; `sockets: array of ProcPortSocket [ []; sockets]`; `sockets_total: nullable integer [count; null; total sockets]` |
| `Egress` | `window_s: nullable number [seconds; null; sampling window]`; `total_in_bps`, `total_out_bps: nullable numbers [bytes/s; null; totals]`; `iface: nullable EgressIface [null; interface rates]`; `top: array of EgressTalker [ []; ranked talkers]` |
| `EgressIface` | `in_bps`, `out_bps: nullable numbers [bytes/s; null; rates]`; `out_in_ratio: nullable number [ratio; null; outbound/inbound]` |
| `EgressTalker` | `pid: nullable integer [—; null; process ID]`; `name: string [—; "?"; process label]`; `in_bps`, `out_bps: nullable numbers [bytes/s; null; rates]`; `total_in_mib`, `total_out_mib: nullable numbers [MiB; null; totals]` |
| `Scanner` | `group`, `label: strings [—; "?"; category and process label]`; `procs: array of strings [ []; process labels]`; `proc_count: integer [count; 0; process count]`; `rss_mib: number [MiB; 0; resident memory]`; `cpu_pct: number [percent; 0; CPU share]`; `state: string [—; idle; state]`; `active_s`, `ago_s: nullable numbers [seconds; null; activity timing]`; `burning: boolean [—; false; burn classification]` |
| `SecurityStack` | `cores: nullable number [cores; null; CPU equivalent]`; `hot`, `taxing: booleans [—; false; condition flags]`; `hot_for_s: nullable number [seconds; null; duration hot]`; `spawns_per_min: nullable number [count/min; null; spawn rate]`; `top: array of SecurityStackTop [ []; leading processes]` |
| `SecurityStackTop` | `group`, `label: strings [—; "?"; category and label]`; `cpu_pct: nullable number [percent; null; CPU share]` |

### Runners, spawns, and run progress

| Object | Fields (`name: type [unit; default; meaning]`) |
|---|---|
| `RunnerFleet` | `runners: array of Runner [ []; runner containers]`; `bursts: array of RunnerBurst [ []; recent bursts]`; `total`, `active_count`, `unresolved: integers [count; 0; counts]`; `cpu_pct_total: nullable number [percent; null; total CPU]`; `mem_mib_total: nullable number [MiB; null; total memory]` |
| `Runner` | `name`, `container: strings [—; "?"; runner/container labels]`; `repo`, `up: nullable strings [—; null; repository and uptime text]`; `cpu_pct: nullable number [percent; null; CPU]`; `mem_mib: nullable number [MiB; null; memory]`; `pids`, `pids_floor: nullable integers [count; null; process counts]`; `active: nullable boolean [—; null; active state]` |
| `RunnerBurst` | `runner: string [—; "?"; runner label]`; `repo: nullable string [—; null; repository]`; `state: string [—; "?"; state]`; `age_s`, `dur_s`, `ended_ago_s: nullable numbers [seconds; null; age/duration]`; `start_unseen: boolean [—; false; start event flag]`; `peak_cpu_pct: nullable number [percent; null; peak CPU]`; `peak_mem_mib: nullable number [MiB; null; peak memory]` |
| `HostedBursts` | `ok: nullable boolean [—; null; query status]`; `error: nullable string [—; null; error text]`; `repos: array of strings [ []; repository labels]`; `checked_ago_s: nullable number [seconds; null; last check age]`; `runs: array of HostedRun [ []; hosted runs]`; `unchecked`, `truncated: integers [count; 0; counts]` |
| `HostedRun` | `repo`, `runner: nullable strings [—; null; labels]`; `workflow`, `status`, `where: strings [—; "?"; workflow/state/location]`; `age_s: nullable number [seconds; null; run age]` |
| `Spawns` | `wsl`, `win: SpawnSide [objects; defaults to empty SpawnSide]` |
| `SpawnSide` | `alloc_per_min`, `visible_per_min`, `self_per_min: nullable numbers [count/min; null; allocation/visible/self rates]`; `top: array of SpawnTop [ []; leading processes]`; `status: string [—; empty; collection status]`; `window_s: nullable number [seconds; null; sample window]` |
| `SpawnTop` | `label: string [—; "?"; process label]`; `count: integer [count; 0; count]`; `pid: nullable integer [—; null; process ID]` |
| `SelfCost` | `window_s: number [seconds; 0; sample window]`; `total_pct`, `python_pct`, `spawns_pct: numbers [one-core percent; 0; total/Python/spawn CPU]`; `windows_pct: nullable number [one-core percent; null; host sweep CPU]` |
| `ProgressRun` | `label: string [—; "?"; output label]`; `state: string [—; running; producer-classified state]`; `percent`, `frames: nullable integers [percent/count; null; progress]`; `message`, `end_message: strings [—; empty; status text]`; `last_t: number [timestamp unit/epoch unspecified; 0; last update]`; `started_t: nullable number [timestamp unit/epoch unspecified; null; start]`; `pid: nullable integer [—; null; process ID]` |

## Minimal example

This intentionally reports no measured tiers; it is enough to parse but paints
no metrics. A producer that has measurements should include those tiers.

```json
{"schema":"monitor-snapshot:v1","t":0,"ts":"","host":"demo","interval":2,"uptime_s":0}
```

For a larger synthetic feed with populated tiers, run
`python3 -m vision_resource_monitor.fake_producer PATH --once`.

## Versioning

The consumer accepts `monitor-snapshot:v1` and ignores unknown object keys, so
adding fields is compatible with this parser. It does not promise that every
producer sends every tier, nor does it validate a field's semantic range.
Changing existing field meaning or type requires care; this parser alone does
not define a formal cross-version migration policy.
