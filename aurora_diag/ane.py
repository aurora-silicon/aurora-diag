# SPDX-License-Identifier: MIT
"""Read-only ANE inventory. No ioctls, firmware boot, or benchmark runs."""

import json
import errno
import grp
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
    "t8140": "H17",
}


def _permission(path):
    try:
        group = grp.getgrgid(os.stat(sysfs.root(path)).st_gid).gr_name
    except (OSError, KeyError):
        group = "accel" if path.startswith("/dev/accel/") else "root"
    return "Permission denied for %s; request access through the %s group or its ACL from the administrator." % (path, group)


def _read(path, issues, optional=False):
    """Keep read failures distinct from missing optional kernel ABI files."""
    try:
        with open(sysfs.root(path), "rb") as f:
            return f.read(4096).decode("ascii", "replace").strip().rstrip("\0")
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EPERM):
            issues.append(_permission(path))
        elif e.errno == errno.ENOENT:
            if not optional:
                issues.append("%s is not exposed by this kernel; install an Aurora kernel with ANE telemetry enabled." % path)
        else:
            issues.append("Cannot read %s (%s); inspect the ANE kernel log and retry after resolving the driver error." % (path, e.strerror or "I/O error"))
    return None


def _dt_status(path, issues):
    try:
        os.stat(sysfs.root(path))
    except OSError as e:
        issues.append(_permission(path) if e.errno in (errno.EACCES, errno.EPERM) else
                      "ANE device-tree node is unavailable; boot the Aurora board DT with the ANE node and its firmware dependencies.")
        return None
    before = len(issues)
    value = _read(path + "/status", issues, optional=True)
    # DT specifies an absent status as available, but unreadable is unknown.
    return value if value is not None else ("okay" if len(issues) == before and not sysfs.exists(path + "/status") else None)


def _is_ane(driver, compat):
    return _ane_driver(driver) or any(
        c.startswith("apple,") and (c.endswith("-ane") or c == "apple,ane") for c in compat)


def _ane_driver(driver):
    return bool(re.fullmatch(r"ane(?:[_-].+)?", driver or ""))


