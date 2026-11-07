# SPDX-License-Identifier: MIT
"""`aurora-diag check`: one pass/warn/fail line per subsystem, service and
probe, and an exit code (0 pass, 1 warn, 2 fail).

Without a baseline the verdicts come from the machine alone: a block the
device tree enables but no driver serves is a warning, a device whose
driver did not bind is a failure, a failed unit is a failure, any kernel
error line (-p 3) is a warning. With --expect BASELINE (a snapshot saved
from a known-good boot) anything the baseline had that is now missing
is a failure, and so is any new kernel error line.

Every probe runs under a deadline (probe.timed), so a hung driver shows
as a failed probe instead of a stalled run.
"""

import os
import re
import time

from . import ane, gpu, identity, services, snapshot, subsystems, sysfs
from .probe import timed

SCHEMA = "aurora-diag/check"
SCHEMA_VERSION = 1
LEVELS = {"pass": 0, "skip": 0, "warn": 1, "fail": 2}


def _r(group, name, level, reason, **extra):
    d = {"group": group, "name": name, "level": level, "reason": reason}
    d.update(extra)
    return d


def run(machine, only=None, expect=None, api=True, timeout=8.0, secs=None):
    t0 = time.monotonic()
    out = []
    base = snapshot.load(expect) if expect else None

    subs, err = timed(subsystems.subsystems, timeout)
    if err:
        out.append(_r("subsys", "probe", "fail", "subsystem probe " + err))
        subs = []
    seen = set()
    for s in subs:
        if s["kind"] != "block" and not (base and base["subsystems"].get(s["node"], {}).get("driver")):
            continue
        seen.add(s["node"])
        lvl = {"ok": "pass", "warn": "warn", "fail": "fail", "off": "skip"}[s["state"]["level"]]
        why = s["state"]["why"]
        if base:
            b = base["subsystems"].get(s["node"])
            if b and b.get("driver") and not s.get("driver"):
                lvl, why = "fail", "was bound to %s in the baseline; %s" % (b["driver"], why)
            elif b and not b.get("driver") and lvl == "warn" and "device" not in s:
                lvl = "pass"  # no driver for it in the baseline either: known gap
                why = "no driver (same as baseline)"
            if b:
                lost, _ = identity.class_delta(b, s, ignore_plugged=True)
                # USB descendants follow attached peripherals; compare binding only.
                if s.get("block") == "usb" or re.search(r"dwc3|xhci", s.get("driver") or ""):
                    lost = []
                if lost:
                    lvl = "fail"
                    why = "lost %s; %s" % (", ".join(lost), why)
        out.append(_r("subsys", "%s %s" % (s.get("title") or s["block"], s["name"]), lvl, why, node=s["node"],
                      infra=s["kind"] != "block"))
    if base:
        for node, b in base["subsystems"].items():
            if node not in seen and b.get("driver") and node not in {s["node"] for s in subs}:
                out.append(_r("subsys", node, "fail", "in the baseline (bound to %s), gone now" % b["driver"], node=node))

    units, err = timed(services.system_units, timeout)
    if err:
        out.append(_r("service", "probe", "fail", "systemctl " + err))
        units = []
    for u in units:
        lvl, why = "pass", "%s/%s" % (u["active"], u["sub"])
        if u["active"] == "failed":
            lvl = "fail"
        elif u["active"] != "active":
            why += " (%s, last result %s)" % (u["enabled"], u.get("result"))
            if u["enabled"] != "enabled":
                lvl = "skip"  # static, disabled, indirect: runs on demand
            elif u.get("result") == "success":
                lvl = "pass"
            else:
                lvl = "warn"
        if services.idle_activatable(u):
            lvl, why = "pass", "on-demand unit, idle and activatable"
        if base:
            was = base.get("services", {}).get(u["unit"])
            was_ok = was == "active" or services.snapshot_idle(u["unit"], was, base.get("service_details", {}))
            if was_ok and u["active"] != "active":
                if services.idle_activatable(u):
                    lvl, why = "pass", "on-demand unit, idle and activatable (active in the baseline)"
                else:
                    lvl, why = "fail", "active or activatable in the baseline, now " + u["active"]
        if lvl != "skip" or base:
            out.append(_r("service", u["unit"], lvl, why))
    if base:
        seen_units = {u["unit"] for u in units}
        for name, was in base.get("services", {}).items():
            if name not in seen_units and (was == "active" or services.snapshot_idle(name, was, base.get("service_details", {}))):
                out.append(_r("service", name, "fail", "active or activatable in the baseline, unit gone now"))

    sg, err = timed(services.speakerguardd, timeout)
    if not err and sg.get("v2") in ("active", "failed"):
        # speakerguardd v2 governs; the older daemons stay down beside it.
        lvl = "fail" if sg["v2"] == "failed" else ("pass" if sg.get("v2_graph") in (None, "pass") else "warn")
        out.append(_r("service", "speaker protection", lvl, "speakerguardd-v2 %s, graph %s, amps %s" % (
            sg["v2"], sg.get("v2_graph"), sg.get("v2_amps"))))
    elif not err and sg.get("service") is not None:
        lvl = "pass" if sg["service"] == "active" else ("fail" if sg["service"] == "failed" else "warn")
        out.append(_r("service", "speaker protection", lvl, "speakerguardd %s, armed %s, hottest coil %s C" % (
            sg["service"], sg.get("armed"), sg.get("hottest_coil_C"))))

    dm = subsystems.dmesg("emerg,alert,crit,err")
    if dm is None:
        out.append(_r("kernel", "dmesg -p 3", "skip", "dmesg unreadable (needs root)"))
    else:
        norm = sorted({snapshot._norm_dmesg(l) for l in dm})
        if base and base.get("dmesg_readable"):
            new = sorted(set(norm) - set(base.get("dmesg_errors", [])))
            out.append(_r("kernel", "dmesg -p 3", "fail" if new else "pass",
                          ("%d new error lines: %s" % (len(new), new[0][:120])) if new else
                          "%d error lines, none new" % len(dm), new=new))
        else:
            out.append(_r("kernel", "dmesg -p 3", "warn" if dm else "pass",
                          ("%d error lines, last: %s" % (len(dm), dm[-1][:120])) if dm else "no error lines"))

    if base:
        cur, err = timed(snapshot._sensor_ids, timeout)
        if not err:
            lost = sorted({snapshot.sensor_id(x) for x in base.get("sensors", [])} - set(cur))
            out.append(_r("sensors", "channels", "fail" if lost else "pass",
                          ("%d missing: %s" % (len(lost), ", ".join(lost[:5]))) if lost else "%d channels, none missing" % len(cur),
                          missing=lost))
    else:
        for cls, what in (("/sys/class/hwmon", "hwmon"), ("/sys/bus/iio/devices", "iio"), ("/sys/class/thermal", "thermal")):
            n = len(sysfs.listdir(cls))
            out.append(_r("sensors", what, "pass" if n else "warn", "%d devices" % n))
        als = [d for d in sysfs.listdir("/sys/bus/iio/devices") if d.startswith("iio:device")]
        for d in als:
            p = "/sys/bus/iio/devices/" + d
            name = sysfs.read(p + "/name") or d
            for f in sysfs.listdir(p):
                if f.startswith("in_") and (f.endswith("_input") or f.endswith("_raw")):
                    v, why = sysfs.read_error(p + "/" + f)
                    out.append(_r("sensors", "%s %s" % (name, f), "pass" if v is not None else "warn",
                                  ("reads %s" % v) if v is not None else "unreadable: %s" % why))

    gpus, err = timed(gpu.gpu_names, timeout)
    if err:
        out.append(_r("gpu", "drm", "fail", "probe " + err))
        gpus = []
    if not gpus:
        out.append(_r("gpu", "render node", "fail", "no DRM device with a render node"))
    for name, d in gpus or []:
        out.append(_r("gpu", name, "pass" if d.get("driver") else "fail",
                      "%s, %s" % (d.get("driver"), " ".join(d["nodes"]))))
    if api:
        vk, err = timed(_vk_summary, 30)
        if err or vk is None:
            out.append(_r("gpu", "vulkan", "fail", "vulkaninfo " + (err or "failed")))
        else:
            hw = [x for x in vk if "CPU" not in (x.get("deviceType") or "")]
            if not hw:
                out.append(_r("gpu", "vulkan", "fail", "only CPU devices: " + ", ".join(x.get("deviceName", "?") for x in vk)))
            for x in hw:
                lvl, why = "pass", "%s, %s %s, Vulkan %s, conformance %s" % (
                    x.get("deviceName"), x.get("driverName"), x.get("driverVersion"), x.get("apiVersion"), x.get("conformanceVersion"))
                if base:
                    b = ((base.get("gpu") or {}).get("vulkan") or {}).get("devices", {}).get(x.get("deviceName"))
                    if b and b.get("apiVersion") and _v(x.get("apiVersion")) < _v(b["apiVersion"]):
                        lvl, why = "fail", "API version dropped from %s: %s" % (b["apiVersion"], why)
                out.append(_r("gpu", "vulkan", lvl, why))
        gl, err = timed(gpu.gl, 30, False)
        if err:
            out.append(_r("gpu", "gl", "fail", "glprobe " + err))
        else:
            for apiname in ("gl_core", "gles"):
                info = gl.get(apiname)
                if not info:
                    out.append(_r("gpu", apiname, "fail", gl.get("error") or "no context"))
                    continue
                lvl = "pass" if info["path"] == "native" else "warn"
                why = "%s | %s | %d extensions | %s" % (info["renderer"], info["version"], info["extension_count"], info["path"])
                if base:
                    b = ((base.get("gpu") or {}).get("gl") or {}).get(apiname) or {}
                    if b.get("extension_count") and info["extension_count"] < b["extension_count"]:
                        lvl, why = "fail", "extensions dropped from %d: %s" % (b["extension_count"], why)
                    elif lvl == "warn" and b.get("path") == info["path"]:
                        lvl, why = "pass", "same %s path as the baseline: %s" % (info["path"], why)
                out.append(_r("gpu", apiname, lvl, why))

    an, err = timed(ane.collect, timeout)
    if err:
        out.append(_r("ane", "probe", "fail", "ANE probe " + err))
    else:
        out.extend(ane.checks(an))
        if base:
            current = {d["platform_device"]: d for d in an["devices"]}
            for name, was in base.get("ane", {}).items():
                now = current.get(name)
                if was.get("driver") and (not now or not now.get("driver")):
                    out.append(_r("ane", name, "fail", "ANE device/driver present in baseline is missing"))

    if secs is None:
        from . import sections
        secs, err = timed(lambda: sections.collect([n for n in sections.ORDER if n != "health"]), 30)
        if err:
            out.append(_r("inventory", "probe", "fail", "inventory " + err))
            secs = {}
    out.extend(inventory_rows(secs, base))

    if base and base.get("warnings") is not None:
        known = set(base["warnings"])
        for r in out:
            if r["level"] == "warn" and "%s/%s" % (r["group"], r["name"]) in known:
                r["level"], r["reason"] = "pass", "known in the baseline: " + r["reason"]

    if only:
        want = [w.strip().lower() for w in only.split(",") if w.strip()]
        # Category rows match by their group or category name (--only display, --only radio).
        out = [r for r in out if any(w in r["group"].lower() or (w == r.get("section") if r["group"] == "inventory"
                                                                  else w in r["name"].lower()) for w in want)]
    worst = max([LEVELS[r["level"]] for r in out] + [0])
    return {"schema": SCHEMA, "version": SCHEMA_VERSION,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "host": os.uname().nodename, "kernel": os.uname().release,
            "baseline": expect, "result": {0: "pass", 1: "warn", 2: "fail"}[worst], "exit": worst,
            "counts": {k: sum(1 for r in out if r["level"] == k) for k in ("pass", "warn", "fail", "skip")},
            "checks": out, "elapsed_s": round(time.monotonic() - t0, 2)}


