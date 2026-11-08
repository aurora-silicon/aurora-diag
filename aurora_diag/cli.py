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
    if names is None:
        names = [n for n in sources.SOURCES if (n != "debugfs" or args.debugfs) and (n != "api" or not args.no_api)] + ["audio"]
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
    names = [n for n in list(sources.SOURCES) + ["audio"]
             if n != "api" or key.startswith("vulkan.") or key.startswith("gl.")]
    rs = all_readings(args, names)
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
    elif args.cmd in DOCUMENT_COMMANDS:
        print(json.dumps(res, default=str))


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
    if args.from_log:
        return cmd_restore_from_log(args, m)
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


def cmd_log(args, m):
    """Print (or follow) the JSON-lines log, optionally filtered."""
    path = args.log or log.log_path()
    events = set(args.event or [])

    def keep(rec):
        return ((not args.session or rec.get("session") == args.session)
                and (not events or rec.get("event") in events))

    def show(line):
        try:
            rec = json.loads(line)
        except ValueError:
            return
        if not keep(rec):
            return
        if args.json:
            sys.stdout.write(line if line.endswith("\n") else line + "\n")
        else:
            rest = {k: v for k, v in rec.items() if k not in ("ts", "mono", "host", "pid", "session", "event")}
            print("%s %s %-10s %s" % (rec.get("ts"), rec.get("session"), rec.get("event"),
                                      json.dumps(rest, default=str)[:300]))

    try:
        f = open(path)
    except OSError as e:
        raise control.Refused("cannot open log %s: %s" % (path, e))
    with f:
        lines = f.readlines()
        for line in (lines[-args.lines:] if args.lines else lines):
            show(line)
        sys.stdout.flush()
        while args.follow:
            line = f.readline()
            if line:
                show(line)
                sys.stdout.flush()
            else:
                time.sleep(0.5)


def changes_from_log(path, session=None):
    """The 'set' events in the log (oldest first), for restore --from-log."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("event") != "set":
                    continue
                if session and rec.get("session") != session:
                    continue
                out.append(rec)
    except OSError as e:
        raise control.Refused("cannot read log %s: %s" % (path, e))
    return out


def cmd_restore_from_log(args, m):
    """Write back the first original value of every path a logged session
    changed, newest change first. Used when the journal in /run is gone
    (another boot keeps nothing to restore, so this is for the same boot)."""
    changes = changes_from_log(args.log or log.log_path(), args.session)
    if not changes:
        raise control.Refused("no 'set' events%s in the log" % (" for session %s" % args.session if args.session else ""))
    first = {}
    for rec in changes:
        first.setdefault(rec["path"], rec)
    done, failed = [], []
    for path, rec in reversed(list(first.items())):
        kind = rec.get("kind", "sysfs")
        try:
            if kind in ("sysfs", "fan"):
                res = control.apply_sysfs(path, rec["original"], m, args.allow_unknown, args.dry_run)
            elif kind == "alsa":
                _, card, numid = path.split(":")
                res = control.apply_alsa(card, numid, rec.get("control", ""), rec["original"], m, args.allow_unknown, args.dry_run)
            else:
                raise control.Refused("kind %s is restored from the journal only" % kind)
            done.append(res)
        except (control.Refused, OSError) as e:
            failed.append({"path": path, "error": str(e)})
    report(args, {"restored": done, "failed": failed, "from_log": True})
    return 1 if failed else 0


def cmd_report(args, m):
    from . import report as rp
    rep = rp.build(m, summary)
    paths = rp.write(rep, args.output) if args.output else None
    log.event("report", files=paths, elapsed_s=rep["elapsed_s"])
    if args.json:
        print(json.dumps(rep, default=str))
    else:
        sys.stdout.write(rp.text(rep))
        if paths:
            print("written: %s %s" % paths, file=sys.stderr)


def cmd_gpu(args, m):
    from . import gpu
    res = {"devices": gpu.gpus(), "readings": {r["id"]: r["value"] for r in gpu.readings()},
           "processes": gpu.per_process(), "mesa": gpu.mesa()}
    if not args.no_api:
        res["vulkan"] = gpu.vulkan()
        res["gl"] = gpu.gl()
    report(args, res)


def cmd_check(args, m):
    from . import check
    res = check.run(m, only=args.only, expect=args.expect, api=not args.no_api, timeout=args.timeout)
    log.event("check", result=res["result"], counts=res["counts"], baseline=args.expect,
              failures=[r for r in res["checks"] if r["level"] in ("warn", "fail")])
    if args.json:
        print(json.dumps(res, default=str))
    else:
        sys.stdout.write(check.render(res, args.quiet, args.verbose))
    return res["exit"]


def cmd_ane(args, m):
    from . import ane
    from .probe import timed
    res, err = timed(ane.collect, 8)
    if err:
        res = {"schema": "aurora-diag/ane", "version": 1, "error": err}
    else:
        res["checks"] = ane.checks(res)
    report(args, res)
    return 2 if err else 0


def cmd_compare(args, m):
    from . import ane, clocks, comparison
    from .probe import timed
    res, err = timed(lambda: comparison.build(m.as_dict(), sources.collect(["hwmon"]),
                                              clocks.all_clocks(), ane.collect()), 15)
    if err:
        res = {"schema": comparison.SCHEMA, "version": 1, "error": err, "rows": []}
    report(args, res)
    return 2 if res.get("error") else 0


def cmd_show(args, m):
    """One category (or all) as the dashboard's drill-down shows it."""
    from . import sections
    names = sections.ORDER if args.name in (None, "all") else [sections.resolve(args.name)]
    if "cpu" in names:
        sections.cpu()   # first sample: the second one gives live per-core load
        time.sleep(args.interval)
    secs = sections.collect(names)
    log.event("show", categories=names, levels={n: s["level"] for n, s in secs.items()})
    if args.json:
        print(json.dumps(secs[names[0]] if len(names) == 1 else {"schema": "aurora-diag/show", "version": 1,
                                                               "host": os.uname().nodename, "kernel": os.uname().release,
                                                               "sections": secs}, default=str))
    else:
        for n in names:
            sys.stdout.write(sections.render(secs[n]) + "\n")


