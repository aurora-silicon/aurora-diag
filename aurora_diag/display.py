# SPDX-License-Identifier: MIT
"""Display bits: the notch, brightness, connector reprobe, Mutter power save.

The notch: appledrm reads show_notch when the DCP probes and keeps the
notch height it decided then (dcp->notch_height feeds the mode list and the
swap offsets). Writing /sys/module/appledrm/parameters/show_notch at runtime
therefore changes nothing until the next probe, which in practice is the
next boot: unbinding a DCP is not an option (both DCPs are components of
one DRM device, and the DCP must be left running by the boot loader to
probe). `display notch` writes the parameter, asks DRM to reprobe the
connector and then reports whether the mode list actually changed, so the
log says plainly whether it took effect live.
"""

import os
import re
import shutil
import subprocess

from . import control, log, sysfs

NOTCH_PARAM = "/sys/module/appledrm/parameters/show_notch"


def connectors():
    out = []
    base = "/sys/class/drm"
    for c in sysfs.listdir(base):
        if c.startswith("card") and "-" in c:
            d = base + "/" + c
            out.append({"name": c, "status": sysfs.read(d + "/status"),
                        "enabled": sysfs.read(d + "/enabled"), "dpms": sysfs.read(d + "/dpms"),
                        "modes": (sysfs.read(d + "/modes") or "").splitlines()})
    return out


def internal():
    return next((c for c in connectors() if "eDP" in c["name"]), None)


def reprobe(name):
    """Ask DRM to detect a connector again (writes 'detect' to its status;
    that clears any forced state rather than setting one)."""
    path = "/sys/class/drm/%s/status" % name
    if not sysfs.exists(path):
        raise control.Refused("no connector %s" % name)
    sysfs.write(path, "detect")
    log.event("action", what="drm reprobe", connector=name)


def notch_status():
    c = internal()
    return {"param": sysfs.read(NOTCH_PARAM), "param_writable": sysfs.writable(NOTCH_PARAM),
            "connector": c["name"] if c else None, "modes": c["modes"][:3] if c else [],
            "live": False,
            "note": "appledrm latches show_notch at DCP probe: a change takes effect at the next boot"}


def set_notch(machine, on, allow_unknown=False):
    before = internal()
    res = control.apply_sysfs(NOTCH_PARAM, "Y" if on else "N", machine, allow_unknown)
    if before:
        try:
            reprobe(before["name"])
        except (OSError, control.Refused) as e:
            res["reprobe_error"] = str(e)
    after = internal()
    live = bool(before and after and before["modes"][:1] != after["modes"][:1])
    res.update({"modes_before": before["modes"][:1] if before else [],
                "modes_after": after["modes"][:1] if after else [], "live": live})
    if not live:
        res["note"] = ("no live effect: appledrm decides the notch at DCP probe, so this applies from "
                       "the next boot (or pass appledrm.show_notch=%d on the kernel command line)" % (1 if on else 0))
    log.event("notch", on=on, live=live)
    return res


def brightness_path():
    base = "/sys/class/backlight"
    for b in sysfs.listdir(base):
        return base + "/" + b
    return None


def set_brightness(machine, value, allow_unknown=False):
    d = brightness_path()
    if not d:
        raise control.Refused("no backlight device")
    mx = sysfs.read_int(d + "/max_brightness") or 0
    v = str(value)
    if v.endswith("%"):
        n = round(mx * float(v[:-1]) / 100.0)
    else:
        n = int(v)
    if not 0 <= n <= mx:
        raise control.Refused("brightness must be 0..%d" % mx)
    return control.apply_sysfs(d + "/brightness", n, machine, allow_unknown)


# -- Mutter power save (the session's DPMS) ----------------------------------

MUTTER = ["--dest", "org.gnome.Mutter.DisplayConfig", "--object-path", "/org/gnome/Mutter/DisplayConfig"]


def _gdbus(*args):
    if not shutil.which("gdbus"):
        raise control.Refused("gdbus is not installed")
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise control.Refused("no session bus: run this as the desktop user inside their session")
    r = subprocess.run(["gdbus", "call", "--session"] + MUTTER + list(args), capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise control.Refused("gdbus failed: %s" % r.stderr.strip())
    return r.stdout.strip()


def power_save_get():
    out = _gdbus("--method", "org.freedesktop.DBus.Properties.Get", "org.gnome.Mutter.DisplayConfig", "PowerSaveMode")
    m = re.search(r"(-?\d+)", out)
    return int(m.group(1)) if m else None


def _restore_mutter(e):
    _gdbus("--method", "org.freedesktop.DBus.Properties.Set", "org.gnome.Mutter.DisplayConfig",
           "PowerSaveMode", "<int32 %s>" % e["restore"])


control.register_restorer("mutter", _restore_mutter)


def power_save_set(mode):
    if mode not in (0, 1, 2, 3):
        raise control.Refused("PowerSaveMode is 0 (on), 1 (standby), 2 (suspend) or 3 (off)")
    before = power_save_get()
    _gdbus("--method", "org.freedesktop.DBus.Properties.Set", "org.gnome.Mutter.DisplayConfig",
           "PowerSaveMode", "<int32 %d>" % mode)
    control._record("mutter", "mutter:PowerSaveMode", str(before), str(mode))
    after = power_save_get()
    log.event("set", kind="mutter", path="PowerSaveMode", original=before, new=mode, readback=after)
    return {"original": before, "new": mode, "readback": after}
