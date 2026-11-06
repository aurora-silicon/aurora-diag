# SPDX-License-Identifier: MIT
"""Generic enumeration of everything the kernel exposes.

Nothing here is a curated list of sensors: each source walks a kernel class
or bus and reports every attribute it finds, so a driver that appears during
bring-up shows up without a change to this tool. A reading is a dict:

    id        stable name for get/set (category.device.attribute)
    cat       category (temp, power, fan, iio, thermal, ...)
    dev       device name as the kernel reports it
    attr      attribute name
    label     human label (hwmon *_label, thermal type, ...) or None
    value     converted value (float/int/str)
    unit      unit of value or None
    raw       the text the kernel returned
    path      the system path it came from
    writable  the kernel offers a store for it (policy decides separately)
"""

import math
import os
import re

from . import sysfs

HWMON_RE = re.compile(r"^(temp|in|curr|power|energy|fan|pwm|humidity|freq|intrusion)(\d+)(?:_(\w+))?$")
HWMON_SCALE = {
    "temp": (1000.0, "C"),
    "in": (1000.0, "V"),
    "curr": (1000.0, "A"),
    "power": (1e6, "W"),
    "energy": (1e6, "J"),
    "fan": (1.0, "rpm"),
    "humidity": (1000.0, "%"),
    "freq": (1e6, "MHz"),
    "pwm": (1.0, None),
}
HWMON_CAT = {"temp": "temp", "in": "power", "curr": "power", "power": "power",
             "energy": "power", "fan": "fan", "pwm": "fan", "humidity": "temp",
             "freq": "power", "intrusion": "misc"}
# Attributes whose value is not a measurement in the channel's unit.
HWMON_UNITLESS = {"label", "alarm", "fault", "enable", "type", "beep", "mode",
                  "crit_alarm", "max_alarm", "min_alarm", "lcrit_alarm",
                  "emergency_alarm", "cap_alarm", "offset_alarm"}

PSY_UNITS = [
    (re.compile(r"^(voltage|constant_charge_voltage)"), 1e6, "V"),
    (re.compile(r"^(current|constant_charge_current|charge_term_current|input_current)"), 1e6, "A"),
    (re.compile(r"^(power|input_power)"), 1e6, "W"),
    (re.compile(r"^energy"), 1e6, "Wh"),
    (re.compile(r"^charge_(full|now|empty|counter)"), 1e6, "Ah"),
    (re.compile(r"^temp"), 10.0, "C"),
]
# Personal identifiers are left out of every listing and log.
PSY_PRIVATE = {"serial_number"}

INTERESTING_MODULES = re.compile(
    r"(apple|macsmc|aurora|asahi|dockchannel|tps6598x|snd_soc|tas27|cs42l|tipd|"
    r"dcp|aop|sep|pmp|pmgr|spi_apple|nvme_apple|pcie_apple|thunderbolt|"
    r"hid_magicmouse|brcmfmac|hci_bcm|mt79|spmi|rtkit|i2c_apple|dwc3|"
    r"typec|ucsi|cpufreq|thermal)")

APPLE_DRIVERS = re.compile(
    r"(apple|macsmc|aurora|asahi|dcp|aop|sep|pmp|pmgr|rtkit|dockchannel|iio_aop|"
    r"snd_soc_apple|snd-soc-macaudio|nvme-apple|pcie-apple|dwc3|tps6598x|"
    r"spmi|isp|avd|mca|admac|dart|sio|thunderbolt)")

