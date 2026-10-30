# SPDX-License-Identifier: MIT
"""Clocks: CPU frequency policies, every DVFS ladder in the device tree,
devfreq devices, and whatever the kernel says about memory speed.

The ladders come from the device tree's operating-points-v2 tables (any
node named opp-table*), linked to the CPUs and devices that point at them
by phandle, so a new SoC needs nothing here.
"""

import re
import struct

from . import sysfs

DT = "/proc/device-tree"


def _be(path, width):
    try:
        with open(sysfs.root(path), "rb") as f:
            data = f.read()
    except OSError:
        return None
    n = len(data) // width
    if n == 0:
        return None
    return list(struct.unpack(">" + ("Q" if width == 8 else "I") * n, data[:n * width]))


def opp_tables():
    """{table name: {"phandle": int, "opps": [{hz, uV, ...}]}}."""
    out = {}
    for name in sysfs.listdir(DT):
        if not name.startswith("opp-table") and not name.startswith("opp_table"):
            continue
        d = DT + "/" + name
        ph = _be(d + "/phandle", 4)
        opps = []
        for o in sysfs.listdir(d):
            od = d + "/" + o
            if not sysfs.isdir(od):
                continue
            hz = _be(od + "/opp-hz", 8)
            entry = {"name": o, "hz": hz[0] if hz else None}
            uv = _be(od + "/opp-microvolt", 4)
            if uv:
                entry["uV"] = uv[0]
            uw = _be(od + "/opp-microwatt", 4)
            if uw:
                entry["uW"] = uw[0]
            lvl = _be(od + "/opp-level", 4)
            if lvl:
                entry["level"] = lvl[0]
            opps.append(entry)
        out[name] = {"phandle": ph[0] if ph else None, "opps": opps}
    return out


def opp_users(tables):
    """{table name: [DT nodes that reference it]} via operating-points-v2."""
    by_ph = {t["phandle"]: n for n, t in tables.items() if t["phandle"] is not None}
    users = {n: [] for n in tables}

    def walk(path, depth):
        if depth > 3:
            return
        for e in sysfs.listdir(path):
            p = path + "/" + e
            if e == "operating-points-v2":
                ph = _be(p, 4)
                if ph and ph[0] in by_ph:
                    users[by_ph[ph[0]]].append(path[len(DT) + 1:] or "/")
            elif sysfs.isdir(p) and not e.startswith("opp-table") and e not in ("reserved-memory", "chosen", "aliases"):
                walk(p, depth + 1)

    walk(DT, 0)
    return users


def cpu_policies():
    out = []
    base = "/sys/devices/system/cpu/cpufreq"
    for p in sysfs.listdir(base):
        if not p.startswith("policy"):
            continue
        d = base + "/" + p
        khz = lambda f: sysfs.read_int(d + "/" + f)
        ladder = [int(x) for x in (sysfs.read(d + "/scaling_available_frequencies") or "").split() if x.isdigit()]
        boost = [int(x) for x in (sysfs.read(d + "/scaling_boost_frequencies") or "").split() if x.isdigit()]
        stats = {}
        for line in (sysfs.read(d + "/stats/time_in_state") or "").splitlines():
            f = line.split()
            if len(f) == 2:
                stats[int(f[0])] = int(f[1])
        out.append({
            "policy": p, "cpus": sysfs.read(d + "/related_cpus"), "driver": sysfs.read(d + "/scaling_driver"),
            "governor": sysfs.read(d + "/scaling_governor"),
            "cur_MHz": _mhz(khz("scaling_cur_freq")), "hw_cur_MHz": _mhz(khz("cpuinfo_cur_freq")),
            "min_MHz": _mhz(khz("scaling_min_freq")), "max_MHz": _mhz(khz("scaling_max_freq")),
            "hw_min_MHz": _mhz(khz("cpuinfo_min_freq")), "hw_max_MHz": _mhz(khz("cpuinfo_max_freq")),
            "ladder_MHz": [_mhz(x) for x in sorted(set(ladder + boost))],
            "boost_MHz": [_mhz(x) for x in boost],
            "time_in_state_10ms": {_mhz(k): v for k, v in sorted(stats.items())},
            "transitions": sysfs.read_int(d + "/stats/total_trans"),
        })
    return out


def _mhz(khz):
    return None if khz is None else round(khz / 1000.0, 1)


