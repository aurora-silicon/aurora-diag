# SPDX-License-Identifier: MIT
"""Class identity shared by snapshot diff and check --expect."""

import re
from collections import Counter

from . import sysfs


def normalize(path):
    """Remove allocation IDs, preserving addresses, channels and connector indices.

    This also handles schema-1 snapshots which recorded only class/name.
    Unknown names are deliberately compared literally.
    """
    path = re.sub(r"\bdma\d+chan(\d+)", r"dmaNchan\1", path)
    path = re.sub(r"\b(card|renderD|input|event|hidraw|hwmon|wakeup|iio:device|"
                  r"i2c-|port|thermal_zone|cooling_device|video|media|usbmon|hiddev|spi)"
                  r"\d+(?=$|[/.:\-])", r"\1N", path)
    path = re.sub(r"\bmtd\d+(ro)?(?=/|$)", r"mtdN\1", path)
    path = re.sub(r"\b(controlC|pcmC|hwC)\d+(?=D|/|$)", r"\1N", path)
    path = re.sub(r"\busb\d+", "usbN", path)
    path = re.sub(r"(^|/)\d+(-\d+(?:\.\d+)*(?::\d+\.\d+)?)(?=/|$)", r"\1N\2", path)
    path = re.sub(r"(tps6598x-source-psy-)\d+(-[0-9a-f]{4})", r"\1N\2", path)
    # I2C adapter number, not the chip address; HID instance, not vendor/product.
    path = re.sub(r"(^|/)\d+(-[0-9a-f]{4})(?=/|$)", r"\1N\2", path)
    path = re.sub(r"(\b[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4})\.[0-9A-Fa-f]+", r"\1.N", path)
    return path


def collect(classes, parent):
    """Raw class/name -> stable relative path and optional kernel name attribute."""
    out = {}
    for cls, name, real, _ in classes:
        if not real.startswith(parent + "/"):
            continue
        p = "/sys/class/%s/%s" % (cls, name)
        label = sysfs.read(p + "/name")
        if cls == "input" and not label:
            label = sysfs.read(p + "/device/name")
        out[cls + "/" + name] = {"path": normalize(real[len(parent) + 1:]),
                                  "name": normalize(label) if label else None}
    return out


def class_delta(old, new, ignore_plugged=False):
    """Return lost/gained raw names, matching multisets of stable identities.

    Use paths only when both records carry them; otherwise use class-specific
    names. Counts matter: two devices collapsing to one still loses a device.
    """
    detailed = all("class_device_ids" in s for s in (old, new))

    def entries(s):
        out = []
        for raw in s.get("class_devices") or []:
            if ignore_plugged and re.match(r"^typec/port\d+-(partner|cable|plug\d*)$", raw):
                continue
            info = s.get("class_device_ids", {}).get(raw) if detailed else None
            key = (raw.split("/", 1)[0], info["path"], info.get("name")) if info else (normalize(raw),)
            out.append((key, raw))
        return out

    a, b = entries(old), entries(new)

    def missing(left, right):
        available = Counter(key for key, _ in right)
        exact = Counter(right)
        unmatched = []
        for key, raw in left:
            if exact[key, raw]:
                exact[key, raw] -= 1
                available[key] -= 1
            else:
                unmatched.append((key, raw))
        result = []
        for key, raw in unmatched:
            if available[key]:
                available[key] -= 1
            else:
                result.append(raw)
        return sorted(result)

    return missing(a, b), missing(b, a)
