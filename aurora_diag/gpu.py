# SPDX-License-Identifier: MIT
"""The GPU: DRM devices, the kernel driver's own reports, per-process GPU
memory, and what the userspace drivers (Vulkan, OpenGL, OpenGL ES) say.

Everything is discovered. DRM devices come from /sys/class/drm, the
driver's attributes from its device directory (whatever files it has),
its debugfs reports from /sys/kernel/debug/<driver>* and dri/N, per-process
use from /proc/*/fdinfo (the DRM fdinfo keys), Vulkan from vulkaninfo and
OpenGL/GLES from glprobe.py, which talks to EGL directly so it works on a
headless machine with no display server and no extra tools installed.

Nothing here writes to the GPU or draws anything. vulkaninfo creates an
instance and a device and exits; glprobe creates contexts with no surface.
"""

import ctypes
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import sysfs

GLPROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "glprobe.py")
TOOL_TIMEOUT = 60


def _reading(id_, attr, value, unit=None, label=None, path=None, dev=None, writable=False, cat="gpu"):
    return {"id": id_, "cat": cat, "dev": dev or id_.split(".")[1], "attr": attr, "label": label,
            "value": value, "unit": unit, "raw": value if isinstance(value, str) else json.dumps(value),
            "path": path, "writable": writable}


def _num(v):
    try:
        return int(v, 0) if re.fullmatch(r"-?(0x[0-9a-fA-F]+|\d+)", v) else float(v)
    except (TypeError, ValueError):
        return v


def _kv(text):
    """'a b' lines and 'a=b c=d' lines into a dict."""
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            toks = line.split()
            prefix = ""
            if toks and "=" not in toks[0]:
                prefix = toks.pop(0) + "."
            for tok in toks:
                k, sep, v = tok.partition("=")
                if sep and not v.startswith("["):
                    out[prefix + k] = _num(v)
        else:
            f = line.split(None, 1)
            if len(f) == 2:
                out[f[0]] = _num(f[1].strip())
    return out


# -- DRM devices --------------------------------------------------------------

class _DrmVersion(ctypes.Structure):
    _fields_ = [("major", ctypes.c_int), ("minor", ctypes.c_int), ("patchlevel", ctypes.c_int),
                ("name_len", ctypes.c_size_t), ("name", ctypes.c_void_p),
                ("date_len", ctypes.c_size_t), ("date", ctypes.c_void_p),
                ("desc_len", ctypes.c_size_t), ("desc", ctypes.c_void_p)]


