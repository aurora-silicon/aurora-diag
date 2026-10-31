# SPDX-License-Identifier: MIT
"""Read-only ANE inventory. No ioctls, firmware boot, or benchmark runs."""

import json
import math
import os
import re

from . import sysfs

BENCHMARK = "/var/lib/omarchy-ane-tools/last-benchmark.json"
# SoC families supported by the ANE ABI and device-tree bindings. Unknown
# chips deliberately stay unknown; an explicit H13..H17 attribute wins.
SOC_GENERATIONS = {
    "t8103": "H13", "t6000": "H13", "t6001": "H13", "t6002": "H13",
    "t8112": "H14", "t6020": "H14", "t6021": "H14", "t6022": "H14",
    "t8122": "H15", "t6030": "H15", "t6031": "H15", "t6034": "H15",
    "t8132": "H16", "t6040": "H16", "t6041": "H16", "t6042": "H16",
}


def number(value):
    """Nonnegative, finite numbers only (bool is not a counter)."""
    if isinstance(value, bool):
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _integer(value):
    if type(value) is int:
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d+", value):
        return int(value)
    n = number(value)
    return int(n) if n is not None and n.is_integer() else None


def _first(path, names):
    for name in names:
        value = sysfs.read(path + "/" + name, limit=4096)
        if value:
            return value, path + "/" + name
    return None, None


def generation(compat, explicit=None):
    m = re.fullmatch(r"[Hh]?(1[3-7])", explicit or "")
    if m:
        return "H" + m[1]
    for c in compat:
        m = re.fullmatch(r"apple,h(1[3-7])-ane", c)
        if m:
            return "H" + m[1]
        chip = c.removeprefix("apple,").removesuffix("-ane")
        if chip in SOC_GENERATIONS:
            return SOC_GENERATIONS[chip]
    return None


def _counters(text):
    out = {}
    for line in (text or "").splitlines():
        m = re.fullmatch(r"([a-z][a-z0-9_]*)\s*(?:=|:|\s)\s*(\d+)", line.strip())
        if m:
            out[m[1]] = int(m[2])
    return out


def _clients(node):
    """Unique open DRM file descriptions, with visibility reported separately.

    Matching the FD's device identity avoids counting GPU clients. No process
    names or command lines are collected. Restricted /proc means a lower bound.
    """
    try:
        target = os.stat(sysfs.root(node))
    except OSError:
        return None, False
    clients = set()
    complete = True
    try:
        pids = os.listdir(sysfs.root("/proc"))
    except OSError:
        return None, False
    for pid in pids:
        if not pid.isdigit():
            continue
        path = "/proc/" + pid + "/fd"
        try:
            fds = os.listdir(sysfs.root(path))
        except FileNotFoundError:
            continue  # process exited
        except OSError:
            complete = False
            continue
        for fd in fds:
            try:
                st = os.stat(sysfs.root(path + "/" + fd))
            except FileNotFoundError:
                continue
            except OSError:
                complete = False
                continue
            if (st.st_dev, st.st_ino, st.st_rdev) != (target.st_dev, target.st_ino, target.st_rdev):
                continue
            info = sysfs.read("/proc/%s/fdinfo/%s" % (pid, fd), limit=4096)
            m = re.search(r"^drm-client-id:\s*(\d+)", info or "", re.M)
            if m:
                clients.add(m[1])
            else:
                complete = False  # cannot deduplicate duplicated/inherited FDs
    return (len(clients) if clients or complete else None), complete


def last_benchmark(devices):
    """Allowlist fields from saved smoke output or the documented benchmark export."""
    path = os.environ.get("AURORA_DIAG_ANE_BENCHMARK", BENCHMARK)
    raw = sysfs.read(path, limit=65537)
    if raw is None:
        return None, None
    try:
        if len(raw.encode()) > 65536:
            raise ValueError("file exceeds 64 KiB")
        b = json.loads(raw)
        if not isinstance(b, dict):
            raise ValueError("expected an object")
        identity = b.get("device")
        def matches_device(d):
            if identity:
                return identity in (d["node"], d["name"])
            if isinstance(b.get("die"), str):
                return b["die"] == d["platform_device"]
            if b.get("chip") not in d["chips"]:
                return False
            return "die" not in b or (type(b["die"]) is int and b["die"] == d["die"])

        matches = [d for d in devices if matches_device(d)]
        if len(matches) != 1:
            raise ValueError("benchmark must identify exactly one ANE device")
        out = {k: b[k] for k in ("name", "chip", "die", "model", "precision", "generated")
               if isinstance(b.get(k), str) and len(b[k]) <= 200}
        for k in ("min_ms", "median_ms", "p50_ms", "p90_ms", "throughput_per_s"):
            if b.get(k) is not None:
                n = number(b[k])
                if n is None:
                    raise ValueError("invalid " + k)
                out[k] = n
        for k in ("batch", "errors"):
            if b.get(k) is not None:
                n = _integer(b[k])
                if n is None or (k == "batch" and n == 0):
                    raise ValueError("invalid " + k)
                out[k] = n
        if type(b.get("die")) is int:
            out["die"] = b["die"]
        for k in ("available", "ok"):
            if isinstance(b.get(k), bool):
                out[k] = b[k]
        if not any(k in out for k in ("median_ms", "p50_ms", "throughput_per_s", "available", "ok", "errors")):
            raise ValueError("no benchmark result fields")
        out.update(device=matches[0]["node"], path=path,
                   saved_at=os.path.getmtime(sysfs.root(path)))
        return out, None
    except (ValueError, OSError, RecursionError) as e:
        return None, "saved ANE benchmark: " + str(e)


