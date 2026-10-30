# SPDX-License-Identifier: MIT
"""aurora-diag: one-stop bring-up diagnostics for Aurora machines.

Development and experimental use only. Reads need no privileges (some
sources, such as debugfs and speakerguardd's state, need root); changes
need root and are always journalled and reversible.
"""

import argparse
import json
import os
import re
import signal
import sys
import time

from . import __version__, audio, control, display, fans, haptics, log, sources, sysfs
from .machine import Machine


def all_readings(args, names=None):
    """Readings from the named sources ('audio' covers speaker protection and
    ALSA); None means every source."""
    names = list(names) if names else list(sources.SOURCES) + ["audio"]
    if getattr(args, "all_params", False):
        sources.SOURCES["modparam"] = lambda: sources.modparams(all_modules=True)
    src = [n for n in names if n in sources.SOURCES]
    extra = [audio.speaker_protection, audio.alsa_controls] if ("audio" in names or "speaker" in names) else []
    return sources.collect(src, extra)


def filtered(readings, args):
    cats = set(args.cat or [])
    rx = re.compile(args.grep, re.I) if getattr(args, "grep", None) else None
    out = []
    for r in readings:
        if cats and r["cat"] not in cats:
            continue
        if rx and not rx.search("%s %s %s" % (r["id"], r.get("label") or "", r.get("dev") or "")):
            continue
        out.append(r)
    return out


def fmt_value(r):
    v = r["value"]
    if isinstance(v, float):
        v = ("%.3f" % v).rstrip("0").rstrip(".")
    v = str(v)
    if "\n" in v:
        v = v.splitlines()[0] + " ..."
    if len(v) > 60:
        v = v[:57] + "..."
    return v + ((" " + r["unit"]) if r.get("unit") else "")


def print_table(readings):
    w = max([len(r["id"]) for r in readings] + [10])
    w = min(w, 70)
    for r in readings:
        lab = r.get("label") or ""
        flag = "w" if r.get("writable") else " "
        print("%-*s %s %-24s %s" % (w, r["id"][:w], flag, fmt_value(r), lab))


def emit_readings(args, readings, event="reading"):
    for r in readings:
        rec = {k: r[k] for k in ("id", "cat", "value", "unit", "label", "path", "writable")}
        if isinstance(rec["value"], str) and len(rec["value"]) > 4096:
            rec["value"] = rec["value"][:4096]
        log.event(event, **rec)
    if not args.json:
        print_table(readings)


# -- summary ----------------------------------------------------------------

def summary(readings, machine):
    by = {r["id"]: r for r in readings}
    out = {"machine": machine.as_dict()}
    lid = [r for r in readings if r["cat"] == "iio" and r.get("unit") == "deg"]
    if lid:
        out["lid_angle_deg"] = lid[0]["value"]
    als = [r for r in readings if r["cat"] == "iio" and r.get("unit") == "lux"]
    if als:
        out["ambient_lux"] = als[0]["value"]
    name, t = fans.hottest()
    if t is not None:
        out["hottest_die"] = {"sensor": name, "C": t}
    temps = [r for r in readings if r["cat"] == "temp" and r["attr"].endswith("_input") and isinstance(r["value"], (int, float))]
    out["temperatures"] = {"%s %s" % (r["dev"], r.get("label") or r["attr"]): r["value"] for r in temps}
    out["thermal_zones"] = {r["label"]: r["value"] for r in readings if r["cat"] == "thermal" and r["attr"] == "temp"}
    fl = fans.fans()
    out["fans"] = fl if fl else "none (fanless)"
    bat = {r["attr"]: r["value"] for r in readings if r["cat"] == "battery" and r["attr"] in ("capacity", "status", "power_now", "temp", "voltage_now")}
    if bat:
        out["battery"] = bat
    pw = {r.get("label") or "%s %s" % (r["dev"], r["attr"]): r["value"] for r in readings if r["cat"] == "power" and r["attr"].startswith("power") and r["attr"].endswith("_input")}
    if pw:
        out["power_W"] = pw
    out["cpufreq_MHz"] = {r["dev"]: r["value"] for r in readings if r["cat"] == "cpufreq" and r["attr"] == "scaling_cur_freq"}
    bl = [r for r in readings if r["cat"] == "backlight" and r["attr"] == "brightness"]
    if bl:
        out["backlight"] = bl[0]["value"]
    sg = [r for r in readings if r["cat"] == "speaker"]
    if sg:
        coils = [r["value"] for r in sg if r["attr"] in ("t_coil_C", "ff_coil_C", "vi_coil_C") and isinstance(r["value"], float)]
        out["speaker_protection"] = {
            "speakerguardd": by.get("speaker.speakerguardd.service", {}).get("value"),
            "speakersafetyd": by.get("speaker.speakersafetyd.service", {}).get("value"),
            "armed": by.get("speaker.speakerguardd.armed", {}).get("value"),
            "hottest_coil_C": max(coils) if coils else None,
        }
    return out


