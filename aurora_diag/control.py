# SPDX-License-Identifier: MIT
"""The control side: one write path, one policy, one change journal.

Every change the tool makes goes through apply(). It
  1. refuses anything the safety policy marks read-only (refusal()),
  2. reads and keeps the original value (a change it cannot undo is refused),
  3. writes, reads back and logs,
  4. records the change in the journal under /run/aurora-diag, so
     `aurora-diag restore` (or the exit of a --hold / TUI session) puts the
     original back, newest change first.
The journal lives on tmpfs: after a reboot the kernel has reset every value,
so there is nothing left to restore.
"""

import fcntl
import json
import os
import re
import shutil
import subprocess
import time

from . import log, sysfs

# -- safety policy ----------------------------------------------------------

# ALSA controls that belong to speaker protection or the speaker path. All of
# them are read-only here: the tool must never disable, bypass, pause or
# loosen speaker protection, and must never raise what reaches a speaker.
SPEAKER_PROTECTED = re.compile(
    r"(unlock|sense|safe|interlock|protect|limit|speaker|spk|amp\b|tweeter|woofer|"
    r"boost)", re.I)

# Module parameters that touch speakers, the boot chain or that only take
# effect at probe in a way that taints the kernel.
DENY_PARAMS = re.compile(
    r"^(snd_soc_macaudio|snd_soc_tas27\w*|snd_soc_cs42l\w*|snd_soc_apple\w*|"
    r"snd_soc_aop\w*|apple_aop\w*|spi_apple_qspi|apple_nvmem\w*|macsmc_reboot|"
    r"nvmem\w*)\.|blow|speaker|fan_control$")

DENY_PATHS = [
    (re.compile(r"^/run/speakerguardd|^/var/lib/speakersafetyd"), "speaker protection state is read-only"),
    (re.compile(r"^/sys/class/sound/|/sound/card\d+/|/sound/"), "sound devices are read-only (speaker protection)"),
    (re.compile(r"^/sys/firmware/"), "firmware interfaces (EFI vars, DT) are read-only: boot chain"),
    (re.compile(r"^/sys/(class|block)/(block|mtd|nvmem)\b|^/sys/bus/(nvmem|spi|mtd)/|^/sys/block/"), "storage, NVRAM and SPI NOR are read-only: boot chain"),
    (re.compile(r"^/dev/"), "device nodes are not written through the generic path"),
    (re.compile(r"^/sys/kernel/debug/"), "debugfs is read-only in this tool"),
    (re.compile(r"^/sys/power/"), "suspend and hibernation controls are actions, not settings"),
    (re.compile(r"^/proc/"), "/proc is read-only in this tool"),
    (re.compile(r"/(uevent|bind|unbind|new_id|remove_id|remove|rescan|reset|driver_override|"
                r"delete_device|new_device|authorized_default)$"), "bind/unbind/remove style actions are refused"),
    (re.compile(r"/iio:device\d+/(in|out)_[^/]*_(raw|input)$"), "a sensor's measurement is not a setting"),
    (re.compile(r"/drm/card\d+-[^/]+/status$"), "connector status is an action: use `aurora-diag display reprobe`"),
    (re.compile(r"/thermal_zone\d+/(mode|emul_temp)$"), "disabling a thermal zone or faking its temperature loosens thermal protection"),
    (re.compile(r"/power_supply/[^/]+/(?!charge_control_start_threshold$|charge_control_end_threshold$|charge_behaviour$)[^/]+$"),
     "battery and charger limits are read-only (only charge thresholds and charge_behaviour may change)"),
]


