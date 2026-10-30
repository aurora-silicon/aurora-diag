# SPDX-License-Identifier: MIT
"""Trackpad click strength through the actuator's HID feature reports.

On the MTP machines (M2, M3, M4) and on the SPI trackpads (M1 Pro/Max) the
actuator firmware plays the click by itself and takes its waveforms from
feature reports 0x22 (press) and 0x23 (release); the third byte of each is
the strength. The firmware defaults read 0x23 / 0x14 on every machine
measured; the kernel may set others from the device tree
(apple,click-strength). Changing them here is live and lasts until the next
reboot or driver probe; restore writes back the bytes read first.
"""

import array
import fcntl
import os

from . import control, log, sysfs

PRESS, RELEASE = 0x22, 0x23
STRENGTH_BYTE = 2


def _ioc(d, t, nr, size):
    return (d << 30) | (size << 16) | (ord(t) << 8) | nr


def HIDIOCGFEATURE(n):
    return _ioc(3, "H", 0x07, n)


def HIDIOCSFEATURE(n):
    return _ioc(3, "H", 0x06, n)


def find_actuator():
    """(/dev/hidrawN, HID_NAME) of the trackpad actuator, or (None, None)."""
    base = "/sys/class/hidraw"
    for h in sysfs.listdir(base):
        ue = sysfs.uevent(base + "/" + h + "/device")
        name = ue.get("HID_NAME", "")
        if "actuator" in name.lower():
            return "/dev/" + h, name
    return None, None


def get_report(dev, rid, size=64):
    fd = os.open(sysfs.root(dev), os.O_RDWR)
    try:
        buf = array.array("B", [rid] + [0] * (size - 1))
        n = fcntl.ioctl(fd, HIDIOCGFEATURE(size), buf, True)
        return bytes(buf[:n])
    finally:
        os.close(fd)


def set_report(dev, data):
    fd = os.open(sysfs.root(dev), os.O_RDWR)
    try:
        buf = array.array("B", data)
        fcntl.ioctl(fd, HIDIOCSFEATURE(len(data)), buf, True)
    finally:
        os.close(fd)


def status(machine):
    if machine.excluded("haptics"):
        return {"supported": False, "reason": "haptics are excluded on %s" % machine.board}
    dev, name = find_actuator()
    if not dev:
        return {"supported": False, "reason": "no trackpad actuator HID device"}
    try:
        p = get_report(dev, PRESS)
        r = get_report(dev, RELEASE)
    except OSError as e:
        return {"supported": True, "device": dev, "name": name, "error": str(e)}
    return {"supported": True, "device": dev, "name": name,
            "press": p[STRENGTH_BYTE], "release": r[STRENGTH_BYTE],
            "press_report": p.hex(), "release_report": r.hex()}


def _restore_hid(e):
    set_report(e["device"], bytes.fromhex(e["restore"]))


control.register_restorer("hid", _restore_hid)


def set_strength(machine, press, release, allow_unknown=False, dry_run=False):
    st = status(machine)
    if not st.get("supported"):
        raise control.Refused(st.get("reason", "unsupported"))
    if "error" in st:
        raise control.Refused("cannot read the actuator reports: %s" % st["error"])
    why = control.refusal("/sys/class/hidraw", machine, allow_unknown)
    if why:
        raise control.Refused(why)
    dev = st["device"]
    results = []
    for rid, val in ((PRESS, press), (RELEASE, release)):
        if val is None:
            continue
        if not 1 <= int(val) <= 255:
            raise control.Refused("strength must be 1..255")
        before = get_report(dev, rid)
        new = bytearray(before)
        new[STRENGTH_BYTE] = int(val)
        if dry_run:
            results.append({"report": hex(rid), "original": before.hex(), "new": new.hex(), "dry_run": True})
            continue
        set_report(dev, bytes(new))
        after = get_report(dev, rid)
        control._record("hid", "hid:%s:%#x" % (dev, rid), before.hex(), new.hex(),
                        extra={"device": dev, "name": st["name"], "report": rid})
        log.event("set", kind="hid", path="%s report %#x" % (dev, rid), original=before.hex(),
                  new=new.hex(), readback=after.hex(), verified=after == bytes(new))
        results.append({"report": hex(rid), "original": before.hex(), "new": new.hex(),
                        "readback": after.hex(), "verified": after == bytes(new)})
    return results