def cmd_demo(args, m):
    """List the demos, or run one: live ones draw in the terminal until
    Ctrl-C (or --seconds), quick runs print their output and result, apps
    run until Ctrl-C and then their off command runs."""
    from . import demos
    if not args.name:
        inv = demos.inventory()
        if args.json:
            print(json.dumps({"schema": "aurora-diag/demos", "version": 1, "host": os.uname().nodename, "demos": inv}, default=str))
            return 0
        for d in inv:
            print("%-7s %-15s %-7s %s" % ({"ok": "READY"}.get(d["level"], d["level"].upper()), d["name"], d["kind"], d["detail"]))
        print("\naurora-diag demo NAME runs one (nothing runs until you ask). --dry-run shows the command.")
        return 0
    demo = demos.get(args.name)
    level, detail = demo.probe()
    if args.stop:
        out = demos.stop(demo, None)
        print(out or "stopped %s" % demo.name)
        return 0
    if level not in ("ok", "warn"):
        print("%s: not available here: %s" % (demo.name, detail), file=sys.stderr)
        return 2
    if demo.name == "whitepoint" and args.set:
        before, after = demos.whitepoint_set(args.set == "on")
        print("ambient white point: %s -> %s" % (before, after))
        return 0
    if demo.kind == "live":
        st = demos.MotionState()
        wp = demos.WhitePointState()
        t_end = time.monotonic() + args.seconds if args.seconds else None
        try:
            while True:
                if demo.name == "whitepoint":
                    s = demos.whitepoint_sample()
                    wp.feed(s)
                    if args.json:
                        print(json.dumps(s, default=str), flush=True)
                    else:
                        sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(demos.whitepoint_plain(demos.whitepoint_art(s, wp))) +
                                         "\n\n  Ctrl-C stops. aurora-diag demo whitepoint --set on|off changes the setting.\n")
                        sys.stdout.flush()
                    time.sleep(1.0)
                elif demo.name == "motion":
                    s = demos.motion_sample()
                    st.feed(s)
                    if args.json:
                        print(json.dumps(dict(s, heading_deg=st.heading, tilt=demos.tilt(s["accel"]) if s.get("accel") and None not in s["accel"] else None)), flush=True)
                    else:
                        sys.stdout.write("\x1b[H\x1b[2J" + "\n".join(demos.motion_art(s, st)) + "\n\n  Ctrl-C stops.\n")
                        sys.stdout.flush()
                    time.sleep(0.1 if not args.json else 0.5)
                else:
                    from . import sections
                    sec = sections.audio_section()
                    if args.json:
                        print(json.dumps(sec, default=str), flush=True)
                    else:
                        sys.stdout.write("\x1b[H\x1b[2J" + sections.render(sec) + "\n  Ctrl-C stops. Nothing plays.\n")
                        sys.stdout.flush()
                    time.sleep(1.0)
                if t_end and time.monotonic() >= t_end:
                    break
        except KeyboardInterrupt:
            pass
        return 0
    cmd = demo.command()
    if args.dry_run or not cmd:
        print(" ".join(cmd) if cmd else "no command for %s here" % demo.name)
        return 0 if cmd else 2
    print("%s: %s\nrunning: %s%s" % (demo.title, demo.what, " ".join(cmd),
                                      "\n(Ctrl-C stops it)" if demo.kind == "launch" else ""), file=sys.stderr)
    p = demos.spawn(demo, cmd)
    out = []
    try:
        for line in p.stdout:
            out.append(line)
            if not args.json:
                sys.stdout.write(line)
        rc = p.wait()
    except KeyboardInterrupt:
        demos.stop(demo, p)
        rc = 130
    res, r = demos.result_line(demo, "".join(out))
    if args.json:
        print(json.dumps({"demo": demo.name, "exit": rc, "result": r, "summary": res,
                          "reference": demo.reference() if demo.reference else None}, default=str))
    elif res:
        print("\nresult: " + res)
    return 0 if rc == 0 else 1