def refusal(path, machine=None, allow_unknown=False):
    """None if the policy allows writing this path, else the reason."""
    if machine is not None and not machine.known and not allow_unknown:
        return "unknown machine (%s): read-only unless --allow-unknown" % (machine.board,)
    if path.startswith("alsa:"):
        return None  # checked by name in alsa_refusal()
    if not (path.startswith("/sys/")):
        return "only /sys attributes and ALSA controls can be changed"
    # Judge the path as given and as the kernel resolves it, so a
    # /sys/devices/... spelling cannot dodge a /sys/class/... rule.
    real = sysfs.realpath(path)
    for p in (path, real):
        if not p.startswith("/sys/"):
            return "only /sys attributes and ALSA controls can be changed"
        for rx, why in DENY_PATHS:
            if rx.search(p):
                return why
    m = re.match(r"^/sys/module/([^/]+)/parameters/([^/]+)$", path)
    if m and DENY_PARAMS.search("%s.%s" % m.groups()):
        if m.group(2) == "fan_control":
            return "macsmc_hwmon.fan_control only takes effect at probe and taints the kernel: boot with macsmc_hwmon.fan_control=1"
        return "module parameter %s.%s is read-only (speaker, boot chain or probe-time safety)" % m.groups()
    m = re.match(r"^(.*/thermal_zone\d+)/trip_point_(\d+)_(temp|hyst)$", path)
    if m:
        typ = sysfs.read("%s/trip_point_%s_type" % (m.group(1), m.group(2)))
        if typ in ("critical", "hot"):
            return "critical and hot trip points are read-only"
    return None


def alsa_refusal(name):
    if SPEAKER_PROTECTED.search(name):
        return "ALSA control '%s' is on the speaker path: read-only (speaker protection)" % name
    return None


# -- journal ----------------------------------------------------------------

def _journal_path():
    return os.path.join(log.run_dir(), "journal.json")


class Journal:
    """A list of changes, kept with an exclusive lock while it is edited."""

    def __enter__(self):
        d = log.run_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        self.f = open(_journal_path(), "a+")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        self.f.seek(0)
        text = self.f.read()
        try:
            self.entries = json.loads(text) if text.strip() else []
        except ValueError:
            self.entries = []
        return self

    def save(self):
        self.f.seek(0)
        self.f.truncate()
        json.dump(self.entries, self.f, indent=1)
        self.f.flush()
        os.fsync(self.f.fileno())

    def __exit__(self, *exc):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


def journal_entries():
    if not os.path.exists(_journal_path()):
        return []
    with Journal() as j:
        return list(j.entries)


def _record(kind, key, original, new, restore_value=None, extra=None):
    with Journal() as j:
        if not any(e["key"] == key for e in j.entries):
            e = {"kind": kind, "key": key, "original": original, "restore": restore_value if restore_value is not None else original,
                 "ts": time.time(), "session": log.session(), "pid": os.getpid()}
            if extra:
                e.update(extra)
            j.entries.append(e)
        for e in j.entries:
            if e["key"] == key:
                e["current"] = new
        j.save()


def _forget(key):
    with Journal() as j:
        j.entries = [e for e in j.entries if e["key"] != key]
        j.save()


# -- value helpers ----------------------------------------------------------

def normalise(raw):
    """What to write back to reproduce a value as the kernel showed it:
    'a [b] c' style selections become 'b'."""
    if raw is None:
        return None
    m = re.search(r"\[([^\]]+)\]", raw)
    if m:
        return m.group(1)
    return raw.strip()


class Refused(Exception):
    pass


# -- sysfs ------------------------------------------------------------------

