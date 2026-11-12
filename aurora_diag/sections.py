# SPDX-License-Identifier: MIT
"""The categories the dashboard groups everything into, one function each.

Every category returns the same shape, so the overview tile, the drill-down
screen, `aurora-diag show NAME [--json]`, the coverage table and the
snapshot all come from one place:

    name, title     "cpu", "CPU"
    level           ok / warn / fail / off: the tile's colour
    headline        two short lines for the overview tile
    spark           one number for the tile's sparkline (or None)
    items           present-or-absent facts, each {key, level, detail}:
                    level "absent" says the machine does not expose it and
                    detail says why ("no accel node under /aop@... in the
                    live device tree"), never guessed
    tables          [{title, columns, rows}]: the drill-down's detail
    facts           stable values for snapshots and diffs (no live numbers)

Nothing here is a per-machine table: each category walks kernel classes,
buses, the device tree and D-Bus, and says plainly when a source is not
there.
"""

import json
import os
import re
import shutil
import socket
import struct
import subprocess
import time

from . import sysfs

DT = "/sys/firmware/devicetree/base"
LEVEL_RANK = {"fail": 3, "warn": 2, "ok": 1, "absent": 0, "off": 0}


def item(key, level, detail, **fields):
    d = {"key": key, "level": level, "detail": detail}
    d.update(fields)
    return d


def table(title, columns, rows):
    return {"title": title, "columns": list(columns), "rows": [list(r) for r in rows]}


def section(name, title, items=(), tables=(), headline="", spark=None, facts=None, level=None, note=None):
    items = list(items)
    if level is None:
        worst = max([LEVEL_RANK.get(i["level"], 0) for i in items] + [0])
        level = {3: "fail", 2: "warn", 1: "ok", 0: "off"}[worst]
    d = {"name": name, "title": title, "level": level, "headline": headline, "spark": spark,
         "items": items, "tables": list(tables), "facts": facts or {}}
    if note:
        d["note"] = note
    return d


def _run(cmd, timeout=6, env=None, user=None):
    """(rc, stdout) of a read-only helper; (None, '') if it is missing."""
    if not shutil.which(cmd[0]):
        return None, ""
    if user is not None and os.geteuid() == 0 and user.pw_uid != 0:
        e = {"XDG_RUNTIME_DIR": "/run/user/%d" % user.pw_uid,
             "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % user.pw_uid,
             "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": user.pw_dir, "LANG": "C.UTF-8"}
        if shutil.which("runuser"):
            cmd = ["runuser", "-u", user.pw_name, "--", "env"] + ["%s=%s" % kv for kv in e.items()] + cmd
        env = None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return None, ""


def _desktop_user():
    """The user with a running session bus (for gdctl, wpctl, ...)."""
    try:
        from . import services
        users = services.desktop_users()
    except Exception:
        users = []
    for u in users:
        if os.path.exists("/run/user/%d/bus" % u.pw_uid):
            if os.geteuid() == 0 or os.geteuid() == u.pw_uid:
                return u
    return None