# debugfs subtrees that are safe to read (no side effects, bounded output).
DEBUGFS_ALLOW = [
    re.compile(r"^apple-pmc(/.*)?$"),
    re.compile(r"^apple-pmp-report(/.*)?$"),
    # The GPU driver: aurora-g1x, and asahi-g1x on kernels before the rename.
    re.compile(r"^aurora[^/]*(/.*)?$"),
    re.compile(r"^asahi[^/]*(/.*)?$"),
    re.compile(r"^[0-9a-f]+\.power-management:[^/]+(/status)?$"),
    re.compile(r"^regulator/regulator_summary$"),
    re.compile(r"^pm_genpd/pm_genpd_summary$"),
    re.compile(r"^wakeup_sources$"),
    re.compile(r"^usb/devices$"),
    re.compile(r"^dri/\d+/state$"),
    re.compile(r"^remoteproc(/.*)?$"),
    re.compile(r"^macsmc(/.*)?$"),
    re.compile(r"^suspend_stats$"),
]


# Directories the debugfs walk may enter.
DEBUGFS_DIRS = re.compile(
    r"^(apple-pmc|apple-pmp-report|aurora[^/]*|asahi[^/]*|[0-9a-f]+\.power-management:[^/]+|"
    r"regulator|pm_genpd|usb|dri|dri/\d+|remoteproc|macsmc)(/[^/]+)*$")


def _num(text):
    if text is None:
        return None
    try:
        return int(text, 0) if re.fullmatch(r"-?\d+", text) else float(text)
    except ValueError:
        return None


def _reading(cat, id_, dev, attr, path, raw, value=None, unit=None, label=None):
    if raw is None and value is None and path and not path.startswith("alsa:"):
        _, why = sysfs.read_error(path)
        if why:
            value = "unreadable (%s)" % why
    return {
        "id": id_, "cat": cat, "dev": dev, "attr": attr, "label": label,
        "value": (raw if raw is not None else "unreadable") if value is None else value,
        "unit": unit if raw is not None else None, "raw": raw,
        "path": path, "writable": (not sysfs.isdir(path)) and sysfs.writable(path),
    }


def _files(d):
    out = []
    for f in sysfs.listdir(d):
        p = d + "/" + f
        if f in ("uevent",) or sysfs.isdir(p):
            continue
        m = sysfs.mode(p)
        if m is None or not (m & 0o444):
            continue
        out.append(f)
    return out


def _unique_names(dirs, namefn):
    """Stable short names: the kernel name, suffixed when it repeats."""
    names = {}
    counts = {}
    for d in dirs:
        n = namefn(d) or os.path.basename(d)
        counts[n] = counts.get(n, 0) + 1
    seen = {}
    for d in dirs:
        n = namefn(d) or os.path.basename(d)
        if counts[n] > 1:
            seen[n] = seen.get(n, 0) + 1
            n = "%s-%d" % (n, seen[n])
        names[d] = n
    return names


# -- hwmon ------------------------------------------------------------------

def hwmon():
    base = "/sys/class/hwmon"
    dirs = [base + "/" + h for h in sysfs.listdir(base)]
    names = _unique_names(dirs, lambda d: sysfs.read(d + "/name"))
    out = []
    for d in dirs:
        dev = names[d]
        files = _files(d)
        labels = {}
        for f in files:
            m = HWMON_RE.match(f)
            if m and m.group(3) == "label":
                labels[m.group(1) + m.group(2)] = sysfs.read(d + "/" + f)
        for f in files:
            m = HWMON_RE.match(f)
            if not m or m.group(3) == "label":
                continue
            kind, ch, sub = m.group(1), m.group(2), m.group(3) or ""
            path = d + "/" + f
            raw = sysfs.read(path)
            div, unit = HWMON_SCALE.get(kind, (1.0, None))
            n = _num(raw)
            if sub in HWMON_UNITLESS or n is None:
                value, unit = (n if n is not None else raw), None
            else:
                value = round(n / div, 3) if div != 1.0 else n
            out.append(_reading(HWMON_CAT.get(kind, "misc"), "hwmon.%s.%s" % (dev, f),
                                dev, f, path, raw, value, unit, labels.get(kind + ch)))
    return out


# -- iio --------------------------------------------------------------------

