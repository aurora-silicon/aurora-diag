# Log format

One JSON object per line. Follow it with `tail -F "$(aurora-diag log-path)"`.

Every line has these fields.

| field | meaning |
| --- | --- |
| ts | UTC time, ISO 8601 |
| mono | CLOCK_MONOTONIC seconds, comparable across processes in one boot |
| host | hostname |
| pid | process id |
| session | random id of one invocation. `restore --session` uses it |
| event | what happened (below) |

## Events

| event | extra fields | when |
| --- | --- | --- |
| start | version, cmd, argv, board, euid | every invocation |
| machine | compatible, model, board, soc, apple, firmware, haptics, fans, root | `machine` |
| reading | id, cat, value, unit, label, path, writable | `sensors`, `get`, `audio` (one per value) |
| summary | the headline numbers | `summary` |
| sample | full, values (id to value) | `watch`. Every value once a minute, changed values otherwise |
| set | kind, path, original, new, readback, verified | a change was made |
| refuse | path, value, reason | the safety policy refused a change |
| set_failed | path, value, original, error | the kernel refused a write |
| dry_run | path, value, original | `--dry-run` |
| restored | kind, path, value, reason | a change was put back |
| restore_failed | kind, path, value, error, reason | putting back failed. The entry stays in the journal |
| fan_set | fan, rpm, lease, max_temp | a fan speed was requested |
| guard_started | how (systemd-run or fork), unit | the fan guard came up |
| guard_start_failed | how, error | it didn't. The fans are handed back |
| guard_run | max_temp | the guard loop started |
| guard_exit | reason, restored, failed | the guard handed the fans back |
| notch | on, live | `display notch` |
| action | what, ... | a one-off action such as a DRM reprobe |
| hold_exit, tui_start, tui_exit | restored, failed, kept | session ends |
| result | result | the command's result object |
| error | cmd, error | the command failed |

`kind` is one of `sysfs`, `fan`, `alsa`, `hid` or `mutter`.

## Bring-up events

| event | extra fields | when |
| --- | --- | --- |
| check | result, counts, baseline, failures | `check` |
| snapshot | file, elapsed_s, probe_errors | `snapshot -o` |
| diff | old, new, regressions, changes | `diff` |
| report | files, elapsed_s | `report` |
| subsystem / subsystems | the subsystem record / count | `subsys` |