def _num(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        try:
            return float(s)
        except (TypeError, ValueError):
            return None


def _fmt_bytes(n):
    if n is None:
        return "-"
    for u in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or u == "TiB":
            return ("%.1f %s" % (n, u)) if u != "B" else "%d B" % n
        n /= 1024.0


def _dt_nodes(pattern):
    """DT node paths (relative) whose name or compatible matches pattern."""
    rx = re.compile(pattern, re.I)
    out = []

    def walk(rel, depth):
        if depth > 4:
            return
        p = DT + ("/" + rel if rel else "")
        for e in sysfs.listdir(p):
            r = rel + "/" + e if rel else e
            if not sysfs.isdir(DT + "/" + r):
                continue
            comp = " ".join(sysfs.dt_strings(DT + "/" + r + "/compatible"))
            if rx.search(e.split("@")[0]) or (comp and rx.search(comp)):
                out.append(r)
            walk(r, depth + 1)
    walk("", 0)
    return out


def _dt_status(rel):
    s = sysfs.dt_value(DT + "/" + rel + "/status")
    return s or "okay"


_MODALIAS = {}


def kernel_knows(compat_rx):
    """Compatibles matching compat_rx that this kernel has a driver for
    (modules.alias and modules.builtin.modinfo)."""
    rel = os.uname().release
    if compat_rx in _MODALIAS:
        return _MODALIAS[compat_rx]
    rx = re.compile(compat_rx, re.I)
    found = set()
    for f, sep in (("modules.alias", None), ("modules.builtin.modinfo", "\0")):
        p = "/lib/modules/%s/%s" % (rel, f)
        try:
            with open(sysfs.root(p), "rb") as fh:
                text = fh.read().decode("utf-8", "replace")
        except OSError:
            continue
        for line in (text.split(sep) if sep else text.splitlines()):
            m = re.search(r"of:N\*T\*C([^\s*]+)", line)
            if m and rx.search(m.group(1)):
                found.add(m.group(1).rstrip("C"))
            m = re.search(r"\.alias=of:N\*T\*C([^\s*\0]+)", line)
            if m and rx.search(m.group(1)):
                found.add(m.group(1).rstrip("C"))
    _MODALIAS[compat_rx] = sorted(found)
    return _MODALIAS[compat_rx]


# -- health --------------------------------------------------------------------

def health(m=None, check_result=None):
    from . import check
    from .machine import Machine
    res = check_result or check.run(m or Machine(), api=False)
    rows = []
    for r in sorted(res["checks"], key=lambda r: ({"fail": 0, "warn": 1, "pass": 2, "skip": 3}[r["level"]], r["group"], r["name"])):
        rows.append([r["level"], r["group"], r["name"], r["reason"]])
    c = res["counts"]
    lvl = {"pass": "ok", "warn": "warn", "fail": "fail"}[res["result"]]
    items = [item("check", lvl, "%s: %d pass, %d warn, %d fail, %d skip" % (res["result"].upper(), c["pass"], c["warn"], c["fail"], c["skip"]))]
    return section("health", "Health", items, [table("Checks (fail, warn, pass, skip)", ("level", "group", "check", "reason"), rows)],
                   headline="%s\n%d pass  %d warn  %d fail" % (res["result"].upper(), c["pass"], c["warn"], c["fail"]),
                   level=lvl, facts={"counts": c})


# -- thermal -------------------------------------------------------------------

def thermal(readings=None):
    from . import fans, sources
    rs = readings if readings is not None else sources.collect(["hwmon", "thermal"])
    name, hot = fans.hottest()
    temps = [r for r in rs if r["cat"] == "temp" and r["attr"].endswith("_input")]
    zones = [r for r in rs if r["cat"] == "thermal" and r["attr"] == "temp"]
    rows = []
    for r in sorted(temps + zones, key=lambda r: -(r["value"] if isinstance(r["value"], (int, float)) else -999)):
        v = r["value"]
        rows.append([r.get("label") or r["attr"], ("%.1f C" % v) if isinstance(v, (int, float)) else str(v),
                     r["dev"], r["id"]])
    trips = []
    for r in rs:
        if r["cat"] == "thermal" and r["attr"].startswith("trip_point_") and r["attr"].endswith("_temp"):
            typ = sysfs.read(r["path"].replace("_temp", "_type")) or ""
            zt = sysfs.read(os.path.dirname(r["path"]) + "/type") or r["dev"]
            trips.append([zt, r["attr"].replace("trip_point_", "trip ").replace("_temp", ""), typ,
                          ("%.1f C" % r["value"]) if isinstance(r["value"], (int, float)) else r["value"]])
    cooling = []
    for c in sysfs.listdir("/sys/class/thermal"):
        if c.startswith("cooling_device"):
            d = "/sys/class/thermal/" + c
            cooling.append([sysfs.read(d + "/type") or c, "%s / %s" % (sysfs.read(d + "/cur_state"), sysfs.read(d + "/max_state")), c])
    policies = []
    for z in sysfs.listdir("/sys/class/thermal"):
        if z.startswith("thermal_zone"):
            d = "/sys/class/thermal/" + z
            policies.append([sysfs.read(d + "/type") or z, sysfs.read(d + "/policy") or "-", sysfs.read(d + "/mode") or "-", z])
    fl = fans.fans()
    unread = [r for r in temps if not isinstance(r["value"], (int, float))]
    items = [item("temperature sensors", "ok" if temps else "absent",
                  "%d hwmon temperature channels%s" % (len(temps), (", %d unreadable" % len(unread)) if unread else "") if temps else "no hwmon temperature channel"),
             item("thermal zones", "ok" if zones else "absent",
                  "%d zones, %d trip points" % (len(zones), len(trips)) if zones else "no thermal zone (the device tree has no thermal-zones node for this machine)"),
             item("cooling devices", "ok" if cooling else "absent", "%d cooling devices" % len(cooling) if cooling else "no cooling device registered"),
             item("fans", "ok" if fl else "absent", ", ".join("%s %s rpm" % (f["label"], f["rpm"]) for f in fl) if fl else "fanless (no fan channel in hwmon)")]
    if hot is None:
        num = [r for r in temps if isinstance(r["value"], (int, float))]
        if num:
            top = max(num, key=lambda r: r["value"])
            hot, name = top["value"], "%s (hwmon)" % (top.get("label") or top["attr"])
    lvl = "fail" if (hot or 0) >= 100 else "warn" if (hot or 0) >= 90 else "ok"
    tables = [table("Every temperature, hottest first", ("sensor", "now", "device", "id"), rows)]
    if trips:
        tables.append(table("Trip points", ("zone", "trip", "type", "temperature"), trips))
    if policies:
        tables.append(table("Thermal zone governors", ("zone", "policy", "mode", "node"), policies))
    if cooling:
        tables.append(table("Cooling devices", ("type", "state / max", "node"), cooling))
    if fl:
        tables.append(table("Fans", ("fan", "rpm", "target", "min", "max"), [[f["label"], f["rpm"], f["target"], f["min"], f["max"]] for f in fl]))
    return section("thermal", "Thermal", items, tables,
                   headline=("%.1f C hottest\n%s" % (hot, (name or "").replace("zone:", ""))) if hot is not None else "no die temperature",
                   spark=hot, level=lvl if hot is not None else "off",
                   facts={"zones": sorted({r.get("label") for r in zones if r.get("label")}),
                          "cooling": sorted(c[0] for c in cooling), "fans": len(fl), "temp_channels": len(temps)})


# -- CPU -----------------------------------------------------------------------

_prev = {"stat": None, "t": None, "procs": {}, "ptime": None}
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _proc_stat():
    out = {}
    for line in (sysfs.read("/proc/stat", limit=1 << 20) or "").splitlines():
        if line.startswith("cpu"):
            f = line.split()
            vals = [int(x) for x in f[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            out[f[0]] = (sum(vals[:8]), idle)
    return out


def _util(now, prev):
    out = {}
    for k, (tot, idle) in now.items():
        p = prev.get(k) if prev else None
        if p and tot - p[0] > 0:
            out[k] = round(100.0 * (1 - (idle - p[1]) / float(tot - p[0])), 1)
        elif tot:
            out[k] = round(100.0 * (1 - idle / float(tot)), 1)
    return out


def _top_procs(n=12):
    now = time.monotonic()
    cur = {}
    for pid in os.listdir(sysfs.root("/proc")):
        if not pid.isdigit():
            continue
        try:
            with open(sysfs.root("/proc/%s/stat" % pid)) as f:
                s = f.read()
        except OSError:
            continue
        r = s.rfind(")")
        comm = s[s.find("(") + 1:r]
        f = s[r + 2:].split()
        if len(f) < 22:
            continue
        cur[pid] = (comm, int(f[11]) + int(f[12]), int(f[21]) * os.sysconf("SC_PAGE_SIZE"), int(f[36]) if len(f) > 36 else None)
    prev, pt = _prev["procs"], _prev["ptime"]
    _prev["procs"], _prev["ptime"] = cur, now
    rows = []
    dt = (now - pt) if pt else None
    for pid, (comm, ticks, rss, cpu) in cur.items():
        if dt and pid in prev:
            pct = 100.0 * (ticks - prev[pid][1]) / CLK_TCK / dt
        else:
            pct = None
        rows.append((pct if pct is not None else -1, pid, comm, rss, cpu))
    rows.sort(key=lambda r: (-r[0], -r[3]))
    return [[pid, comm, ("%.1f%%" % pct) if pct >= 0 else "-", _fmt_bytes(rss), cpu if cpu is not None else "-"]
            for pct, pid, comm, rss, cpu in rows[:n]], dt is not None


def cpu():
    from . import clocks
    now = _proc_stat()
    util = _util(now, _prev["stat"])
    sampled = _prev["stat"] is not None
    _prev["stat"] = now
    pols = clocks.cpu_policies()
    pol_of = {}
    for p in pols:
        for c in (p.get("cpus") or "").split():
            pol_of["cpu" + c] = p
    parts = {}
    cur_proc = None
    for line in (sysfs.read("/proc/cpuinfo", limit=1 << 20) or "").splitlines():
        if line.startswith("processor"):
            cur_proc = "cpu" + line.split(":")[1].strip()
        elif line.startswith("CPU part") and cur_proc:
            parts[cur_proc] = line.split(":")[1].strip()
    cores = []
    base = "/sys/devices/system/cpu"
    cpus = sorted([c for c in sysfs.listdir(base) if re.fullmatch(r"cpu\d+", c)], key=lambda c: int(c[3:]))
    for c in cpus:
        d = base + "/" + c
        online = sysfs.read(d + "/online")
        khz = sysfs.read_int(d + "/cpufreq/scaling_cur_freq")
        p = pol_of.get(c) or {}
        u = util.get(c)
        bar = ("█" * int(round((u or 0) / 10))).ljust(10, "░") if u is not None else ""
        idle = []
        for s in sysfs.listdir(d + "/cpuidle"):
            if s.startswith("state"):
                idle.append("%s %s" % (sysfs.read(d + "/cpuidle/%s/name" % s), sysfs.read(d + "/cpuidle/%s/usage" % s)))
        cores.append([c, p.get("policy", "-"), sysfs.read(d + "/cpu_capacity") or "-", parts.get(c, "-"),
                      ("%.0f MHz" % (khz / 1000.0)) if khz else ("offline" if online == "0" else "-"),
                      ("%5.1f%%" % u) if u is not None else "-", bar, ", ".join(idle)])
    clusters = []
    for p in pols:
        clusters.append([p["policy"], p.get("cpus"), p.get("driver"), p.get("governor"),
                         "%s MHz" % p.get("cur_MHz"), "%s - %s" % (p.get("min_MHz"), p.get("max_MHz")),
                         "%s - %s" % (p.get("hw_min_MHz"), p.get("hw_max_MHz")), len(p.get("ladder_MHz") or []),
                         p.get("transitions")])
    idle_states = []
    for s in sysfs.listdir(base + "/cpu0/cpuidle"):
        if s.startswith("state"):
            tot_t = sum(sysfs.read_int("%s/%s/cpuidle/%s/time" % (base, c, s)) or 0 for c in cpus)
            tot_u = sum(sysfs.read_int("%s/%s/cpuidle/%s/usage" % (base, c, s)) or 0 for c in cpus)
            up = (_num((sysfs.read("/proc/uptime") or "0").split()[0]) or 1) * len(cpus)
            idle_states.append([s, sysfs.read("%s/cpu0/cpuidle/%s/name" % (base, s)), sysfs.read("%s/cpu0/cpuidle/%s/desc" % (base, s)),
                                "%s us" % sysfs.read("%s/cpu0/cpuidle/%s/latency" % (base, s)),
                                "%s us" % sysfs.read("%s/cpu0/cpuidle/%s/residency" % (base, s)),
                                tot_u, "%.1f%%" % (100.0 * tot_t / 1e6 / up)])
    load = (sysfs.read("/proc/loadavg") or "").split()
    procs, psampled = _top_procs()
    total = util.get("cpu")
    items = [item("cpufreq", "ok" if pols else "absent",
                  "%d policies (%s)" % (len(pols), ", ".join(sorted({p.get("driver") or "?" for p in pols}))) if pols else "no cpufreq policy (no cpufreq driver bound)"),
             item("cpuidle", "ok" if idle_states else "absent",
                  "%s: %s" % (sysfs.read(base + "/cpuidle/current_driver"), ", ".join(s[1] for s in idle_states)) if idle_states else "no cpuidle states"),
             item("cores", "ok", "%d CPUs, %d online" % (len(cpus), sum(1 for c in cores if "offline" not in c[4])))]
    tables = [table("Per core%s" % ("" if sampled else " (since boot; live from the next refresh)"),
                    ("cpu", "cluster", "capacity", "part", "frequency", "busy", "", "idle entries"), cores),
              table("Clusters (cpufreq policies)", ("policy", "cpus", "driver", "governor", "now", "limits MHz", "hardware MHz", "steps", "transitions"), clusters)]
    if idle_states:
        tables.append(table("Idle states, all CPUs", ("state", "name", "description", "exit latency", "target residency", "entries", "share of CPU time"), idle_states))
    tables.append(table("Top processes%s" % ("" if psampled else " (CPU share from the next refresh)"), ("pid", "command", "cpu", "resident", "on cpu"), procs))
    freqs = ["%s %s" % (p["policy"].replace("policy", "p"), int(p["cur_MHz"])) for p in pols if p.get("cur_MHz")]
    return section("cpu", "CPU", items, tables,
                   headline="%s busy  load %s\n%s MHz" % (("%.0f%%" % total) if total is not None else "-", load[0] if load else "-", "  ".join(freqs)),
                   spark=total, level="ok",
                   facts={"cpus": len(cpus), "policies": {p["policy"]: {"cpus": p.get("cpus"), "driver": p.get("driver"),
                                                                    "hw_max_MHz": p.get("hw_max_MHz")} for p in pols},
                          "idle_states": [s[1] for s in idle_states], "idle_driver": sysfs.read(base + "/cpuidle/current_driver")})


# -- GPU -----------------------------------------------------------------------

def gpu(slow=None):
    from . import gpu as g
    devs = g.gpu_names()
    rs = g.readings(live_only=True)
    items, rows = [], []
    util = None
    facts = {}
    for name, d in devs:
        kd = d.get("kernel_driver") or {}
        items.append(item("drm " + name, "ok" if d.get("driver") else "fail",
                          "kernel driver %s %s, nodes %s" % (kd.get("name", d.get("driver")), kd.get("version", ""), " ".join(d["nodes"]))))
        facts[name] = {"driver": d.get("driver"), "kernel_name": kd.get("name"), "version": kd.get("version")}
    if not devs:
        items.append(item("drm", "fail", "no DRM device with a render node"))
    for r in rs:
        v, u = r["value"], r.get("unit")
        if r["attr"] == "util":
            util = v
        if r["attr"].startswith("gpu_memory/") and isinstance(v, int) and v > 1 << 20:
            v, u = round(v / 2**20, 1), "MiB"
        rows.append([r["id"].split(".", 2)[-1], "%s%s" % (v, (" " + u) if u else ""), r.get("label") or ""])
    freq = []
    for dv in sysfs.listdir("/sys/class/devfreq"):
        if re.search(r"gpu|agx", dv, re.I):
            d = "/sys/class/devfreq/" + dv
            freq.append([dv, sysfs.read(d + "/cur_freq"), sysfs.read(d + "/governor")])
    st = {r["attr"].split("/", 1)[1]: r["value"] for r in rs if r["attr"].startswith("agx_stats/") or r["attr"].startswith("agx_stats.")} or \
        {r["id"].split("agx_stats.", 1)[1]: r["value"] for r in rs if "agx_stats." in r["id"]}
    hz = st.get("achieved_frequency_hz")
    pstate_hz = next((r["value"] for r in rs if r["attr"] == "pstate_hz"), None)
    util_idle = next((r["value"] for r in rs if r["attr"] == "util_idle"), None)
    if isinstance(hz, (int, float)):
        items.append(item("frequency", "ok", "%.0f MHz achieved, %s MHz requested, pstate %s" % (
            hz / 1e6, (st.get("requested_frequency_hz") or 0) / 1e6 if isinstance(st.get("requested_frequency_hz"), (int, float)) else "?", st.get("pstate"))))
    elif freq:
        items.append(item("frequency", "ok", "devfreq " + ", ".join("%s %s" % (f[0], f[1]) for f in freq)))
    elif isinstance(st.get("requested_frequency_hz"), (int, float)):
        items.append(item("frequency", "ok", "%.0f MHz requested (pstate %s); achieved clock not exposed" % (
            st["requested_frequency_hz"] / 1e6, st.get("pstate"))))
    elif pstate_hz == 0:
        # The first operating point is 0 Hz: the firmware has the GPU off.
        items.append(item("frequency", "ok", "off (pstate %s, 0 Hz in the device-tree operating points); the GPU is idle" % st.get("pstate")))
    elif isinstance(pstate_hz, (int, float)):
        items.append(item("frequency", "ok", "%.0f MHz requested (pstate %s of the device-tree operating points); achieved clock not exposed" % (
            pstate_hz / 1e6, st.get("pstate"))))
    elif "pstate_status" in st and st.get("achieved_frequency_hz") == "unsupported":
        # G16S/G17P: agx_stats says the firmware reports no GPU clock. The
        # pstate is a logical selector, shown as it is.
        ps = ("logical pstate %s" % st.get("pstate")) if st.get("pstate_status") == "valid" else \
            ("pstate_status=%s" % st.get("pstate_status"))
        items.append(item("frequency", "off", "not available on this GPU: the firmware does not report the GPU clock (%s)" % ps))
    else:
        items.append(item("frequency", "absent", "not exposed: agx_stats achieved_frequency_hz=%s, pstate_status=%s, and no devfreq device" % (
            st.get("achieved_frequency_hz", "missing"), st.get("pstate_status", "missing"))))
    if isinstance(util, (int, float)):
        items.append(item("utilisation", "ok", "%s%% busy" % util))
    elif util_idle:
        items.append(item("utilisation", "ok", "idle: no firmware utilisation sample right now (agx_stats utilisation_status=%s); "
                          "it reports while the GPU runs" % util_idle))
    else:
        items.append(item("utilisation", "absent", "not exposed: agx_stats util_device_percent=%s, utilisation_status=%s" % (
            st.get("util_device_percent", "missing"), st.get("utilisation_status", "missing"))))
    temps = g.gpu_temps()
    tables = [table("Live", ("reading", "value", "label"), rows)]
    if temps:
        tables.append(table("GPU temperatures", ("sensor", "C"), sorted(temps.items())))
    slow = slow or {}
    vk = slow.get("vulkan") or {}
    api = []
    for d in vk.get("devices", []) if isinstance(vk, dict) else []:
        if "CPU" in (d.get("deviceType") or ""):
            continue
        for k in ("deviceName", "driverName", "driverID", "driverInfo", "driverVersion", "apiVersion", "conformanceVersion", "extension_count"):
            api.append(["vulkan", k, d.get(k)])
    gl = slow.get("gl") or {}
    for a in ("gl_core", "gl_compat", "gles"):
        i = gl.get(a) if isinstance(gl, dict) else None
        if i:
            api.append([a, "renderer", "%s (%s)" % (i["renderer"], i["path"])])
            api.append([a, "version", "%s, GLSL %s" % (i["version"], i["glsl_version"])])
    if api:
        tables.append(table("Vulkan, OpenGL, GLES", ("api", "field", "value"), api))
    clients = cached("gpu_clients", 5, g.per_process)
    if clients:
        tables.append(table("Clients", ("process", "resident", "clients"),
                            [["%s[%d]" % (p["comm"], p["pid"]), "%.1f MiB" % (p["resident_KiB"] / 1024), p["clients"]] for p in clients]))
    mem = next((r["value"] for r in rs if r["attr"] == "gpu_memory/user_resident"), None)
    head = []
    if util is not None:
        head.append("busy %s%%" % util)
    if mem is not None:
        head.append("%.0f MiB" % (mem / 2**20))
    plausible = [v for v in temps.values() if v >= 15]
    t = max(plausible) if plausible else None
    if temps:
        # With the GPU power-gated its SMC sensors read near 0 C; that is not a fault.
        items.append(item("temperature", "ok",
                          ("%d GPU sensors, hottest %.1f C" % (len(temps), t)) if plausible else
                          "%d GPU sensors, none above 15 C right now (%s C): the GPU is power-gated, or the SMC keys are not die temperatures" % (
                              len(temps), ", ".join("%g" % v for v in sorted(set(temps.values()))[:6]))))
    else:
        items.append(item("temperature", "absent", "no hwmon temperature labelled GPU"))
    return section("gpu", "GPU", items, tables,
                   headline="%s\n%s%s" % ("  ".join(head) or "-", ", ".join(n for n, _ in devs) or "no GPU",
                                          ("  %.0f C" % t) if t is not None else ""),
                   spark=util if isinstance(util, (int, float)) else None, facts=facts)


# -- memory --------------------------------------------------------------------

def memory():
    from . import clocks
    mi = {}
    for line in (sysfs.read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        f = v.split()
        if f and f[0].isdigit():
            mi[k] = int(f[0]) * 1024
    tot, avail = mi.get("MemTotal"), mi.get("MemAvailable")
    used = (tot - avail) if tot and avail is not None else None
    rows = [[k, _fmt_bytes(mi.get(k))] for k in ("MemTotal", "MemFree", "MemAvailable", "Buffers", "Cached", "Shmem",
                                                 "AnonPages", "Mapped", "Slab", "KernelStack", "PageTables",
                                                 "SwapTotal", "SwapFree", "Dirty", "Writeback", "HugePages_Total") if k in mi]
    swaps = []
    for line in (sysfs.read("/proc/swaps") or "").splitlines()[1:]:
        f = line.split()
        if len(f) >= 5:
            swaps.append([f[0], f[1], _fmt_bytes(int(f[2]) * 1024), _fmt_bytes(int(f[3]) * 1024), f[4]])
    zram = []
    for b in sysfs.listdir("/sys/block"):
        if b.startswith("zram"):
            d = "/sys/block/" + b
            mm = (sysfs.read(d + "/mm_stat") or "").split()
            alg = sysfs.read(d + "/comp_algorithm") or ""
            m = re.search(r"\[([^\]]+)\]", alg)
            zram.append([b, _fmt_bytes(sysfs.read_int(d + "/disksize")), m.group(1) if m else alg,
                         _fmt_bytes(int(mm[0])) if mm else "-", _fmt_bytes(int(mm[1])) if mm else "-",
                         _fmt_bytes(int(mm[2])) if len(mm) > 2 else "-"])
    mem = clocks.memory()
    speed = mem.get("speed")
    dram = []
    for t in sysfs.listdir(DT):
        if re.search(r"opp-table.*(dram|mem|dcs)", t):
            for o in sysfs.listdir(DT + "/" + t):
                raw = None
                try:
                    with open(sysfs.root(DT + "/%s/%s/opp-hz" % (t, o)), "rb") as f:
                        raw = f.read()
                except OSError:
                    pass
                if raw:
                    hz = int.from_bytes(raw[-8:], "big") if len(raw) >= 8 else int.from_bytes(raw, "big")
                    dram.append([t, o, "%.0f MHz" % (hz / 1e6)])
    mc = [n for n in sysfs.listdir(DT) if n.startswith("memory-controller")]
    items = [item("RAM", "ok", "%s total, %s available" % (_fmt_bytes(tot), _fmt_bytes(avail))),
             item("DRAM speed", "ok" if isinstance(speed, dict) else "absent",
                  json.dumps(speed) if isinstance(speed, dict) else str(speed)),
             item("DRAM DVFS table", "ok" if dram else "absent",
                  ("%d levels in %s" % (len(dram), dram[0][0])) if dram else "no DRAM opp table in the device tree"),
             item("memory controller", "ok" if mc else "absent",
                  ", ".join(mc) if mc else "no memory-controller node in the device tree"),
             item("swap", "ok" if swaps else "absent", "%d swap areas" % len(swaps) if swaps else "no swap configured"),
             item("zram", "ok" if zram else "absent", ", ".join(z[0] for z in zram) if zram else "no zram device")]
    tables = [table("Memory", ("field", "value"), rows)]
    if swaps:
        tables.append(table("Swap", ("device", "type", "size", "used", "priority"), swaps))
    if zram:
        tables.append(table("zram", ("device", "disk size", "algorithm", "stored", "compressed", "RAM used"), zram))
    if dram:
        tables.append(table("DRAM levels (device tree)", ("table", "level", "frequency"), dram))
    pct = round(100.0 * used / tot, 1) if used is not None and tot else None
    return section("memory", "Memory", items, tables,
                   headline="%s / %s used\nswap %s" % (_fmt_bytes(used), _fmt_bytes(tot),
                                                        _fmt_bytes((mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) if "SwapTotal" in mi else None)),
                   spark=pct, level="ok",
                   facts={"total_GiB": round((tot or 0) / 2**30), "swap": [s[0] for s in swaps], "zram": [z[0] for z in zram],
                          "dram_levels": len(dram), "memory_controller": bool(mc)})


# -- power and battery -----------------------------------------------------------

def power(readings=None):
    from . import sources
    rs = readings if readings is not None else sources.collect(["power_supply", "hwmon"])
    supplies = {}
    for r in rs:
        if r["id"].startswith("psy."):
            supplies.setdefault(r["dev"], {})[r["attr"]] = r
    items, tables = [], []
    bat = next((s for s, f in supplies.items() if (f.get("type") or {}).get("value") == "Battery"), None)
    if bat:
        f = supplies[bat]
        v = lambda k: (f.get(k) or {}).get("value")
        full, design = v("energy_full") or v("charge_full"), v("energy_full_design") or v("charge_full_design")
        health = round(100.0 * full / design, 1) if isinstance(full, (int, float)) and design else None
        items.append(item("battery", "ok", "%s%% %s, %s cycles, health %s%%" % (v("capacity"), v("status"), v("cycle_count"), health)))
    else:
        items.append(item("battery", "absent", "no power_supply of type Battery (desktop, or the SMC battery driver is not bound)"))
    for s, f in sorted(supplies.items()):
        rows = [[k, ("%s %s" % (r["value"], r.get("unit") or "")).strip(), "w" if r.get("writable") else ""]
                for k, r in sorted(f.items())]
        tables.append(table("%s (%s)" % (s, (f.get("type") or {}).get("value", "?")), ("attribute", "value", ""), rows))
    ac = [s for s, f in supplies.items() if (f.get("type") or {}).get("value") in ("Mains", "USB") and (f.get("online") or {}).get("value") == 1]
    items.append(item("external power", "ok" if supplies else "absent",
                      ("online: " + ", ".join(ac)) if ac else ("on battery" if supplies else "no power supplies")))
    pw = [r for r in rs if r["cat"] == "power" and r["attr"].startswith("power") and r["attr"].endswith("_input") and isinstance(r["value"], (int, float))]
    pw.sort(key=lambda r: -abs(r["value"]))
    tables.insert(0, table("Power meters (hwmon), largest first", ("meter", "W", "id"),
                           [[r.get("label") or r["attr"], "%.3f" % r["value"], r["id"]] for r in pw]))
    tot = next((r for r in pw if (r.get("label") or "").lower().startswith("total system power")), None)
    items.append(item("power meters", "ok" if pw else "absent", "%d power channels" % len(pw) if pw else "no hwmon power channel"))
    bp = (supplies.get(bat) or {}).get("power_now", {}).get("value") if bat else None
    head = []
    if tot:
        head.append("system %.2f W" % tot["value"])
    if bat:
        f = supplies[bat]
        head.append("battery %s%% %s" % ((f.get("capacity") or {}).get("value"), (f.get("status") or {}).get("value", "")))
    return section("power", "Power & battery", items, tables,
                   headline="\n".join(head) or "-", spark=tot["value"] if tot else (bp if isinstance(bp, (int, float)) else None),
                   level="ok", facts={"supplies": sorted(supplies), "battery": bool(bat), "meters": len(pw)})


# -- audio and speaker protection ---------------------------------------------------

_cache = {}


def cached(key, ttl, fn):
    """fn() at most every ttl seconds (subprocess helpers in live categories)."""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    v = fn()
    _cache[key] = (now, v)
    return v


def audio_section(rs=None):
    from . import audio
    rs = rs if rs is not None else audio.speaker_protection()
    byid = {r["id"]: r["value"] for r in rs}
    coils = [r["value"] for r in rs if r["attr"] in ("t_coil_C", "ff_coil_C", "vi_coil_C") and isinstance(r["value"], float)]
    sg = {"service": byid.get("speaker.speakerguardd.service"), "armed": byid.get("speaker.speakerguardd.armed"),
          "governor": byid.get("speaker.speakerguardd.shadow.gov"), "hottest_coil_C": max(coils) if coils else None}
    blocks = {}
    meta = []
    for r in rs:
        parts = r["id"].split(".")
        if r["id"].startswith("speaker.speakerguardd.") and len(parts) >= 5:
            blk, name, field = parts[2], parts[3], ".".join(parts[4:])
            blocks.setdefault(blk, {}).setdefault(name, {})[field] = r
        elif r["id"].startswith("speaker."):
            meta.append([".".join(parts[1:]), "%s%s" % (r["value"], (" " + r["unit"]) if r.get("unit") else "")])
    tables = [table("Protection state", ("field", "value"), meta)]
    hottest = None
    for blk, spk in blocks.items():
        fields = []
        for n in spk.values():
            for k in n:
                if k not in fields:
                    fields.append(k)
        rows = []
        for n, f in spk.items():
            row = [n]
            for k in fields:
                r = f.get(k)
                v = r["value"] if r else ""
                if isinstance(v, float):
                    v = "%.3f" % v if abs(v) < 10 else "%.2f" % v
                row.append(v)
                if r and k in ("t_coil_C",) and isinstance(r["value"], float):
                    hottest = max(hottest or -1e9, r["value"])
            rows.append(row)
        title = {"speaker": "Speakers (speakerguardd model: coil and magnet temperature, power, measured V)",
                 "asahi": "Reference model (asahi): temperatures, holds, gain",
                 "shadow": "Shadow governor"}.get(blk, blk)
        tables.append(table(title, ["speaker"] + fields, rows))
    amps = []
    for h in sysfs.listdir("/sys/class/hwmon"):
        d = "/sys/class/hwmon/" + h
        nm = sysfs.read(d + "/name") or ""
        if re.search(r"tas\d|cs35|sn012|ssm|max98", nm):
            dev = os.path.basename(sysfs.realpath(d + "/device")) if sysfs.exists(d + "/device") else h
            t = sysfs.read_int(d + "/temp1_input")
            amps.append([nm, dev, ("%.1f C" % (t / 1000.0)) if t is not None else "unreadable"])
    if amps:
        tables.append(table("Amplifier die temperatures", ("amp", "device", "temperature"), amps))
    cards = []
    for line in (sysfs.read("/proc/asound/cards") or "").splitlines():
        m = re.match(r"\s*(\d+)\s+\[(\S+)\s*\]:\s*(.*)", line)
        if m:
            cards.append([m.group(1), m.group(2), m.group(3)])
    if cards:
        tables.append(table("ALSA cards", ("card", "id", "driver - name"), cards))
    ctl = []
    try:
        for r in cached("alsa", 15, audio.alsa_controls):
            if re.search(r"(sense|mute|switch|unlock|amp gain|volume)", r["attr"], re.I) and re.search(r"(speaker|amp|tas|sense|master|headphone)", r["attr"] + " " + str(r.get("label")), re.I):
                ctl.append([r["dev"], r["attr"], r["value"]])
    except Exception:
        pass
    if ctl:
        tables.append(table("Speaker path controls (read only)", ("card", "control", "value"), ctl[:80]))
    u = cached("desktop_user", 30, _desktop_user)
    mute = None
    if u:
        rc, out = cached("wpctl", 15, lambda: _run(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"], user=u, timeout=4))
        if rc == 0 and out.strip():
            mute = out.strip()
            tables.insert(1, table("Default output (PipeWire, %s)" % u.pw_name, ("field", "value"), [["volume", out.strip()]]))
    svc = sg.get("service")
    v2 = byid.get("speaker.speakerguardd-v2.service")
    v2_graph = byid.get("speaker.speakerguardd-v2.graph.result")
    v2_amps = [v for k, v in byid.items() if k.startswith("speaker.speakerguardd-v2.session.") and k.endswith(".amps")]
    if v2 in ("active", "failed"):
        # speakerguardd v2 governs the speakers; the older daemons stay down beside it.
        v2_lvl = "fail" if v2 == "failed" else ("ok" if v2_graph in (None, "pass") else "warn")
        prot = item("speakerguardd", v2_lvl, "speakerguardd v2 %s, graph %s, amps %s" % (
            v2, v2_graph or "unreadable (needs root)", v2_amps[0] if v2_amps else "-"))
        svc_shown = "v2 " + v2
    else:
        prot = item("speakerguardd", "ok" if svc == "active" else ("absent" if svc in (None, "unknown", "inactive") and not blocks else "warn"),
                    "speakerguardd %s, armed %s, governor %s, hottest coil %s C" % (svc, sg.get("armed"), sg.get("governor"), sg.get("hottest_coil_C")))
        v2_lvl = None
        svc_shown = svc
    items = [prot,
             item("speaker model", "ok" if blocks else ("off" if v2_lvl else "absent"),
                  ("%d speakers modelled" % len(blocks.get("speaker", {}))) if blocks else
                  ("speakerguardd v2 keeps its model inside the daemon" if v2_lvl else
                   "no /run/speakerguardd/state (needs root, or speakerguardd is not running)")),
             item("amplifier temperatures", "ok" if amps else "absent", "%d amps" % len(amps) if amps else "no amplifier hwmon device"),
             item("ALSA cards", "ok" if cards else "absent", ", ".join(c[1] for c in cards) if cards else "no sound card"),
             item("default output", "ok" if mute else "absent", mute or "no desktop session with PipeWire to ask")]
    lvl = v2_lvl or ("ok" if svc == "active" else ("warn" if blocks or svc == "failed" else "off"))
    return section("audio", "Audio & speaker protection", items, tables,
                   headline=("speakerguardd v2 %s\ngraph %s" % (v2, v2_graph or "-")) if v2_lvl else
                   "%s, armed %s\nhottest coil %s" % (svc or "-", sg.get("armed"), ("%.1f C" % hottest) if hottest is not None else "-"),
                   spark=hottest, level=lvl,
                   facts={"cards": [c[1] for c in cards], "speakers": sorted(blocks.get("speaker", {})), "amps": len(amps),
                          "speakerguardd": svc_shown})


# -- sensors: IIO, motion, lid, buttons, jack ------------------------------------------

EV_SW, EV_KEY = 0x05, 0x01
SW_NAMES = {0: "lid closed", 1: "tablet mode", 2: "headphone inserted", 3: "rfkill all", 4: "microphone inserted",
            5: "dock", 6: "line out inserted", 7: "jack physically inserted", 8: "video out inserted",
            9: "camera lens cover", 10: "keypad slide", 11: "front proximity", 12: "rotate lock",
            13: "line in inserted", 14: "mute device", 15: "pen inserted", 16: "machine cover"}
KEY_NAMES = {116: "power", 142: "sleep", 143: "wakeup", 113: "mute", 114: "volume down", 115: "volume up",
             224: "brightness down", 225: "brightness up", 229: "kbd illum down", 230: "kbd illum up", 240: "unknown"}


def _bits(hexwords):
    """A /sys/class/input capability bitmap (space separated hex longs,
    most significant first) as a set of bit numbers."""
    out = set()
    words = (hexwords or "").split()
    for i, w in enumerate(reversed(words)):
        v = int(w, 16)
        b = 0
        while v:
            if v & 1:
                out.add(i * 64 + b)
            v >>= 1
            b += 1
    return out


def _evdev_switches(event, nbits=17):
    """EVIOCGSW: the current switch state bitmap (needs read access to the
    event node, root or the input group)."""
    import fcntl
    n = (nbits + 7) // 8
    req = (2 << 30) | (n << 16) | (ord("E") << 8) | 0x1b
    try:
        fd = os.open(sysfs.root("/dev/input/" + event), os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        return None, e.strerror
    try:
        buf = bytearray(n)
        fcntl.ioctl(fd, req, buf)
        return {b for b in range(nbits) if buf[b // 8] & (1 << (b % 8))}, None
    except OSError as e:
        return None, e.strerror
    finally:
        os.close(fd)


def inputs():
    """Switches (lid, jack) with their state, and which devices carry the
    power button and other system keys."""
    out = []
    for i in sysfs.listdir("/sys/class/input"):
        if not re.fullmatch(r"input\d+", i):
            continue
        d = "/sys/class/input/" + i
        name = sysfs.read(d + "/name") or i
        ev = _bits(sysfs.read(d + "/capabilities/ev"))
        sw = _bits(sysfs.read(d + "/capabilities/sw")) if EV_SW in ev else set()
        keys = _bits(sysfs.read(d + "/capabilities/key")) if EV_KEY in ev else set()
        event = next((e for e in sysfs.listdir(d) if e.startswith("event")), None)
        state, why = (None, None)
        if sw and event:
            state, why = _evdev_switches(event)
        sys_keys = sorted(KEY_NAMES[k] for k in keys & set(KEY_NAMES))
        if sw or sys_keys:
            out.append({"input": i, "event": event, "name": name,
                        "switches": {SW_NAMES.get(b, "sw%d" % b): (b in state) if state is not None else None for b in sorted(sw)},
                        "switch_error": why, "keys": sys_keys,
                        "phys": sysfs.read(d + "/phys")})
    return out


def motion():
    """Accelerometers, gyroscopes, magnetometers: IIO devices with those
    channels, the device-tree nodes that should produce them, and why a
    missing one is missing."""
    base = "/sys/bus/iio/devices"
    found = {}
    for dv in sysfs.listdir(base):
        if not dv.startswith("iio:device"):
            continue
        d = base + "/" + dv
        files = sysfs.listdir(d)
        for kind, rx in (("accelerometer", r"^in_accel_"), ("gyroscope", r"^in_anglvel_"), ("magnetometer", r"^in_magn_")):
            ch = [f for f in files if re.match(rx, f) and re.search(r"_(raw|input)$", f)]
            if ch:
                vals, errs = {}, {}
                scale = sysfs.read_float(d + "/" + (rx[1:].rstrip("_") + "_scale"))
                for f in ch:
                    raw, why = sysfs.read_error(d + "/" + f)
                    v = _num(raw)
                    if v is None:
                        errs[f] = why or raw
                    vals[f] = round(v * scale, 5) if (v is not None and scale and f.endswith("_raw")) else v
                found.setdefault(kind, []).append({"device": dv, "name": sysfs.read(d + "/name"), "values": vals, "errors": errs,
                                                   "scale": scale,
                                                   "sampling_frequency": sysfs.read(d + "/sampling_frequency") or sysfs.read(d + "/" + rx[1:] + "sampling_frequency"),
                                                   "label": sysfs.read(d + "/label"),
                                                   "mount_matrix": sysfs.read(d + "/in_mount_matrix") or sysfs.read(d + "/mount_matrix")})
    # Motion sensor nodes: named accel/gyro/... or with a motion compatible.
    # Not the trackpad's tp_accel interface, which is a haptics channel.
    nodes = _dt_nodes(r"^(accel|accel-base|gyro|accelerometer|gyroscope|imu|magnetometer|compass)$|"
                      r",[\w-]*(aop-(motion|accel|gyro)|accelerometer|gyroscope|imu)\b")
    aops = [n for n in sysfs.listdir(DT) if n.startswith("aop@")] + [n for n in _dt_nodes(r"^aop$") if "/" in n]
    drivers = kernel_knows(r"apple,.*(motion|accel|gyro)")
    res = []
    for kind, dtname in (("accelerometer", "accel"), ("gyroscope", "gyro")):
        if found.get(kind):
            for f in found[kind]:
                unit = "m/s^2" if dtname == "accel" else "rad/s"
                if f["errors"] and len(f["errors"]) == len(f["values"]):
                    why = sorted(set(f["errors"].values()))
                    res.append(item(kind, "warn", "%s (%s) is registered but its channels do not read: %s (the AOP has sent no report)" % (
                        f["name"], f["device"], ", ".join(w for w in why if w)), unit=unit, device=f["device"], iio_name=f["name"]))
                else:
                    res.append(item(kind, "ok", "%s (%s): %s %s" % (f["name"], f["device"], ", ".join(
                        "%s=%s" % (k.replace("in_", "").replace("_raw", "").replace("_input", ""), v) for k, v in f["values"].items()), unit),
                        unit=unit, device=f["device"], iio_name=f["name"], values=f["values"]))
            continue
        mine = [n for n in nodes if re.search(dtname, n.split("/")[-1])]
        if mine:
            n = mine[0]
            st = _dt_status(n)
            cal = [p for p in sysfs.listdir(DT + "/" + n) if "cal" in p]
            comp = sysfs.dt_strings(DT + "/" + n + "/compatible")
            from . import subsystems
            dev = subsystems.linux_devices().get(n)
            if st not in ("okay", "ok"):
                why = "DT node /%s is %s" % (n, st)
            elif not cal:
                why = "DT node /%s has no calibration properties (the m1n1 stage 2 that loaded this tree did not copy them from the ADT)" % n
            elif not dev:
                why = "DT node /%s has no Linux device%s" % (n, "" if drivers else "; this kernel has no driver for %s" % " / ".join(comp))
            elif not sysfs.basename_of_link(dev + "/driver"):
                why = "/%s: device %s not bound (driver %s)" % (n, os.path.basename(dev), "available" if drivers else "missing from this kernel")
            else:
                why = "/%s bound but registered no IIO channels" % n
            res.append(item(kind, "warn", why, dt_node="/" + n))
        else:
            where = ("under /" + aops[0]) if aops else "anywhere (no AOP node either)"
            drv = ("this kernel has a driver (%s)" % ", ".join(drivers)) if drivers else \
                  "and this kernel (%s) has no Apple motion driver" % os.uname().release
            res.append(item(kind, "absent", "no %s node %s in the live device tree, %s" % (dtname, where, drv)))
    if found.get("magnetometer"):
        res.append(item("magnetometer", "ok", ", ".join(f["name"] for f in found["magnetometer"])))
    return res, found


def sensors(readings=None):
    from . import sources
    rs = readings if readings is not None else sources.collect(["iio", "hwmon"])
    iio = [r for r in rs if r["cat"] == "iio"]
    devs = {}
    for r in iio:
        devs.setdefault(r["dev"], []).append(r)
    items = []
    # Colour temperature: the documented in_colortemp channel, else the older in_cct one.
    for kind, rx, unit in (("ambient light", r"illuminance", "lux"), ("colour temperature", r"in_colortemp|in_cct", "K"),
                           ("lid angle", r"angl", "deg")):
        rr = next((r for r in sorted(iio, key=lambda r: "in_cct" in r["attr"]) if re.search(rx, r["attr"])), None)
        if rr:
            v = rr["value"]
            items.append(item(kind, "ok" if isinstance(v, (int, float)) else "warn",
                              ("%s %s (%s)" % (v, unit, rr["dev"])) if isinstance(v, (int, float)) else "%s: %s" % (rr["dev"], v)))
        else:
            items.append(item(kind, "absent", "no IIO channel matching %s (no AOP %s service bound)" % (rx, kind)))
    mot, found = motion()
    items.extend(mot)
    ins = inputs()
    lid = next((x for x in ins if "lid closed" in x["switches"]), None)
    items.append(item("lid switch", "ok" if lid else "absent",
                      ("%s: %s" % (lid["name"], "closed" if lid["switches"]["lid closed"] else "open" if lid["switches"]["lid closed"] is not None else "state unreadable (%s)" % lid["switch_error"]))
                      if lid else "no input device reports SW_LID"))
    pwr = [x for x in ins if "power" in x["keys"] and not (x.get("phys") or "").startswith("usb")] or \
        [x for x in ins if "power" in x["keys"]]
    items.append(item("power button", "ok" if pwr else "absent", ", ".join(x["name"] for x in pwr) if pwr else "no input device reports KEY_POWER"))
    jack = next((x for x in ins if "headphone inserted" in x["switches"]), None)
    items.append(item("headphone jack", "ok" if jack else "absent",
                      ("%s: %s" % (jack["name"], ", ".join(k for k, v in jack["switches"].items() if v) or "nothing plugged in")) if jack else "no jack switch"))
    hw = {}
    for r in rs:
        if r["id"].startswith("hwmon.") and r["attr"].endswith("_input"):
            t = re.match(r"[a-z]+", r["attr"]).group(0)
            e = hw.setdefault(r["dev"], {})
            e[t] = e.get(t, 0) + 1
            if not isinstance(r["value"], (int, float)):
                e["unreadable"] = e.get("unreadable", 0) + 1
    tables = [table("IIO channels", ("device", "channel", "value", "label"),
                    [[r["dev"], r["attr"], "%s%s" % (r["value"], (" " + r["unit"]) if r.get("unit") else ""), r.get("label") or ""] for r in iio])]
    for kind in ("accelerometer", "gyroscope", "magnetometer"):
        for f in found.get(kind, []):
            tables.append(table("%s %s" % (kind, f["name"]), ("field", "value"),
                                [[k, v if v is not None else "unreadable (%s)" % f["errors"].get(k)] for k, v in f["values"].items()] + [["scale", f["scale"]], ["sampling_frequency", f["sampling_frequency"]],
                                                                            ["mount_matrix", f["mount_matrix"]], ["label", f["label"]]]))
    tables.append(table("Switches and system keys", ("device", "switches", "keys", "event"),
                        [[x["name"], ", ".join("%s=%s" % (k, {True: "yes", False: "no", None: "?"}[v]) for k, v in x["switches"].items()) or "-",
                          ", ".join(x["keys"]) or "-", x["event"]] for x in ins]))
    tables.append(table("hwmon devices (channel counts)", ("device", "temp", "in", "curr", "power", "fan", "unreadable"),
                        [[d, e.get("temp", 0), e.get("in", 0), e.get("curr", 0), e.get("power", 0), e.get("fan", 0), e.get("unreadable", 0)]
                         for d, e in sorted(hw.items())]))
    lux = next((r["value"] for r in iio if "illuminance" in r["attr"]), None)
    ang = next((r["value"] for r in iio if "angl" in r["attr"]), None)
    acc = found.get("accelerometer")
    head = []
    if isinstance(ang, (int, float)):
        head.append("lid %.0f deg" % ang)
    if isinstance(lux, (int, float)):
        head.append("%.0f lux" % lux)
    head2 = "motion: %s" % ("yes" if acc else "absent")
    return section("sensors", "Sensors & motion", items, tables, headline="%s\n%s" % ("  ".join(head) or "-", head2),
                   spark=lux if isinstance(lux, (int, float)) else None,
                   level="ok" if not any(i["level"] in ("warn", "fail") for i in items) else "warn",
                   facts={"iio": sorted({"%s/%s" % (r["dev"], r["attr"]) for r in iio}),
                          "motion": {i["key"]: i["level"] for i in mot},
                          "switches": sorted({"%s:%s" % (x["name"], k) for x in ins for k in x["switches"]}),
                          "hwmon": {d: {k: v for k, v in e.items() if k != "unreadable"} for d, e in hw.items()}})


# -- display -------------------------------------------------------------------------

def _dcp_nodes():
    """(dt path, kind, Linux device, driver) for every DCP, in device-tree
    order when the flattened tree is readable (appledrm numbers its CRTCs
    in that order)."""
    from . import kms, subsystems
    rels = [n for n in sysfs.listdir(DT) if re.match(r"dcp(ext)?\d*@", n)]
    rels += [n for n in _dt_nodes(r"^dcp(ext)?\d*$") if "/" in n and not n.endswith("piodma")]
    order = kms.fdt_node_order()
    if order:
        pos = {p.lstrip("/"): i for i, p in enumerate(order)}
        rels.sort(key=lambda r: pos.get(r, 1 << 30))
    devs = subsystems.linux_devices()
    out = []
    ext_n = 0
    for r in rels:
        comp = sysfs.dt_strings(DT + "/" + r + "/compatible")
        ext = any("dcpext" in c for c in comp) or sysfs.exists(DT + "/" + r + "/phys")
        dev = devs.get(r)
        drv = sysfs.basename_of_link(dev + "/driver") if dev else None
        label = ("dcpext%d" % ext_n) if ext else "dcp"
        if ext:
            ext_n += 1
        out.append({"node": r, "label": label, "external": ext, "compatible": comp, "status": _dt_status(r),
                    "device": os.path.basename(dev) if dev else None, "driver": drv,
                    "firmware": sysfs.dt_value(DT + "/" + r + "/apple,firmware-version"),
                    "firmware_compat": sysfs.dt_value(DT + "/" + r + "/apple,firmware-compat"),
                    "notch_height": sysfs.dt_value(DT + "/" + r + "/apple,notch-height"),
                    "connector_type": sysfs.dt_value(DT + "/" + r + "/apple,connector-type")})
    return out, bool(order)


def _debugfs_dri(devname):
    """The debugfs directory of the DRM device on this platform device
    (its name file reads "apple dev=display-subsystem unique=...")."""
    base = "/sys/kernel/debug/dri"
    for d in sysfs.listdir(base):
        nm = sysfs.read(base + "/" + d + "/name") or ""
        if ("dev=%s " % devname) in nm + " " and sysfs.isdir(base + "/" + d):
            return base + "/" + d
    return None


def _read_bin(path, limit=1 << 16):
    try:
        with open(sysfs.root(path), "rb") as f:
            return f.read(limit)
    except OSError:
        return None


def _gdctl():
    u = _desktop_user()
    if not u:
        return None, "no desktop session bus to ask"
    rc, out = _run(["gdctl", "show"], user=u, timeout=5)
    if rc is None:
        return None, "gdctl is not installed"
    if rc != 0 or not out.strip():
        return None, "gdctl show failed (no GNOME session?)"
    return [l.rstrip() for l in out.splitlines() if l.strip()], None


def display():
    from . import edid as E
    from . import kms
    dcps, ordered = _dcp_nodes()
    cards = []
    for c in sysfs.listdir("/sys/class/drm"):
        if re.fullmatch(r"card\d+", c) and sysfs.isdir("/sys/class/drm/%s/device" % c):
            drv = sysfs.basename_of_link("/sys/class/drm/%s/device/driver" % c)
            conns = [x for x in sysfs.listdir("/sys/class/drm") if x.startswith(c + "-")]
            if conns:
                cards.append((c, drv, conns))
    items, tables, facts = [], [], {"connectors": {}, "dcps": {}}
    summary_rows = []
    head = None
    for card, drv, conns in cards:
        conns = sorted(conns, key=lambda c: (0 if "-eDP" in c else 1, c))
        st = kms.card_state("/dev/dri/" + card)
        kerr = st.get("error")
        dbg = _debugfs_dri(os.path.basename(sysfs.realpath("/sys/class/drm/%s/device" % card)))
        kconn = {c["name"]: c for c in st.get("connectors", []) if "name" in c}
        for cn in conns:
            short = cn[len(card) + 1:]
            d = "/sys/class/drm/" + cn
            k = kconn.get(short) or {}
            status = sysfs.read(d + "/status")
            modes = (sysfs.read(d + "/modes") or "").splitlines()
            blob = k.get("edid_blob") or E.edid_file(d + "/edid")
            ed = E.edid(blob) if blob else {}
            props = k.get("props") or {}
            crtc = k.get("crtc")
            pv = lambda n: (props.get(n) or {}).get("value")
            rows = [["status", "%s, %s, dpms %s" % (status, sysfs.read(d + "/enabled"), sysfs.read(d + "/dpms"))]]
            dcp = None
            if crtc is not None and crtc.get("index") is not None and dcps and crtc["index"] < len(dcps):
                dcp = dcps[crtc["index"]]
            elif short.startswith("eDP"):
                dcp = next((x for x in dcps if not x["external"]), None)
            elif k.get("possible_crtcs") and dcps:
                cand = [dcps[i]["label"] for i in k["possible_crtcs"] if i < len(dcps)]
                rows.append(["can be driven by", ", ".join(cand)])
            if dcp:
                rows.append(["driven by", "%s %s (%s, %s)%s" % (dcp["label"], dcp["node"], dcp["device"] or "no device",
                                                                 ("bound to " + dcp["driver"]) if dcp["driver"] else "NOT bound",
                                                                 "" if ordered else " [order guessed: /sys/firmware/fdt needs root]")])
            if ed and "error" not in ed:
                rows.append(["monitor", "%s %s (product %s, %s)" % (ed.get("manufacturer"), ed.get("name") or "-", ed.get("product_code"), ed.get("year"))])
                if ed.get("size_cm"):
                    rows.append(["size", "%d x %d cm (%.1f in)" % (ed["size_cm"][0], ed["size_cm"][1], ed.get("diagonal_in"))])
                if ed.get("preferred"):
                    p = ed["preferred"]
                    rows.append(["EDID preferred timing", "%dx%d @ %s Hz" % (p["width"], p["height"], p["refresh_Hz"])])
                if ed.get("range_vertical_Hz"):
                    rows.append(["EDID refresh range", "%d - %d Hz" % tuple(ed["range_vertical_Hz"])])
                hdr = ed.get("hdr_static_metadata")
                if hdr:
                    rows.append(["EDID HDR static metadata", "%s; max %s nits, frame avg %s, min %s" % (
                        ", ".join(hdr["eotfs"]), hdr.get("max_luminance_nits"), hdr.get("max_frame_avg_nits"), hdr.get("min_luminance_nits"))])
                if ed.get("colorimetry"):
                    rows.append(["EDID colorimetry", ", ".join(ed["colorimetry"])])
                for kk in ("hdmi_vrr_Hz", "freesync_Hz", "adaptive_sync"):
                    if ed.get(kk):
                        rows.append(["EDID " + kk, json.dumps(ed[kk])])
                rows.append(["EDID", "%d bytes, version %s, extensions %s" % (ed["bytes"], ed["version"], ", ".join(ed["extensions"]) or "none")])
            elif status == "connected":
                rows.append(["EDID", "empty (the panel or DCP did not provide one; the display may be off)"])
            if crtc and crtc.get("mode"):
                mo = crtc["mode"]
                rows.append(["current mode", "%dx%d @ %s Hz (crtc-%d)" % (mo["w"], mo["h"], mo["refresh_Hz"], crtc["index"])])
                for pn in ("ACTIVE", "VRR_ENABLED", "GAMMA_LUT_SIZE"):
                    if pn in crtc.get("props", {}):
                        rows.append(["crtc " + pn, crtc["props"][pn]["value"]])
            if k.get("modes"):
                rates = sorted({m["refresh_Hz"] for m in k["modes"]}, reverse=True)
                rows.append(["modes", "%d (%s); refresh %s Hz" % (len(k["modes"]), ", ".join(sorted({"%dx%d" % (m["w"], m["h"]) for m in k["modes"]}, key=lambda s: -int(s.split("x")[0]) * int(s.split("x")[1]))[:6]),
                                                                 ", ".join("%g" % r for r in rates[:12]))])
            elif modes:
                rows.append(["modes", "%d (%s)" % (len(modes), ", ".join(modes[:6]))])
            for pn in ("vrr_capable", "Colorspace", "HDR_OUTPUT_METADATA", "max bpc", "link-status", "non-desktop", "content type", "Broadcast RGB", "scaling mode", "panel orientation"):
                if pn in props:
                    p = props[pn]
                    val = p["value"]
                    if pn == "HDR_OUTPUT_METADATA":
                        val = json.dumps(k.get("hdr_output_metadata")) if val else "none set"
                    extra = (" of " + ", ".join(p["options"])) if p.get("options") else (" range %s" % p["range"]) if p.get("range") else ""
                    rows.append(["prop " + pn, "%s%s" % (val, extra)])
            if k.get("mm") and any(k["mm"]):
                rows.append(["physical size", "%d x %d mm" % tuple(k["mm"])])
            vrr = ddisp = None
            if dbg:
                cd = dbg + "/" + short
                vr = sysfs.read(cd + "/vrr_range")
                if vr:
                    mn = re.search(r"Min:\s*(\d+)", vr)
                    mx = re.search(r"Max:\s*(\d+)", vr)
                    if mn and mx:
                        vrr = (int(mn.group(1)), int(mx.group(1)))
                        rows.append(["DCP vrr_range", "%d - %d Hz" % vrr if vrr[1] else "none"])
                ob = sysfs.read(cd + "/output_bpc")
                if ob:
                    rows.append(["DCP output_bpc", ob.replace("\n", " ")])
                da = E.dcp_dict(_read_bin(cd + "/DisplayAttributes"))
                ddisp = E.display_attributes(da)
                for kk, vv in ddisp.items():
                    rows.append(["DCP " + kk, json.dumps(vv) if isinstance(vv, (dict, list)) else vv])
                tr = E.transport(E.dcp_dict(_read_bin(cd + "/Transport")))
                if tr:
                    rows.append(["DCP transport", ", ".join("%s %s" % kv for kv in tr.items())])
            elif dcps:
                rows.append(["DCP attributes", "debugfs not readable (run as root for vrr_range, DisplayAttributes, Transport)"])
            vcap = pv("vrr_capable")
            max_rate = max([m["refresh_Hz"] for m in k.get("modes", [])] or [0])
            promotion = None
            if short.startswith("eDP"):
                promotion = bool((vrr and vrr[1] > 60) or (vcap and max_rate > 60))
                rows.append(["ProMotion", ("yes, %d - %d Hz" % vrr) if promotion and vrr else ("yes" if promotion else "no (fixed refresh, max %g Hz)" % max_rate if max_rate else "unknown (no modes)")])
            name = ed.get("name") if ed and "error" not in ed else None
            if ddisp and ddisp.get("product_name"):
                name = name or ddisp["product_name"]
            lvl = "ok" if status == "connected" else "off"
            if status == "connected" and not modes and not k.get("modes"):
                lvl = "warn"
            hdr_ok = bool(ed.get("hdr")) or bool(ddisp and (ddisp.get("SupportsPQEOTF") or ddisp.get("SupportsHLGEOTF")))
            summ = "%s%s%s%s" % (name or ("connected" if status == "connected" else "disconnected"),
                                  (", %dx%d@%gHz" % (crtc["mode"]["w"], crtc["mode"]["h"], crtc["mode"]["refresh_Hz"])) if crtc and crtc.get("mode") else "",
                                  (", VRR %d-%d" % vrr) if vrr and vrr[1] else (", VRR capable" if vcap else ""),
                                  ", HDR" if hdr_ok else "")
            items.append(item(short, lvl, summ + ((" via " + dcp["label"]) if dcp else "")))
            summary_rows.append([short, status, name or "-", ("%dx%d@%g" % (crtc["mode"]["w"], crtc["mode"]["h"], crtc["mode"]["refresh_Hz"])) if crtc and crtc.get("mode") else "-",
                                 "yes" if vcap else "no", ("%d-%d" % vrr) if vrr and vrr[1] else "-", "yes" if hdr_ok else "no",
                                 dcp["label"] if dcp else "-"])
            tables.append(table("%s  %s" % (short, name or status), ("field", "value"), rows))
            facts["connectors"][short] = {"props": sorted(props), "internal": short.startswith("eDP"),
                                          "dcp": dcp["node"] if dcp and short.startswith("eDP") else None}
            if short.startswith("eDP"):
                facts["connectors"][short].update({"edid_name": name, "vrr_capable": vcap, "promotion": promotion,
                                                   "max_refresh": max_rate or None, "hdr": hdr_ok})
                if status == "connected":
                    head = "%s%s\n%s" % (name or short, (" %gHz" % crtc["mode"]["refresh_Hz"]) if crtc and crtc.get("mode") else "",
                                         ("ProMotion %d-%d" % vrr) if promotion and vrr else ("VRR" if vcap else ""))
        if kerr:
            items.append(item("kms " + card, "warn" if os.geteuid() == 0 else "absent", "KMS properties unreadable: %s%s" % (kerr, "" if os.geteuid() == 0 else " (run as root)")))
    if summary_rows:
        tables.insert(0, table("Connectors", ("connector", "status", "monitor", "mode", "vrr cap", "vrr range", "HDR", "DCP"), summary_rows))
    if dcps:
        rows = [[x["label"], x["node"], " ".join(x["compatible"]), x["status"], x["device"] or "-", x["driver"] or "NOT bound",
                 x["firmware"] or "-", x["firmware_compat"] or "-"] for x in dcps]
        tables.insert(1, table("Display coprocessors", ("dcp", "node", "compatible", "dt", "device", "driver", "firmware", "fw compat"), rows))
        for x in dcps:
            ok = x["driver"] and x["status"] in ("okay", "ok")
            items.append(item(x["label"], "ok" if ok else ("off" if x["status"] not in ("okay", "ok") else "fail"),
                              "%s %s, firmware %s" % (x["node"], ("bound to " + x["driver"]) if x["driver"] else ("disabled" if x["status"] not in ("okay", "ok") else "not bound"), x["firmware"] or "?")))
            facts["dcps"][x["node"]] = {"label": x["label"], "driver": x["driver"], "firmware": x["firmware"]}
    else:
        items.append(item("dcp", "absent", "no DCP node in the device tree"))
    chosen = []
    for p in sysfs.listdir("/proc/device-tree/chosen"):
        if re.search(r"dcp|display|framebuffer|preflight|panel", p):
            v = sysfs.dt_value("/proc/device-tree/chosen/" + p)
            chosen.append([p, v if v is not None and len(str(v)) < 80 else ("%d bytes" % len(str(v)))])
    if chosen:
        tables.append(table("Boot loader display hand-off (/chosen)", ("property", "value"), chosen))
    bl = []
    for b in sysfs.listdir("/sys/class/backlight"):
        d = "/sys/class/backlight/" + b
        cur, mx = sysfs.read_int(d + "/brightness"), sysfs.read_int(d + "/max_brightness")
        parent = os.path.basename(os.path.dirname(sysfs.realpath(d + "/device"))) if sysfs.exists(d + "/device") else "-"
        bl.append([b, "%s / %s (%s%%)" % (cur, mx, round(100.0 * cur / mx) if cur is not None and mx else "?"),
                   sysfs.read_int(d + "/actual_brightness"), sysfs.read(d + "/bl_power"), sysfs.read(d + "/type"),
                   os.path.basename(sysfs.realpath(d + "/device")) if sysfs.exists(d + "/device") else parent])
    if bl:
        tables.append(table("Backlight", ("device", "brightness", "actual", "bl_power", "type", "parent"), bl))
        items.append(item("backlight", "ok", ", ".join("%s %s" % (b[0], b[1]) for b in bl)))
        facts["backlight"] = {b[0]: b[1].split("/")[1].split()[0] for b in bl}
    else:
        items.append(item("backlight", "absent", "no backlight class device"))
    notch = next((x["notch_height"] for x in dcps if x["notch_height"]), None)
    show = sysfs.read("/sys/module/appledrm/parameters/show_notch")
    items.append(item("notch", "ok" if notch else "absent",
                      ("notch %s px (apple,notch-height), appledrm show_notch=%s" % (notch, show)) if notch else "no apple,notch-height on any DCP node (no notch, or the DT does not describe it)"))
    gd, why = _gdctl()
    if gd:
        tables.append(table("GNOME's view (gdctl show)", ("line",), [[l] for l in gd[:80]]))
        items.append(item("GNOME display config", "ok", "%d monitors listed by gdctl" % sum(1 for l in gd if re.search(r"Monitor ", l))))
    else:
        items.append(item("GNOME display config", "absent", why))
    con = [i for i in items if i["level"] == "ok" and re.match(r"(eDP|DP|HDMI|USB|DSI)", i["key"])]
    return section("display", "Display", items, tables,
                   headline=head or ("%d connected" % len(con)) + ("\n" + ", ".join(i["key"] for i in con) if con else ""),
                   level="ok" if con or dcps else "off", facts=facts)


# -- radio ---------------------------------------------------------------------------

def _ethtool_drvinfo(ifname):
    """ETHTOOL_GDRVINFO through SIOCETHTOOL: driver, version, firmware."""
    import array
    import fcntl
    buf = array.array("B", struct.pack("I", 3) + b"\0" * 192)
    ifr = struct.pack("16sP", ifname.encode()[:15], buf.buffer_info()[0])
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        fcntl.ioctl(s.fileno(), 0x8946, ifr)
    except OSError:
        return None
    finally:
        s.close()
    b = buf.tobytes()
    st = lambda o, n: b[o:o + n].split(b"\0")[0].decode(errors="replace")
    return {"driver": st(4, 32), "version": st(36, 32), "firmware": st(68, 32), "bus": st(100, 32)}


def _band(freq):
    if freq is None:
        return None
    if freq < 2500:
        return "2.4 GHz"
    if freq < 5925:
        return "5 GHz"
    if freq < 7200:
        return "6 GHz"
    return "60 GHz"


def radio():
    items, tables, facts = [], [], {"wifi": {}, "bluetooth": {}, "rfkill": []}
    rf = []
    for r in sysfs.listdir("/sys/class/rfkill"):
        d = "/sys/class/rfkill/" + r
        rf.append([r, sysfs.read(d + "/name"), sysfs.read(d + "/type"), sysfs.read(d + "/soft"), sysfs.read(d + "/hard"), sysfs.read(d + "/state")])
    facts["rfkill"] = sorted({x[2] for x in rf if x[2]})
    regd = None
    rc, out = _run(["iw", "reg", "get"], timeout=4)
    m = re.search(r"country (\w+):\s*(\S+)?", out or "")
    if m:
        regd = "%s%s" % (m.group(1), (" " + m.group(2)) if m.group(2) else "")
    head = []
    wl = [n for n in sysfs.listdir("/sys/class/net") if sysfs.isdir("/sys/class/net/%s/wireless" % n) or sysfs.exists("/sys/class/net/%s/phy80211" % n)]
    for n in wl:
        d = "/sys/class/net/" + n
        dev = d + "/device"
        drv = sysfs.basename_of_link(dev + "/driver")
        ue = sysfs.uevent(dev)
        ei = _ethtool_drvinfo(n) or {}
        phy = sysfs.basename_of_link(d + "/phy80211")
        rows = [["driver", "%s%s" % (drv or ei.get("driver") or "?", (" " + ei["version"]) if ei.get("version") else "")],
                ["firmware (ethtool)", ei.get("firmware") or "not reported by the driver"],
                ["firmware (kernel log)", _fw_from_dmesg(drv) or "no firmware line in the log"],
                ["bus", "%s %s" % (ue.get("PCI_SLOT_NAME") or ei.get("bus") or "-", ue.get("PCI_ID") or ue.get("OF_COMPATIBLE_0") or "")],
                ["phy", phy or "-"], ["state", "%s, carrier %s" % (sysfs.read(d + "/operstate"), sysfs.read(d + "/carrier"))],
                ["regulatory domain", regd or "unknown (iw not installed?)"]]
        rc, link = _run(["iw", "dev", n, "link"], timeout=4)
        lk = {}
        if rc == 0 and link:
            for line in link.splitlines():
                k, _, v = line.strip().partition(":")
                if v:
                    lk[k.strip()] = v.strip()
        rc, info = _run(["iw", "dev", n, "info"], timeout=4)
        width = None
        mi = re.search(r"channel (\d+) \((\d+) MHz\)(?:, width: (\d+) MHz)?", info or "")
        if mi:
            width = mi.group(3)
        freq = _num((lk.get("freq") or "").split()[0]) if lk.get("freq") else (int(mi.group(2)) if mi else None)
        ssid = lk.get("SSID")
        if rc is None and not lk:
            wireless = (sysfs.read("/proc/net/wireless") or "").splitlines()
            for line in wireless[2:]:
                if line.strip().startswith(n + ":"):
                    f = line.split()
                    lk["signal"] = f[3].rstrip(".") + " dBm"
        rows += [["SSID", ssid or "not connected"], ["frequency", "%s MHz (%s)%s" % (freq, _band(freq), (", %s MHz wide" % width) if width else "") if freq else "-"],
                 ["channel", mi.group(1) if mi else "-"], ["signal", lk.get("signal", "-")], ["rx bitrate", lk.get("rx bitrate", "-")],
                 ["tx bitrate", lk.get("tx bitrate", "-")], ["tx power", (re.search(r"txpower ([\d.]+ dBm)", info or "") or [None, "-"])[1] if info else "-"]]
        rc, ps = _run(["iw", "dev", n, "get", "power_save"], timeout=4)
        if rc == 0 and ps:
            rows.append(["power save", ps.split(":")[-1].strip()])
        if os.geteuid() == 0:
            rc, sd = _run(["iw", "dev", n, "scan", "dump"], timeout=6)
            if rc == 0:
                bss = re.findall(r"^BSS ", sd, re.M)
                bands = {}
                for f in re.findall(r"^\s*freq: ([\d.]+)", sd, re.M):
                    b = _band(float(f))
                    bands[b] = bands.get(b, 0) + 1
                rows.append(["scan results (cached, no new scan)", "%d BSS%s" % (len(bss), (" (" + ", ".join("%s %d" % kv for kv in sorted(bands.items())) + ")") if bands else "")])
        else:
            rows.append(["scan results", "needs root (iw scan dump)"])
        tables.append(table("Wi-Fi %s" % n, ("field", "value"), rows))
        fwlog = _fw_from_dmesg(drv)
        fwv = (re.search(r"version (\S+)", fwlog or "") or [None, None])[1] or ei.get("firmware") or "not reported"
        items.append(item("Wi-Fi " + n, "ok", "%s, firmware %s, %s%s" % (drv, fwv, ssid or "not connected",
                                                                     (", %s %s" % (_band(freq), lk.get("signal", ""))) if ssid else "")))
        facts["wifi"][n] = {"driver": drv, "firmware": ei.get("firmware"), "phy": bool(phy)}
        if ssid:
            head.append("%s %s" % (ssid[:16], lk.get("signal", "")))
    if not wl:
        items.append(item("Wi-Fi", "absent", "no wireless network interface (no cfg80211 device registered)"))
    hcis = sysfs.listdir("/sys/class/bluetooth")
    hcis = [h for h in hcis if re.fullmatch(r"hci\d+", h)]
    bt_info = {}
    if hcis:
        rc, out = _run(["btmgmt", "info"], timeout=4)
        cur = None
        for line in (out or "").splitlines():
            m = re.match(r"(hci\d+):\s+Primary controller", line)
            if m:
                cur = bt_info.setdefault(m.group(1), {})
                continue
            if cur is not None:
                m = re.search(r"addr \S+ version (\d+) manufacturer (\d+) class (\S+)", line)
                if m:
                    cur.update({"hci_version": m.group(1), "manufacturer": m.group(2), "class": m.group(3)})
                m = re.match(r"\s*current settings: (.*)", line)
                if m:
                    cur["settings"] = m.group(1).split()
                m = re.match(r"\s*name (.*)", line)
                if m and "name" not in cur:
                    cur["name"] = m.group(1)
        rc, out = _run(["bluetoothctl", "devices", "Paired"], timeout=5)
        paired = len([l for l in (out or "").splitlines() if l.startswith("Device ")]) if rc == 0 else None
        rc, out = _run(["bluetoothctl", "devices", "Connected"], timeout=5)
        connected = len([l for l in (out or "").splitlines() if l.startswith("Device ")]) if rc == 0 else None
    for h in hcis:
        d = "/sys/class/bluetooth/" + h
        dev = d + "/device"
        drv = sysfs.basename_of_link(dev + "/driver")
        bi = bt_info.get(h, {})
        powered = ("powered" in bi.get("settings", [])) if bi.get("settings") is not None else None
        fw = _fw_from_dmesg(drv, bt=True)
        rows = [["driver", drv or "-"], ["bus", os.path.basename(sysfs.realpath(dev)) if sysfs.exists(dev) else "-"],
                ["powered", {True: "yes", False: "no", None: "unknown (btmgmt not available)"}[powered]],
                ["HCI version", bi.get("hci_version", "-")], ["manufacturer id", bi.get("manufacturer", "-")],
                ["settings", " ".join(bi.get("settings", [])) or "-"], ["firmware", fw or "not reported"],
                ["paired devices", paired if paired is not None else "unknown"], ["connected devices", connected if connected is not None else "unknown"]]
        tables.append(table("Bluetooth %s" % h, ("field", "value"), rows))
        items.append(item("Bluetooth " + h, "ok" if powered is not False else "warn",
                          "%s, %s, %s paired, %s connected" % (drv, "powered" if powered else "off" if powered is False else "power unknown", paired, connected)))
        facts["bluetooth"][h] = {"driver": drv}
        head.append("BT %s, %s paired" % ("on" if powered else "off" if powered is False else "?", paired))
    if not hcis:
        items.append(item("Bluetooth", "absent", "no hci device (no Bluetooth controller bound)"))
    if rf:
        tables.append(table("rfkill", ("node", "name", "type", "soft", "hard", "state"), rf))
    others = [x for x in rf if x[2] not in ("wlan", "bluetooth")]
    items.append(item("other radios", "ok" if others else "absent",
                      ", ".join("%s %s" % (x[1], x[2]) for x in others) if others else
                      "none: rfkill lists only %s (no NFC, UWB, WWAN or GPS radio exposed)" % (", ".join(facts["rfkill"]) or "nothing")))
    return section("radio", "Radio", items, tables, headline="\n".join(head[:2]) or "no radio",
                   level="ok" if wl or hcis else "off", facts=facts)


def _fw_from_dmesg(drv, bt=False):
    from . import subsystems
    lines = subsystems.dmesg("emerg,alert,crit,err,warn,notice,info") or []
    rx = re.compile(r"(Bluetooth: hci\d+:.*(firmware|version|Firmware)|hci_bcm4377.*(firmware|Firmware)|BCM.*firmware)" if bt else
                    r"(%s).*([Ff]irmware|version)" % re.escape(drv or "wifi"))
    hit = None
    for line in lines:
        if rx.search(line) and ("Firmware" in line or "firmware" in line or "version" in line):
            hit = line
    if not hit:
        return None
    m = re.search(r"([Ff]irmware:?\s*)(.*)", hit)
    return (m.group(2) if m else hit)[:100]


# -- storage -------------------------------------------------------------------------

def storage():
    items, tables, facts = [], [], {"nvme": {}, "block": []}
    for n in sysfs.listdir("/sys/class/nvme"):
        d = "/sys/class/nvme/" + n
        model = (sysfs.read(d + "/model") or "").strip()
        fw = (sysfs.read(d + "/firmware_rev") or "").strip()
        rows = [["model", model], ["firmware", fw], ["state", sysfs.read(d + "/state")], ["transport", sysfs.read(d + "/transport")],
                ["queues", sysfs.read(d + "/queue_count")]]
        ns = [x for x in sysfs.listdir(d) if re.fullmatch(r"nvme\d+n\d+", x)]
        for x in ns:
            sz = sysfs.read_int("/sys/block/%s/size" % x)
            rows.append(["namespace " + x, _fmt_bytes(sz * 512 if sz is not None else None)])
        temp = None
        hw = [h for h in sysfs.listdir(d) if h.startswith("hwmon")]
        for h in hw:
            t = sysfs.read_int("%s/%s/temp1_input" % (d, h))
            if t is not None:
                temp = t / 1000.0
                rows.append(["temperature (hwmon)", "%.1f C" % temp])
        smart, why = _smart("/dev/" + n)
        sm_items = []
        if smart:
            h = smart.get("nvme_smart_health_information_log") or {}
            for k, lab in (("critical_warning", "critical warning"), ("temperature", "temperature C"), ("available_spare", "available spare %"),
                           ("available_spare_threshold", "spare threshold %"), ("percentage_used", "percentage used"),
                           ("data_units_read", "data units read"), ("data_units_written", "data units written"),
                           ("host_reads", "host reads"), ("host_writes", "host writes"), ("power_cycles", "power cycles"),
                           ("power_on_hours", "power on hours"), ("unsafe_shutdowns", "unsafe shutdowns"),
                           ("media_errors", "media errors"), ("num_err_log_entries", "error log entries")):
                if k in h:
                    v = h[k]
                    if k.startswith("data_units"):
                        v = "%s (%.2f TB)" % (v, v * 512000 / 1e12)
                    rows.append(["SMART " + lab, v])
                    sm_items.append(k)
            if smart.get("smart_status") is not None:
                rows.append(["SMART overall", "passed" if smart["smart_status"].get("passed") else "FAILED"])
        else:
            rows.append(["SMART", why])
        tables.append(table("NVMe %s" % n, ("field", "value"), rows))
        hl = (smart or {}).get("nvme_smart_health_information_log") or {}
        lvl = "ok"
        if hl.get("critical_warning"):
            lvl = "warn"
        items.append(item("NVMe " + n, lvl, "%s fw %s%s%s" % (model, fw, (", %.0f C" % temp) if temp is not None else "",
                                                           (", %s%% used, %s unsafe shutdowns" % (hl.get("percentage_used"), hl.get("unsafe_shutdowns"))) if hl else "")))
        items.append(item("SMART " + n, "ok" if smart else "absent", "%d health fields" % len(sm_items) if smart else why))
        facts["nvme"][n] = {"model": model, "firmware": fw, "namespaces": len(ns)}
    if not sysfs.listdir("/sys/class/nvme"):
        items.append(item("NVMe", "absent", "no NVMe controller"))
    blk = []
    for b in sysfs.listdir("/sys/block"):
        if re.match(r"(loop|ram|zram|dm-)", b):
            continue
        d = "/sys/block/" + b
        sz = sysfs.read_int(d + "/size")
        blk.append([b, _fmt_bytes(sz * 512 if sz is not None else None), (sysfs.read(d + "/device/model") or "").strip() or "-",
                    "removable" if sysfs.read(d + "/removable") == "1" else "fixed", "rotational" if sysfs.read(d + "/queue/rotational") == "1" else "ssd",
                    sysfs.read(d + "/queue/scheduler") or "-"])
        facts["block"].append(b)
    if blk:
        tables.append(table("Block devices", ("device", "size", "model", "", "", "scheduler"), blk))
    fs = []
    for mnt in ("/", "/boot", "/boot/efi", "/home", "/var"):
        try:
            st = os.statvfs(mnt)
        except OSError:
            continue
        tot, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        if tot:
            fs.append([mnt, _fmt_bytes(tot), _fmt_bytes(tot - free), "%.0f%%" % (100.0 * (tot - free) / tot)])
    if fs:
        tables.append(table("File systems", ("mount", "size", "used", "use"), fs))
    t = next((float(r[1].split()[0]) for tb in tables for r in tb["rows"] if r[0] == "temperature (hwmon)"), None)
    first = next((i for i in items if i["key"].startswith("NVMe ")), None)
    return section("storage", "Storage", items, tables,
                   headline=("%s\n%s" % (first["detail"].split(" fw ")[0][:30], ", ".join(x for x in first["detail"].split(", ")[1:3]))) if first else "no NVMe",
                   spark=t, level="ok" if first else "off", facts=facts)


def _smart(dev):
    if os.geteuid() != 0:
        return None, "needs root (smartctl)"
    if not shutil.which("smartctl"):
        return None, "smartctl is not installed (smartmontools)"
    rc, out = _run(["smartctl", "-j", "-a", dev], timeout=8)
    try:
        d = json.loads(out)
    except ValueError:
        return None, "smartctl gave no JSON"
    d.pop("serial_number", None)
    if not d.get("nvme_smart_health_information_log"):
        return None, "no SMART health log (%s)" % "; ".join(m.get("string", "") for m in d.get("smartctl", {}).get("messages", []))[:120]
    return d, None


# -- engines -----------------------------------------------------------------------

ENGINE_GROUPS = [
    ("ane", "ANE (Neural Engine)", r"^ane"), ("ave", "AVE (video encoder)", r"^ave"), ("avd", "AVD (video decoder)", r"^avd"),
    ("isp", "ISP (camera)", r"^isp"), ("sep", "SEP (Secure Enclave, Touch ID)", r"^sep"), ("aop", "AOP (always-on processor)", r"^aop"),
    ("pmp", "PMP (power manager)", r"^pmp"), ("dcp", "DCP (display)", r"^dcp"), ("gpu", "GPU (AGX)", r"^gpu"),
    ("smc", "SMC", r"^smc"), ("mtp", "MTP (keyboard, trackpad)", r"^(mtp|dockchannel)"), ("sio", "SIO (audio DMA)", r"^sio"),
    ("ans", "ANS (NVMe)", r"^(ans|nvme)"), ("jpeg", "JPEG", r"^jpeg"), ("prores", "ProRes", r"^prores"), ("scaler", "Scaler", r"^scaler"),
]


def engines(subs=None):
    from . import services, subsystems
    subs = subs if subs is not None else subsystems.subsystems()
    chosen = {}
    for p in sysfs.listdir("/proc/device-tree/chosen"):
        if re.search(r"crc|fw|firmware|version", p, re.I):
            chosen[p] = sysfs.dt_value("/proc/device-tree/chosen/" + p)
    items, tables, facts = [], [], {}
    summary = []
    for key, title, rx in ENGINE_GROUPS:
        crx = re.compile(r"^[a-z]+,(t\d+-)?%s($|[-,\d])" % rx.lstrip("^"))
        recs = [s for s in subs if (re.match(rx, s.get("block") or "") or any(crx.match(c) for c in s.get("compatible") or []))
                and not (s.get("name") or "").endswith("piodma")
                and "dart" not in (s.get("name") or "") and "mbox" not in (s.get("name") or "")]
        recs = [s for s in recs if s.get("kind") == "block" or s.get("driver")]
        if not recs:
            items.append(item(key, "absent", "no %s node in the device tree" % key.upper()))
            continue
        rows = []
        for s in recs:
            rows += [["node", s["node"]], ["compatible", " ".join(s.get("compatible") or [])], ["dt status", s["dt_status"]],
                     ["device", s.get("device") or "-"], ["driver", s.get("driver") or "not bound"],
                     ["state", "%s: %s" % (s["state"]["level"], s["state"]["why"])]]
            if s.get("dt_firmware"):
                rows.append(["firmware (DT)", s["dt_firmware"]])
            if s.get("firmware_log"):
                rows.append(["firmware (log)", s["firmware_log"][:140]])
            if s.get("class_devices"):
                rows.append(["class devices", ", ".join(s["class_devices"])[:200]])
            if s.get("dev_nodes"):
                rows.append(["/dev nodes", ", ".join(s["dev_nodes"])])
            if s.get("last_problem"):
                rows.append(["last kernel error", s["last_problem"][:160]])
            if s.get("sysfs"):
                for k in ("performance_level", "performance_error", "wedged", "memory_vote", "ane_stats", "recoveries", "recovery_count",
                          "fw_crc", "firmware_crc", "state", "crash_count", "reset_count"):
                    v = sysfs.read(s["sysfs"] + "/" + k)
                    if v is not None:
                        rows.append([k, v.replace("\n", "; ")[:160]])
            rows.append(["", ""])
        for p, v in chosen.items():
            if re.search(r"(^|[,_-])%s([,_-]|$)" % key, p, re.I):
                rows.append(["/chosen " + p, v])
        if key == "ane":
            acc = [a for a in sysfs.listdir("/sys/class/accel")]
            rows.append(["/dev/accel", ", ".join(acc) or "none"])
            try:
                from . import ane
                for d in ane.collect().get("devices", []):
                    for k in ("generation", "firmware_version", "performance_level", "power_state", "wedged", "client_count"):
                        if d.get(k) is not None:
                            rows.append(["%s %s" % (d.get("node"), k), d[k]])
                    for k, v in sorted((d.get("counters") or {}).items()):
                        rows.append(["%s counter %s" % (d.get("node"), k), v])
                    for k, v in sorted((d.get("error_counters") or {}).items()):
                        rows.append(["%s recovery/error %s" % (d.get("node"), k), v])
            except Exception as e:
                rows.append(["ane probe", repr(e)])
        if key in ("ave", "avd", "isp"):
            v4 = []
            for v in sysfs.listdir("/sys/class/video4linux"):
                dv = os.path.basename(sysfs.realpath("/sys/class/video4linux/%s/device" % v))
                if any(dv == s.get("device") for s in recs):
                    v4.append("%s (%s)" % (v, sysfs.read("/sys/class/video4linux/%s/name" % v)))
            rows.append(["v4l2 nodes", ", ".join(v4) or "none"])
        if key == "sep":
            t = services.touchid()
            rows += [["/dev/sep-bio", "present" if t.get("sep_bio_node") else "absent"], ["enrolled prints", json.dumps(t.get("fprint_users"))],
                     ["pam_fprintd in", ", ".join(t.get("pam_fprintd_in") or []) or "none"]]
        if key == "aop":
            kids = []
            for s in recs:
                for c in sysfs.listdir(DT + "/" + s["node"]):
                    if sysfs.isdir(DT + "/" + s["node"] + "/" + c):
                        kids.append(c)
            rows.append(["AOP services in DT", ", ".join(kids) or "none"])
        bound = [s for s in recs if s.get("driver")]
        enabled = [s for s in recs if s["dt_status"] in ("okay", "ok")]
        worst = max([LEVEL_RANK.get(s["state"]["level"], 0) for s in recs] + [0])
        lvl = {3: "fail", 2: "warn", 1: "ok", 0: "off"}[worst]
        if not enabled:
            lvl = "off"
        det = "%d/%d bound%s" % (len(bound), len(recs), (" (" + ", ".join(sorted({s["driver"] for s in bound})) + ")") if bound else "")
        if not enabled:
            det = "disabled in the device tree"
        elif not bound:
            det = recs[0]["state"]["why"]
        items.append(item(key, lvl, det))
        summary.append([lvl, title, det])
        tables.append(table(title, ("field", "value"), rows))
        facts[key] = {"nodes": sorted(s["node"] for s in recs), "bound": sorted({s["driver"] for s in bound}),
                      "dev_nodes": sorted({d for s in recs for d in (s.get("dev_nodes") or [])})}
    tables.insert(0, table("Engines", ("state", "engine", "detail"), summary))
    n_ok = sum(1 for i in items if i["level"] == "ok")
    return section("engines", "Engines", items, tables,
                   headline="%d bound, %d issues\n%s" % (n_ok, sum(1 for i in items if i["level"] in ("warn", "fail")),
                                                       " ".join(i["key"].upper() for i in items if i["level"] == "ok")[:40]),
                   facts=facts)


# -- system: uptime, boot log, suspend, buses ---------------------------------------------

def system():
    from . import subsystems
    up = _num((sysfs.read("/proc/uptime") or "0").split()[0]) or 0
    err = subsystems.dmesg("emerg,alert,crit,err")
    warn = subsystems.dmesg("warn")
    items = [item("uptime", "ok", "%dd %02d:%02d" % (up // 86400, up % 86400 // 3600, up % 3600 // 60)),
             item("boot log", ("warn" if err else "ok") if err is not None else "absent",
                  ("%d error, %d warning lines" % (len(err), len(warn or []))) if err is not None else "dmesg unreadable (needs root)")]
    rows = [["kernel", os.uname().release], ["build", os.uname().version], ["cmdline", (sysfs.read("/proc/cmdline") or "")[:200]],
            ["uptime", items[0]["detail"]], ["boot log", items[1]["detail"]]]
    ss = {}
    for f in sysfs.listdir("/sys/power/suspend_stats"):
        ss[f] = sysfs.read("/sys/power/suspend_stats/" + f)
    if ss:
        items.append(item("suspend", "warn" if (_num(ss.get("fail")) or 0) > 0 else "ok",
                          "%s ok, %s failed, mem_sleep %s" % (ss.get("success"), ss.get("fail"), sysfs.read("/sys/power/mem_sleep"))))
        rows.append(["suspend stats", ", ".join("%s=%s" % kv for kv in sorted(ss.items()) if kv[1] not in (None, "", "0"))])
    rows += [["sleep states", sysfs.read("/sys/power/state")], ["mem_sleep", sysfs.read("/sys/power/mem_sleep")],
             ["wakeup count", sysfs.read("/sys/power/wakeup_count")]]
    tables = [table("System", ("field", "value"), rows)]
    if err:
        tables.append(table("Kernel errors (dmesg -l err)", ("line",), [[l[:200]] for l in err[-40:]]))
    pci = []
    for f in sysfs.listdir("/sys/bus/pci/devices"):
        d = "/sys/bus/pci/devices/" + f
        pci.append([f, "%s:%s" % ((sysfs.read(d + "/vendor") or "")[2:], (sysfs.read(d + "/device") or "")[2:]), sysfs.read(d + "/class"),
                    sysfs.basename_of_link(d + "/driver") or "-",
                    "%s x%s" % (sysfs.read(d + "/current_link_speed") or "-", sysfs.read(d + "/current_link_width") or "-"),
                    "%s x%s" % (sysfs.read(d + "/max_link_speed") or "-", sysfs.read(d + "/max_link_width") or "-")])
    if pci:
        tables.append(table("PCIe", ("device", "id", "class", "driver", "link now", "link max"), pci))
    items.append(item("PCIe", "ok" if pci else "absent", "%d functions" % len(pci) if pci else "no PCI devices"))
    usb = []
    for u in sysfs.listdir("/sys/bus/usb/devices"):
        if ":" in u:
            continue
        d = "/sys/bus/usb/devices/" + u
        if sysfs.read(d + "/idVendor") is None:
            continue
        usb.append([u, "%s:%s" % (sysfs.read(d + "/idVendor"), sysfs.read(d + "/idProduct")),
                    ("%s %s" % (sysfs.read(d + "/manufacturer") or "", sysfs.read(d + "/product") or "")).strip(),
                    "%s Mb/s" % sysfs.read(d + "/speed"), sysfs.read(d + "/power/control") or "-"])
    if usb:
        tables.append(table("USB", ("device", "id", "name", "speed", "power"), usb))
    tc = []
    for p in sysfs.listdir("/sys/class/typec"):
        if re.fullmatch(r"port\d+", p):
            d = "/sys/class/typec/" + p
            partner = sysfs.exists("/sys/class/typec/%s-partner" % p)
            br = lambda v: (re.search(r"\[([^\]]+)\]", v or "") or [None, v])[1]
            tc.append([p, br(sysfs.read(d + "/data_role")), br(sysfs.read(d + "/power_role")), sysfs.read(d + "/power_operation_mode"),
                       sysfs.read(d + "/usb_power_delivery_revision"), "partner" if partner else "-",
                       (sysfs.read("/sys/class/typec/%s-partner/identity/id_header" % p) or "") if partner else ""])
    if tc:
        tables.append(table("Type-C ports", ("port", "data", "power", "power mode", "PD rev", "partner", "partner id header"), tc))
    items.append(item("Type-C", "ok" if tc else "absent", "%d ports, %d partners" % (len(tc), sum(1 for t in tc if t[5] == "partner")) if tc else "no typec class ports"))
    tb = sysfs.listdir("/sys/bus/thunderbolt/devices")
    items.append(item("USB4 / Thunderbolt", "ok" if tb else "absent", ", ".join(tb) if tb else "no thunderbolt bus devices"))
    return section("system", "System", items, tables,
                   headline="up %s\n%s err, %s warn in dmesg" % (items[0]["detail"], len(err) if err is not None else "?", len(warn) if warn is not None else "?"),
                   level="warn" if err else "ok",
                   facts={"pci": {r[0]: "%s %s" % (r[1], r[3]) for r in pci}, "typec_ports": len(tc), "suspend": sysfs.read("/sys/power/mem_sleep"),
                          "thunderbolt": len(tb)})


# -- registry ---------------------------------------------------------------------------

ORDER = ["health", "thermal", "cpu", "gpu", "memory", "display", "radio", "storage", "power", "audio", "engines", "sensors", "system"]
FUNCS = {"health": health, "thermal": thermal, "cpu": cpu, "gpu": gpu, "memory": memory, "display": display,
         "radio": radio, "storage": storage, "power": power, "audio": audio_section, "engines": engines,
         "sensors": sensors, "system": system}
# Cheap enough to refresh every second; the rest every 15 s.
LIVE = ("thermal", "cpu", "gpu", "memory", "power", "audio", "sensors")
ALIASES = {"speaker": "audio", "speakers": "audio", "battery": "power", "wifi": "radio", "bluetooth": "radio", "bt": "radio",
           "nvme": "storage", "disk": "storage", "motion": "sensors", "iio": "sensors", "ram": "memory", "check": "health",
           "temps": "thermal", "temperature": "thermal", "edid": "display", "dcp": "display", "ane": "engines", "ave": "engines",
           "avd": "engines", "isp": "engines", "sep": "engines", "pcie": "system", "usb": "system", "typec": "system",
           "suspend": "system"}


def resolve(name):
    n = name.lower()
    n = ALIASES.get(n, n)
    if n not in FUNCS:
        raise KeyError("unknown category %r (one of: %s)" % (name, ", ".join(ORDER)))
    return n


def collect(names=None, timeout=20, **kw):
    """{name: section} for the named categories, each under a deadline."""
    from .probe import timed
    out = {}
    for n in names or ORDER:
        fn = FUNCS[n]
        args = {}
        if n == "health" and kw.get("check_result") is not None:
            args["check_result"] = kw["check_result"]
        if n == "gpu" and kw.get("slow") is not None:
            args["slow"] = kw["slow"]
        if n == "engines" and kw.get("subs") is not None:
            args["subs"] = kw["subs"]
        v, err = timed(lambda: fn(**args), timeout)
        if err:
            v = section(n, n.title(), [item(n, "warn", "probe failed: " + err)], level="warn", headline="probe failed")
        out[n] = v
    return out


def coverage(secs):
    rows = []
    for n in ORDER:
        s = secs.get(n)
        if not s:
            continue
        for i in s["items"]:
            rows.append({"section": n, "item": i["key"], "level": i["level"], "detail": i["detail"]})
    return rows


def facts(secs):
    return {n: s.get("facts", {}) for n, s in secs.items() if n != "health"}


def render(s):
    """A category as text, for `aurora-diag show NAME`."""
    lines = ["%s  [%s]" % (s["title"], s["level"].upper()), "  " + s["headline"].replace("\n", "  |  "), ""]
    w = max([len(i["key"]) for i in s["items"]] + [8])
    for i in s["items"]:
        lines.append("  %-6s %-*s %s" % (i["level"].upper(), w, i["key"], i["detail"]))
    for t in s["tables"]:
        lines += ["", "== " + t["title"]]
        cols = t["columns"]
        rows = [[_cell(c) for c in r] for r in t["rows"]]
        widths = [min(max([len(str(c))] + [len(r[k]) if k < len(r) else 0 for r in rows]), 60) for k, c in enumerate(cols)]
        if len(cols) > 1:
            lines.append("  " + "  ".join(str(c).ljust(widths[k]) for k, c in enumerate(cols)).rstrip())
        for r in rows:
            lines.append("  " + "  ".join(r[k][:max(widths[k], 60) if k == len(cols) - 1 else widths[k]].ljust(widths[k]) for k in range(min(len(r), len(cols)))).rstrip())
    return "\n".join(lines) + "\n"


def _cell(c):
    if isinstance(c, float):
        return "%.6g" % c
    if isinstance(c, (dict, list)):
        return json.dumps(c, default=str)
    return "" if c is None else str(c)
