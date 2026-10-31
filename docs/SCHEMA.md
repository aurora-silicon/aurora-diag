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
        {"group": "subsys" | "service" | "kernel" | "sensors" | "gpu",
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
                             "sensors" | "dmesg" | "gpu" | "vulkan" | "gl" | "compliance",
                  "key", "old", "new", "why"}]}

Exit code: 2 with regressions, 1 with other changes, 0 when nothing changed.

## report (`aurora-diag report --json`)

A superset for humans and bug trackers: machine, kernel, firmware, GPU devices and readings, per-process GPU memory, Mesa ICDs and packages, the full Vulkan and GL results, compliance, clocks, the headline sensors, fans, haptics, bound platform drivers and the last kernel errors and warnings. `report -o DIR` also writes `aurora-diag-report-<host>-<time>.json` and `.txt`.

## readings (`list --json`, `get --json`, `watch --json`)

JSON lines in the log format ([LOG.md](LOG.md)); each reading is an event `reading` with `id`, `cat`, `value`, `unit`, `label`, `path` and `writable`. `id` is stable (`<cat>.<device>.<attribute>`) and is what `get` and `set` take.