def _dt_nodes(devices):
    """Inventory disabled/unbound nodes too; accelerator class alone misses them."""
    nodes = {}

    def add(path, driver=None, platform=None):
        compat = sysfs.dt_strings(path + "/compatible")
        if not _is_ane(driver, compat) and not os.path.basename(sysfs.realpath(path)).startswith("ane@"):
            return
        key = sysfs.realpath(path)
        issues = []
        if not compat:
            _read(path + "/compatible", issues, optional=True)
            if not issues:
                issues.append("ANE DT compatible is missing or empty; boot the Aurora board DT with the correct apple,SoC-ane compatible.")
        old = nodes.get(key, {})
        nodes[key] = dict(path=key, compatible=compat, generation=generation(compat),
                          status=_dt_status(path, issues), driver=driver or old.get("driver"),
                          platform_device=platform or old.get("platform_device"), issues=issues)

    base = "/sys/firmware/devicetree/base"
    if not sysfs.isdir(base):
        base = "/proc/device-tree"
    scan_issues = []
    def walk_error(error):
        if error.errno == errno.ENOENT:
            return
        if error.errno in (errno.EACCES, errno.EPERM):
            scan_issues.append(_permission(sysfs.unroot(error.filename)))
        else:
            scan_issues.append("Cannot enumerate the ANE device tree due to an I/O error; inspect the kernel log and retry after fixing DT sysfs access.")
    for parent, _, files in os.walk(sysfs.root(base), onerror=walk_error):
        if "compatible" in files or os.path.basename(parent).startswith("ane@"):
            add(sysfs.unroot(parent))
    for name in sysfs.listdir("/sys/bus/platform/devices"):
        path = "/sys/bus/platform/devices/" + name
        add(path + "/of_node", sysfs.basename_of_link(path + "/driver"), name)
    for d in devices:
        add(d["path"] + "/of_node", d["driver"], d["platform_device"])
    for n in nodes.values():
        n["driver_bound"] = _ane_driver(n["driver"])
        n["accelerators"] = [d["name"] for d in devices if d["dt_node"] == n["path"]]
    return sorted(nodes.values(), key=lambda n: n["path"]), list(dict.fromkeys(scan_issues))


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
        if not _is_ane(driver, compat):
            continue
        issues = []
        gen, _ = _first(path, ("generation", "ane_generation", "chip_family"))
        fw, fw_path = _first(path, ("firmware_version", "fw_version"))
        if fw is None:
            fw_path = path + "/of_node/apple,firmware-version"
            fw = sysfs.dt_value(fw_path)
            if fw is None:
                fw_path = None
        perf = _read(path + "/performance_level", issues, optional=True)
        perf_path = path + "/performance_level" if perf is not None else None
        if perf is None and not sysfs.exists(path + "/performance_level"):
            perf, perf_path = _first(path, ("perf_level",))
        memory_vote = _read(path + "/memory_vote", issues, optional=True)
        performance_error = _read(path + "/performance_error", issues, optional=True)
        for field, value, pattern in (("performance_level", perf, r"(?:[0-5]|unsupported)"),
                                      ("memory_vote", memory_vote, r"(?:\d+\s+\d+|unsupported)"),
                                      ("performance_error", performance_error, r"(?:0|-[1-9]\d*)")):
            if value is not None and not re.fullmatch(pattern, value):
                issues.append("Malformed ANE %s telemetry; install a kernel implementing the documented ANE performance ABI." % field)
        counters = _counters(_read(path + "/ane_stats", issues))
        if sysfs.exists(path + "/ane_stats") and not {"busy_ns", "jobs"} <= counters.keys():
            issues.append("ANE busy time or job count is unavailable in ane_stats; enable ANE stats in the Aurora kernel and check its telemetry ABI.")
        errors = {k: v for k, v in counters.items() if re.search(r"error|fault|timeout|reset|crash", k)}
        for k in ("errors", "error_count", "timeouts", "faults", "resets", "crashes"):
            n = _integer(sysfs.read(path + "/" + k))
            if n is not None:
                errors[k] = n
        node = "/dev/accel/" + name
        if sysfs.exists(node) and not os.access(sysfs.root(node), os.R_OK | os.W_OK):
            issues.append(_permission(node))
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
        wedged_text = _read(path + "/wedged", issues)
        if wedged_text is not None and _integer(wedged_text) is None:
            issues.append("Malformed ANE wedged flag; install a matching Aurora ANE kernel and inspect its sysfs ABI.")
        devices.append({"name": name, "node": node, "node_present": sysfs.exists(node),
                        "path": path, "platform_device": os.path.basename(sysfs.realpath(path)),
                        "driver": driver, "driver_bound": _ane_driver(driver), "compatible": compat, "chips": chips, "die": die,
                        "generation": generation(compat, gen), "firmware_version": fw,
                        "firmware_source": fw_path, "client_count": count, "clients_complete": complete,
                        "performance_level": perf, "performance_source": perf_path,
                        "memory_vote": memory_vote, "performance_error": performance_error,
                        "dt_node": sysfs.realpath(path + "/of_node"),
                        "dt_status": _dt_status(path + "/of_node", issues),
                        "power_state": _read(path + "/power/runtime_status", issues),
                        "wedged": _integer(wedged_text),
                        "issues": issues,
                        "counters": counters, "error_counters": errors, "last_benchmark": None})
    dt_nodes, issues = _dt_nodes(devices)
    bench, error = last_benchmark(devices)
    if bench:
        next(d for d in devices if d["node"] == bench["device"])["last_benchmark"] = bench
    return {"schema": "aurora-diag/ane", "version": 1,
            "devices": devices, "dt_nodes": dt_nodes, "issues": issues, "benchmark_error": error}