# -- commands ---------------------------------------------------------------

def cmd_machine(args, m):
    d = m.as_dict()
    d["haptics"] = haptics.status(m).get("supported")
    d["fans"] = len(fans.fans())
    d["root"] = os.geteuid() == 0
    log.event("machine", **d)
    if not args.json:
        for k, v in d.items():
            print("%-12s %s" % (k, v))


def cmd_sensors(args, m):
    names = args.source or None
    if names is None and not args.debugfs:
        names = [n for n in sources.SOURCES if n != "debugfs"] + ["audio"]
    rs = filtered(all_readings(args, names), args)
    emit_readings(args, rs)


def cmd_summary(args, m):
    s = summary(all_readings(args, list(sources.FAST_SOURCES) + ["audio"]), m)
    log.event("summary", **s)
    if not args.json:
        print(json.dumps(s, indent=1, default=str))


def find(args, key):
    if key.startswith("/"):
        raw = sysfs.read(key)
        return {"id": key, "cat": "path", "dev": None, "attr": os.path.basename(key), "label": None,
                "value": raw, "unit": None, "raw": raw, "path": key, "writable": sysfs.writable(key)}
    rs = all_readings(args)
    for r in rs:
        if r["id"] == key:
            return r
    cand = [r["id"] for r in rs if key.lower() in r["id"].lower()]
    raise control.Refused("no reading %r%s" % (key, (" (did you mean: %s)" % ", ".join(cand[:8])) if cand else ""))


def cmd_get(args, m):
    r = find(args, args.key)
    emit_readings(args, [r])


def hold_until_signal(reason):
    """Keep the process alive until a signal, then put this session's
    changes back."""
    got = {"sig": None}

    def h(sig, frame):
        got["sig"] = sig

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, h)
    print("holding; Ctrl-C (or SIGTERM) restores this session's changes", file=sys.stderr)
    try:
        while got["sig"] is None:
            if fans.fan_entries() and any(e.get("session") == log.session() for e in fans.fan_entries()):
                lease = fans.read_lease() or {}
                fans.write_lease(15, lease.get("max_temp", fans.DEFAULT_MAX_TEMP))
            time.sleep(1)
    finally:
        restored, failed = control.restore(session=log.session(), reason=reason)
        log.event("hold_exit", restored=len(restored), failed=len(failed))
        print("restored %d, failed %d" % (len(restored), len(failed)), file=sys.stderr)


def report(args, res):
    log.event("result", result=res)
    if not args.json:
        print(json.dumps(res, indent=1, default=str))


def cmd_set(args, m):
    r = find(args, args.key)
    if r["path"] and r["path"].startswith("alsa:"):
        _, card, numid = r["path"].split(":")
        res = control.apply_alsa(card, numid, r["attr"], args.value, m, args.allow_unknown, args.dry_run)
    else:
        res = control.apply_sysfs(r["path"], args.value, m, args.allow_unknown, args.dry_run)
    report(args, res)
    if args.hold and not args.dry_run:
        hold_until_signal("hold exit")


def cmd_watch(args, m):
    names = args.source or list(sources.FAST_SOURCES) + ["audio"]
    last = {}
    n = 0
    full_every = max(1, int(60 / max(args.interval, 0.1)))
    try:
        while True:
            rs = filtered(all_readings(args, names), args)
            cur = {r["id"]: r["value"] for r in rs}
            changed = {k: v for k, v in cur.items() if last.get(k) != v}
            if n % full_every == 0:
                log.event("sample", full=True, values=cur)
            elif changed:
                log.event("sample", full=False, values=changed)
            if not args.json:
                sys.stdout.write("\033[H\033[2J" if sys.stdout.isatty() else "")
                print("aurora-diag watch  %s  %s  every %.1fs (Ctrl-C quits)" % (m.board, time.strftime("%H:%M:%S"), args.interval))
                print_table(rs)
                sys.stdout.flush()
            last = cur
            n += 1
            if args.count and n >= args.count:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass


def cmd_fan(args, m):
    if args.fan_cmd == "list" or args.fan_cmd is None:
        fl = fans.fans()
        name, t = fans.hottest()
        d = {"fans": fl, "fan_control": fans.fan_control_param(), "hottest_die": [name, t],
             "guard": fans.guard_active(), "lease": fans.read_lease()}
        report(args, d)
    elif args.fan_cmd == "set":
        res = fans.set_fan(m, args.fan, args.rpm, args.lease, args.max_temp, args.allow_unknown, args.dry_run)
        report(args, res)
        if args.hold and not args.dry_run:
            hold_until_signal("hold exit")
    elif args.fan_cmd == "auto":
        restored, failed = fans.auto(reason=args.reason)
        report(args, {"restored": len(restored), "failed": len(failed)})


