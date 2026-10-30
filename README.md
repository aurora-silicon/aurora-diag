# aurora-diag

A one-stop diagnostics tool for bringing up Linux on Apple silicon Macs. It shows everything the kernel exposes, lets you change anything it lets you change (with a few safety exclusions), and logs every reading and every change as JSON lines so an agent can follow along.

This is a development and experimental tool. No image installs it and no platform package depends on it. Install it on purpose when you are testing or bringing up a machine.

## What it reads

Nothing is a curated list. Each source walks a kernel class or bus, so a driver that appears during bring-up shows up without a change here.

- every hwmon channel with its label and unit (SMC temperatures, rails and power meters, battery, NVMe, Wi-Fi, amps, fans)
- iio sensors, including the ambient light sensor and the lid angle sensor (`aop-sensors-las`)
- thermal zones with their trip points, cooling devices and their states
- power supplies, Type-C ports, partners and PD state, USB devices
- cpufreq and devfreq, backlight and LEDs, DRM connectors, input and HID devices, rfkill, network, Bluetooth, V4L2, regulators, remote processors
- which Apple platform drivers are bound (SEP, AOP, DCP, ISP, PMP, SMC and the rest) and their module parameters
- allowlisted debugfs reports as root (PMC and PMP state, GPU firmware reports, genpd, regulators, wakeup sources)
- speaker protection, read only: speakerguardd's live model (coil and magnet temperatures, recent power, sensed voltage, gain reduction, each governor's view when it runs in shadow mode) and whether speakersafetyd runs
- ALSA controls on every card

## What it changes

Anything writable the kernel exposes, through one write path that reads the old value first, writes, reads back, logs, and records the change in a journal under `/run/aurora-diag`. `aurora-diag restore` puts everything back. `--hold`, the TUI and the fan guard put their changes back when they exit.

These stay read only whatever you ask:

- speaker protection: speakerguardd and speakersafetyd state, every ALSA control on the speaker path (volumes, unlock, V/I sense, amp gain, speaker routing) and the speaker drivers' module parameters. The tool never opens a PCM, so it can't play or record audio.
- thermal zone mode, emulated temperatures, and critical or hot trip points
- battery and charger limits (only the charge thresholds and `charge_behaviour` can change)
- anything that could break the boot chain: EFI variables, NVRAM, SPI NOR, block devices
- bind, unbind, remove and similar actions, `/sys/power`, debugfs and `/proc`

Machines are recognised by the SoC compatible string in the device tree. On a machine whose SoC the tool doesn't know, everything is read only unless you pass `--allow-unknown`.

### Fans

`aurora-diag fan set N RPM` needs a kernel whose macsmc-hwmon probed with `macsmc_hwmon.fan_control=1`. Every manual speed runs under a guard that hands the fans back to the SMC when its lease runs out (10 minutes by default), when a die temperature passes `--max-temp` (90 C by default, never above 100 C), when the die temperatures can't be read for 5 seconds, or when the guard stops for any reason. It runs as a transient systemd unit whose `ExecStopPost` hands the fans back even after a crash or SIGKILL. Machines without fans are refused, and so are machines with no readable die temperature.

### Trackpad click

`aurora-diag haptic set PRESS RELEASE` changes the strength byte of the actuator's feature reports 0x22 and 0x23 live. It works on the MTP trackpads (M2 and later) and the SPI ones (M1 Pro and Max). The MacBook Neo is excluded.

### Display

`aurora-diag display notch on|off` writes `appledrm.show_notch`, asks DRM to reprobe the panel and reports whether the mode changed. appledrm decides the notch when the DCP probes, so on current kernels it takes effect from the next boot and the tool says so. `display brightness`, `display reprobe` and `display power-save` (Mutter's PowerSaveMode, run as the desktop user) cover the rest.

## Use

    aurora-diag                     # headline numbers
    aurora-diag sensors [--cat temp] [--grep RX] [--debugfs]
    aurora-diag watch [--interval 1]
    aurora-diag get ID | set ID VALUE [--hold]
    aurora-diag fan list | set N RPM | auto
    aurora-diag haptic show | set PRESS RELEASE
    aurora-diag display status | notch on|off | brightness N|N%
    aurora-diag kbd [N]
    aurora-diag audio
    aurora-diag journal | restore
    aurora-diag tui

Add `--json` for JSON lines on stdout and `--dry-run` to check a change against the policy without making it. Reading needs no privileges, though debugfs and speakerguardd's state need root. Changes need root.

## Logs

As root the log goes to `/var/log/aurora-diag/aurora-diag.jsonl`, otherwise to `$XDG_STATE_HOME/aurora-diag/`. `aurora-diag log-path` prints it. The events are described in [docs/LOG.md](docs/LOG.md).

## Tests

    make check

The tests run against a fake `/sys` tree (`AURORA_DIAG_ROOT`), so they need no hardware and no root.

## Licence

MIT
