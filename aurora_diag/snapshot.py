# SPDX-License-Identifier: MIT
"""Snapshots: the state of a machine that should not change between two
kernel revisions unless someone meant it to, and the diff of two of them.

A snapshot records what is present and how it is configured, not values
that move by themselves (temperatures, power): which drivers are bound to
which blocks, firmware versions, class devices, kernel errors, services,
clock ladders, the GPU's Vulkan and GL identity and counts, and which
sensor channels exist. Schema: docs/SCHEMA.md ("aurora-diag/snapshot", 1).
"""

import json
import os
import re
import socket
import time

from . import __version__, ane, clocks, gpu, services, subsystems, sysfs
from .probe import timed

SCHEMA = "aurora-diag/snapshot"
SCHEMA_VERSION = 1


def _sensor_ids():
    """Every hwmon/iio/thermal channel by stable name (label if any)."""
    out = []
    base = "/sys/class/hwmon"
    for h in sysfs.listdir(base):
        d = base + "/" + h
        name = sysfs.read(d + "/name") or h
        for f in sysfs.listdir(d):
            m = re.fullmatch(r"([a-z]+)(\d+)_input", f)
            if m:
                lab = sysfs.read("%s/%s%s_label" % (d, m.group(1), m.group(2)))
                out.append("hwmon/%s/%s" % (name, lab or (m.group(1) + m.group(2))))
    for d in sysfs.listdir("/sys/bus/iio/devices"):
        if d.startswith("iio:device"):
            p = "/sys/bus/iio/devices/" + d
            name = sysfs.read(p + "/name") or d
            for f in sysfs.listdir(p):
                if f.startswith("in_") and (f.endswith("_raw") or f.endswith("_input")):
                    out.append("iio/%s/%s" % (name, f))
    for z in sysfs.listdir("/sys/class/thermal"):
        if z.startswith("thermal_zone"):
            out.append("thermal/%s" % (sysfs.read("/sys/class/thermal/%s/type" % z) or z))
    return sorted(set(out))


def _norm_dmesg(line):
    """Kernel log lines without the parts that change every boot."""
    line = re.sub(r"0x[0-9a-fA-F]+", "0x?", line)
    line = re.sub(r"\b\d+\b", "N", line)
    return line.strip()


