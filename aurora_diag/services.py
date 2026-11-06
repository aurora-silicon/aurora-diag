# SPDX-License-Identifier: MIT
"""Userspace that the hardware depends on: is it installed, running, and
holding the state it needs.

Units are found by pattern in what systemd knows about (system units and,
for each logged-in desktop user, user units), so a renamed or new unit
still shows up. Touch ID state is read from files only (enrolled print
files and the SEP host state); nothing asks fprintd or the SEP anything.
"""

import glob
import configparser
import os
import pwd
import re
import shutil
import subprocess

from . import audio, sysfs

UNIT_RE = re.compile(r"^(aurora[-\w]*|speaker\w*|fprintd|iio-sensor-proxy|power-profiles-daemon|tuned|tuned-ppd|"
                     r"thermald|upower|bluetooth|NetworkManager|wpa_supplicant|iwd|gdm|sddm|greetd|"
                     r"systemd-backlight@.*|alsa-state|alsa-restore|asahi[-\w]*|macsmc[-\w]*|"
                     r"pipewire|pipewire-pulse|wireplumber|shairport-sync|nqptp)\.(service|socket|timer)$")
USER_UNITS = ("pipewire.service", "pipewire-pulse.service", "wireplumber.service")
PROPERTIES = ("Id,ActiveState,SubState,UnitFileState,ExecMainStartTimestamp,NRestarts,Result,"
              "LoadState,Type,BusName,TriggeredBy")


def _run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return None, ""


def system_units():
    if not shutil.which("systemctl"):
        return []
    rc, out = _run(["systemctl", "list-unit-files", "--no-legend", "--no-pager", "--type=service,socket,timer"])
    names = [l.split()[0] for l in out.splitlines() if l.split() and UNIT_RE.match(l.split()[0])]
    names = [n for n in names if "@" not in n or not n.endswith("@.service")]
    res = []
    if not names:
        return res
    return _show_units(["systemctl"], names)


def _show_units(cmd, names, bus_names=None):
    rc, out = _run(cmd + ["show", "--no-pager", "-p", PROPERTIES] + names)
    res = []
    cur = {}
    for line in out.splitlines() + [""]:
        if not line.strip():
            if cur:
                res.append(cur)
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    # TriggeredBy may name a socket outside UNIT_RE. Read its state as well.
    sockets = sorted({n for u in res for n in u.get("TriggeredBy", "").split() if n.endswith(".socket")})
    active_sockets = set()
    if sockets:
        _, socket_out = _run(cmd + ["show", "--no-pager", "-p", "Id,ActiveState,LoadState"] + sockets)
        for block in socket_out.strip().split("\n\n"):
            props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if props.get("ActiveState") == "active" and props.get("LoadState") == "loaded":
                active_sockets.add(props.get("Id"))
    registered = _bus_names() if bus_names is None else bus_names
    return [{"unit": u.get("Id"), "active": u.get("ActiveState"), "sub": u.get("SubState"),
             "enabled": u.get("UnitFileState"), "since": u.get("ExecMainStartTimestamp") or None,
             "restarts": int(u["NRestarts"]) if u.get("NRestarts", "").isdigit() else None,
             "result": u.get("Result"), "load": u.get("LoadState"),
             "type": u.get("Type"), "bus_name": u.get("BusName") or None,
             "bus_activatable": u.get("Type") == "dbus" and u.get("BusName") in registered,
             "active_sockets": sorted(active_sockets.intersection(u.get("TriggeredBy", "").split()))}
            for u in res]


def _bus_names():
    """Read D-Bus activation registrations without starting a service."""
    names = set()
    for base in ("/etc", "/usr/local/share", "/usr/share"):
        directory = base + "/dbus-1/system-services"
        for name in sysfs.listdir(directory):
            if not name.endswith(".service"):
                continue
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(sysfs.read(directory + "/" + name) or "")
                names.add(parser.get("D-BUS Service", "Name"))
            except configparser.Error:
                continue
    return names


def idle_activatable(unit):
    """An idle, successfully stopped unit with a usable activation source."""
    return (unit.get("active") == "inactive" and unit.get("load") == "loaded"
            and unit.get("enabled") not in ("masked", "masked-runtime")
            and unit.get("result") in ("success", None, "")
            and bool(unit.get("bus_activatable") or unit.get("active_sockets")))


def snapshot_idle(unit, state, details):
    if unit in details:
        return idle_activatable(dict(details[unit], active=state))
    # Old schema-1 files have no activation metadata. fprintd is a known
    # D-Bus-activated daemon; do not generalize this to arbitrary idle units.
    return unit == "fprintd.service" and state == "inactive"


def activation_details(unit):
    """Only activation facts belong in snapshots, not timestamps or counters."""
    return {k: unit.get(k) for k in ("load", "enabled", "result", "type", "bus_name",
                                    "bus_activatable", "active_sockets")}