def drm_version(node):
    """DRM_IOCTL_VERSION on a device node: the kernel driver's name, version
    and description. None when the node cannot be opened."""
    try:
        fd = os.open(sysfs.root(node), os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        bufs = [ctypes.create_string_buffer(256) for _ in range(3)]
        v = _DrmVersion(0, 0, 0, 255, ctypes.addressof(bufs[0]), 255, ctypes.addressof(bufs[1]),
                        255, ctypes.addressof(bufs[2]))
        req = (3 << 30) | (ctypes.sizeof(_DrmVersion) << 16) | (ord("d") << 8) | 0x00
        fcntl.ioctl(fd, req, v, True)
        return {"name": bufs[0].value.decode(errors="replace"), "date": bufs[1].value.decode(errors="replace"),
                "desc": bufs[2].value.decode(errors="replace"),
                "version": "%d.%d.%d" % (v.major, v.minor, v.patchlevel)}
    except OSError:
        return None
    finally:
        os.close(fd)


def drm_devices():
    """{device sysfs path: {driver, nodes, ...}} for every DRM device."""
    devs = {}
    base = "/sys/class/drm"
    for n in sysfs.listdir(base):
        if "-" in n or not (n.startswith("card") or n.startswith("renderD")):
            continue
        dev = sysfs.realpath(base + "/" + n + "/device")
        d = devs.setdefault(dev, {"device": os.path.basename(dev), "path": dev, "nodes": [],
                                  "driver": sysfs.basename_of_link(base + "/" + n + "/device/driver")})
        d["nodes"].append("/dev/dri/" + n)
        if n.startswith("renderD"):
            d["render_node"] = "/dev/dri/" + n
        else:
            d["primary_node"] = "/dev/dri/" + n
            d["minor"] = n[4:]
    for d in devs.values():
        d["connectors"] = [c.split("-", 1)[1] for c in sysfs.listdir(base)
                           if "-" in c and c.startswith("card%s-" % d.get("minor", "?"))]
        d["has_render"] = "render_node" in d
        node = d.get("render_node") or d.get("primary_node")
        ver = drm_version(node) if node else None
        if ver:
            d["kernel_driver"] = ver
    return devs


def gpus():
    """DRM devices with a render node: the ones that execute work."""
    return [d for d in drm_devices().values() if d["has_render"]]


# Driver attributes in the device directory that are not the driver's own.
_GENERIC = {"driver", "driver_override", "modalias", "uevent", "of_node", "subsystem", "power",
            "drm", "waiting_for_supplier", "consumers", "suppliers"}


def driver_attrs(dev):
    """{name: (path, text)} for every readable file the driver added to its
    device directory (agx_stats, gpu_memory, ... on the AGX driver)."""
    out = {}
    for f in sysfs.listdir(dev):
        p = dev + "/" + f
        if f in _GENERIC or f.startswith("supplier:") or f.startswith("consumer:") or sysfs.isdir(p):
            continue
        out[f] = (p, sysfs.read_timeout(p, timeout=1.0))
    return out


def debugfs_reports(dev):
    """{name: (path, text)} of the driver's debugfs reports (root only):
    /sys/kernel/debug/<driver>*/ and /sys/kernel/debug/dri/<minor>/name."""
    out = {}
    base = "/sys/kernel/debug"
    drv = dev.get("driver") or ""
    if not drv or not os.access(sysfs.root(base), os.R_OK | os.X_OK):
        return out
    for e in sysfs.listdir(base):
        if e.startswith(drv) and sysfs.isdir(base + "/" + e):
            for f in sysfs.listdir(base + "/" + e):
                p = base + "/" + e + "/" + f
                if not sysfs.isdir(p):
                    t = sysfs.read_timeout(p, timeout=2.0, limit=32768)
                    if t is not None:
                        out["%s/%s" % (e, f)] = (p, t)
    if "minor" in dev:
        p = "%s/dri/%s/name" % (base, dev["minor"])
        t = sysfs.read(p)
        if t is not None:
            out["dri/%s/name" % dev["minor"]] = (p, t)
    return out


# -- utilisation ----------------------------------------------------------------

_busy = {}


def utilisation(dev, stats):
    """Busy percentage from the driver's busy_ns counter over the time since
    the last call (or over a 200 ms window on the first call)."""
    if "busy_ns" not in stats or not isinstance(stats["busy_ns"], int):
        return None
    key = dev["path"]
    now = time.monotonic_ns()
    prev = _busy.get(key)
    if prev is None:
        time.sleep(0.2)
        again = _kv(sysfs.read(dev["path"] + "/agx_stats"))
        prev = (now, stats["busy_ns"])
        now, busy = time.monotonic_ns(), again.get("busy_ns", stats["busy_ns"])
    else:
        busy = stats["busy_ns"]
    _busy[key] = (now, busy)
    dt = now - prev[0]
    if dt <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (busy - prev[1]) / dt)), 1)


def _dt_bytes(path):
    try:
        with open(sysfs.root(path), "rb") as f:
            return f.read(4096)
    except OSError:
        return None


def opp_table_hz(dev):
    """The GPU's operating points from its device-tree node, lowest index
    first: [opp-hz of each child of the table its operating-points-v2
    phandle names]. Empty when the node has no table."""
    of = sysfs.realpath(dev + "/of_node")
    ph = _dt_bytes(of + "/operating-points-v2") if of else None
    if not ph or len(ph) < 4:
        return []
    want = ph[:4]
    base = "/sys/firmware/devicetree/base"
    for parent in (base, base + "/soc"):
        for n in sorted(sysfs.listdir(parent)):
            node = parent + "/" + n
            if not sysfs.isdir(node) or _dt_bytes(node + "/phandle") != want:
                continue
            out = []
            for e in sorted(sysfs.listdir(node)):
                hz = _dt_bytes(node + "/" + e + "/opp-hz")
                if hz and len(hz) in (4, 8):
                    out.append(int.from_bytes(hz, "big"))
            return out
    return []


# -- per-process GPU use ----------------------------------------------------------