def collect():
    devices = []
    names = set(sysfs.listdir("/sys/class/accel")) | set(sysfs.listdir("/dev/accel"))
    for name in sorted(names):
        if not re.fullmatch(r"accel\d+", name):
            continue
        path = "/sys/class/accel/" + name + "/device"
        driver = sysfs.basename_of_link(path + "/driver")
        compat = sysfs.dt_strings(path + "/of_node/compatible")
        # Other accelerator drivers use this class too.
        if not ("ane" in (driver or "").lower() or any(c.endswith("-ane") or c == "apple,ane" for c in compat)):
            continue
        gen, _ = _first(path, ("generation", "ane_generation", "chip_family"))
        fw, fw_path = _first(path, ("firmware_version", "fw_version"))
        if fw is None:
            fw_path = path + "/of_node/apple,firmware-version"
            fw = sysfs.dt_value(fw_path)
            if fw is None:
                fw_path = None
        perf, perf_path = _first(path, ("performance_level", "perf_level"))
        counters = _counters(sysfs.read(path + "/ane_stats"))
        errors = {k: v for k, v in counters.items() if re.search(r"error|fault|timeout|reset|crash", k)}
        for k in ("errors", "error_count", "timeouts", "faults", "resets", "crashes"):
            n = _integer(sysfs.read(path + "/" + k))
            if n is not None:
                errors[k] = n
        node = "/dev/accel/" + name
        count, _ = _first(path, ("client_count", "clients"))
        count = _integer(count)
        if count is None:
            count, complete = _clients(node)
        else:
            complete = True
        chips = [c.removeprefix("apple,").removesuffix("-ane") for c in compat if c.startswith("apple,t")]
        # omarchy-ane-smoke identifies a die by the first 64-bit DT reg
        # address divided by Apple's die stride (0x20_0000_0000).
        reg = sysfs.dt_value(path + "/of_node/reg")
        die = None
        try:
            cells = [int(c) for c in (reg or "").split(".")]
            if len(cells) >= 2:
                die = ((cells[0] << 32) | cells[1]) // 0x2000000000
        except ValueError:
            pass
        devices.append({"name": name, "node": node, "node_present": sysfs.exists(node),
                        "path": path, "platform_device": os.path.basename(sysfs.realpath(path)),
                        "driver": driver, "compatible": compat, "chips": chips, "die": die,
                        "generation": generation(compat, gen), "firmware_version": fw,
                        "firmware_source": fw_path, "client_count": count, "clients_complete": complete,
                        "performance_level": perf, "performance_source": perf_path,
                        "power_state": sysfs.read(path + "/power/runtime_status"),
                        "wedged": _integer(sysfs.read(path + "/wedged")),
                        "counters": counters, "error_counters": errors, "last_benchmark": None})
    bench, error = last_benchmark(devices)
    if bench:
        next(d for d in devices if d["node"] == bench["device"])["last_benchmark"] = bench
    return {"schema": "aurora-diag/ane", "version": 1,
            "devices": devices, "benchmark_error": error}


def checks(data):
    def row(name, level, reason, **extra):
        return dict(group="ane", name=name, level=level, reason=reason, **extra)

    out = []
    if not data["devices"]:
        out.append(row("device", "warn", "no ANE accelerator device exposed"))
    if data.get("benchmark_error"):
        out.append(row("last benchmark", "warn", data["benchmark_error"]))
    for d in data["devices"]:
        prefix = d["node"]
        out.append(row(prefix, "pass" if d["driver"] and d["node_present"] else "fail",
                       "driver %s, generation %s" % (d["driver"] or "unbound", d["generation"] or "unknown"), device=prefix))
        missing = [k for k in ("generation", "firmware_version", "client_count", "performance_level", "power_state")
                   if d[k] is None]
        if not d["clients_complete"]:
            missing.append("complete client visibility")
        out.append(row(prefix + " telemetry", "warn" if missing else "pass",
                       "unavailable: " + ", ".join(missing) if missing else "all status fields exposed", device=prefix))
        bad = {k: v for k, v in d["error_counters"].items() if v}
        if d["wedged"] or d["power_state"] == "error":
            level = "fail"
        elif bad or (d["wedged"] is None and not d["error_counters"]):
            level = "warn"
        else:
            level = "pass"
        out.append(row(prefix + " health", level,
                       "wedged %s; power %s; error counters %s (cumulative)" %
                       (d["wedged"], d["power_state"], d["error_counters"] or "unavailable"), device=prefix))
        b = d["last_benchmark"]
        if b:
            failed = b.get("ok") is False or bool(b.get("errors"))
            out.append(row(prefix + " last benchmark", "fail" if failed else "warn" if b.get("available") is False else "pass",
                           "saved result, not a live test: %s; median %s ms, errors %s" %
                           (b.get("name", b.get("model", "benchmark")), b.get("median_ms", b.get("p50_ms")), b.get("errors")), device=prefix))
        else:
            out.append(row(prefix + " last benchmark", "warn", "no saved benchmark for this device", device=prefix))
    return out


def readings():
    out = []
    for d in collect()["devices"]:
        for key in ("driver", "generation", "firmware_version", "client_count", "clients_complete",
                    "performance_level", "power_state", "wedged", "counters", "error_counters", "last_benchmark"):
            value = d[key]
            out.append(dict(id="ane.%s.%s" % (d["name"], key), cat="ane", dev=d["name"], attr=key,
                            value=value, raw=value, unit=None, label=key, path=d["path"], writable=False))
    return out