def iio():
    base = "/sys/bus/iio/devices"
    dirs = [base + "/" + d for d in sysfs.listdir(base) if d.startswith("iio:device")]
    names = _unique_names(dirs, lambda d: sysfs.read(d + "/name"))
    out = []
    for d in dirs:
        dev = names[d]
        files = _files(d)
        fs = set(files)
        for f in files:
            if not (f.startswith("in_") or f.startswith("out_")):
                continue
            if f.endswith("_scale") or f.endswith("_offset") or f.endswith("_available"):
                continue
            path = d + "/" + f
            raw = sysfs.read(path)
            n = _num(raw)
            value = n if n is not None else raw
            unit = None
            if f.endswith("_raw") and n is not None:
                stem = f[:-4]
                typ = stem.split("_")[0] + "_" + stem.split("_")[1] if "_" in stem else stem
                scale = next((sysfs.read_float(d + "/" + s) for s in (stem + "_scale", typ + "_scale") if s in fs), None)
                off = next((sysfs.read_float(d + "/" + s) for s in (stem + "_offset", typ + "_offset") if s in fs), None)
                value = (n + (off or 0)) * (scale if scale is not None else 1)
                value = round(value, 4)
            label = None
            if "illuminance" in f:
                unit, label = "lux", "ambient light"
            elif "_cct" in f or "colortemp" in f:
                unit, label = "K", "colour temperature"
            elif "anglvel" in f:
                unit, label = ("rad/s" if f.endswith("_raw") else None), "gyroscope"
            elif "accel" in f:
                unit, label = ("m/s^2" if f.endswith("_raw") else None), "accelerometer"
            elif "magn" in f:
                label = "magnetometer"
            elif "angl" in f:
                # IIO angles are radians (ABI: in_angl_raw * scale); show degrees.
                if f.endswith("_raw") and isinstance(value, (int, float)) and any(
                        x in fs for x in (f[:-4] + "_scale", "in_angl_scale")):
                    value = round(math.degrees(value), 2)
                unit, label = "deg", "lid angle" if "las" in dev or "lid" in dev else "angle"
            elif "temp" in f:
                unit = "C"
            out.append(_reading("iio", "iio.%s.%s" % (dev, f), dev, f, path, raw, value, unit, label))
    return out


# -- thermal ----------------------------------------------------------------

def thermal():
    base = "/sys/class/thermal"
    out = []
    for z in sysfs.listdir(base):
        d = base + "/" + z
        if z.startswith("thermal_zone"):
            typ = sysfs.read(d + "/type") or z
            for f in _files(d):
                if f in ("type", "available_policies", "integral_cutoff", "k_d", "k_i", "k_po", "k_pu", "slope", "offset"):
                    if f != "available_policies":
                        continue
                path = d + "/" + f
                raw = sysfs.read(path)
                value, unit = raw, None
                if (f == "temp" or (f.startswith("trip_point_") and (f.endswith("_temp") or f.endswith("_hyst")))) and _num(raw) is not None:
                    value, unit = round(_num(raw) / 1000.0, 3), "C"
                label = typ
                if f.startswith("trip_point_") and not f.endswith("_type"):
                    tt = sysfs.read(d + "/" + f.rsplit("_", 1)[0] + "_type")
                    label = "%s %s" % (typ, tt)
                out.append(_reading("thermal", "thermal.%s.%s" % (z, f), z, f, path, raw, value, unit, label))
        elif z.startswith("cooling_device"):
            typ = sysfs.read(d + "/type") or z
            for f in ("cur_state", "max_state"):
                path = d + "/" + f
                raw = sysfs.read(path)
                if raw is None:
                    continue
                out.append(_reading("cooling", "cooling.%s.%s" % (z, f), z, f, path, raw, _num(raw), None, typ))
    return out


# -- power supply -------------------------------------------------------------