# Items whose presence follows what is plugged in, not the kernel.
VOLATILE = re.compile(r"^(display/(DP|HDMI|USB|DSI)-|system/(USB4|Type-C)|power/external power|radio/other)")


def inventory_rows(secs, base=None):
    """One row per category item (sections.py): ok passes, warn warns, a
    fail fails, absent and off are skipped with their reason. With a
    baseline, an item that was ok there and is absent or failing now fails,
    and a warning the baseline shares passes as a known gap."""
    from . import sections
    out = []
    bitems = (base or {}).get("inventory_items") or {}
    seen = set()
    for row in sections.coverage(secs):
        key = "%s/%s" % (row["section"], row["item"])
        seen.add(key)
        lvl = {"ok": "pass", "warn": "warn", "fail": "fail"}.get(row["level"], "skip")
        why = ("%s: %s" % (row["level"], row["detail"])) if lvl == "skip" else row["detail"]
        if base is not None and key in bitems:
            b = bitems[key]
            if b == "ok" and row["level"] in ("absent", "fail", "warn") and not VOLATILE.match(key):
                lvl, why = ("fail" if row["level"] != "warn" else "warn"), "ok in the baseline, now %s: %s" % (row["level"], row["detail"])
            elif lvl in ("warn", "fail") and b == row["level"]:
                lvl, why = "pass", "same as baseline: " + row["detail"]
        out.append(_r("inventory", key, lvl, why[:300], section=row["section"]))
    if base is not None:
        for key, b in sorted(bitems.items()):
            if key not in seen and b == "ok" and not VOLATILE.match(key) and key.split("/")[0] in secs:
                out.append(_r("inventory", key, "fail", "ok in the baseline, gone now", section=key.split("/")[0]))
    return out