def devfreq():
    out = []
    for dv in sysfs.listdir("/sys/class/devfreq"):
        d = "/sys/class/devfreq/" + dv
        hz = lambda f: sysfs.read_int(d + "/" + f)
        out.append({"device": dv, "governor": sysfs.read(d + "/governor"),
                    "cur_MHz": _hz_mhz(hz("cur_freq")), "min_MHz": _hz_mhz(hz("min_freq")),
                    "max_MHz": _hz_mhz(hz("max_freq")),
                    "ladder_MHz": [_hz_mhz(int(x)) for x in (sysfs.read(d + "/available_frequencies") or "").split() if x.isdigit()]})
    return out


def _hz_mhz(hz):
    return None if hz is None else round(hz / 1e6, 1)


def memory():
    """Memory size and speed. Speed is reported only if some kernel
    interface states it (devfreq on a memory controller, a DT clock or
    the SMBIOS memory device table); otherwise it says so."""
    out = {"size_bytes": None, "speed": None}
    for line in (sysfs.read("/proc/meminfo") or "").splitlines():
        if line.startswith("MemTotal:"):
            out["size_bytes"] = int(line.split()[1]) * 1024
    for d in devfreq():
        if re.search(r"(mem|mcc|dmc|ddr|dram|dcs)", d["device"], re.I):
            out["speed"] = {"source": "devfreq " + d["device"], "cur_MHz": d["cur_MHz"]}
    dmi = "/sys/firmware/dmi/entries"
    for e in sysfs.listdir(dmi):
        if e.startswith("17-"):  # SMBIOS type 17: memory device
            raw = None
            try:
                with open(sysfs.root(dmi + "/" + e + "/raw"), "rb") as f:
                    raw = f.read()
            except OSError:
                pass
            if raw and len(raw) > 0x16:
                speed = struct.unpack("<H", raw[0x15:0x17])[0]
                if speed:
                    out["speed"] = {"source": "SMBIOS type 17", "MT_s": speed}
    if out["speed"] is None:
        out["speed"] = "not exposed by the kernel (no memory-controller devfreq, no SMBIOS type 17)"
    return out


def all_clocks():
    tables = opp_tables()
    users = opp_users(tables)
    ladders = {}
    for name, t in tables.items():
        ladders[name] = {"used_by": users.get(name, []),
                         "opps": [{"MHz": _hz_mhz(o["hz"]) if o.get("hz") is not None else None,
                                   **{k: v for k, v in o.items() if k in ("uV", "uW", "level")}}
                                  for o in t["opps"]]}
    return {"cpu": cpu_policies(), "devfreq": devfreq(), "dt_ladders": ladders, "memory": memory()}


def readings():
    """Flat readings for list/watch: per-policy current/min/max, ladders."""
    out = []

    def r(id_, attr, value, unit=None, label=None, path=None, writable=False):
        out.append({"id": id_, "cat": "clock", "dev": id_.split(".")[1], "attr": attr, "label": label,
                    "value": value, "unit": unit, "raw": str(value), "path": path, "writable": writable})

    c = all_clocks()
    for p in c["cpu"]:
        pre = "clock.cpu-%s" % p["policy"]
        for k in ("cur_MHz", "hw_cur_MHz", "min_MHz", "max_MHz", "hw_min_MHz", "hw_max_MHz"):
            r("%s.%s" % (pre, k[:-4]), k[:-4], p[k], "MHz", "cpus " + (p["cpus"] or ""))
        r(pre + ".governor", "governor", p["governor"])
        r(pre + ".ladder", "ladder", p["ladder_MHz"], "MHz", "%d steps" % len(p["ladder_MHz"]))
    for name, t in c["dt_ladders"].items():
        mhz = [o["MHz"] for o in t["opps"]]
        r("clock.%s.ladder" % name, "ladder", mhz, "MHz", "DT, used by %s" % (", ".join(t["used_by"]) or "nothing"))
    for d in c["devfreq"]:
        r("clock.devfreq-%s.cur" % d["device"], "cur", d["cur_MHz"], "MHz")
    m = c["memory"]
    r("clock.memory.size", "size", round((m["size_bytes"] or 0) / 2**30, 2), "GiB")
    r("clock.memory.speed", "speed", m["speed"] if isinstance(m["speed"], str) else json_short(m["speed"]))
    return out


def json_short(d):
    return ", ".join("%s %s" % (k, v) for k, v in d.items())
