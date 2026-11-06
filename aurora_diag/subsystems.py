# SPDX-License-Identifier: MIT
"""Every hardware block the device tree describes, reported against what
the kernel actually did with it.

For each device-tree node with an "apple," (or any) compatible the module
answers: is it enabled in the DT, which Linux device stands for it, is a
driver bound (or why was it deferred), which firmware version the DT or the
kernel log names, which class devices and /dev nodes it produced, and the
last error or warning the kernel logged about it. PCI functions (Wi-Fi,
Bluetooth, USB controllers behind PCIe) are reported the same way.

Nothing is keyed by machine. The only table is BLOCK_TITLES, which gives
readable names to DT node names (dcp, isp, sep, ...); an unknown node name
is still reported, under its own name.
"""

import os
import re
import shutil
import subprocess

from . import identity, sysfs

DT_BASE = "/sys/firmware/devicetree/base"

# DT node names that are Apple blocks a person asks about, with readable
# names. Everything else is still listed as infrastructure.
BLOCK_TITLES = {
    "dcp": "DCP (internal display)", "dcpext": "DCPEXT (external display)",
    "display-subsystem": "Display subsystem (DRM)", "gpu": "GPU (AGX)",
    "isp": "ISP (camera)", "sep": "SEP (Secure Enclave, Touch ID)", "ane": "ANE (Neural Engine)",
    "avd": "AVD (video decoder)", "ave": "AVE (video encoder)", "aop": "AOP (always-on processor)",
    "pmp": "PMP (power manager)", "pmp-report": "PMP report", "smc": "SMC", "nvme": "ANS / NVMe",
    "ans": "ANS", "mtp": "MTP (keyboard, trackpad)", "sound": "Audio (macaudio)",
    "sound-jack": "Headphone jack", "pcie": "PCIe", "sio": "SIO (audio DMA)",
    "mca": "MCA (audio I2S)", "admac": "ADMAC", "usb": "USB (dwc3)", "atcphy": "ATC PHY (Type-C)",
    "phy": "Type-C PHY", "spi": "SPI (keyboard, trackpad, NOR)", "i2c": "I2C (amps, PD controllers)",
    "fpwm": "Keyboard backlight PWM", "pwm": "PWM", "led-controller": "LED controller",
    "spmi": "SPMI (PMU)", "nvmem": "NVMEM", "efuse": "eFuses", "bluetooth": "Bluetooth",
    "wifi": "Wi-Fi", "wlan": "Wi-Fi", "network": "Network", "jpeg": "JPEG", "prores": "ProRes",
    "scaler": "Scaler", "dockchannel": "DockChannel (HID transport)", "hpm": "Type-C port controller",
    "isp-": "ISP", "mtp-": "MTP",
}
INFRA = re.compile(r"^(iommu|dart|mailbox|interrupt-controller|pinctrl|power-management|clock|clock-.*|"
                   r"dma-controller|l2-cache.*|cpus|cpufreq|cpuidle|memory|memory-controller|reserved-memory|"
                   r"chosen|aliases|opp-table.*|fifo|gpio|timer|psci|watchdog|wdt|serial|pmgr|pmu|"
                   r"framebuffer|soc|arm-io|smc-gpio|apcie-.*|reboot|rtc|hwmon)$")


def _node_name(dt_rel):
    return dt_rel.rsplit("/", 1)[-1]


def _base(name):
    return name.split("@", 1)[0]


def _strings(path):
    return sysfs.dt_strings(path)


def _status(dt_path):
    s = _strings(dt_path + "/status")
    return s[0] if s else "okay"


def dt_nodes():
    """Device-tree nodes with a compatible, at the top level and one below
    (buses such as soc/ or arm-io/), as paths relative to the DT root."""
    out = []

    def walk(rel, depth):
        p = DT_BASE + ("/" + rel if rel else "")
        for e in sysfs.listdir(p):
            r = (rel + "/" + e) if rel else e
            full = DT_BASE + "/" + r
            if not sysfs.isdir(full):
                continue
            if sysfs.exists(full + "/compatible"):
                out.append(r)
            if depth < 2 and e not in ("cpus", "reserved-memory", "chosen", "aliases") and not e.startswith("opp-table"):
                walk(r, depth + 1)

    walk("", 0)
    return out