def _v(s):
    try:
        return tuple(int(x) for x in (s or "0").split("."))
    except ValueError:
        return (0,)


def _vk_summary():
    import shutil
    if not shutil.which("vulkaninfo"):
        return None
    rc, out, _ = gpu._run(["vulkaninfo", "--summary"], env=gpu._env(), timeout=25)
    if not out:
        return None
    return gpu._parse_summary(out)[1]


def render(res, quiet=False, verbose=False):
    """Short by default: passing infrastructure nodes and skipped checks are
    counted on one line each; --verbose lists every row."""
    mark = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "skip"}
    lines = []
    w = max([len(r["name"]) for r in res["checks"]] + [10])
    w = min(w, 44)
    infra_ok = [r for r in res["checks"] if r.get("infra") and r["level"] == "pass"]
    skipped = [r for r in res["checks"] if r["level"] == "skip"]
    for r in res["checks"]:
        if quiet and r["level"] in ("pass", "skip"):
            continue
        if not verbose and (r in infra_ok or r in skipped):
            continue
        lines.append("%s  %-8s %-*s %s" % (mark[r["level"]], r["group"], w, r["name"][:w], r["reason"][:160]))
    if not verbose and not quiet:
        if infra_ok:
            lines.append("PASS  %-8s %-*s %s" % ("subsys", w, "infrastructure", "%d nodes (DARTs, mailboxes, buses, ...) bound" % len(infra_ok)))
        if skipped:
            lines.append("skip  %-8s %-*s %s" % ("", w, "%d checks" % len(skipped), "disabled in the DT or on-demand units (-v lists them)"))
    c = res["counts"]
    lines.append("%s: %d pass, %d warn, %d fail, %d skip  (%s, %s%s, %.1fs)" % (
        res["result"].upper(), c["pass"], c["warn"], c["fail"], c["skip"], res["host"], res["kernel"],
        (", baseline " + os.path.basename(res["baseline"])) if res["baseline"] else "", res["elapsed_s"]))
    return "\n".join(lines) + "\n"
