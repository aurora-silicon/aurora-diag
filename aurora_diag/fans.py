# SPDX-License-Identifier: MIT
"""Fan control with a watchdog that always hands the fans back to the SMC.

The kernel side (macsmc-hwmon) makes fanN_target writable only when the
driver probed with fan_control=1. Writing a speed puts the fan in manual
mode; writing 0 drops the request and the fan goes back to the firmware
(and, with thermal cooling devices on the fan, never below what the zones
ask for). The kernel also hands every fan back on suspend, shutdown and
unbind.

What this module adds is a userspace dead-man switch. Every manual speed is
covered by a guard process that writes 0 back to every fan the tool set
when any of these happens:
  * the lease runs out (default 10 minutes; the TUI and --hold renew it),
  * a die temperature passes --max-temp (default 90 C, capped at 100 C),
  * the die temperatures cannot be read for 5 s (fail closed),
  * the guard is told to stop (SIGTERM/SIGINT/SIGHUP),
  * the guard dies by any means: it runs as a transient systemd unit whose
    ExecStopPost restores the fans even after SIGKILL or a crash.
A machine with no fan channels (the MacBook Neo, the Airs) is refused.
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

from . import control, log, sysfs

GUARD_UNIT = "aurora-diag-fan-guard"
DEFAULT_LEASE = 600
DEFAULT_MAX_TEMP = 90.0
HARD_MAX_TEMP = 100.0
DIE_ZONE = re.compile(r"(cpu|gpu|soc|die|ane|pmgr)", re.I)
DIE_LABEL = re.compile(r"(die|soc|cpu|gpu|p-core|e-core|pcore|ecore)", re.I)


def fans():
    out = []
    base = "/sys/class/hwmon"
    for h in sysfs.listdir(base):
        d = base + "/" + h
        for f in sysfs.listdir(d):
            m = re.fullmatch(r"fan(\d+)_input", f)
            if not m:
                continue
            n = m.group(1)
            p = lambda a: "%s/fan%s_%s" % (d, n, a)
            out.append({
                "hwmon": h, "name": sysfs.read(d + "/name"), "fan": int(n),
                "label": sysfs.read(p("label")) or "Fan %s" % n,
                "rpm": sysfs.read_int(p("input")), "min": sysfs.read_int(p("min")),
                "max": sysfs.read_int(p("max")), "target": sysfs.read_int(p("target")),
                "target_path": p("target"), "target_writable": sysfs.writable(p("target")),
            })
    return out


def fan_control_param():
    return sysfs.read("/sys/module/macsmc_hwmon/parameters/fan_control")


def die_temps():
    """{name: C} for every die-like thermal zone and hwmon temperature."""
    out = {}
    base = "/sys/class/thermal"
    for z in sysfs.listdir(base):
        if not z.startswith("thermal_zone"):
            continue
        typ = sysfs.read(base + "/" + z + "/type") or z
        if DIE_ZONE.search(typ):
            t = sysfs.read_int(base + "/" + z + "/temp")
            if t is not None:
                out["zone:" + typ] = t / 1000.0
    hb = "/sys/class/hwmon"
    for h in sysfs.listdir(hb):
        d = hb + "/" + h
        for f in sysfs.listdir(d):
            m = re.fullmatch(r"temp(\d+)_label", f)
            if not m:
                continue
            label = sysfs.read(d + "/" + f) or ""
            if DIE_LABEL.search(label):
                t = sysfs.read_int("%s/temp%s_input" % (d, m.group(1)))
                if t is not None:
                    out["hwmon:" + label] = t / 1000.0
    return out


def hottest():
    t = die_temps()
    if not t:
        return None, None
    k = max(t, key=t.get)
    return k, t[k]


# -- lease -------------------------------------------------------------------

def _lease_path():
    return os.path.join(log.run_dir(), "fan-lease.json")


def write_lease(seconds, max_temp):
    os.makedirs(log.run_dir(), mode=0o700, exist_ok=True)
    tmp = _lease_path() + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"deadline": time.monotonic() + seconds, "max_temp": max_temp,
                   "session": log.session()}, f)
    os.replace(tmp, _lease_path())


def read_lease():
    try:
        with open(_lease_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def fan_entries():
    return [e for e in control.journal_entries() if e["kind"] == "fan"]


# -- set / auto -------------------------------------------------------------

def set_fan(machine, index, rpm, lease=DEFAULT_LEASE, max_temp=DEFAULT_MAX_TEMP,
            allow_unknown=False, dry_run=False, start_guard=True):
    fl = fans()
    if not fl:
        raise control.Refused("this machine has no fans the SMC reports (fanless): fan control refused")
    fan = next((f for f in fl if f["fan"] == index), None)
    if fan is None:
        raise control.Refused("no fan %d (have %s)" % (index, ", ".join(str(f["fan"]) for f in fl)))
    if not fan["target_writable"]:
        raise control.Refused(
            "fan%d_target is read-only: macsmc_hwmon.fan_control=%s. Manual fan control needs the "
            "driver probed with macsmc_hwmon.fan_control=1 (boot parameter; it taints the kernel)"
            % (index, fan_control_param()))
    max_temp = min(float(max_temp), HARD_MAX_TEMP)
    if rpm != 0:
        lo, hi = fan["min"], fan["max"]
        if lo is not None and rpm < lo:
            raise control.Refused("%d rpm is under fan%d's minimum %d" % (rpm, index, lo))
        if hi is not None and rpm > hi:
            raise control.Refused("%d rpm is over fan%d's maximum %d" % (rpm, index, hi))
        name, t = hottest()
        if t is None:
            raise control.Refused("no die temperature is readable, so the guard could not protect a manual speed")
        if t >= max_temp:
            raise control.Refused("%s is %.1f C, at or over the %.1f C limit" % (name, t, max_temp))
    if dry_run:
        return {"fan": index, "rpm": rpm, "dry_run": True}
    if rpm != 0:
        write_lease(lease, max_temp)
    res = control.apply_sysfs(fan["target_path"], rpm, machine, allow_unknown,
                              kind="fan", restore_value="0", extra={"fan": index})
    if rpm == 0:
        control.restore(kinds=["fan"], keys=[fan["target_path"]], reason="fan auto")
    elif start_guard:
        try:
            res["guard"] = ensure_guard(lease, max_temp)
        except Exception:
            auto(reason="guard failed to start")
            raise
    log.event("fan_set", fan=index, rpm=rpm, lease=lease, max_temp=max_temp)
    return res


def auto(reason="fan auto"):
    restored, failed = control.restore(kinds=["fan"], reason=reason)
    # Belt and braces: 0 to every writable target, journalled or not.
    for f in fans():
        if f["target_writable"]:
            try:
                sysfs.write(f["target_path"], 0)
            except OSError:
                pass
    return restored, failed


# -- guard -------------------------------------------------------------------

def _self_cmd():
    """How to run this same code again from a unit: the installed command
    when it is this one, else the interpreter on this package directory."""
    exe = shutil.which("aurora-diag")
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if exe and os.path.dirname(os.path.realpath(exe)) != os.path.join(pkg, "bin"):
        mod = os.path.dirname(os.path.abspath(__file__))
        try:
            import aurora_diag as installed  # noqa: F401
            if os.path.dirname(os.path.abspath(installed.__file__)) == mod:
                return [exe]
        except ImportError:
            pass
    return [sys.executable, "-I", "-c",
            "import sys; sys.path.insert(0, %r); from aurora_diag.cli import main; sys.exit(main())" % pkg]


def _systemd():
    return shutil.which("systemd-run") and os.path.isdir("/run/systemd/system") and os.geteuid() == 0


def guard_active():
    if _systemd():
        r = subprocess.run(["systemctl", "is-active", GUARD_UNIT + ".service"], capture_output=True, text=True)
        if r.stdout.strip() in ("active", "activating"):
            return True
    pidf = os.path.join(log.run_dir(), "fan-guard.pid")
    try:
        pid = int(open(pidf).read())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _wait_active(seconds=3.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if guard_active() and os.path.exists(os.path.join(log.run_dir(), "fan-guard.pid")):
            return True
        time.sleep(0.1)
    return False


def ensure_guard(lease, max_temp):
    """Start the guard and confirm it runs. A guard that does not come up
    is an error: the caller hands the fans back."""
    if guard_active():
        return "running"
    cmd = _self_cmd()
    env = []
    for k in ("AURORA_DIAG_RUN", "AURORA_DIAG_LOG", "AURORA_DIAG_ROOT"):
        if os.environ.get(k):
            env.append("--setenv=%s=%s" % (k, os.environ[k]))
    if _systemd():
        subprocess.run(["systemctl", "reset-failed", GUARD_UNIT + ".service"], capture_output=True)
        stop = " ".join(shlex.quote(c) for c in cmd + ["fan", "auto", "--reason", "guard-stopped"])
        r = subprocess.run(["systemd-run", "--unit", GUARD_UNIT, "--collect", "--quiet",
                            "--property=ExecStopPost=" + stop] + env +
                           cmd + ["fan-guard", "--max-temp", str(max_temp)],
                           capture_output=True, text=True)
        if r.returncode == 0 and _wait_active():
            log.event("guard_started", how="systemd-run", unit=GUARD_UNIT)
            return "systemd"
        log.event("guard_start_failed", how="systemd-run", error=r.stderr.strip() or "unit did not stay active")
    # Fallback: a detached process. It cannot survive SIGKILL the way the
    # unit's ExecStopPost does; the lease still bounds a manual speed.
    pid = os.fork()
    if pid == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        try:
            run_guard(max_temp)
        finally:
            os._exit(0)
    os.waitpid(pid, 0)
    if _wait_active():
        log.event("guard_started", how="fork")
        return "forked"
    log.event("guard_start_failed", how="fork")
    raise control.Refused("the fan guard did not start")


def run_guard(max_temp=DEFAULT_MAX_TEMP, interval=1.0, blind_limit=5.0):
    """The guard loop. Returns once no manual fan is left."""
    max_temp = min(float(max_temp), HARD_MAX_TEMP)
    os.makedirs(log.run_dir(), mode=0o700, exist_ok=True)
    pidf = os.path.join(log.run_dir(), "fan-guard.pid")
    with open(pidf, "w") as f:
        f.write(str(os.getpid()))
    stop = {"why": None}

    def on_signal(sig, frame):
        stop["why"] = "signal %d" % sig

    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, on_signal)
    log.event("guard_run", max_temp=max_temp)
    blind_since = None
    why = None
    try:
        while True:
            if stop["why"]:
                why = stop["why"]
                break
            if not fan_entries():
                why = "no manual fans left"
                break
            lease = read_lease()
            limit = min(float((lease or {}).get("max_temp", max_temp)), max_temp)
            if lease is None or time.monotonic() > lease.get("deadline", 0):
                why = "lease expired"
                break
            name, t = hottest()
            now = time.monotonic()
            if t is None:
                blind_since = blind_since or now
                if now - blind_since >= blind_limit:
                    why = "die temperatures unreadable for %.0f s" % blind_limit
                    break
            else:
                blind_since = None
                if t >= limit:
                    why = "%s at %.1f C passed %.1f C" % (name, t, limit)
                    break
            time.sleep(interval)
    finally:
        restored, failed = auto(reason="guard: %s" % (why or "exit"))
        log.event("guard_exit", reason=why or "exit", restored=len(restored), failed=len(failed))
        try:
            os.unlink(pidf)
        except OSError:
            pass
        try:
            os.unlink(_lease_path())
        except OSError:
            pass