def build(machine, api=True, budget=None):
    """The snapshot dict. budget: per-probe timeout seconds."""
    t0 = time.monotonic()
    errors = {}

    def probe(name, fn, default, timeout=10):
        v, err = timed(fn, timeout if budget is None else budget)
        if err:
            errors[name] = err
            return default
        return v

    subs = probe("subsystems", subsystems.subsystems, [])
    svc = probe("services", services.system_units, [])
    users = probe("user_services", services.user_units, [])
    clk = probe("clocks", clocks.all_clocks, {})
    an = probe("ane", ane.collect, {"devices": []})
    snap = {
        "schema": SCHEMA, "version": SCHEMA_VERSION, "tool_version": __version__,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": socket.gethostname(),
        "machine": {"model": machine.model, "compatible": machine.compatible},
        "kernel": {"release": os.uname().release, "version": os.uname().version,
                   "cmdline": sysfs.read("/proc/cmdline")},
        "firmware": machine.firmware(),
        "ane": {d["platform_device"]: {k: d[k] for k in ("driver", "generation", "firmware_version", "compatible")}
                for d in an["devices"]},
        "subsystems": {s["node"]: {"title": s.get("title") or s["block"], "kind": s["kind"],
                                   "driver": s.get("driver"), "level": s["state"]["level"],
                                   "why": s["state"]["why"], "dt_status": s["dt_status"],
                                   "dt_firmware": s.get("dt_firmware"),
                                   "class_devices": sorted(s.get("class_devices") or [])}
                       for s in subs},
        "services": {u["unit"]: u["active"] for u in svc},
        "user_services": {"%s/%s" % (u["user"], u["unit"]): u["active"] for u in users},
        "clocks": {
            "cpu": {p["policy"]: {"ladder_MHz": p["ladder_MHz"], "hw_min_MHz": p["hw_min_MHz"],
                                  "hw_max_MHz": p["hw_max_MHz"], "governor": p["governor"], "driver": p["driver"]}
                    for p in clk.get("cpu", [])},
            "dt_ladders": {k: [o["MHz"] for o in v["opps"]] for k, v in clk.get("dt_ladders", {}).items()},
            "memory": clk.get("memory"),
        },
        "sensors": probe("sensors", _sensor_ids, []),
        "dmesg_errors": sorted({_norm_dmesg(l) for l in (subsystems.dmesg("emerg,alert,crit,err") or [])}),
        "dmesg_readable": subsystems.dmesg("emerg,alert,crit,err") is not None,
        "gpu": {"devices": {n: {"driver": d["driver"], "nodes": d["nodes"],
                                "kernel_driver": (d.get("kernel_driver") or {}).get("version")}
                            for n, d in probe("gpu", gpu.gpu_names, [])}},
    }
    if api:
        vk = probe("vulkan", gpu.vulkan, {"error": "timed out"}, 60)
        gl = probe("gl", gpu.gl, {"error": "timed out"}, 60)
        snap["gpu"]["vulkan"] = ({"error": vk["error"]} if "error" in vk else
                                 {"instance": vk.get("instance_version"),
                                  "devices": {d.get("deviceName"): {k: d.get(k) for k in (
                                      "apiVersion", "driverVersion", "driverID", "driverName", "driverInfo",
                                      "conformanceVersion", "extension_count", "features_enabled",
                                      "features_total", "format_count")} for d in vk.get("devices", [])}})
        snap["gpu"]["gl"] = {api_: ({k: gl[api_].get(k) for k in ("renderer", "version", "glsl_version", "profile",
                                                                  "extension_count", "path")} if gl.get(api_) else None)
                             for api_ in ("gl_core", "gl_compat", "gles")}
        if gl.get("error"):
            snap["gpu"]["gl"]["error"] = gl["error"]
        try:
            from . import compliance
            snap["gpu"]["compliance"] = compliance.summary(vk, gl)
        except Exception as e:  # compliance is advisory
            errors["compliance"] = repr(e)
    from . import sections
    secs = probe("inventory", lambda: sections.collect([n for n in sections.ORDER if n != "health"], subs=subs), {}, 40)
    snap["inventory"] = sections.facts(secs)
    snap["inventory_items"] = {"%s/%s" % (r["section"], r["item"]): r["level"] for r in sections.coverage(secs)}
    # Warnings this boot already has: check --expect treats them as known gaps.
    from . import check
    ck = probe("check", lambda: check.run(machine, api=False, secs=secs), None, 30)
    snap["warnings"] = sorted("%s/%s" % (r["group"], r["name"]) for r in (ck or {}).get("checks", []) if r["level"] == "warn")
    snap["probe_errors"] = errors
    snap["elapsed_s"] = round(time.monotonic() - t0, 2)
    return snap


def load(path):
    with open(path) as f:
        s = json.load(f)
    if s.get("schema") != SCHEMA:
        raise ValueError("%s is not an aurora-diag snapshot" % path)
    if s.get("version", 0) > SCHEMA_VERSION:
        raise ValueError("%s has schema version %s, newer than this tool (%d)" % (path, s.get("version"), SCHEMA_VERSION))
    return s


# -- diff ---------------------------------------------------------------------

def _change(out, sev, section, key, old, new, why=""):
    out.append({"severity": sev, "section": section, "key": key, "old": old, "new": new, "why": why})


