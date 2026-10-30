# Hardware tests for the write path

Nothing below has run on hardware yet: the first build was tested read only (list, get, watch, check, snapshot, diff, report) on the M4 Pro (J616s), M1 Pro and M2 at night. These are the change tests to run in the daytime, with someone at the machine for the haptic one. Copy the tree to the machine (or install the package) and run as root.

Before each test, note the session id that `set` prints in the log (`aurora-diag log -n 5`).

## 1. Fan override and restore (J616s, M1 Pro; the M2 is fanless and must refuse)

The fans are read only until macsmc-hwmon probes with fan control on. That is an existing driver option, enabled for a test boot only:

    # test boot with macsmc_hwmon.fan_control=1 on the kernel command line, then
    aurora-diag fan list                         # target_writable: true, fan_control: Y
    aurora-diag fan set 1 3000 --lease 60        # guard unit starts; fan 1 spins to ~3000 rpm
    aurora-diag watch --source hwmon --grep Fan --count 20
    aurora-diag fan auto                         # fans back to the SMC, target 0
    aurora-diag fan set 1 3500 --lease 30        # then wait 30 s: the guard hands it back by itself
    aurora-diag log --event guard_exit           # reason "lease expired"
    systemctl status aurora-diag-fan-guard       # gone

Pass: speeds follow, `fan auto` and the lease both return the fans to 0 target, the log has a `set` and a `restored` (or `guard_exit`) for every change. On the M2, `fan set` must say the machine is fanless.

## 2. Trackpad click strength (J616s MTP, M1 Pro SPI, M2 MTP)

    aurora-diag haptic show                      # press/release bytes, firmware default 0x23/0x14 so far
    aurora-diag haptic set 0x40 0x30             # firmer click: press the trackpad
    aurora-diag haptic set 0x10 0x08             # softer click
    aurora-diag restore                          # back to the original bytes
    aurora-diag haptic show                      # equals the first show

Pass: the click changes immediately and `restore` brings back the bytes read first.

## 3. Keyboard backlight (all three)

    aurora-diag kbd                              # current brightness
    aurora-diag kbd 128 --hold                   # lit while held; Ctrl-C restores
    aurora-diag set leds.kbd_backlight.brightness 64
    aurora-diag journal                          # one pending change
    aurora-diag restore --from-log --session <id>   # restore from the log instead of the journal
    aurora-diag journal

Pass: the keyboard lights, both restores return the original value, and the night rule (`aurora-kbd-off`) is unaffected afterwards.

## 4. Refusals (all three, safe at any time)

    aurora-diag --dry-run set /sys/module/macsmc_hwmon/parameters/fan_control Y   # refused: probe time
    aurora-diag --dry-run set thermal.thermal_zone0.mode disabled                 # refused
    aurora-diag set alsa.<card>.Speaker_Volume 100                                # refused: speaker path

Pass: each is refused with a reason and a `refuse` event in the log.
