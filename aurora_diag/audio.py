# SPDX-License-Identifier: MIT
"""Audio and speaker protection: READ ONLY for protection, by construction.

This module only ever reads speakerguardd's and speakersafetyd's published
state and the ALSA control values. It never opens a PCM, so it cannot play
or capture audio. Writes to mixer controls go through control.py, whose
policy refuses every control that belongs to speaker protection (see
SPEAKER_PROTECTED there).
"""

import json
import glob
import os
import re
import shutil
import subprocess

from . import sysfs

SG_DIR = "/run/speakerguardd"
# speakerguardd v2 publishes the graph it installed (key=value lines) and one
# session file per seat user (space separated key=value pairs).
SGV2_GRAPH = "/run/speakerguardd-graph/state"
SGV2_SESSIONS = SG_DIR + "/*/helper/session"


def _systemctl_active(unit):
    if not shutil.which("systemctl"):
        return None
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def parse_kv(text, sep=None):
    """key=value pairs, one per line (sep None) or split on sep."""
    out = {}
    for tok in (text or "").split(sep) if sep else (text or "").splitlines():
        k, eq, v = tok.strip().partition("=")
        if eq and k:
            out[k] = v
    return out


def parse_speakerguardd_state(text):
    """Parse /run/speakerguardd/state. The '#' header line before each block
    names the fields, so new blocks or columns are picked up as they come."""
    out = {"meta": {}, "blocks": {}}
    if not text:
        return None
    lines = text.splitlines()
    out["meta"]["magic"] = lines[0] if lines else None
    header = {}
    for line in lines[1:]:
        if line == "end":
            break
        if line.startswith("#"):
            f = line[1:].split()
            if f:
                header[f[0]] = f[1:]
            continue
        f = line.split()
        if not f:
            continue
        if f[0] in header and len(f) - 1 == len(header[f[0]]):
            rec = {}
            for k, v in zip(header[f[0]], f[1:]):
                try:
                    rec[k] = float(v)
                except ValueError:
                    rec[k] = v
            out["blocks"].setdefault(f[0], []).append(rec)
        elif len(f) == 2:
            try:
                out["meta"][f[0]] = int(f[1])
            except ValueError:
                out["meta"][f[0]] = f[1]
    return out


def tail_jsonl(path, nbytes=65536):
    """The last complete JSON object of a growing JSON-lines file."""
    try:
        with open(sysfs.root(path), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


def speaker_protection():
    """Readings for the protection daemons (speakerguardd v2 and the older
    speakerguardd and speakersafetyd). Unreadable state (not root) is
    reported as such, never guessed."""
    out = []

    def rd(id_, attr, value, unit=None, label=None, path=None):
        out.append({"id": id_, "cat": "speaker", "dev": id_.split(".")[1], "attr": attr,
                    "label": label, "value": value, "unit": unit, "raw": str(value),
                    "path": path, "writable": False})

    for unit in ("speakerguardd-v2", "speakerguardd", "speakersafetyd"):
        rd("speaker.%s.service" % unit, "service", _systemctl_active(unit + ".service") or "unknown")

    if sysfs.exists(SGV2_GRAPH):
        text = sysfs.read(SGV2_GRAPH)
        if text is None:
            rd("speaker.speakerguardd-v2.graph", "graph", "unreadable (needs root)", path=SGV2_GRAPH)
        else:
            for k, v in parse_kv(text).items():
                if k != "user":
                    rd("speaker.speakerguardd-v2.graph.%s" % k, k, v, path=SGV2_GRAPH)
    for f in sorted(glob.glob(sysfs.root(SGV2_SESSIONS))):
        path = sysfs.unroot(f)
        uid = path.split("/")[3]
        text = sysfs.read(path)
        if text is None:
            rd("speaker.speakerguardd-v2.session.%s" % uid, "session", "unreadable (needs root)", path=path)
            continue
        for k, v in parse_kv(text, sep=" ").items():
            if k in ("links", "carrier", "amps", "wiring"):
                rd("speaker.speakerguardd-v2.session.%s.%s" % (uid, k), k, v, path=path)

    state_path = SG_DIR + "/state"
    if sysfs.exists(state_path):
        text = sysfs.read(state_path)
        if text is None:
            rd("speaker.speakerguardd.state", "state", "unreadable (needs root)", path=state_path)
        else:
            st = parse_speakerguardd_state(text)
            if st:
                for k, v in st["meta"].items():
                    rd("speaker.speakerguardd.%s" % k, k, v, path=state_path)
                for block, recs in st["blocks"].items():
                    for rec in recs:
                        name = str(rec.get("name", "?"))
                        for k, v in rec.items():
                            if k == "name":
                                continue
                            unit = None
                            m = re.search(r"_(C|W|V|dB)$", k)
                            if m:
                                unit = m.group(1)
                            rd("speaker.speakerguardd.%s.%s.%s" % (block, name, k), k, v, unit,
                               "%s %s" % (block, name), state_path)
    shadow = SG_DIR + "/shadow.jsonl"
    if sysfs.exists(shadow):
        last = tail_jsonl(shadow)
        if last is None:
            rd("speaker.speakerguardd.shadow", "shadow", "unreadable (needs root)", path=shadow)
        else:
            for k in ("t", "gov", "armed", "guard"):
                if k in last:
                    rd("speaker.speakerguardd.shadow.%s" % k, k, last[k], path=shadow)
            for spk in last.get("spk", []):
                n = str(spk.get("n", "?")).replace(" ", "_")
                for k, v in spk.items():
                    if k == "n":
                        continue
                    rd("speaker.speakerguardd.shadow.%s.%s" % (n, k), k,
                       v if not isinstance(v, list) else ",".join(str(x) for x in v), None,
                       "shadow " + n, shadow)
    # speakersafetyd keeps no live state file; its blackbox dumps show faults.
    bb = "/var/lib/speakersafetyd/blackbox"
    if sysfs.isdir(bb):
        rd("speaker.speakersafetyd.blackbox_dumps", "blackbox_dumps", len(sysfs.listdir(bb)), path=bb)
    return out


def alsa_controls():
    """ALSA cards and every control value (amixer, read only)."""
    out = []
    cards = sysfs.read("/proc/asound/cards") or ""
    ids = {}
    for c in sysfs.listdir("/proc/asound"):
        if re.fullmatch(r"card\d+", c):
            ids[c[4:]] = sysfs.read("/proc/asound/%s/id" % c)
    for num, cid in sorted(ids.items()):
        out.append({"id": "alsa.%s.card" % cid, "cat": "audio", "dev": cid, "attr": "card",
                    "label": None, "value": num, "unit": None, "raw": num,
                    "path": "/proc/asound/card%s" % num, "writable": False})
        if not shutil.which("amixer"):
            continue
        try:
            r = subprocess.run(["amixer", "-c", num, "contents"], capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            continue
        cur = None
        for line in r.stdout.splitlines():
            m = re.match(r"numid=(\d+),iface=(\w+),name='(.*)'", line)
            if m:
                cur = {"numid": m.group(1), "iface": m.group(2), "name": m.group(3)}
                continue
            if cur and line.strip().startswith(": values="):
                val = line.split("=", 1)[1]
                out.append({"id": "alsa.%s.%s" % (cid, cur["name"].replace(" ", "_")), "cat": "audio",
                            "dev": cid, "attr": cur["name"], "label": "numid=%s %s" % (cur["numid"], cur["iface"]),
                            "value": val, "unit": None, "raw": val, "path": "alsa:%s:%s" % (num, cur["numid"]),
                            "writable": True})
                cur = None
    if not cards:
        return out
    return out