def cmd_panic_log(args, m):
    """The last panic iBoot kept for the next OS (read only; needs root)."""
    from . import paniclog
    results = paniclog.find(args.device)
    if not results:
        msg = "no panic-log namespace found (an Apple NVMe namespace other than 1, at most 16 MiB)"
        if args.json:
            print(json.dumps({"schema": "aurora-diag/panic-log", "version": 1, "results": [], "error": msg}))
        else:
            print(msg)
        return 1
    saved = []
    if args.save:
        for res in results:
            raw = paniclog.read_device(res["device"]) if args.raw and res.get("state") == "record" else None
            for rec in res.get("records", []):
                saved += paniclog.save(rec, args.save, raw)
    log.event("panic-log", results=[{k: v for k, v in r.items() if k != "records"} for r in results],
              records=[{k: v for k, v in rec.items() if k not in ("panic_text", "other_text")}
                       for r in results for rec in r.get("records", [])], saved=saved)
    if args.json:
        doc = {"schema": "aurora-diag/panic-log", "version": 1, "results": results, "saved": saved}
        if not args.full:
            for r in doc["results"]:
                for rec in r.get("records", []):
                    rec["panic_text"] = "\n".join(rec["panic_text"].splitlines()[:args.lines])
        print(json.dumps(doc, default=str))
    else:
        for res in results:
            for ln in paniclog.describe(res, lines=10 ** 6 if args.full else args.lines):
                print(ln)
        for path in saved:
            print("saved " + path)
    if any(r.get("state") == "error" for r in results):
        return 2
    return 0


def cmd_coverage(args, m):
    """Every category item, present or absent with the reason."""
    from . import sections
    secs = sections.collect([n for n in sections.ORDER if n != "health"])
    rows = sections.coverage(secs)
    log.event("coverage", items=len(rows), absent=sum(1 for r in rows if r["level"] == "absent"))
    if args.json:
        print(json.dumps({"schema": "aurora-diag/coverage", "version": 1, "host": os.uname().nodename,
                          "kernel": os.uname().release, "items": rows}, default=str))
        return
    w = max(len(r["item"]) for r in rows) if rows else 10
    for r in rows:
        print("%-8s %-9s %-*s %s" % (r["level"].upper(), r["section"], min(w, 26), r["item"][:26], r["detail"]))