def cmd_fan_guard(args, m):
    fans.run_guard(args.max_temp)


def cmd_haptic(args, m):
    if args.h_cmd in (None, "show"):
        report(args, haptics.status(m))
    else:
        res = haptics.set_strength(m, args.press, args.release, args.allow_unknown, args.dry_run)
        report(args, res)
        if args.hold and not args.dry_run:
            hold_until_signal("hold exit")


def cmd_display(args, m):
    c = args.d_cmd
    if c in (None, "status"):
        report(args, {"connectors": display.connectors(), "notch": display.notch_status(),
                      "backlight": display.brightness_path()})
    elif c == "notch":
        if args.state == "status":
            report(args, display.notch_status())
        else:
            report(args, display.set_notch(m, args.state == "on", args.allow_unknown))
    elif c == "brightness":
        report(args, display.set_brightness(m, args.value, args.allow_unknown))
    elif c == "reprobe":
        display.reprobe(args.connector)
        report(args, {"reprobed": args.connector})
    elif c == "power-save":
        report(args, display.power_save_set(args.mode))
    if getattr(args, "hold", False):
        hold_until_signal("hold exit")


def cmd_kbd(args, m):
    leds = [r for r in sources.backlight() if r["cat"] == "leds" and "kbd_backlight" in r["dev"] and r["attr"] == "brightness"]
    if not leds:
        raise control.Refused("no keyboard backlight LED")
    if args.value is None:
        report(args, {"led": leds[0]["dev"], "brightness": leds[0]["value"]})
        return
    report(args, control.apply_sysfs(leds[0]["path"], args.value, m, args.allow_unknown, args.dry_run))
    if args.hold:
        hold_until_signal("hold exit")


def cmd_audio(args, m):
    rs = filtered(audio.speaker_protection() + audio.alsa_controls(), args)
    emit_readings(args, rs)


def cmd_journal(args, m):
    es = control.journal_entries()
    log.event("journal", entries=es)
    if not args.json:
        if not es:
            print("no pending changes")
        for e in es:
            print("%-6s %-60s %s -> %s (restore %s) session %s" % (e["kind"], e["key"], e["original"], e.get("current"), e["restore"], e["session"]))


def cmd_restore(args, m):
    if args.kind == ["fan"] or (args.kind and "fan" in args.kind):
        restored, failed = fans.auto(reason=args.reason)
        if args.kind != ["fan"]:
            r2, f2 = control.restore(session=args.session, kinds=[k for k in args.kind if k != "fan"], reason=args.reason)
            restored += r2
            failed += f2
    else:
        restored, failed = control.restore(session=args.session, kinds=args.kind, reason=args.reason)
        if not args.kind and not args.session:
            fans.auto(reason=args.reason)
    report(args, {"restored": [e["key"] for e in restored], "failed": [e["key"] for e in failed]})
    return 1 if failed else 0


def cmd_tui(args, m):
    from . import tui
    tui.run(args, m)


def cmd_log_path(args, m):
    print(log.log_path())


