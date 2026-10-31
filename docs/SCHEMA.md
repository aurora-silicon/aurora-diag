# JSON formats

Every document an agent parses carries `schema` and `version`. A reader should check both: the version goes up only when a field changes meaning or goes away. New fields can appear in any version.

## check (`aurora-diag check --json`)

    {
      "schema": "aurora-diag/check", "version": 1,
      "generated": "2026-10-30T11:44:00Z", "host": "mbp", "kernel": "7.3.0-aurora-fleet10",
      "baseline": "/path/good.json" | null,
      "result": "pass" | "warn" | "fail", "exit": 0 | 1 | 2,
      "counts": {"pass": 65, "warn": 2, "fail": 0, "skip": 10},
      "checks": [
        {"group": "subsys" | "service" | "kernel" | "sensors" | "gpu" | "ane",
         "name": "SEP (Secure Enclave, Touch ID) sep@406600000",
         "level": "pass" | "warn" | "fail" | "skip",
         "reason": "bound to apple_sep",
         "node": "sep@406600000",          (subsys only: DT path, stable across boots)
         "infra": false,                   (subsys only: DARTs, mailboxes, buses)
         "new": [...], "missing": [...]}   (kernel and sensors rows with a baseline)
      ],
      "elapsed_s": 0.5
    }

## snapshot (`aurora-diag snapshot`)

    {
      "schema": "aurora-diag/snapshot", "version": 1, "tool_version": "0.2.0",
      "generated", "host",
      "machine": {"model", "compatible": [...]},
      "kernel": {"release", "version", "cmdline"},
      "firmware": {"asahi,system-fw-version": "27.0", "u-boot,version": ..., ...},
      "ane": {"<platform device>": {"driver", "generation", "firmware_version", "compatible": [...]}},
      "subsystems": {"<DT path>": {"title", "kind": "block" | "infra", "driver", "level": "ok" | "warn" | "fail" | "off",
                                   "why", "dt_status", "dt_firmware", "class_devices": [...]}},
                     (PCI functions use "pci/<bdf>" as the key)
      "services": {"unit": "active" | "inactive" | "failed" | ...},
      "user_services": {"user/unit": "active" | ...},
      "clocks": {"cpu": {"policyN": {"ladder_MHz": [...], "hw_min_MHz", "hw_max_MHz", "governor", "driver"}},
                 "dt_ladders": {"opp-table-gpu": [MHz, ...]},
                 "memory": {"size_bytes", "speed"}},
      "sensors": ["hwmon/<name>/<label>", "iio/<name>/<channel>", "thermal/<type>", ...],
      "dmesg_errors": ["normalised -p 3 lines (numbers -> N, hex -> 0x?)"],
      "dmesg_readable": true,
      "gpu": {"devices": {"aurora": {"driver", "nodes", "kernel_driver"}},
              "vulkan": {"instance", "devices": {"<name>": {"apiVersion", "driverVersion", "driverID", "driverName",
                         "driverInfo", "conformanceVersion", "extension_count", "features_enabled",
                         "features_total", "format_count"}}},
              "gl": {"gl_core" | "gl_compat" | "gles": {"renderer", "version", "glsl_version", "profile",
                     "extension_count", "path": "native" | "zink" | "llvmpipe"}},
              "compliance": {"vulkan:<name>" | "gl:<api>": {"missing": [...], ...}}},
      "probe_errors": {"probe": "timed out after 10s"},
      "elapsed_s": 0.8
    }

Values that move by themselves (temperatures, power, frequencies right now) are not in a snapshot, so two snapshots of an unchanged machine diff clean.

## diff (`aurora-diag diff --json`)

    {"schema": "aurora-diag/diff", "version": 1, "old", "new", "old_kernel", "new_kernel",
     "regressions": 1,
     "changes": [{"severity": "regression" | "change" | "improvement",
                  "section": "kernel" | "firmware" | "subsystems" | "services" | "user_services" | "clocks" |
                             "sensors" | "dmesg" | "gpu" | "ane" | "vulkan" | "gl" | "compliance",
                  "key", "old", "new", "why"}]}