def fdinfo_clients():
    """Every DRM client in /proc/*/fdinfo, once per client id."""
    clients = {}
    for pid in sysfs.listdir("/proc"):
        if not pid.isdigit():
            continue
        fdi = "/proc/%s/fdinfo" % pid
        for fd in sysfs.listdir(fdi):
            text = sysfs.read(fdi + "/" + fd, limit=8192)
            if not text or "drm-driver:" not in text:
                continue
            kv = {}
            for line in text.splitlines():
                k, _, v = line.partition(":")
                if k.startswith("drm-"):
                    kv[k] = v.strip()
            key = (kv.get("drm-pdev") or kv.get("drm-driver"), kv.get("drm-client-id"), pid)
            if key in clients:
                continue
            kv["pid"] = int(pid)
            kv["comm"] = sysfs.read("/proc/%s/comm" % pid)
            clients[key] = kv
    return list(clients.values())


def _kib(v):
    m = re.match(r"(\d+)\s*(\w*)", v or "")
    if not m:
        return None
    n, u = int(m.group(1)), m.group(2).lower()
    return n * {"": 1 / 1024.0, "kib": 1, "mib": 1024, "gib": 1024 * 1024}.get(u, 1)


def per_process():
    """GPU memory and engine time per process, summed over its clients."""
    procs = {}
    for c in fdinfo_clients():
        p = procs.setdefault(c["pid"], {"pid": c["pid"], "comm": c["comm"], "driver": c.get("drm-driver"),
                                        "clients": 0, "total_KiB": 0, "resident_KiB": 0, "engines_ns": {}})
        p["clients"] += 1
        for k, v in c.items():
            if k.startswith("drm-total-"):
                p["total_KiB"] += _kib(v) or 0
            elif k.startswith("drm-resident-"):
                p["resident_KiB"] += _kib(v) or 0
            elif k.startswith("drm-engine-") and not k.startswith("drm-engine-capacity"):
                n = re.match(r"(\d+)", v)
                if n:
                    e = k[len("drm-engine-"):]
                    p["engines_ns"][e] = p["engines_ns"].get(e, 0) + int(n.group(1))
    return sorted(procs.values(), key=lambda p: -p["resident_KiB"])


# -- temperatures -------------------------------------------------------------------

def gpu_temps():
    """hwmon temperature channels whose label names the GPU."""
    out = {}
    base = "/sys/class/hwmon"
    for h in sysfs.listdir(base):
        d = base + "/" + h
        for f in sysfs.listdir(d):
            m = re.fullmatch(r"temp(\d+)_label", f)
            if not m:
                continue
            lab = sysfs.read(d + "/" + f) or ""
            if re.search(r"\bgpu\b", lab, re.I):
                v = sysfs.read_int(d + "/temp%s_input" % m.group(1))
                if v is not None:
                    out[lab] = v / 1000.0
    return out


# -- live readings (list, watch, get) ----------------------------------------------------

def gpu_names():
    """Stable short names: the driver, numbered when several GPUs share it."""
    gs = gpus()
    out, seen = [], {}
    for d in gs:
        drv = d["driver"] or "gpu"
        n = sum(1 for x in gs if x["driver"] == d["driver"])
        seen[drv] = seen.get(drv, -1) + 1
        out.append((drv if n == 1 else "%s%d" % (drv, seen[drv]), d))
    return out