def parser():
    p = argparse.ArgumentParser(prog="aurora-diag", description=__doc__.splitlines()[0])
    p.add_argument("--version", action="version", version="aurora-diag " + __version__)
    p.add_argument("--json", action="store_true", help="JSON lines on stdout (the same records as the log)")
    p.add_argument("--log", help="log file (default /var/log/aurora-diag/aurora-diag.jsonl as root, else $XDG_STATE_HOME/aurora-diag/)")
    p.add_argument("--allow-unknown", action="store_true", help="allow changes on a machine whose SoC this tool does not know")
    p.add_argument("--dry-run", action="store_true", help="check a change against the policy and show it, without writing")
    sub = p.add_subparsers(dest="cmd")

    def filt(sp):
        sp.add_argument("--cat", action="append", help="only this category (repeatable): temp power fan iio thermal cooling battery cpufreq devfreq display backlight leds typec usb input radio net bt v4l2 regulator coproc driver modparam sep debugfs speaker audio")
        sp.add_argument("--source", action="append", help="only this source (repeatable): " + " ".join(list(sources.SOURCES) + ["audio"]))
        sp.add_argument("--grep", help="regex over id, label and device")
        sp.add_argument("--all-params", action="store_true", help="every module's parameters, not just platform ones")

    sub.add_parser("machine", help="what this machine is and what the tool can do on it")
    s = sub.add_parser("sensors", help="every reading the kernel exposes")
    filt(s)
    s.add_argument("--debugfs", action="store_true", help="include allowlisted debugfs reports (root)")
    s = sub.add_parser("summary", help="the headline numbers")
    filt(s)
    s = sub.add_parser("get", help="one reading by id or path")
    s.add_argument("key")
    s = sub.add_parser("set", help="change any writable attribute the policy allows (journalled)")
    s.add_argument("key")
    s.add_argument("value")
    s.add_argument("--hold", action="store_true", help="stay in the foreground and restore on exit")
    s = sub.add_parser("watch", help="live view; logs changed values")
    filt(s)
    s.add_argument("--interval", type=float, default=1.0)
    s.add_argument("--count", type=int, default=0, help="stop after N samples")

    f = sub.add_parser("fan", help="fans: list, set a speed under a watchdog, back to auto")
    fs = f.add_subparsers(dest="fan_cmd")
    fs.add_parser("list")
    x = fs.add_parser("set")
    x.add_argument("fan", type=int)
    x.add_argument("rpm", type=int, help="0 hands the fan back to the SMC")
    x.add_argument("--lease", type=int, default=fans.DEFAULT_LEASE, help="seconds before the guard hands the fan back (default 600)")
    x.add_argument("--max-temp", type=float, default=fans.DEFAULT_MAX_TEMP, help="die temperature at which the guard hands every fan back (default 90, max 100)")
    x.add_argument("--hold", action="store_true")
    x = fs.add_parser("auto")
    x.add_argument("--reason", default="fan auto")
    x = sub.add_parser("fan-guard", help=argparse.SUPPRESS)
    x.add_argument("--max-temp", type=float, default=fans.DEFAULT_MAX_TEMP)

    h = sub.add_parser("haptic", help="trackpad click strength (live, restorable)")
    hs = h.add_subparsers(dest="h_cmd")
    hs.add_parser("show")
    x = hs.add_parser("set")
    x.add_argument("press", type=lambda v: int(v, 0))
    x.add_argument("release", type=lambda v: int(v, 0))
    x.add_argument("--hold", action="store_true")

    d = sub.add_parser("display", help="notch, brightness, connector reprobe, Mutter power save")
    ds = d.add_subparsers(dest="d_cmd")
    ds.add_parser("status")
    x = ds.add_parser("notch")
    x.add_argument("state", choices=["on", "off", "status"])
    x.add_argument("--hold", action="store_true")
    x = ds.add_parser("brightness")
    x.add_argument("value", help="absolute or N%%")
    x.add_argument("--hold", action="store_true")
    x = ds.add_parser("reprobe")
    x.add_argument("connector", help="e.g. card2-eDP-1")
    x = ds.add_parser("power-save")
    x.add_argument("mode", type=int)
    x.add_argument("--hold", action="store_true")

    x = sub.add_parser("kbd", help="keyboard backlight")
    x.add_argument("value", nargs="?")
    x.add_argument("--hold", action="store_true")
    x = sub.add_parser("audio", help="speaker protection state and ALSA controls (read only for protection)")
    filt(x)
    sub.add_parser("journal", help="changes not yet restored")
    x = sub.add_parser("restore", help="put back journalled changes (all by default) and hand fans to the SMC")
    x.add_argument("--session")
    x.add_argument("--kind", action="append", help="sysfs fan alsa hid mutter")
    x.add_argument("--reason", default="restore")
    sub.add_parser("tui", help="interactive view for humans")
    sub.add_parser("log-path", help="where the log goes")
    return p


COMMANDS = {
    "machine": cmd_machine, "sensors": cmd_sensors, "summary": cmd_summary, "get": cmd_get,
    "set": cmd_set, "watch": cmd_watch, "fan": cmd_fan, "fan-guard": cmd_fan_guard,
    "haptic": cmd_haptic, "display": cmd_display, "kbd": cmd_kbd, "audio": cmd_audio,
    "journal": cmd_journal, "restore": cmd_restore, "tui": cmd_tui, "log-path": cmd_log_path,
}


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.cmd:
        args.cmd = "summary"
        args.cat = args.source = args.grep = None
    log.setup(stdout_json=args.json, path=args.log)
    m = Machine()
    log.event("start", version=__version__, cmd=args.cmd, argv=sys.argv[1:], board=m.board, euid=os.geteuid())
    try:
        rc = COMMANDS[args.cmd](args, m)
    except control.Refused as e:
        log.event("error", cmd=args.cmd, error=str(e))
        if not args.json:
            print("aurora-diag: %s" % e, file=sys.stderr)
        return 2
    except PermissionError as e:
        log.event("error", cmd=args.cmd, error=str(e))
        print("aurora-diag: %s (changes need root)" % e, file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0
    return rc or 0