def linux_devices():
    """{DT path relative to the root: sysfs device path} for every device
    on any bus that has an of_node."""
    out = {}
    for bus in sysfs.listdir("/sys/bus"):
        d = "/sys/bus/%s/devices" % bus
        for dev in sysfs.listdir(d):
            p = d + "/" + dev
            of = p + "/of_node"
            if not sysfs.exists(of):
                continue
            real = sysfs.realpath(of)
            m = re.search(r"/devicetree/base/(.*)$", real)
            if not m:
                continue
            # Several devices can share a DT node (an I2C controller and its
            # adapter, an NVMe controller and its class device): prefer the
            # one with a driver, then the platform device.
            rank = (bool(sysfs.basename_of_link(p + "/driver")), bus == "platform")
            cur = out.get(m.group(1))
            if cur is None or rank > cur[0]:
                out[m.group(1)] = (rank, sysfs.realpath(p))
    return {k: v[1] for k, v in out.items()}


def class_devices():
    """[(class, name, device path, /dev name)] for every class device."""
    out = []
    for cls in sysfs.listdir("/sys/class"):
        for n in sysfs.listdir("/sys/class/" + cls):
            p = "/sys/class/%s/%s" % (cls, n)
            real = sysfs.realpath(p)
            devname = None
            for line in (sysfs.read(p + "/uevent", 4096) or "").splitlines():
                if line.startswith("DEVNAME="):
                    devname = "/dev/" + line[8:]
            out.append((cls, n, real, devname))
    return out


def deferred():
    """{device name: reason} from debugfs devices_deferred (root)."""
    out = {}
    for line in (sysfs.read("/sys/kernel/debug/devices_deferred") or "").splitlines():
        f = line.split(None, 1)
        if f:
            out[f[0]] = f[1].strip() if len(f) > 1 else "deferred"
    return out


_dmesg_cache = {}