def power_supply():
    base = "/sys/class/power_supply"
    out = []
    for s in sysfs.listdir(base):
        d = base + "/" + s
        for f in _files(d):
            if f in PSY_PRIVATE:
                continue
            path = d + "/" + f
            raw = sysfs.read(path)
            value, unit = raw, None
            n = _num(raw)
            if n is not None:
                value = n
                for rx, div, u in PSY_UNITS:
                    if rx.match(f):
                        value, unit = round(n / div, 4), u
                        break
            cat = "battery" if (sysfs.read(d + "/type") == "Battery") else "power"
            out.append(_reading(cat, "psy.%s.%s" % (s, f), s, f, path, raw, value, unit))
    return out


# -- cpufreq / devfreq ---------------------------------------------------------

def cpufreq():
    base = "/sys/devices/system/cpu/cpufreq"
    out = []
    for p in sysfs.listdir(base):
        d = base + "/" + p
        if not p.startswith("policy"):
            if p == "boost":
                raw = sysfs.read(d)
                out.append(_reading("cpufreq", "cpufreq.boost", "cpufreq", "boost", d, raw, _num(raw)))
            continue
        for f in _files(d):
            path = d + "/" + f
            raw = sysfs.read(path)
            value, unit = raw, None
            if f.endswith("_freq") and _num(raw) is not None:
                value, unit = _num(raw) / 1000.0, "MHz"
            out.append(_reading("cpufreq", "cpufreq.%s.%s" % (p, f), p, f, path, raw, value, unit))
    return out


def devfreq():
    base = "/sys/class/devfreq"
    out = []
    for dv in sysfs.listdir(base):
        d = base + "/" + dv
        for f in _files(d):
            path = d + "/" + f
            raw = sysfs.read(path)
            value, unit = raw, None
            if f.endswith("_freq") and _num(raw) is not None:
                value, unit = _num(raw) / 1e6, "MHz"
            out.append(_reading("devfreq", "devfreq.%s.%s" % (dv, f), dv, f, path, raw, value, unit))
    return out


# -- display, lights ----------------------------------------------------------

def backlight():
    out = []
    for cls, cat in (("/sys/class/backlight", "backlight"), ("/sys/class/leds", "leds")):
        for b in sysfs.listdir(cls):
            d = cls + "/" + b
            for f in ("brightness", "max_brightness", "actual_brightness", "bl_power", "trigger"):
                path = d + "/" + f
                raw = sysfs.read(path)
                if raw is None:
                    continue
                value = _num(raw) if _num(raw) is not None else raw
                if f == "trigger":
                    m = re.search(r"\[([^\]]+)\]", raw)
                    value = m.group(1) if m else raw
                out.append(_reading(cat, "%s.%s.%s" % (cat, b, f), b, f, path, raw, value))
    return out


def drm():
    base = "/sys/class/drm"
    out = []
    for c in sysfs.listdir(base):
        if "-" not in c or not c.startswith("card"):
            continue
        d = base + "/" + c
        for f in ("status", "enabled", "dpms", "modes", "vrr_capable"):
            path = d + "/" + f
            raw = sysfs.read(path)
            if raw is None:
                continue
            value = raw.replace("\n", ",") if f == "modes" else raw
            out.append(_reading("display", "drm.%s.%s" % (c, f), c, f, path, raw, value))
        edid = sysfs.root(d + "/edid")
        try:
            with open(edid, "rb") as fh:
                n = len(fh.read())
            out.append(_reading("display", "drm.%s.edid_bytes" % c, c, "edid_bytes", d + "/edid", str(n), n))
        except OSError:
            pass
    return out


# -- buses and devices --------------------------------------------------------

def typec():
    base = "/sys/class/typec"
    out = []
    for t in sysfs.listdir(base):
        d = base + "/" + t
        for f in _files(d):
            path = d + "/" + f
            raw = sysfs.read(path)
            if raw is None:
                continue
            value = raw
            m = re.search(r"\[([^\]]+)\]", raw)
            if m:
                value = m.group(1)
            out.append(_reading("typec", "typec.%s.%s" % (t, f), t, f, path, raw, value))
        idd = d + "/identity"
        if sysfs.isdir(idd):
            for f in _files(idd):
                raw = sysfs.read(idd + "/" + f)
                out.append(_reading("typec", "typec.%s.identity.%s" % (t, f), t, "identity/" + f, idd + "/" + f, raw))
    for p in sysfs.listdir("/sys/class/usb_power_delivery"):
        d = "/sys/class/usb_power_delivery/" + p
        for f in _files(d):
            raw = sysfs.read(d + "/" + f)
            out.append(_reading("typec", "pd.%s.%s" % (p, f), p, f, d + "/" + f, raw))
    return out