Exit code: 2 with regressions, 1 with other changes, 0 when nothing changed.

## report (`aurora-diag report --json`)

A superset for humans and bug trackers: machine, kernel, firmware, GPU devices and readings, per-process GPU memory, Mesa ICDs and packages, the full Vulkan and GL results, compliance, clocks, the headline sensors, fans, haptics, bound platform drivers and the last kernel errors and warnings. `report -o DIR` also writes `aurora-diag-report-<host>-<time>.json` and `.txt`.

## readings (`list --json`, `get --json`, `watch --json`)

JSON lines in the log format ([LOG.md](LOG.md)); each reading is an event `reading` with `id`, `cat`, `value`, `unit`, `label`, `path` and `writable`. `id` is stable (`<cat>.<device>.<attribute>`) and is what `get` and `set` take.

## ANE (`aurora-diag ane --json`)

```json
{
  "schema": "aurora-diag/ane", "version": 1,
  "devices": [{
    "name": "accel0", "node": "/dev/accel/accel0", "node_present": true,
    "path": "/sys/class/accel/accel0/device", "platform_device": "285c04000.ane",
    "driver": "ane", "compatible": ["apple,t6000-ane", "apple,ane"],
    "chips": ["t6000"], "die": 0, "generation": "H13",
    "firmware_version": null, "firmware_source": null,
    "client_count": 0, "clients_complete": true,
    "performance_level": null, "performance_source": null,
    "power_state": "suspended", "wedged": 0,
    "counters": {"jobs": 20, "busy_ns": 2000}, "error_counters": {},
    "last_benchmark": null
  }],
  "benchmark_error": null,
  "checks": [{"group": "ane", "name": "/dev/accel/accel0 health", "level": "pass",
              "reason": "wedged 0; power suspended; error counters unavailable (cumulative)", "device": "/dev/accel/accel0"}]
}
```

`checks` is added by the CLI; the dashboard's `slow.ane` contains the inventory alone. A timed-out CLI probe instead emits `schema`, `version`, `error` and exits 2. `check --only ane` uses the ordinary check schema/exit rules (0/1/2). ANE rows may omit `device` for probe/absence/invalid saved-file warnings.

Unknown/unreadable fields are null, not zero. `generation` is H13..H17 when explicitly exposed or mapped from a known compatible. `firmware_source` identifies a driver attribute (`firmware_version`/`fw_version`) or a DT property; DT firmware is not evidence that firmware is running. `performance_level` is the driver's text from `performance_level`/`perf_level`, with no implied frequency unit. `power_state` is `power/runtime_status`. `wedged` is the kernel's nonnegative integer, null if absent/malformed. Health passes on zero wedged status, warns on nonzero cumulative errors or absent health evidence, and fails when wedged is nonzero or runtime power state is `error`. Suspension is healthy.

`counters` parses integer key/value lines in `ane_stats`; `error_counters` retains keys containing error/fault/timeout/reset/crash and optional standalone `errors`, `error_count`, `timeouts`, `faults`, `resets`, `crashes`. An empty map means no counters exposed. `client_count` uses sysfs when available or unique DRM client IDs from device-matching `/proc/*/fd` and fdinfo. `clients_complete: false` makes the number a lower bound; null means no reliable number was available. Processes may exit during collection, so the count is a sample, not an atomic system snapshot.

`last_benchmark` retains only `name`, `chip`, `die`, `model`, `precision`, `generated`, `available`, `ok`, `errors`, `batch`, `min_ms`, `median_ms`, `p50_ms`, `p90_ms`, `throughput_per_s`; it adds `device`, `path`, and `saved_at` (Unix file mtime). It accepts raw saved omarchy-ane-smoke JSON (`die` is its numeric die index, determined from DT reg divided by `0x2000000000`), or an export such as:

```json
{
  "device": "/dev/accel/accel0",
  "model": "mobilenetv1-1.0-224-random", "precision": "fp16", "batch": 1,
  "ok": true, "errors": 0, "generated": "2026-10-31T00:00:00Z",
  "p50_ms": 0.4, "throughput_per_s": 2500
}
```