def diff(a, b):
    """Changes from snapshot a (old) to b (new), each with a severity:
    regression, change or improvement."""
    out = []
    for k in ("release", "version"):
        if a["kernel"].get(k) != b["kernel"].get(k):
            _change(out, "change", "kernel", k, a["kernel"].get(k), b["kernel"].get(k))
    for k in sorted(set(a["firmware"]) | set(b["firmware"])):
        if a["firmware"].get(k) != b["firmware"].get(k):
            _change(out, "change", "firmware", k, a["firmware"].get(k), b["firmware"].get(k))
    rank = {"ok": 3, "warn": 2, "fail": 1, "off": 0}
    sa, sb = a["subsystems"], b["subsystems"]
    for k in sorted(set(sa) | set(sb)):
        x, y = sa.get(k), sb.get(k)
        if x is None:
            _change(out, "change", "subsystems", k, None, y["level"], "new node: " + y["why"])
            continue
        if y is None:
            _change(out, "regression" if x["driver"] else "change", "subsystems", k, x["level"], None, "node gone from the device tree")
            continue
        if x.get("driver") != y.get("driver"):
            sev = "regression" if x.get("driver") and not y.get("driver") else ("improvement" if y.get("driver") and not x.get("driver") else "change")
            _change(out, sev, "subsystems", k + " driver", x.get("driver"), y.get("driver"), y["why"])
        if x["level"] != y["level"]:
            sev = "regression" if rank.get(y["level"], 0) < rank.get(x["level"], 0) else "improvement"
            _change(out, sev, "subsystems", k + " state", x["level"], y["level"], y["why"])
        if x.get("dt_firmware") != y.get("dt_firmware"):
            _change(out, "change", "subsystems", k + " firmware", x.get("dt_firmware"), y.get("dt_firmware"))
        lost = sorted(set(x["class_devices"]) - set(y["class_devices"]))
        gained = sorted(set(y["class_devices"]) - set(x["class_devices"]))
        # Numbered class devices (hidraw3 -> hidraw4) are renames, not losses.
        strip = lambda l: sorted(re.sub(r"\d+$", "", c) for c in l)
        if strip(lost) != strip(gained):
            if lost:
                _change(out, "regression", "subsystems", k + " class devices", lost, None, "lost")
            if gained:
                _change(out, "improvement", "subsystems", k + " class devices", None, gained, "new")
    for sec in ("services", "user_services"):
        xa, xb = a.get(sec, {}), b.get(sec, {})
        for k in sorted(set(xa) | set(xb)):
            if xa.get(k) != xb.get(k):
                sev = "regression" if xa.get(k) == "active" else ("improvement" if xb.get(k) == "active" else "change")
                _change(out, sev, sec, k, xa.get(k), xb.get(k))
    ca, cb = a.get("clocks", {}), b.get("clocks", {})
    for k in sorted(set(ca.get("cpu", {})) | set(cb.get("cpu", {}))):
        x, y = ca.get("cpu", {}).get(k), cb.get("cpu", {}).get(k)
        if x != y:
            sev = "regression" if (x and y and (y.get("hw_max_MHz") or 0) < (x.get("hw_max_MHz") or 0)) or (x and not y) else "change"
            _change(out, sev, "clocks", "cpu " + k, x, y)
    for k in sorted(set(ca.get("dt_ladders", {})) | set(cb.get("dt_ladders", {}))):
        if ca.get("dt_ladders", {}).get(k) != cb.get("dt_ladders", {}).get(k):
            _change(out, "change", "clocks", "ladder " + k, ca.get("dt_ladders", {}).get(k), cb.get("dt_ladders", {}).get(k))
    lost = sorted(set(a.get("sensors", [])) - set(b.get("sensors", [])))
    new = sorted(set(b.get("sensors", [])) - set(a.get("sensors", [])))
    if lost:
        _change(out, "regression", "sensors", "missing", lost, None, "%d channels gone" % len(lost))
    if new:
        _change(out, "improvement", "sensors", "new", None, new, "%d new channels" % len(new))
    if a.get("dmesg_readable") and b.get("dmesg_readable"):
        newerr = sorted(set(b["dmesg_errors"]) - set(a["dmesg_errors"]))
        gone = sorted(set(a["dmesg_errors"]) - set(b["dmesg_errors"]))
        if newerr:
            _change(out, "regression", "dmesg", "new error lines", None, newerr, "%d new -p 3 lines" % len(newerr))
        if gone:
            _change(out, "improvement", "dmesg", "errors gone", gone, None)
    ga, gb = a.get("gpu", {}), b.get("gpu", {})
    aa, ab = a.get("ane", {}), b.get("ane", {})
    for name in sorted(set(aa) | set(ab)):
        x, y = aa.get(name), ab.get(name)
        if x != y:
            sev = "regression" if x and (not y or (x.get("driver") and not y.get("driver"))) else "change"
            _change(out, sev, "ane", name, x, y)
    if ga.get("devices") != gb.get("devices"):
        _change(out, "change", "gpu", "devices", ga.get("devices"), gb.get("devices"))
    va, vb = (ga.get("vulkan") or {}).get("devices", {}), (gb.get("vulkan") or {}).get("devices", {})
    for name in sorted(set(va) | set(vb)):
        x, y = va.get(name), vb.get(name)
        if x is None or y is None:
            _change(out, "regression" if y is None else "improvement", "vulkan", name, bool(x), bool(y))
            continue
        for k in sorted(set(x) | set(y)):
            if x.get(k) != y.get(k):
                sev = "change"
                if isinstance(x.get(k), int) and isinstance(y.get(k), int):
                    sev = "regression" if y[k] < x[k] else "improvement"
                _change(out, sev, "vulkan", "%s %s" % (name, k), x.get(k), y.get(k))
    gla, glb = ga.get("gl") or {}, gb.get("gl") or {}
    for api in ("gl_core", "gl_compat", "gles"):
        x, y = gla.get(api), glb.get(api)
        if (x is None) != (y is None):
            _change(out, "regression" if y is None else "improvement", "gl", api, bool(x), bool(y))
            continue
        for k in sorted(set(x or {}) | set(y or {})):
            if (x or {}).get(k) != (y or {}).get(k):
                sev = "change"
                if k == "extension_count":
                    sev = "regression" if y[k] < x[k] else "improvement"
                if k == "path" and y.get(k) != "native":
                    sev = "regression"
                _change(out, sev, "gl", "%s %s" % (api, k), x.get(k), y.get(k))
    ia, ib = a.get("inventory_items") or {}, b.get("inventory_items") or {}
    if ia and ib:
        for k in sorted(set(ia) | set(ib)):
            x, y = ia.get(k), ib.get(k)
            if x == y:
                continue
            good = ("ok",)
            sev = "regression" if x in good and y not in good else ("improvement" if y in good and x not in good else "change")
            _change(out, sev, "inventory", k, x, y)
    fa, fb = _flat(a.get("inventory") or {}), _flat(b.get("inventory") or {})
    if fa and fb:
        for k in sorted(set(fa) | set(fb)):
            if fa.get(k) != fb.get(k):
                x, y = fa.get(k), fb.get(k)
                sev = "regression" if (x and not y and re.search(r"(driver|bound|firmware|model)", k)) else "change"
                _change(out, sev, "inventory", k, x, y)
    cpa, cpb = (ga.get("compliance") or {}), (gb.get("compliance") or {})
    for k in sorted(set(cpa) | set(cpb)):
        x, y = cpa.get(k) or {}, cpb.get(k) or {}
        if x.get("missing") != y.get("missing"):
            more = len(y.get("missing") or []) > len(x.get("missing") or [])
            _change(out, "regression" if more else "improvement", "compliance", k, x.get("missing"), y.get("missing"))
    return out