def usb():
    base = "/sys/bus/usb/devices"
    out = []
    for u in sysfs.listdir(base):
        if ":" in u:
            continue
        d = base + "/" + u
        fields = {}
        for f in ("idVendor", "idProduct", "manufacturer", "product", "speed", "busnum", "devnum", "bDeviceClass", "maxchild"):
            v = sysfs.read(d + "/" + f)
            if v is not None:
                fields[f] = v
        if not fields:
            continue
        desc = "%s:%s %s %s %sMb/s" % (fields.get("idVendor", "?"), fields.get("idProduct", "?"),
                                       fields.get("manufacturer", ""), fields.get("product", ""), fields.get("speed", "?"))
        out.append(_reading("usb", "usb.%s" % u, u, "device", d, desc.strip(), desc.strip()))
        pc = d + "/power/control"
        if sysfs.exists(pc):
            out.append(_reading("usb", "usb.%s.power_control" % u, u, "power/control", pc, sysfs.read(pc)))
    return out


def input_devices():
    out = []
    text = sysfs.read("/proc/bus/input/devices", limit=262144) or ""
    for block in text.split("\n\n"):
        name = handlers = None
        for line in block.splitlines():
            if line.startswith("N: Name="):
                name = line[8:].strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line[12:].strip()
        if name:
            out.append(_reading("input", "input.%s" % name.replace(" ", "_"), name, "handlers",
                                "/proc/bus/input/devices", handlers or "", handlers or ""))
    base = "/sys/class/hidraw"
    for h in sysfs.listdir(base):
        ue = sysfs.uevent(base + "/" + h + "/device")
        name = ue.get("HID_NAME", "")
        out.append(_reading("input", "hidraw.%s" % h, h, "name", base + "/" + h + "/device/uevent",
                            name, "%s (%s)" % (name, ue.get("HID_ID", ""))))
    return out


def radios():
    out = []
    base = "/sys/class/rfkill"
    for r in sysfs.listdir(base):
        d = base + "/" + r
        name = sysfs.read(d + "/name")
        typ = sysfs.read(d + "/type")
        for f in ("soft", "hard", "state"):
            raw = sysfs.read(d + "/" + f)
            if raw is not None:
                out.append(_reading("radio", "rfkill.%s.%s" % (name or r, f), name or r, f, d + "/" + f, raw, _num(raw), None, typ))
    for n in sysfs.listdir("/sys/class/net"):
        d = "/sys/class/net/" + n
        for f in ("operstate", "carrier", "mtu"):
            raw = sysfs.read(d + "/" + f)
            if raw is not None:
                out.append(_reading("net", "net.%s.%s" % (n, f), n, f, d + "/" + f, raw, _num(raw) if f != "operstate" else raw))
        drv = sysfs.basename_of_link(d + "/device/driver")
        if drv:
            out.append(_reading("net", "net.%s.driver" % n, n, "driver", d + "/device/driver", drv))
    for h in sysfs.listdir("/sys/class/bluetooth"):
        d = "/sys/class/bluetooth/" + h
        drv = sysfs.basename_of_link(d + "/device/driver") or ""
        out.append(_reading("bt", "bt.%s" % h, h, "present", d, "present", "present", None, drv))
    return out


def video():
    out = []
    for v in sysfs.listdir("/sys/class/video4linux"):
        d = "/sys/class/video4linux/" + v
        raw = sysfs.read(d + "/name")
        out.append(_reading("v4l2", "v4l2.%s.name" % v, v, "name", d + "/name", raw))
    return out