def desktop_users():
    out = []
    for p in pwd.getpwall():
        if p.pw_uid >= 1000 and p.pw_uid < 60000 and os.path.isdir("/run/user/%d" % p.pw_uid):
            out.append(p)
    return out


def user_units():
    res = []
    if not shutil.which("systemctl"):
        return res
    for p in desktop_users():
        if os.geteuid() == 0:
            cmd = ["systemctl", "--user", "-M", "%s@" % p.pw_name]
        elif os.geteuid() == p.pw_uid:
            cmd = ["systemctl", "--user"]
        else:
            continue
        for u in _show_units(cmd, list(USER_UNITS), bus_names=set()):
            res.append(dict(u, user=p.pw_name))
    return res


def touchid():
    """Enrolment and SEP host-state files (root reads them; never opened
    beyond a stat)."""
    out = {"fprint_users": {}, "sep_state_files": []}
    for d in glob.glob(sysfs.root("/var/lib/fprint") + "/*"):
        user = os.path.basename(d)
        prints = [f for f in glob.glob(d + "/**/*", recursive=True) if os.path.isfile(f)]
        out["fprint_users"][user] = len(prints)
    if not os.access(sysfs.root("/var/lib/fprint"), os.R_OK | os.X_OK) and sysfs.exists("/var/lib/fprint"):
        out["fprint_users"] = "unreadable (needs root)"
    for f in sorted(glob.glob(sysfs.root("/var/lib") + "/aurora-sep*")):
        try:
            st = os.stat(f)
            out["sep_state_files"].append({"file": sysfs.unroot(f), "bytes": st.st_size, "mtime": int(st.st_mtime)})
        except OSError:
            pass
    out["sep_bio_node"] = sysfs.exists("/dev/sep-bio")
    pam = []
    for f in glob.glob(sysfs.root("/etc/pam.d") + "/*"):
        text = sysfs.read(sysfs.unroot(f)) or ""
        if "pam_fprintd" in text:
            pam.append(os.path.basename(f))
    out["pam_fprintd_in"] = sorted(pam)
    return out


def speakerguardd():
    rs = audio.speaker_protection()
    by = {r["id"]: r["value"] for r in rs}
    coils = {r["id"].replace("speaker.speakerguardd.", ""): r["value"] for r in rs
             if r["attr"] in ("t_coil_C", "ff_coil_C", "vi_coil_C", "t_magnet_C") and isinstance(r["value"], float)}
    return {"service": by.get("speaker.speakerguardd.service"), "armed": by.get("speaker.speakerguardd.armed"),
            "governor": by.get("speaker.speakerguardd.shadow.gov"),
            "speakersafetyd": by.get("speaker.speakersafetyd.service"),
            "model_temperatures_C": coils, "hottest_coil_C": max(coils.values()) if coils else None}


def all_services():
    return {"system": system_units(), "user": user_units(), "touchid": touchid(), "speakerguardd": speakerguardd()}


def _level(active):
    return {"active": "ok", "activating": "warn", "reloading": "warn", "failed": "fail"}.get(active, "off")


def readings():
    out = []
    for u in system_units():
        lvl = _level(u["active"])
        out.append({"id": "service.%s" % u["unit"], "cat": "service", "dev": u["unit"], "attr": "active",
                    "label": "%s/%s, %s%s" % (u["active"], u["sub"], u["enabled"],
                                               (", %d restarts" % u["restarts"]) if u.get("restarts") else ""),
                    "value": lvl if lvl != "off" else u["active"], "unit": None, "raw": u["active"], "path": None,
                    "writable": False})
    for u in user_units():
        out.append({"id": "service.user.%s.%s" % (u["user"], u["unit"]), "cat": "service", "dev": u["unit"],
                    "attr": "active", "label": "user %s" % u["user"], "value": u["active"], "unit": None,
                    "raw": u["active"], "path": None, "writable": False})
    t = touchid()
    fu = t["fprint_users"]
    out.append({"id": "service.touchid.enrolled", "cat": "service", "dev": "touchid", "attr": "enrolled",
                "label": "print files per user in /var/lib/fprint", "value": fu if isinstance(fu, str) else (fu or "none"),
                "unit": None, "raw": str(fu), "path": "/var/lib/fprint", "writable": False})
    out.append({"id": "service.touchid.sep_state", "cat": "service", "dev": "touchid", "attr": "sep_state",
                "label": "aurora SEP host state files", "value": len(t["sep_state_files"]), "unit": "files",
                "raw": str(len(t["sep_state_files"])), "path": "/var/lib", "writable": False})
    out.append({"id": "service.touchid.pam", "cat": "service", "dev": "touchid", "attr": "pam",
                "label": "PAM stacks using pam_fprintd", "value": ",".join(t["pam_fprintd_in"]) or "none",
                "unit": None, "raw": "", "path": "/etc/pam.d", "writable": False})
    return out