def dmesg(levels="emerg,alert,crit,err"):
    if levels in _dmesg_cache:
        return _dmesg_cache[levels]
    lines = None
    if shutil.which("dmesg"):
        try:
            r = subprocess.run(["dmesg", "--notime", "--level=" + levels], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                lines = r.stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            pass
    _dmesg_cache[levels] = lines
    return lines


def _last(lines, needles):
    if not lines:
        return None
    for line in reversed(lines):
        if any(n in line for n in needles if n):
            return clean(line)
    return None


CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
FW_PATTERNS = [re.compile(p, re.I) for p in (r"firmware.*version|version.*firmware", r"\bversion\b",
                                             r"RTKit: Initializing", r"\bfirmware\b")]


def clean(line):
    return CTRL.sub("", line).strip()[:300]


FW_VERSION = [re.compile(p, re.I) for p in (
    r"firmware[^:]*version[:=\s]+([\w.~+\-]+)", r"\bversion[:=\s]+([A-Za-z][\w.~+\-]*\d[\w.~+\-]*)",
    r"(protocol version \d+)", r"\bversion[:=\s]+([\w.~+\-]+)")]


def firmware_short(rec):
    """A short firmware label for tables: the DT version, else the version
    the kernel log names."""
    if rec.get("dt_firmware"):
        return rec["dt_firmware"]
    line = rec.get("firmware_log") or ""
    for rx in FW_VERSION:
        m = rx.search(line)
        if m:
            return m.group(1)
    return ""


def _firmware_line(lines, dname):
    """The kernel's most telling line about a device's firmware: a version
    line beats a plain firmware mention, and the newest of each wins."""
    if not lines:
        return None
    mine = [l for l in lines if dname in l]
    for rx in FW_PATTERNS:
        for line in reversed(mine):
            if rx.search(line):
                return clean(line)
    return None


def subsystems():
    """One record per DT node with a compatible, plus PCI functions."""
    devs = linux_devices()
    classes = class_devices()
    defer = deferred()
    errs = dmesg("emerg,alert,crit,err")
    warns = dmesg("warn")
    allmsg = dmesg("emerg,alert,crit,err,warn,notice,info")
    out = []
    for rel in dt_nodes():
        name = _node_name(rel)
        base = _base(name)
        dtp = DT_BASE + "/" + rel
        compat = _strings(dtp + "/compatible")
        st = _status(dtp)
        rec = {"node": rel, "name": name, "block": base, "title": BLOCK_TITLES.get(base),
               "kind": "infra" if (INFRA.match(base) or base not in BLOCK_TITLES) else "block",
               "compatible": compat, "dt_status": st}
        fw = sysfs.dt_value(dtp + "/apple,firmware-version")
        if fw:
            rec["dt_firmware"] = fw
        dev = devs.get(rel)
        if dev:
            dname = os.path.basename(dev)
            rec["device"] = dname
            rec["sysfs"] = dev
            drv = sysfs.basename_of_link(dev + "/driver")
            rec["driver"] = drv
            if not drv and dname in defer:
                rec["deferred"] = defer[dname]
            kids = [(c, n, dn) for (c, n, real, dn) in classes if real.startswith(dev + "/")]
            rec["class_devices"] = ["%s/%s" % (c, n) for c, n, _ in kids]
            rec["class_device_ids"] = identity.collect(classes, dev)
            rec["dev_nodes"] = sorted({dn for _, _, dn in kids if dn})
            rec["last_problem"] = _last(errs, [dname])
            rec["last_warning"] = _last(warns, [dname])
            rec["firmware_log"] = _firmware_line(allmsg, dname)
        rec["state"] = _state(rec)
        out.append(rec)
    out.extend(pci_functions(classes, errs))
    return out


def _state(rec):
    """ok / warn / fail / off, and why."""
    if rec["dt_status"] not in ("okay", "ok"):
        return {"level": "off", "why": "disabled in the device tree (%s)" % rec["dt_status"]}
    if "device" not in rec:
        return {"level": "warn", "why": "no Linux device for this node (no driver matches its compatible)"}
    if not rec.get("driver"):
        return {"level": "fail", "why": "not bound%s" % ((": " + rec["deferred"]) if rec.get("deferred") else "")}
    if rec.get("last_problem"):
        return {"level": "warn", "why": "bound to %s; kernel error: %s" % (rec["driver"], rec["last_problem"][:160])}
    return {"level": "ok", "why": "bound to %s" % rec["driver"]}


def pci_functions(classes, errs):
    out = []
    base = "/sys/bus/pci/devices"
    for f in sysfs.listdir(base):
        d = base + "/" + f
        real = sysfs.realpath(d)
        cls = sysfs.read(d + "/class") or ""
        drv = sysfs.basename_of_link(d + "/driver")
        kids = [(c, n, dn) for (c, n, r, dn) in classes if r.startswith(real + "/")]
        what = {"0x02": "network", "0x0c": "serial bus (USB/Thunderbolt)", "0x06": "bridge",
                "0x01": "storage", "0x0d": "wireless", "0x08": "system"}.get(cls[:4], "class " + cls)
        if any(c == "bluetooth" for c, _, _ in kids):
            what = "Bluetooth"
        elif any(c == "ieee80211" for c, _, _ in kids):
            what = "Wi-Fi"
        rec = {"node": "pci/" + f, "name": f, "block": "pci", "title": "PCI %s" % what, "kind": "block" if cls[:4] != "0x06" else "infra",
               "compatible": ["pci%s,%s" % ((sysfs.read(d + "/vendor") or "")[2:], (sysfs.read(d + "/device") or "")[2:])],
               "dt_status": "okay", "device": f, "sysfs": real, "driver": drv,
               "class_devices": ["%s/%s" % (c, n) for c, n, _ in kids],
               "class_device_ids": identity.collect(classes, real),
               "dev_nodes": sorted({dn for _, _, dn in kids if dn}),
               "last_problem": _last(errs, [f, drv] if drv else [f])}
        rec["state"] = _state(rec)
        out.append(rec)
    return out


def readings():
    out = []
    for s in subsystems():
        if s["kind"] != "block":
            continue
        out.append({"id": "subsys.%s" % s["name"].replace(".", "_"), "cat": "subsys", "dev": s["name"],
                    "attr": "state", "label": "%s: %s" % (s.get("title") or s["block"], s["state"]["why"]),
                    "value": s["state"]["level"], "unit": None, "raw": s["state"]["level"],
                    "path": s.get("sysfs"), "writable": False})
    return out