def regulators():
    out = []
    for r in sysfs.listdir("/sys/class/regulator"):
        d = "/sys/class/regulator/" + r
        name = sysfs.read(d + "/name") or r
        for f in ("state", "microvolts", "microamps", "status", "num_users"):
            raw = sysfs.read(d + "/" + f)
            if raw is None:
                continue
            value, unit = _num(raw) if _num(raw) is not None else raw, None
            if f == "microvolts" and _num(raw) is not None:
                value, unit = _num(raw) / 1e6, "V"
            if f == "microamps" and _num(raw) is not None:
                value, unit = _num(raw) / 1e6, "A"
            out.append(_reading("regulator", "regulator.%s.%s" % (name, f), name, f, d + "/" + f, raw, value, unit))
    return out


def remoteproc():
    out = []
    for r in sysfs.listdir("/sys/class/remoteproc"):
        d = "/sys/class/remoteproc/" + r
        for f in ("name", "state", "firmware"):
            raw = sysfs.read(d + "/" + f)
            if raw is not None:
                out.append(_reading("coproc", "rproc.%s.%s" % (r, f), r, f, d + "/" + f, raw))
    return out


def drivers():
    """Which Apple platform drivers are bound, and to what: shows at a glance
    whether SEP, AOP, DCP, ISP, PMP, SMC and friends probed."""
    out = []
    base = "/sys/bus/platform/drivers"
    for drv in sysfs.listdir(base):
        if not APPLE_DRIVERS.search(drv):
            continue
        d = base + "/" + drv
        devs = [x for x in sysfs.listdir(d)
                if x != "module" and os.path.islink(sysfs.root(d + "/" + x))]
        out.append(_reading("driver", "driver.%s" % drv, drv, "bound", d, ",".join(devs) or "-",
                            len(devs), None, ",".join(devs) or "not bound"))
    return out


def modparams(all_modules=False):
    out = []
    base = "/sys/module"
    for m in sysfs.listdir(base):
        if not all_modules and not INTERESTING_MODULES.search(m):
            continue
        d = base + "/" + m + "/parameters"
        for f in sysfs.listdir(d):
            path = d + "/" + f
            raw = sysfs.read(path)
            out.append(_reading("modparam", "param.%s.%s" % (m, f), m, f, path, raw,
                                raw, None, "mode %o" % (sysfs.mode(path) or 0)))
    return out


def sep():
    out = []
    present = sysfs.exists("/dev/sep-bio")
    out.append(_reading("sep", "sep.sep-bio", "sep", "sep-bio", "/dev/sep-bio",
                        "present" if present else "absent"))
    for drv in ("apple_sep", "apple-sep"):
        d = "/sys/bus/platform/drivers/" + drv
        if sysfs.isdir(d):
            devs = [x for x in sysfs.listdir(d) if "." in x]
            out.append(_reading("sep", "sep.driver", drv, "bound", d, ",".join(devs) or "-"))
    return out


def debugfs():
    """Allowlisted debugfs reports (root only). Values are text blobs."""
    out = []
    base = "/sys/kernel/debug"
    if not sysfs.isdir(base) or not os.access(sysfs.root(base), os.R_OK | os.X_OK):
        return out

    def walk(rel, depth):
        if depth > 3:
            return
        p = base + "/" + rel if rel else base
        for e in sysfs.listdir(p):
            r = rel + "/" + e if rel else e
            full = base + "/" + r
            if sysfs.isdir(full):
                if DEBUGFS_DIRS.match(r):
                    walk(r, depth + 1)
                continue
            if not any(rx.match(r) for rx in DEBUGFS_ALLOW):
                continue
            m = sysfs.mode(full) or 0
            if not (m & 0o444):
                continue
            raw = sysfs.read_timeout(full, timeout=2.0, limit=32768)
            if raw is None:
                continue
            out.append(_reading("debugfs", "debugfs.%s" % r, r.split("/")[0], r, full, raw,
                                raw, None, raw.splitlines()[0][:80] if raw else ""))
    walk("", 0)
    return out


