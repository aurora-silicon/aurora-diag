# SPDX-License-Identifier: MIT
"""One report to attach to a bug: a JSON document plus readable text.

machine and boot chain, kernel, GPU (kernel driver, Mesa, Vulkan, OpenGL
and GLES), the headline sensors, speaker protection, fans, haptics, which
platform drivers are bound, and the kernel's recent errors and warnings.
"""

import json
import os
import platform
import shutil
import socket
import subprocess
import time

from . import __version__, ane, audio, clocks, compliance, fans, gpu, haptics, sources, sysfs
from .probe import timed


def _dmesg():
    if not shutil.which("dmesg"):
        return None
    try:
        r = subprocess.run(["dmesg", "--level=emerg,alert,crit,err,warn", "--notime"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return {"error": (r.stderr.strip() or "dmesg failed")[:200]}
    lines = r.stdout.splitlines()
    return {"count": len(lines), "last": lines[-40:]}


def kernel():
    u = platform.uname()
    return {"release": u.release, "version": u.version, "machine": u.machine,
            "cmdline": sysfs.read("/proc/cmdline"), "tainted": sysfs.read_int("/proc/sys/kernel/tainted"),
            "os_release": _os_release()}


def _os_release():
    out = {}
    for line in (sysfs.read("/etc/os-release") or "").splitlines():
        k, _, v = line.partition("=")
        if k in ("NAME", "VERSION", "ID", "VARIANT_ID", "BUILD_ID", "PRETTY_NAME"):
            out[k] = v.strip('"')
    return out


def build(machine, summary_fn):
    t0 = time.monotonic()
    fast = sources.collect(list(sources.FAST_SOURCES) + ["driver"], [audio.speaker_protection])
    gpus = []
    for name, d in gpu.gpu_names():
        g = dict(d)
        g["name"] = name
        g["readings"] = {r["id"]: r["value"] for r in fast if r["cat"] == "gpu" and r["dev"] == name}
        gpus.append(g)
    rep = {
        "tool": "aurora-diag", "version": __version__,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": socket.gethostname(), "euid": os.geteuid(),
        "machine": machine.as_dict(),
        "kernel": kernel(),
        "gpu": {"devices": gpus, "processes": gpu.per_process(), "temps": gpu.gpu_temps()},
        "mesa": gpu.mesa(),
        "vulkan": gpu.vulkan(),
        "gl": gpu.gl(),
        "summary": summary_fn(fast, machine),
        "clocks": clocks.all_clocks(),
        "fans": fans.fans(),
        "haptics": haptics.status(machine),
        "drivers": {r["dev"]: r["label"] for r in fast if r["cat"] == "driver"},
        "dmesg": _dmesg(),
    }
    an, err = timed(ane.collect, 8)
    rep["ane"] = an if err is None else {"error": err}
    rep["compliance"] = compliance.summary(rep["vulkan"], rep["gl"])
    for d in rep["vulkan"].get("devices", []):
        d.pop("feature_values", None)
    rep["elapsed_s"] = round(time.monotonic() - t0, 2)
    return rep


def _w(lines, k, v, ind=2):
    lines.append("%s%-28s %s" % (" " * ind, k, v))


def text(rep):
    L = []
    m = rep["machine"]
    L.append("aurora-diag report %s  %s  %s" % (rep["version"], rep["host"], rep["generated"]))
    L.append("")
    L.append("Machine")
    _w(L, "model", m["model"])
    _w(L, "compatible", " ".join(m["compatible"]))
    for k, v in sorted(m["firmware"].items()):
        _w(L, k, v)
    k = rep["kernel"]
    L.append("Kernel")
    _w(L, "release", k["release"])
    _w(L, "version", k["version"])
    _w(L, "cmdline", k["cmdline"])
    _w(L, "tainted", k["tainted"])
    _w(L, "os", k["os_release"].get("PRETTY_NAME"))
    L.append("ANE")
    an = rep.get("ane") or {}
    if an.get("error"):
        _w(L, "probe", an["error"])
    elif not an.get("devices"):
        _w(L, "devices", "no ANE accelerator device exposed")
    for n in an.get("dt_nodes", []):
        _w(L, n["path"], "status %s, driver %s, generation %s, compatible %s" %
           (n["status"], n["driver"] or "unbound", n["generation"] or "unknown", ", ".join(n["compatible"])))
    for d in an.get("devices", []):
        _w(L, d["node"], "%s, %s" % (d["driver"], d["generation"] or "unknown"))
        for key in ("dt_node", "dt_status", "compatible", "firmware_version", "firmware_source", "client_count", "clients_complete", "performance_level", "memory_vote", "performance_error",
                    "power_state", "wedged", "error_counters", "counters", "last_benchmark"):
            _w(L, key, d[key], 4)
    if an.get("benchmark_error"):
        _w(L, "last benchmark", an["benchmark_error"])
    if not an.get("error"):
        for c in ane.checks(an):
            if c["level"] != "pass":
                _w(L, c["level"], c["reason"])
    L.append("GPU (kernel)")
    for g in rep["gpu"]["devices"]:
        kd = g.get("kernel_driver") or {}
        _w(L, g["device"], "%s %s %s, nodes %s" % (g["driver"], kd.get("version", ""), kd.get("desc", ""), " ".join(g["nodes"])))
        for rk, rv in g["readings"].items():
            short = rk.split(".", 2)[2] if rk.count(".") >= 2 else rk
            _w(L, short, rv, 4)
    for p in rep["gpu"]["processes"][:12]:
        _w(L, "client %s[%d]" % (p["comm"], p["pid"]), "%.1f MiB resident, %d clients" % (p["resident_KiB"] / 1024.0, p["clients"]), 4)
    L.append("Mesa")
    for icd in rep["mesa"]["vulkan_icds"]:
        _w(L, os.path.basename(icd["file"]), "%s (api %s)" % (icd["library"], icd["api"]))
    for pkg in sorted(set(rep["mesa"]["packages"].values())):
        _w(L, "package", pkg)
    vk = rep["vulkan"]
    L.append("Vulkan")
    if "error" in vk:
        _w(L, "error", vk["error"])
    else:
        _w(L, "instance", vk["instance_version"])
        for d in vk["devices"]:
            L.append("  GPU%d %s" % (d["index"], d.get("deviceName")))
            for key in ("deviceType", "apiVersion", "driverVersion", "driverID", "driverName", "driverInfo",
                        "conformanceVersion", "extension_count", "features_enabled", "features_total", "format_count"):
                if d.get(key) is not None:
                    _w(L, key, d[key], 4)
            if d.get("core_features_disabled"):
                _w(L, "core features off", ", ".join(d["core_features_disabled"]), 4)
            for h in d.get("memory_heaps", []):
                _w(L, "heap%d" % h["index"], "%.2f GiB (budget %s) %s" % (h["size"] / 2**30,
                   "%.2f GiB" % (h["budget"] / 2**30) if h.get("budget") else "?", " ".join(h["flags"])), 4)
            lim = d.get("limits", {})
            for key in ("maxImageDimension2D", "maxComputeWorkGroupInvocations", "maxComputeSharedMemorySize",
                        "maxBoundDescriptorSets", "maxPushConstantsSize", "maxMemoryAllocationCount",
                        "maxSamplerAnisotropy", "maxViewports", "maxColorAttachments", "timestampPeriod"):
                if key in lim:
                    _w(L, key, lim[key], 4)
    gl = rep["gl"]
    L.append("OpenGL / GLES (EGL %s, platform %s)" % ((gl.get("egl") or {}).get("version"), gl.get("platform")))
    if gl.get("error"):
        _w(L, "error", gl["error"])
    for api in ("gl_core", "gl_compat", "gles"):
        info = gl.get(api)
        if not info:
            _w(L, api, "no context")
            continue
        _w(L, api, "%s | %s | GLSL %s | %s profile | %d extensions | %s" % (
            info["renderer"], info["version"], info["glsl_version"], info["profile"],
            info["extension_count"], info["path"]))
        lim = info.get("limits", {})
        _w(L, "", ", ".join("%s %s" % (k2.replace("MAX_", ""), lim[k2]) for k2 in (
            "MAX_TEXTURE_SIZE", "MAX_SAMPLES", "MAX_COLOR_ATTACHMENTS", "MAX_COMPUTE_WORK_GROUP_INVOCATIONS",
            "MAX_TEXTURE_MAX_ANISOTROPY") if k2 in lim), 4)
    _w(L, "zink forced", gl.get("zink_forced") or "no")
    L.append("Compliance (spot checks, see docs/COMPLIANCE.md)")
    for k, v in rep.get("compliance", {}).items():
        _w(L, k, "%s, %d checked, missing: %s%s" % (
            v.get("api") or v.get("claimed"), v.get("required_checked", v.get("checked", 0)),
            ", ".join(v.get("missing") or []) or "none",
            (", conformance %s%s" % (v["conformance"], "" if v.get("conformance_submitted") else " (not submitted)")) if "conformance" in v else ""))
    L.append("Clocks")
    for p in rep["clocks"]["cpu"]:
        _w(L, "cpu " + p["policy"], "%s MHz now, %s-%s, %d-step ladder, %s" % (p["cur_MHz"], p["hw_min_MHz"], p["hw_max_MHz"], len(p["ladder_MHz"]), p["governor"]))
    for name, t in sorted(rep["clocks"]["dt_ladders"].items()):
        _w(L, name, " ".join("%.0f" % o["MHz"] for o in t["opps"] if o.get("MHz") is not None) + " MHz")
    m = rep["clocks"]["memory"]
    _w(L, "memory", "%.1f GiB, speed %s" % ((m["size_bytes"] or 0) / 2**30, m["speed"]))
    L.append("Sensors")
    s = rep["summary"]
    for key in ("lid_angle_deg", "ambient_lux", "hottest_die", "backlight", "battery", "power_W", "speaker_protection"):
        if key in s:
            _w(L, key, json.dumps(s[key], default=str))
    _w(L, "fans", ", ".join("%s %s rpm" % (f["label"], f["rpm"]) for f in rep["fans"]) or "none")
    h = rep["haptics"]
    _w(L, "haptics", ("press %s release %s (%s)" % (h.get("press"), h.get("release"), h.get("name"))) if h.get("supported") else h.get("reason"))
    temps = s.get("temperatures", {})
    _w(L, "temperatures", "%d channels" % len(temps))
    L.append("Drivers")
    for d, v in sorted(rep["drivers"].items()):
        _w(L, d, v)
    dm = rep["dmesg"]
    L.append("Kernel errors and warnings")
    if not dm:
        _w(L, "dmesg", "unavailable")
    elif "error" in dm:
        _w(L, "dmesg", dm["error"])
    else:
        _w(L, "count", dm["count"])
        for line in dm["last"]:
            L.append("    " + line[:200])
    L.append("")
    L.append("generated in %.1f s" % rep["elapsed_s"])
    return "\n".join(str(x) for x in L) + "\n"


def write(rep, outdir):
    os.makedirs(outdir, exist_ok=True)
    stem = os.path.join(outdir, "aurora-diag-report-%s-%s" % (rep["host"], rep["generated"].replace(":", "")))
    with open(stem + ".json", "w") as f:
        json.dump(rep, f, indent=1, default=str)
    with open(stem + ".txt", "w") as f:
        f.write(text(rep))
    return stem + ".json", stem + ".txt"