def _flat(d, pre=""):
    out = {}
    for k, v in d.items():
        key = "%s.%s" % (pre, k) if pre else str(k)
        if isinstance(v, dict) and v:
            out.update(_flat(v, key))
        else:
            out[key] = json.dumps(v, sort_keys=True, default=str) if isinstance(v, (list, dict)) else v
    return out


def render_diff(changes):
    if not changes:
        return "no changes\n"
    mark = {"regression": "-", "improvement": "+", "change": "~"}
    lines = []
    for c in sorted(changes, key=lambda c: ({"regression": 0, "change": 1, "improvement": 2}[c["severity"]], c["section"], c["key"])):
        def short(v):
            s = json.dumps(v, default=str) if not isinstance(v, str) else v
            return s if len(s) <= 120 else s[:117] + "..."
        lines.append("%s %-11s %-40s %s -> %s%s" % (mark[c["severity"]], c["section"], c["key"][:40], short(c["old"]),
                                                    short(c["new"]), ("  (%s)" % c["why"][:120]) if c["why"] else ""))
    n = {s: sum(1 for c in changes if c["severity"] == s) for s in ("regression", "change", "improvement")}
    lines.append("%d regressions, %d changes, %d improvements" % (n["regression"], n["change"], n["improvement"]))
    return "\n".join(lines) + "\n"
