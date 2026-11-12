# SPDX-License-Identifier: MIT
"""`aurora-diag status`: what works on this Mac, on one page.

Four parts, all read only:

- software: distribution, kernel release and build, the fleet tag in the
  kernel release, the kernel package, the boot chain versions the loaders
  publish in /chosen, Mesa and this tool;
- hardware: one row per block (engines, display, radio, audio, sensors,
  storage, power, GPU, Touch ID) with two answers, "reported" (the device
  tree and the kernel expose it: a node, a bound driver, a device) and
  "working" (it is bound with no kernel error and gives its readings or
  devices), plus the block's firmware version where one is published;
- GPU APIs: Vulkan driver, API and conformance version, OpenGL and GLES;
- services: speaker protection, Touch ID, sensors, radio and power daemons.

Rows come from the same category probes as `show` and the dashboard, so
the three never disagree. Nothing here is per machine: a block the device
tree does not have is simply absent.
"""

import os
import platform
import re
import shutil
import subprocess
import time

from . import __version__, sections, subsystems, sysfs
from .probe import timed

SCHEMA = "aurora-diag/status"
SCHEMA_VERSION = 1

# category item level -> the word a person reads
WORKING = {"ok": "works", "warn": "partial", "fail": "broken", "off": "off", "absent": "absent"}
# category -> item keys shown as hardware rows (None: every item)
HW_ROWS = [
    ("engines", None),
    ("gpu", ("drm",)),
    ("display", ("eDP", "backlight", "notch")),
    ("radio", ("Wi-Fi", "Bluetooth")),
    ("audio", ("speakerguardd", "ALSA cards", "amplifier temperatures")),
    ("sensors", None),
    ("storage", ("NVMe",)),
    ("power", ("battery", "external power", "power meters")),
    ("thermal", ("temperature sensors", "fans")),
]
SERVICES = ("fprintd", "aurora-sep", "aurora-touchid-boot", "iio-sensor-proxy", "bluetooth", "NetworkManager",
            "power-profiles-daemon", "tuned-ppd", "tuned", "upower")
TITLES = {k: t for k, t, _ in sections.ENGINE_GROUPS}
FLEET = re.compile(r"(?:^|[-.+_])(fleet\d+[a-z0-9]*)", re.I)


def _run(cmd, timeout=5):
    if not shutil.which(cmd[0]):
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def os_release():
    out = {}
    for line in (sysfs.read("/etc/os-release") or "").splitlines():
        k, _, v = line.partition("=")
        if k in ("NAME", "VERSION", "ID", "VARIANT_ID", "BUILD_ID", "PRETTY_NAME", "IMAGE_VERSION"):
            out[k] = v.strip('"')
    return out