def readings(live_only=False):
    out = []
    for name, dev in gpu_names():
        pre = "gpu.%s" % name
        out.append(_reading(pre + ".driver", "driver", dev["driver"], path=dev["path"], dev=name))
        out.append(_reading(pre + ".nodes", "nodes", " ".join(dev["nodes"]), path=dev["path"], dev=name))
        kd = dev.get("kernel_driver")
        if kd:
            out.append(_reading(pre + ".kernel_driver", "kernel_driver",
                                "%s %s (%s)" % (kd["name"], kd["version"], kd["desc"]), dev=name))
        rt = dev["path"] + "/power/runtime_status"
        out.append(_reading(pre + ".runtime_status", "runtime_status", sysfs.read(rt), path=rt, dev=name))
        for f, (p, text) in sorted(driver_attrs(dev["path"]).items()):
            kv = _kv(text) if text and ("\n" in text or "=" in text or " " in text) else None
            if kv:
                for k, v in kv.items():
                    out.append(_reading("%s.%s.%s" % (pre, f, k), "%s/%s" % (f, k), v, path=p, dev=name))
                if f == "agx_stats":
                    u = utilisation(dev, kv)
                    if u is not None:
                        out.append(_reading(pre + ".util", "util", u, "%", "busy over the sample window", p, name))
                    elif kv.get("utilisation_status") == "valid" and isinstance(kv.get("util_device_percent"), (int, float)):
                        out.append(_reading(pre + ".util", "util", kv["util_device_percent"], "%",
                                            "firmware device utilisation", p, name))
                    elif kv.get("utilisation_status") in ("stale", "unavailable"):
                        out.append(_reading(pre + ".util_idle", "util_idle", kv["utilisation_status"], None,
                                            "no recent firmware utilisation sample (the GPU is idle)", p, name))
                    ps = kv.get("pstate")
                    if isinstance(ps, int) and not isinstance(kv.get("achieved_frequency_hz"), (int, float)):
                        opps = opp_table_hz(dev["path"])
                        if 0 <= ps < len(opps):
                            out.append(_reading(pre + ".pstate_hz", "pstate_hz", opps[ps], "Hz",
                                                "pstate %d in the device-tree operating-point table" % ps, p, name))
            else:
                out.append(_reading("%s.%s" % (pre, f), f, _num(text) if text is not None else "unreadable",
                                    path=p, dev=name, writable=sysfs.writable(p)))
        for k, (p, text) in sorted(debugfs_reports(dev).items()):
            if live_only and not k.endswith("/power"):
                continue
            first = {}
            for line in (text or "").splitlines()[:40]:
                if line.startswith("role=") or "words=" in line:
                    continue
                first.update(_kv(line))
            for kk, v in first.items():
                out.append(_reading("%s.debugfs.%s.%s" % (pre, k.replace("/", "."), kk), kk, v, path=p, dev=name))
        for lab, t in gpu_temps().items():
            out.append(_reading("%s.temp.%s" % (pre, lab.replace(" ", "_")), "temp", t, "C", lab, dev=name))
        if not live_only:
            for p in per_process():
                if p["driver"] != dev["driver"]:
                    continue
                out.append(_reading("%s.client.%s-%d" % (pre, p["comm"], p["pid"]), "client",
                                    round(p["resident_KiB"] / 1024.0, 1), "MiB",
                                    "%d clients%s" % (p["clients"], (" engines " + json.dumps(p["engines_ns"])) if p["engines_ns"] else ""),
                                    dev=name))
    return out


# -- userspace drivers -----------------------------------------------------------------

def _env():
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR"):
        # One private directory per user, reused, so repeated runs leave
        # nothing behind.
        d = os.path.join(tempfile.gettempdir(), "aurora-diag-xdg-%d" % os.getuid())
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            env["XDG_RUNTIME_DIR"] = d
        except OSError:
            pass
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    return env


def _run(cmd, env=None, cwd=None, timeout=TOOL_TIMEOUT):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return None, "", str(e)


def _parse_summary(text):
    devs = []
    cur = None
    in_devices = False
    for line in text.splitlines():
        if line.startswith("Devices:"):
            in_devices = True
            continue
        if not in_devices:
            m = re.match(r"Vulkan Instance Version:\s*(\S+)", line)
            if m:
                devs.append({"_instance": m.group(1)})
            continue
        m = re.match(r"GPU(\d+):", line.strip())
        if m:
            cur = {"index": int(m.group(1))}
            devs.append(cur)
            continue
        m = re.match(r"\s+(\w+)\s*=\s*(.*)$", line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2).strip()
    inst = next((d["_instance"] for d in devs if "_instance" in d), None)
    return inst, [d for d in devs if "_instance" not in d]


def _parse_heaps(text):
    """memoryHeaps of each device from vulkaninfo's text output."""
    per_dev = []
    for block in re.split(r"\nVkPhysicalDeviceMemoryProperties:\n", text)[1:]:
        heaps = []
        for m in re.finditer(r"memoryHeaps\[(\d+)\]:\s*\n\s*size\s*=\s*(\d+).*\n(?:\s*budget\s*=\s*(\d+).*\n)?(?:\s*usage\s*=\s*(\d+).*\n)?\s*flags:[^\n]*\n((?:\t{3,}\S+\n)*)", block):
            heaps.append({"index": int(m.group(1)), "size": int(m.group(2)),
                          "budget": int(m.group(3)) if m.group(3) else None,
                          "usage": int(m.group(4)) if m.group(4) else None,
                          "flags": m.group(5).split()})
        types = len(re.findall(r"memoryTypes\[\d+\]:", block.split("VkPhysicalDevice", 1)[0]))
        per_dev.append({"heaps": heaps, "type_count": types})
    return per_dev


def _count_features(feats):
    on = total = 0
    for struct in feats.values():
        if isinstance(struct, dict):
            for v in struct.values():
                if isinstance(v, bool):
                    total += 1
                    on += v
    return on, total