def cmd_snapshot(args, m):
    from . import snapshot
    snap = snapshot.build(m, api=not args.no_api)
    text = json.dumps(snap, indent=1, default=str)
    if args.output:
        with open(args.output, "w") as f:
            f.write(text + "\n")
        log.event("snapshot", file=args.output, elapsed_s=snap["elapsed_s"], probe_errors=snap["probe_errors"])
        if not args.json:
            print("snapshot written to %s (%d subsystems, %d services, %d sensor channels, %.1fs)" % (
                args.output, len(snap["subsystems"]), len(snap["services"]), len(snap["sensors"]), snap["elapsed_s"]),
                file=sys.stderr)
    if args.json or not args.output:
        print(text)


def cmd_diff(args, m):
    from . import snapshot
    a, b = snapshot.load(args.old), snapshot.load(args.new)
    ch = snapshot.diff(a, b)
    if args.quiet:
        ch = [c for c in ch if c["severity"] == "regression"]
    regress = sum(1 for c in ch if c["severity"] == "regression")
    log.event("diff", old=args.old, new=args.new, regressions=regress, changes=len(ch))
    if args.json:
        print(json.dumps({"schema": "aurora-diag/diff", "version": 1, "old": args.old, "new": args.new,
                          "old_kernel": a["kernel"]["release"], "new_kernel": b["kernel"]["release"],
                          "regressions": regress, "changes": ch}, default=str))
    else:
        print("%s (%s) -> %s (%s)" % (args.old, a["kernel"]["release"], args.new, b["kernel"]["release"]))
        sys.stdout.write(snapshot.render_diff(ch))
    return 2 if regress else (1 if ch else 0)


def cmd_clocks(args, m):
    from . import clocks
    report(args, clocks.all_clocks())


def cmd_subsys(args, m):
    from . import subsystems
    subs = subsystems.subsystems()
    if not args.all:
        subs = [s for s in subs if s["kind"] == "block"]
    if args.json:
        for s in subs:
            log.event("subsystem", **s)
        return
    log.event("subsystems", count=len(subs))
    for s in subs:
        print("%-5s %-34s %-22s %s" % (s["state"]["level"].upper(), (s.get("title") or s["block"])[:34], s["name"][:22], s["state"]["why"][:110]))


def cmd_services(args, m):
    from . import services
    report(args, services.all_services())


def cmd_screenshot(args, m):
    try:
        from . import dashboard, tui_textual
    except ImportError as e:
        raise control.Refused("screenshots need Textual (python3-textual): %s" % e)
    cap = dashboard.load(args.snapshot)
    if not args.no_redact:
        cap = dashboard.redact(cap, args.host)
    elif args.host:
        cap["host"] = args.host
    files = tui_textual.screenshots(cap, args.output, args.prefix or cap.get("host", "capture"),
                                    args.tab.split(",") if args.tab else None,
                                    "textual-light" if args.light else "textual-dark", args.png)
    log.event("screenshot", files=files)
    for f in files:
        print(f)