Numbers must be finite and nonnegative; batch must be a positive integer. Results must identify exactly one device (explicit node/name, platform device string in `die`, or matching chip plus optional numeric die). An invalid, ambiguous, oversized (>64 KiB) or wrong-device file produces `benchmark_error` and leaves the benchmark null. Missing files are normal and produce no parse error. Saved results are never described as live tests. No saved benchmark is executed by the collector.

Generic readings use category `ane` and IDs `ane.accelN.<field>`, always `writable: false`. Stable ANE identity fields are also included in `snapshot.ane`; changing clients, power, counters, performance level or benchmark results does not change that snapshot. Missing drivers/devices regress in `diff` and fail `check --expect`. Readers of older version-1 snapshots must tolerate absent `ane`.

## macOS comparison (`aurora-diag compare --json`)

```json
{
  "schema": "aurora-diag/comparison", "version": 1,
  "source": "j616s-perf-baseline-20261031/COMPARE.md", "captured": "2026-10-31",
  "os": "macOS 27.0 (26A428)", "machine": "apple,j616s", "machine_matches": true,
  "rows": [{"id": "idle.phpc", "label": "CPU-side heatpipe power", "value": 2.61,
            "unit": "W", "context": "idle.phpc; idle on AC", "linux": 3.64,
            "measurement": "live", "delta_pct": 39.46, "level": "warn",
            "reason": "current reading; baseline was idle, conditions unverified"}]
}
```

`value` is macOS, `linux` is current Linux telemetry or null, `measurement` is `live`, `saved` or `unavailable`. `delta_pct` is `(linux / value - 1) * 100`, rounded to two decimals; it is null for unavailable values, a different machine or zero baseline. It expresses numeric difference, not an improvement score. Only matching advertised CPU state counts or frequency bounds produce `pass`; telemetry and saved benchmark rows remain `warn` because workload/display conditions are unverified. Missing telemetry never becomes zero. A baseline read/validation failure emits `schema`, `version`, `error`, `rows: []` and CLI exit 2. Comparison deltas do not affect `check` health.

Live metric IDs: `cpu.p.states`, `cpu.p.min`, `cpu.p.max`, `cpu.e.states`, `cpu.e.min`, `cpu.e.max`, `idle.pstr`, `idle.phpc`, `idle.pmvc`, `idle.pzc0`, `idle.non_display` (PSTR minus PDBR) and `idle.tcmz`. P/E policy types are identified by two distinct maximum clocks, with consistent ladders per type; ambiguous/single-type ladders stay unavailable. Sensors require matching labels/units. CPU requests/achieved current clocks are never substituted for ladder maxima. No workloads, display/idle checks, or system writes are performed.

Saved-only metrics: `ane.b1.rate`, `ane.b1.p50`, `ane.b8.rate`, `ane.b8.p50`. They require exactly one result with matching model, fp16 precision, batch, `ok: true`, zero errors if given, and no `available: false`. Batch-8 throughput is images/s and latency is ms per batch. Add-fixture never matches MobileNet. The result's timestamp appears in the row reason; no freshness claim is made.

The baseline export (`aurora_diag/macos_baseline.json`, installed with the module) uses `schema: "aurora-diag/macos-baseline"`, `version: 1`, a `machine` DT compatible, optional `source`/`captured`/`os`, and `metrics` containing unique `id`, `label`, `value`, `unit`, `context`. Only numeric macOS values and their measurement context are exported from COMPARE.md; historical Linux results are not used. Override with `AURORA_DIAG_MACOS_BASELINE`; override paths and the ANE benchmark path honor `AURORA_DIAG_ROOT` in tests.

## dashboard (`tui --dump FILE`)

`schema: "aurora-diag/dashboard"`, `version: 1` adds `slow.ane` and `slow.comparison` with the structures above (or an `error` from a timed-out probe). Both are refreshed every 15 seconds. The Textual ANE and vs macOS panels render these captured values without probing the viewing machine. Older captures may omit both fields. Table cells and trailing rows update in place, preserving scroll offset and cursor where the remaining content permits.