def vulkan():
    """Vulkan instance and every physical device, from vulkaninfo."""
    if not shutil.which("vulkaninfo"):
        return {"error": "vulkaninfo is not installed (vulkan-tools)"}
    env = _env()
    rc, out, err = _run(["vulkaninfo", "--summary"], env=env)
    if rc is None or not out:
        return {"error": "vulkaninfo --summary failed: %s" % (err.strip()[-300:])}
    inst, devs = _parse_summary(out)
    res = {"instance_version": inst, "devices": [],
           "loader_warnings": sorted({l.strip() for l in err.splitlines() if "Loader Message" in l})[:10]}
    rc, full, _ = _run(["vulkaninfo"], env=env)
    heaps = _parse_heaps(full or "")
    for i, d in enumerate(devs):
        dev = {k: d.get(k) for k in ("deviceName", "deviceType", "apiVersion", "driverVersion", "vendorID",
                                     "deviceID", "driverID", "driverName", "driverInfo", "conformanceVersion",
                                     "deviceUUID", "driverUUID")}
        dev["index"] = d["index"]
        if i < len(heaps):
            dev["memory_heaps"] = heaps[i]["heaps"]
            dev["memory_type_count"] = heaps[i]["type_count"]
        if "CPU" not in (d.get("deviceType") or ""):
            with tempfile.TemporaryDirectory(prefix="aurora-diag-vk-") as tmp:
                _run(["vulkaninfo", "--json=%d" % d["index"]], env=env, cwd=tmp)
                files = glob.glob(os.path.join(tmp, "*.json"))
                if files:
                    try:
                        with open(files[0]) as f:
                            prof = json.load(f)
                        dev.update(_profile_summary(prof))
                    except (OSError, ValueError) as e:
                        dev["profile_error"] = str(e)
        res["devices"].append(dev)
    return res


def _profile_summary(prof):
    cap = prof.get("capabilities", {}).get("device", {})
    props = cap.get("properties", {})
    core = props.get("VkPhysicalDeviceProperties", {})
    feats = cap.get("features", {})
    on, total = _count_features(feats)
    values = {}
    for struct in feats.values():
        if isinstance(struct, dict):
            for k, v in struct.items():
                if isinstance(v, bool):
                    values[k] = values.get(k, False) or v
    exts = cap.get("extensions", {})
    v12 = props.get("VkPhysicalDeviceVulkan12Properties", {})
    out = {
        "extension_count": len(exts),
        "extensions": sorted(exts),
        "features_enabled": on,
        "features_total": total,
        "feature_structs": len(feats),
        "core_features_disabled": sorted(k for k, v in feats.get("VkPhysicalDeviceFeatures", {}).items() if v is False),
        "limits": core.get("limits", {}),
        "sparse": core.get("sparseProperties", {}),
        "queue_families": [q.get("VkQueueFamilyProperties", q) for q in cap.get("queueFamiliesProperties", [])],
        "format_count": len(cap.get("formats", {})),
        "feature_values": values,
    }
    if v12:
        cv = v12.get("conformanceVersion")
        if isinstance(cv, dict):
            cv = "%s.%s.%s.%s" % (cv.get("major"), cv.get("minor"), cv.get("subminor"), cv.get("patch"))
        out["conformance"] = cv
    drm = props.get("VkPhysicalDeviceDrmPropertiesEXT")
    if drm:
        out["drm"] = drm
    return out


def gl(tools=True):
    """OpenGL, OpenGL ES and EGL through glprobe.py (headless EGL)."""
    env = _env()
    rc, out, err = _run([sys.executable, "-I", GLPROBE], env=env)
    res = None
    if out.strip():
        try:
            res = json.loads(out.strip().splitlines()[-1])
        except ValueError:
            res = None
    if res is None:
        res = {"error": "glprobe failed (rc %s): %s" % (rc, err.strip()[-300:])}
    if rc not in (0, None) and "error" not in res:
        res["error"] = "glprobe rc %s" % rc
    # A display server's view, when there is one and the tools exist.
    for tool, args in ((("eglinfo", ["-B"]), ("glxinfo", ["-B"])) if tools else ()):
        if shutil.which(tool) and (tool == "eglinfo" or os.environ.get("DISPLAY")):
            rc2, o2, _ = _run([tool] + args, timeout=30)
            if rc2 == 0 and o2:
                res.setdefault("tools", {})[tool] = o2[:8192]
    res["zink_forced"] = zink_forced()
    return res