def cmd_tui(args, m):
    if args.dump:
        from . import dashboard
        print(dashboard.dump(m, args.dump))
        return
    if not args.curses:
        try:
            from . import tui_textual
        except ImportError as e:
            if args.snapshot:
                raise control.Refused("showing a capture needs Textual (python3-textual): %s" % e)
            print("Textual is not installed (python3-textual / python-textual): using the plain view", file=sys.stderr)
        else:
            return tui_textual.run(args, m)
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
    for name in ("list", "sensors"):
        s = sub.add_parser(name, help="every reading the kernel and the drivers expose" if name == "list" else "same as list")
        filt(s)
        s.add_argument("--debugfs", action="store_true", help="include allowlisted debugfs reports (root)")
        s.add_argument("--no-api", action="store_true", help="skip Vulkan and GL (they run vulkaninfo and an EGL probe)")
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
    x = sub.add_parser("fan-guard")
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
    x.add_argument("--from-log", action="store_true", help="restore the original values of the 'set' events in the log (use --session to pick one)")
    x = sub.add_parser("log", help="print or follow the JSON-lines log")
    x.add_argument("-n", "--lines", type=int, default=50, help="last N lines (0 = all)")
    x.add_argument("-f", "--follow", action="store_true")
    x.add_argument("--session")
    x.add_argument("--event", action="append", help="only this event (repeatable): set refuse restored sample ...")
    x = sub.add_parser("report", help="one bug report: machine, kernel, firmware, GPU, Mesa, Vulkan, GL, sensors")
    x.add_argument("-o", "--output", help="also write the JSON and text report into this directory")
    x = sub.add_parser("gpu", help="GPU: DRM devices, driver stats, clients, Mesa, Vulkan, GL")
    x.add_argument("--no-api", action="store_true", help="skip Vulkan and GL")
    x = sub.add_parser("check", help="pass/warn/fail per subsystem, service and probe; exit 0/1/2")
    x.add_argument("--only", help="comma list of groups or names (subsys,service,kernel,sensors,gpu,sep,dcp,...)")
    x.add_argument("--expect", help="baseline snapshot from a known-good boot: anything it had that is missing now fails")
    x.add_argument("--quiet", "-q", action="store_true", help="print only warnings and failures")
    x.add_argument("--verbose", "-v", action="store_true", help="list every row, infrastructure and skipped ones too")
    x.add_argument("--no-api", action="store_true", help="skip the Vulkan and GL probes")
    x.add_argument("--timeout", type=float, default=8.0, help="deadline per probe in seconds (default 8)")
    x = sub.add_parser("snapshot", help="save the machine's state for diff / check --expect")
    x.add_argument("-o", "--output", help="file to write (default: stdout)")
    x.add_argument("--no-api", action="store_true", help="skip Vulkan and GL")
    x = sub.add_parser("diff", help="what changed between two snapshots (exit 2 on regressions)")
    x.add_argument("old")
    x.add_argument("new")
    x.add_argument("--quiet", "-q", action="store_true", help="only regressions")
    sub.add_parser("clocks", help="CPU policies, DT DVFS ladders, devfreq, memory")
    x = sub.add_parser("subsys", help="every hardware block: DT, driver, firmware, devices, last error")
    x.add_argument("--all", action="store_true", help="include infrastructure nodes (DARTs, mailboxes, ...)")
    sub.add_parser("services", help="userspace services, Touch ID state, speaker protection")
    sub.add_parser("ane", help="ANE devices, firmware, clients, power, health and saved benchmark")
    sub.add_parser("compare", help="passive Linux readings vs the J616s macOS baseline")
    x = sub.add_parser("show", help="one category as the dashboard's drill-down shows it: " + ", ".join(
        ["health", "thermal", "cpu", "gpu", "memory", "display", "radio", "storage", "power", "audio", "engines", "sensors", "system", "all"]))
    x.add_argument("name", nargs="?", default="all")
    x.add_argument("--interval", type=float, default=0.5, help="CPU sampling interval for per-core load (default 0.5 s)")
    sub.add_parser("coverage", help="every category item: present, or absent with the reason")
    x = sub.add_parser("demo", help="list the demos, or run one (motion, speakers, ave, avd, ane, gpu, camera-*, captions-*, ...)")
    x.add_argument("name", nargs="?")
    x.add_argument("--seconds", type=float, default=0, help="live demos: stop after N seconds")
    x.add_argument("--stop", action="store_true", help="run the demo's off command (camera, mic denoise)")
    x.add_argument("--set", choices=["on", "off"], help="whitepoint: turn ambient white point adaptation on or off (user setting)")
    x = sub.add_parser("screenshot", help="render dashboard tabs of a capture to SVG (and PNG with --png)")
    x.add_argument("snapshot", help="a capture from `tui --dump FILE`")
    x.add_argument("-o", "--output", default=".", help="directory (default .)")
    x.add_argument("--prefix", help="file name prefix (default: the capture's host)")
    x.add_argument("--host", help="show this name instead of the capture's hostname")
    x.add_argument("--tab", help="comma list of tabs (overview,sensors,clocks,fans,gpu,ane,compare,subsys,services,coverage,log) "
                   "and drill-downs (show-cpu, show-health, show-audio, ...)")
    x.add_argument("--light", action="store_true", help="light theme")
    x.add_argument("--no-redact", action="store_true", help="keep Wi-Fi names, MAC addresses and the host name (redacted by default)")
    x.add_argument("--png", action="store_true", help="also write PNGs (rsvg-convert, cairosvg, or qlmanage+sips on macOS)")
    x = sub.add_parser("tui", help="interactive dashboard for humans")
    x.add_argument("--snapshot", help="show a saved `list --json` capture instead of this machine (read only)")
    x.add_argument("--curses", action="store_true", help="the plain curses view even when Textual is installed")
    x.add_argument("--dump", metavar="FILE", help="write a capture of everything the dashboard shows (10 s of samples) and exit")
    x = sub.add_parser("panic-log", help="the last panic iBoot kept for the next OS (macOS today), read only; needs root")
    x.add_argument("--device", help="read this block device instead of finding the panic-log namespace")
    x.add_argument("--lines", type=int, default=12, help="panic text lines to show (default 12)")
    x.add_argument("--full", action="store_true", help="the whole panic text")
    x.add_argument("--save", metavar="DIR", help="write the panic text (and other log) to DIR, mode 0600")
    x.add_argument("--raw", action="store_true", help="with --save, also the whole namespace image (holds machine identifiers)")
    sub.add_parser("log-path", help="where the log goes")
    return p