def apply_sysfs(path, value, machine, allow_unknown=False, dry_run=False, kind="sysfs", restore_value=None, extra=None):
    why = refusal(path, machine, allow_unknown)
    if why:
        log.event("refuse", path=path, value=str(value), reason=why)
        raise Refused(why)
    if not sysfs.exists(path):
        raise Refused("no such attribute: %s" % path)
    if not sysfs.writable(path):
        raise Refused("the kernel offers no store for %s (mode %o)" % (path, sysfs.mode(path) or 0))
    before_raw = sysfs.read(path)
    before = normalise(before_raw)
    if before is None and restore_value is None:
        raise Refused("cannot read %s back, so a change could not be undone" % path)
    if dry_run:
        log.event("dry_run", path=path, value=str(value), original=before)
        return {"path": path, "original": before, "new": str(value), "dry_run": True}
    try:
        sysfs.write(path, value)
    except OSError as e:
        log.event("set_failed", path=path, value=str(value), original=before, error=str(e))
        raise Refused("write failed: %s" % e)
    after = normalise(sysfs.read(path))
    _record(kind, path, before, str(value), restore_value, extra)
    ok = after == str(value).strip() or _numeq(after, value)
    log.event("set", kind=kind, path=path, original=before, new=str(value), readback=after, verified=ok)
    return {"path": path, "original": before, "new": str(value), "readback": after, "verified": ok}


def _numeq(a, b):
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


# -- ALSA -------------------------------------------------------------------

def _amixer(*args):
    if not shutil.which("amixer"):
        raise Refused("amixer is not installed")
    r = subprocess.run(["amixer"] + list(args), capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        raise Refused("amixer failed: %s" % r.stderr.strip())
    return r.stdout


def _alsa_value(card, numid):
    out = _amixer("-c", str(card), "cget", "numid=%s" % numid)
    for line in out.splitlines():
        if line.strip().startswith(": values="):
            return line.split("=", 1)[1].strip()
    return None


def apply_alsa(card, numid, name, value, machine, allow_unknown=False, dry_run=False):
    why = refusal("alsa:", machine, allow_unknown) or alsa_refusal(name)
    if why:
        log.event("refuse", path="alsa:%s:%s" % (card, numid), control=name, value=str(value), reason=why)
        raise Refused(why)
    key = "alsa:%s:%s" % (card, numid)
    before = _alsa_value(card, numid)
    if before is None:
        raise Refused("cannot read ALSA control %s back" % key)
    if dry_run:
        return {"path": key, "original": before, "new": str(value), "dry_run": True}
    _amixer("-c", str(card), "cset", "numid=%s" % numid, str(value))
    after = _alsa_value(card, numid)
    _record("alsa", key, before, str(value), extra={"card": card, "numid": numid, "name": name})
    log.event("set", kind="alsa", path=key, control=name, original=before, new=str(value), readback=after)
    return {"path": key, "original": before, "new": str(value), "readback": after}


# -- restore ----------------------------------------------------------------

_restorers = {}


def register_restorer(kind, fn):
    """fn(entry) puts one change back; raises on failure."""
    _restorers[kind] = fn


def _restore_sysfs(e):
    sysfs.write(e["key"], e["restore"])


def _restore_alsa(e):
    _amixer("-c", str(e["card"]), "cset", "numid=%s" % e["numid"], e["restore"])


register_restorer("sysfs", _restore_sysfs)
register_restorer("fan", _restore_sysfs)
register_restorer("alsa", _restore_alsa)


def restore(session=None, kinds=None, reason="restore", keys=None):
    """Put back every journalled change (newest first), optionally only one
    session's or some kinds. Returns (restored, failed)."""
    restored, failed = [], []
    if not os.path.exists(_journal_path()):
        return restored, failed
    with Journal() as j:
        keep = []
        for e in reversed(j.entries):
            if ((session and e.get("session") != session) or (kinds and e["kind"] not in kinds)
                    or (keys and e["key"] not in keys)):
                keep.append(e)
                continue
            fn = _restorers.get(e["kind"])
            try:
                if fn is None:
                    raise RuntimeError("no restorer for kind %s" % e["kind"])
                fn(e)
                restored.append(e)
                log.event("restored", kind=e["kind"], path=e["key"], value=e["restore"], reason=reason)
            except Exception as ex:  # keep it in the journal for a later try
                failed.append(e)
                keep.append(e)
                log.event("restore_failed", kind=e["kind"], path=e["key"], value=e["restore"], error=str(ex), reason=reason)
        j.entries = list(reversed(keep))
        j.save()
    return restored, failed