def zink_forced():
    """Where the environment forces Zink (or another Mesa driver)."""
    out = {}
    for k in ("MESA_LOADER_DRIVER_OVERRIDE", "GALLIUM_DRIVER", "LIBGL_ALWAYS_SOFTWARE"):
        if os.environ.get(k):
            out["env:" + k] = os.environ[k]
    for pat in ("/etc/environment", "/etc/environment.d/*.conf", "/usr/lib/environment.d/*.conf",
                "/etc/gdm/*.conf", "/usr/share/gdm/env.d/*", "/etc/profile.d/*.sh"):
        for f in glob.glob(sysfs.root(pat)):
            text = sysfs.read(sysfs.unroot(f)) or ""
            for line in text.splitlines():
                if re.search(r"MESA_LOADER_DRIVER_OVERRIDE|GALLIUM_DRIVER", line) and not line.lstrip().startswith("#"):
                    out[sysfs.unroot(f)] = line.strip()
    return out


def mesa():
    """Installed Mesa: the Vulkan ICDs and the packages that own them."""
    icds = []
    for d in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d", "/run/opengl-driver/share/vulkan/icd.d"):
        for f in sorted(glob.glob(sysfs.root(d) + "/*.json")):
            try:
                with open(f) as fh:
                    j = json.load(fh)
            except (OSError, ValueError):
                continue
            icd = j.get("ICD", {})
            icds.append({"file": sysfs.unroot(f), "library": icd.get("library_path"), "api": icd.get("api_version")})
    pkgs = {}
    owner_of = [i["library"] for i in icds if i.get("library", "").startswith("/")]
    for lib in glob.glob("/usr/lib*/libEGL_mesa.so*")[:1] + glob.glob("/usr/lib*/dri/*_dri.so")[:1]:
        owner_of.append(lib)
    for lib in owner_of:
        if shutil.which("rpm"):
            rc, o, _ = _run(["rpm", "-qf", lib], timeout=10)
        elif shutil.which("pacman"):
            rc, o, _ = _run(["pacman", "-Qqo", lib], timeout=10)
        else:
            rc, o = None, ""
        if rc == 0 and o.strip():
            pkgs[lib] = o.strip()
    if not owner_of and os.path.isdir("/run/opengl-driver"):
        pkgs["/run/opengl-driver"] = os.path.realpath("/run/opengl-driver")
    return {"vulkan_icds": icds, "packages": pkgs}


def api_readings(vk=None, gli=None):
    """Vulkan and GL facts as flat readings for list --json."""
    out = []
    vk = vk if vk is not None else vulkan()
    if "error" in vk:
        out.append(_reading("vulkan.error", "error", vk["error"], cat="vulkan", dev="vulkan"))
    else:
        out.append(_reading("vulkan.instance_version", "instance_version", vk["instance_version"], cat="vulkan", dev="vulkan"))
        for d in vk["devices"]:
            pre = "vulkan.gpu%d" % d["index"]
            for k, v in d.items():
                if k in ("index", "feature_values"):
                    continue
                if k == "limits":
                    for lk, lv in v.items():
                        out.append(_reading("%s.limits.%s" % (pre, lk), lk, lv, cat="vulkan", dev="gpu%d" % d["index"]))
                    continue
                out.append(_reading("%s.%s" % (pre, k), k, v, cat="vulkan", dev="gpu%d" % d["index"]))
    gli = gli if gli is not None else gl()
    if gli.get("error"):
        out.append(_reading("gl.error", "error", gli["error"], cat="gl", dev="gl"))
    for k, v in (gli.get("egl") or {}).items():
        out.append(_reading("gl.egl.%s" % k, k, v, cat="gl", dev="egl"))
    for api in ("gl_core", "gl_compat", "gles"):
        info = gli.get(api)
        if not info:
            out.append(_reading("gl.%s" % api, api, "no context", cat="gl", dev=api))
            continue
        for k, v in info.items():
            if k == "limits":
                for lk, lv in v.items():
                    out.append(_reading("gl.%s.limits.%s" % (api, lk), lk, lv, cat="gl", dev=api))
                continue
            out.append(_reading("gl.%s.%s" % (api, k), k, v, cat="gl", dev=api))
    out.append(_reading("gl.zink_forced", "zink_forced", gli.get("zink_forced") or "no", cat="gl", dev="gl"))
    return out