def haptic():
    """The trackpad actuator's click strength (root: it reads the hidraw
    node's feature reports; nothing is written)."""
    from . import haptics
    dev, name = haptics.find_actuator()
    if not dev:
        return []
    out = []
    for rid, attr in ((haptics.PRESS, "press"), (haptics.RELEASE, "release")):
        try:
            rep = haptics.get_report(dev, rid)
            raw = rep.hex()
            value = rep[haptics.STRENGTH_BYTE]
        except OSError as e:
            raw, value = None, "unreadable (%s)" % e.strerror
        r = _reading("haptic", "haptic.%s.%s" % (os.path.basename(dev), attr), name, attr, dev, raw,
                     value, None, "report %#x strength byte; `aurora-diag haptic set`" % rid)
        r["writable"] = False  # changed through `haptic set`, which journals the whole report
        out.append(r)
    return out


def _gpu():
    from . import gpu
    return gpu.readings()


def _gpu_live():
    from . import gpu
    return gpu.readings(live_only=True)


def _clock():
    from . import clocks
    return clocks.readings()


def _ane():
    from . import ane
    return ane.readings()


def _subsys():
    from . import subsystems
    return subsystems.readings()


def _service():
    from . import services
    return services.readings()


def _api():
    from . import gpu
    return gpu.api_readings()


SOURCES = {
    "hwmon": hwmon, "iio": iio, "thermal": thermal, "power_supply": power_supply,
    "cpufreq": cpufreq, "devfreq": devfreq, "backlight": backlight, "drm": drm,
    "typec": typec, "usb": usb, "input": input_devices, "radio": radios,
    "v4l2": video, "regulator": regulators, "remoteproc": remoteproc,
    "driver": drivers, "modparam": modparams, "sep": sep, "haptic": haptic,
    "gpu": _gpu, "ane": _ane, "clock": _clock, "subsys": _subsys, "service": _service,
    "api": _api, "debugfs": debugfs,
}
# Sources only asked for by name: the GPU without the per-process walk,
# for watch and the TUI's once-a-second refresh.
HIDDEN_SOURCES = {"gpu_live": _gpu_live}

FAST_SOURCES = ("hwmon", "iio", "thermal", "power_supply", "cpufreq", "devfreq", "backlight", "gpu_live")
# Sources that run external programs and take seconds (Vulkan, GL).
SLOW_SOURCES = ("api",)


def collect(names=None, extra=()):
    """All readings from the named sources (default: all), with unique ids."""
    out = []
    for n in (names if names is not None else SOURCES.keys()):
        fn = SOURCES.get(n) or HIDDEN_SOURCES.get(n)
        if fn is None:
            continue
        try:
            out.extend(fn())
        except Exception as e:  # a broken driver must not take the tool down
            out.append({"id": "error.%s" % n, "cat": "error", "dev": n, "attr": "error",
                        "label": None, "value": repr(e), "unit": None, "raw": repr(e),
                        "path": None, "writable": False})
    for fn in extra:
        try:
            out.extend(fn())
        except Exception as e:
            out.append({"id": "error.%s" % getattr(fn, "__name__", "extra"), "cat": "error",
                        "dev": "extra", "attr": "error", "label": None, "value": repr(e),
                        "unit": None, "raw": repr(e), "path": None, "writable": False})
    # Show a value as writable only when the kernel offers a store AND the
    # safety policy would let a change through.
    from . import control
    for r in out:
        if r.get("writable") and r.get("path"):
            p = r["path"]
            if p.startswith("alsa:"):
                r["writable"] = control.alsa_refusal(r["attr"]) is None
            else:
                r["writable"] = control.refusal(p) is None
    seen = {}
    for r in out:
        i = r["id"]
        if i in seen:
            seen[i] += 1
            r["id"] = "%s#%d" % (i, seen[i])
        else:
            seen[i] = 1
    return out