def checks(data):
    def row(name, level, reason, **extra):
        return dict(group="ane", name=name, level=level, reason=reason, **extra)

    out = []
    if not data["devices"]:
        if not data.get("dt_nodes"):
            out.append(row("device", "warn", "No ANE device or DT node is exposed; install the Aurora kernel and bootloader DT for this SoC with the ANE node, DARTs and power domains described."))
    for reason in data.get("issues", []):
        out.append(row("device tree", "warn", reason))
    for n in data.get("dt_nodes", []):
        for reason in n["issues"]:
            out.append(row(n["path"], "warn", reason))
        if n["status"] not in ("okay", "ok", None):
            fix = "install a board DT enabling the ANE and its DARTs and power domains"
            if n["generation"] in ("H16", "H17"):
                fix += " with bootloader firmware TEXT/DATA reservations and a Linux ownership provider where required"
            out.append(row(n["path"], "fail", "ANE DT status is %s; %s." % (n["status"], fix)))
        elif not n["driver_bound"]:
            fix = "install kernel-aurora (Fedora), linux-aurora (ALARM), or nixos-aurora linux-asahi with the ANE driver for %s and inspect probe errors for its DART and power-domain dependencies" % (n["generation"] or "this SoC")
            if n["generation"] == "H14":
                fix += " and fetch missing apple/ane firmware with omarchy-ane-firmware-fetch (on NixOS, configure aurora.ane.firmwarePackage and rebuild)"
            elif n["generation"] in ("H16", "H17"):
                fix += " including bootloader-preloaded firmware reservations and any required Linux ownership provider"
            cause = "ANE driver is unbound" if not n["driver"] else "ANE node is bound to %s instead of an ANE driver" % n["driver"]
            out.append(row(n["path"], "fail", "%s; %s." % (cause, fix)))
        elif not n["accelerators"]:
            out.append(row(n["path"], "fail", "ANE driver is bound without an accelerator class device; inspect driver registration errors and install the matching Aurora ANE kernel."))
    if data.get("benchmark_error"):
        out.append(row("last benchmark", "warn", data["benchmark_error"] + "; replace the saved result with valid device-matched benchmark JSON."))
    for d in data["devices"]:
        prefix = d["node"]
        bound = d.get("driver_bound", _ane_driver(d["driver"]))
        out.append(row(prefix, "pass" if bound and d["node_present"] else "fail",
                       ("driver %s, generation %s" % (d["driver"], d["generation"] or "unknown")) if bound and d["node_present"]
                       else "ANE driver or accelerator node is missing; install the Aurora ANE kernel driver and its accelerator udev rules.", device=prefix))
        for reason in d.get("issues", []):
            out.append(row(prefix + " access", "warn", reason, device=prefix))
        missing = [k for k in ("generation", "firmware_version", "client_count", "power_state")
                   if d[k] is None]
        if not d["clients_complete"]:
            missing.append("complete client visibility")
        out.append(row(prefix + " telemetry", "warn" if missing else "pass",
                       "Unavailable telemetry (%s); use a matching Aurora ANE kernel and grant the administrator access to restricted status files." % ", ".join(missing) if missing else "all status fields exposed", device=prefix))
        bad = {k: v for k, v in d["error_counters"].items() if v}
        perf_error = d.get("performance_error")
        if d["wedged"] or d["power_state"] == "error" or (perf_error and re.fullmatch(r"-[1-9]\d*", perf_error)):
            level = "fail"
        elif bad or (d["wedged"] is None and not d["error_counters"]):
            level = "warn"
        else:
            level = "pass"
        reason = "wedged %s; power %s; error counters %s (cumulative)" % (d["wedged"], d["power_state"], d["error_counters"] or "unavailable")
        if d["wedged"]:
            reason = "ANE is wedged; ask the administrator to use the driver's bounded sysfs reset or reboot if recovery fails."
        elif d["power_state"] == "error":
            reason = "ANE runtime PM is in error; inspect the kernel power-domain errors and reboot after fixing the matching kernel/DT."
        elif level == "fail":
            reason = "ANE performance transition failed with errno %s; inspect the kernel PMP/interconnect error and install the matching Aurora kernel/DT." % perf_error
        elif level == "warn":
            reason += "; inspect the ANE kernel log and enable missing health telemetry before retrying workloads."
        out.append(row(prefix + " health", level, reason, device=prefix))
        b = d["last_benchmark"]
        if b:
            failed = b.get("ok") is False or bool(b.get("errors"))
            out.append(row(prefix + " last benchmark", "fail" if failed else "warn" if b.get("available") is False else "pass",
                           "saved result, not a live test: %s; median %s ms, errors %s" %
                           (b.get("name", b.get("model", "benchmark")), b.get("median_ms", b.get("p50_ms")), b.get("errors")) +
                           ("; inspect the smoke failure and replace the saved result after a successful separate run." if failed or b.get("available") is False else ""), device=prefix))
        else:
            out.append(row(prefix + " last benchmark", "warn", "No saved benchmark exists for this device; save a result from the separately installed ANE smoke tool.", device=prefix))
    return out


def readings():
    out = []
    for d in collect()["devices"]:
        for key in ("driver", "driver_bound", "generation", "compatible", "dt_status", "firmware_version", "client_count", "clients_complete",
                    "performance_level", "memory_vote", "performance_error", "power_state", "wedged", "counters", "error_counters", "last_benchmark"):
            value = d[key]
            out.append(dict(id="ane.%s.%s" % (d["name"], key), cat="ane", dev=d["name"], attr=key,
                            value=value, raw=value, unit=None, label=key, path=d["path"], writable=False))
    return out