COMMANDS = {
    "machine": cmd_machine, "sensors": cmd_sensors, "list": cmd_sensors, "log": cmd_log,
    "report": cmd_report, "gpu": cmd_gpu, "ane": cmd_ane, "compare": cmd_compare, "check": cmd_check, "snapshot": cmd_snapshot,
    "diff": cmd_diff, "screenshot": cmd_screenshot, "show": cmd_show, "coverage": cmd_coverage, "demo": cmd_demo, "clocks": cmd_clocks, "subsys": cmd_subsys, "services": cmd_services, "summary": cmd_summary, "get": cmd_get,
    "set": cmd_set, "watch": cmd_watch, "fan": cmd_fan, "fan-guard": cmd_fan_guard,
    "haptic": cmd_haptic, "display": cmd_display, "kbd": cmd_kbd, "audio": cmd_audio,
    "journal": cmd_journal, "restore": cmd_restore, "tui": cmd_tui, "log-path": cmd_log_path,
    "panic-log": cmd_panic_log,
}


DOCUMENT_COMMANDS = ("panic-log", "check", "snapshot", "diff", "report", "gpu", "ane", "compare", "clocks", "services", "show", "coverage")
GLOBAL_FLAGS = ("--json", "--allow-unknown", "--dry-run")


def hoist_globals(argv):
    """Let the global options appear after the subcommand too
    (`aurora-diag list --json`)."""
    front, rest = [], []
    it = iter(argv)
    for a in it:
        if a in GLOBAL_FLAGS:
            front.append(a)
        elif a == "--log":
            front += [a, next(it, "")]
        elif a.startswith("--log="):
            front.append(a)
        else:
            rest.append(a)
    return front + rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(hoist_globals(argv))
    if not args.cmd:
        args.cmd = "summary"
        args.cat = args.source = args.grep = None
    # Commands that print one JSON document keep stdout for that document;
    # their events still go to the log file.
    log.setup(stdout_json=args.json and args.cmd not in DOCUMENT_COMMANDS, path=args.log)
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
    except (ValueError, OSError) as e:
        if args.cmd in ("diff", "check"):
            print("aurora-diag: %s" % e, file=sys.stderr)
            return 2
        raise
    return rc or 0