def kernel_package(release):
    """The package that owns the running kernel's modules, if the distro says."""
    for base in ("/lib/modules/", "/usr/lib/modules/"):
        path = base + release
        if not os.path.isdir(path):
            continue
        if shutil.which("rpm"):
            return _run(["rpm", "-qf", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}\\n", path + "/vmlinuz"]) or _run(["rpm", "-qf", path])
        if shutil.which("pacman"):
            return _run(["pacman", "-Qo", "-q", path])
    real = os.path.realpath("/run/booted-system/kernel") if os.path.exists("/run/booted-system/kernel") else None
    return real


def fleet_tag(release, package=None):
    """The fleet kernel tag (fleetNN) in the kernel release or its package
    release, else None. Distro kernels have none."""
    for s in (release, package or ""):
        m = FLEET.search(s or "")
        if m:
            return m.group(1)
    return None


def software(machine, gpu_mesa=None):
    u = platform.uname()
    pkg = kernel_package(u.release)
    osr = os_release()
    fw = machine.firmware()
    chosen = {}
    for p in sysfs.listdir("/proc/device-tree/chosen"):
        if re.search(r"(firmware|fw|iboot).*version|version", p, re.I) and p not in fw:
            chosen[p] = sysfs.dt_value("/proc/device-tree/chosen/" + p)
    return {
        "distro": osr.get("PRETTY_NAME") or osr.get("NAME"),
        "os_release": osr,
        "kernel": {"release": u.release, "build": u.version, "package": pkg, "fleet": fleet_tag(u.release, pkg),
                   "tainted": sysfs.read_int("/proc/sys/kernel/tainted"),
                   "uptime_s": int(float((sysfs.read("/proc/uptime") or "0").split()[0]))},
        "boot_chain": fw,
        "chosen": chosen,
        "mesa": gpu_mesa,
        "aurora_diag": __version__,
    }


def _firmware_by_block(subs, secs):
    """block key -> firmware label, from the DT/kernel log (engines), the
    radio drivers and the NVMe controller."""
    out = {}
    for key, _title, rx in sections.ENGINE_GROUPS:
        for s in subs:
            if re.match(rx, s.get("block") or "") and s.get("driver"):
                fw = subsystems.firmware_short(s)
                if fw:
                    out.setdefault(key, fw)
    radio = (secs.get("radio") or {}).get("facts", {})
    for dev, d in (radio.get("wifi") or {}).items():
        if d.get("firmware"):
            out["Wi-Fi " + dev] = d["firmware"]
    for dev, d in (radio.get("bluetooth") or {}).items():
        if d.get("firmware"):
            out["Bluetooth " + dev] = d["firmware"]
    for dev, d in ((secs.get("storage") or {}).get("facts", {}).get("nvme") or {}).items():
        if d.get("firmware"):
            out["NVMe " + dev] = d["firmware"].rstrip(".")
    for key, d in ((secs.get("display") or {}).get("facts", {}).get("dcps") or {}).items():
        if d.get("firmware"):
            out.setdefault(d.get("label") or key, d["firmware"])
    return out


def _want(key, keys):
    if keys is None:
        return True
    return any(key.startswith(k) for k in keys)


def hardware(secs, subs):
    fw = _firmware_by_block(subs, secs)
    rows = []
    for name, keys in HW_ROWS:
        s = secs.get(name)
        if not s:
            continue
        for i in s["items"]:
            if not _want(i["key"], keys):
                continue
            lvl = i["level"]
            label = i["key"]
            firmware = fw.get(label) or fw.get(label.lower())
            rows.append({"category": name, "key": label, "block": TITLES.get(label, label) if name == "engines" else label,
                         "level": lvl,
                         "reported": lvl not in ("absent",),
                         "working": lvl == "ok",
                         "state": WORKING.get(lvl, lvl),
                         "firmware": firmware, "detail": i["detail"]})
    return rows


def touchid_row():
    from . import services
    t, err = timed(services.touchid, 5)
    if err:
        return {"category": "security", "key": "touchid", "block": "Touch ID", "level": "warn", "reported": False, "working": False,
                "state": "partial", "firmware": None, "detail": "probe " + err}
    fu = t.get("fprint_users")
    node = t.get("sep_bio_node")
    if not node:
        lvl, det = "absent", "no /dev/sep-bio (no SEP biometric driver bound)"
    elif isinstance(fu, str):
        lvl, det = "ok", "/dev/sep-bio present; enrolled prints %s" % fu
    elif fu:
        lvl, det = "ok", "/dev/sep-bio present; %d user(s) enrolled; pam_fprintd in %s" % (
            len(fu), ", ".join(t.get("pam_fprintd_in") or []) or "none")
    else:
        lvl, det = "warn", "/dev/sep-bio present; no fingerprint enrolled"
    return {"category": "security", "key": "touchid", "block": "Touch ID", "level": lvl, "reported": bool(node), "working": lvl == "ok",
            "state": WORKING[lvl], "firmware": None, "detail": det}


def apis(timeout=12):
    from . import gpu
    out = {}
    vk, err = timed(gpu.vulkan, timeout)
    if err or not isinstance(vk, dict):
        out["vulkan"] = {"error": err or "no result"}
    elif vk.get("error"):
        out["vulkan"] = {"error": vk["error"]}
    else:
        devs = [d for d in vk.get("devices", []) if "CPU" not in (d.get("deviceType") or "")]
        out["vulkan"] = {"instance": vk.get("instance_version"),
                         "devices": [{k: d.get(k) for k in ("deviceName", "driverName", "driverInfo", "apiVersion",
                                                           "conformanceVersion")} for d in devs]}
    gl, err = timed(lambda: gpu.gl(tools=False), timeout)
    if err or not isinstance(gl, dict):
        out["gl"] = {"error": err or "no result"}
    else:
        for api in ("gl_core", "gl_compat", "gles"):
            info = gl.get(api)
            if isinstance(info, dict):
                out[api] = {k: info.get(k) for k in ("version", "renderer", "vendor", "path") if info.get(k) is not None}
        if gl.get("error"):
            out["gl_error"] = gl["error"]
    return out


def services_rows():
    from . import services
    rows = []
    sg, err = timed(services.speakerguardd, 6)
    if not err:
        if sg.get("v2") in ("active", "failed"):
            lvl = "fail" if sg["v2"] == "failed" else ("ok" if sg.get("v2_graph") in (None, "pass") else "warn")
            rows.append({"unit": "speaker protection", "level": lvl,
                         "detail": "speakerguardd v2 %s, graph %s" % (sg["v2"], sg.get("v2_graph") or "unreadable (needs root)")})
        else:
            svc = sg.get("service")
            alt = sg.get("speakersafetyd")
            if svc == "active" or alt == "active":
                lvl, det = "ok", "speakerguardd %s, speakersafetyd %s" % (svc, alt)
            elif "failed" in (svc, alt):
                lvl, det = "fail", "speakerguardd %s, speakersafetyd %s" % (svc, alt)
            else:
                lvl, det = "off", "no speaker protection daemon running (speakers stay locked by the kernel)"
            rows.append({"unit": "speaker protection", "level": lvl, "detail": det})
    units, err = timed(services.system_units, 8)
    for u in units or []:
        name = u["unit"].rsplit(".", 1)[0]
        if name not in SERVICES and not (name.startswith("aurora-") and u["active"] == "failed"):
            continue
        if u["active"] == "failed":
            lvl = "fail"
        elif u["active"] == "active":
            lvl = "ok"
        elif u.get("bus_activatable") or u.get("active_sockets") or u.get("enabled") == "static":
            lvl = "ok"
        else:
            lvl = "off"
        state = "%s/%s" % (u["active"], u["sub"])
        if lvl == "ok" and u["active"] != "active":
            state += " (starts on demand)"
        rows.append({"unit": u["unit"], "level": lvl, "detail": state})
    return rows


def build(machine, api=True, timeout=20):
    t0 = time.monotonic()
    names = [n for n, _ in HW_ROWS]
    subs, err = timed(subsystems.subsystems, timeout)
    subs = subs or []
    secs = sections.collect(names, timeout=timeout, subs=subs or None)
    from . import gpu
    mesa, _ = timed(gpu.mesa, 10)
    hw = hardware(secs, subs) + [touchid_row()]
    doc = {
        "schema": SCHEMA, "version": SCHEMA_VERSION,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": {k: v for k, v in machine.as_dict().items() if k != "firmware"},
        "software": software(machine, mesa),
        "hardware": hw,
        "apis": apis() if api else None,
        "services": services_rows(),
        "euid": os.geteuid(),
    }
    counts = {}
    for r in hw:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    doc["counts"] = counts
    doc["elapsed_s"] = round(time.monotonic() - t0, 2)
    return doc


def _line(L, k, v, w=22):
    L.append("  %-*s %s" % (w, k, "-" if v in (None, "", {}) else v))


def text(doc):
    m, sw = doc["machine"], doc["software"]
    k = sw["kernel"]
    L = ["%s  (%s, %s)" % (m.get("model") or "unknown Mac", m.get("board"), m.get("soc")), ""]
    L.append("Software")
    _line(L, "distribution", sw.get("distro"))
    _line(L, "kernel", k["release"])
    _line(L, "kernel build", k["build"])
    _line(L, "kernel package", k.get("package"))
    _line(L, "fleet", k.get("fleet") or "none (not a fleet kernel)")
    _line(L, "tainted", k.get("tainted"))
    for name, v in sorted((sw.get("boot_chain") or {}).items()):
        _line(L, name, v)
    for name, v in sorted((sw.get("chosen") or {}).items()):
        _line(L, name, v)
    mesa = sw.get("mesa") or {}
    pk = mesa.get("packages") if isinstance(mesa, dict) else None
    if pk:
        _line(L, "mesa", ", ".join(sorted({str(v) for v in pk.values()}))[:120])
    _line(L, "aurora-diag", sw.get("aurora_diag"))
    L += ["", "Hardware  (reported: the kernel exposes it; working: bound, no kernel error, readings or devices present)"]
    L.append("  %-8s %-30s %-8s %-8s %-20s %s" % ("state", "block", "reported", "working", "firmware", "detail"))
    for r in doc["hardware"]:
        L.append("  %-8s %-30s %-8s %-8s %-20s %s" % (r["state"].upper(), r["block"][:30], "yes" if r["reported"] else "no",
                                                    "yes" if r["working"] else "no", (r.get("firmware") or "-")[:20],
                                                    str(r["detail"])[:90]))
    a = doc.get("apis")
    if a:
        L += ["", "GPU APIs"]
        vk = a.get("vulkan") or {}
        if vk.get("error"):
            _line(L, "Vulkan", vk["error"])
        for d in vk.get("devices", []):
            _line(L, "Vulkan", "%s, %s %s, API %s, conformance %s" % (d.get("deviceName"), d.get("driverName"),
                                                                      d.get("driverInfo") or "", d.get("apiVersion"),
                                                                      d.get("conformanceVersion") or "-"))
        for api, title in (("gl_core", "OpenGL core"), ("gl_compat", "OpenGL compat"), ("gles", "OpenGL ES")):
            if a.get(api):
                _line(L, title, "%s (%s)" % (a[api].get("version"), a[api].get("renderer")))
        if a.get("gl_error"):
            _line(L, "OpenGL", a["gl_error"])
    L += ["", "Services"]
    for r in doc["services"]:
        L.append("  %-8s %-30s %s" % (r["level"].upper(), r["unit"], r["detail"]))
    c = doc["counts"]
    L += ["", "%d works, %d partial, %d broken, %d off or absent  (%.1f s%s)" % (
        c.get("works", 0), c.get("partial", 0), c.get("broken", 0), c.get("off", 0) + c.get("absent", 0),
        doc["elapsed_s"], "" if doc["euid"] == 0 else "; run as root to see more")]
    return "\n".join(L) + "\n"


def exit_code(doc):
    """0 when nothing reported is broken, 1 for partial blocks, 2 for broken ones."""
    states = {r["state"] for r in doc["hardware"]} | {{"fail": "broken", "warn": "partial"}.get(r["level"], "") for r in doc["services"]}
    return 2 if "broken" in states else (1 if "partial" in states else 0)
