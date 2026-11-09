# SPDX-License-Identifier: MIT
"""dj-check: one read-only run that checks the published speaker fix,
collects what the speaker buzz investigation needs, checks the Touch ID
defaults and lists the AWDL / Wi-Fi / Wake-on-LAN pieces, then prints a
PASS/FAIL table and writes a redacted report the user can post.

Everything here only reads. It changes no setting, starts or stops no unit
and writes only the report files (in the invoking user's home, or --out).
The one exception is the optional listening step at the very end: it plays
a short test tone through the normal PipeWire output, and only after the
user answers y at the prompt. It never touches the speaker protection or
the volume.

Works on Fedora, Arch Linux ARM and NixOS (systemd, journalctl and either
rpm, pacman or nix-store). Run it as root (sudo): the kernel log, the ADT
and the audit log need it.
"""

import glob
import json
import math
import os
import platform
import pwd
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

from . import __version__

PASS, FAIL, WARN, INFO, SKIP = "PASS", "FAIL", "WARN", "INFO", "SKIP"
ORDER = {FAIL: 0, WARN: 1, PASS: 2, INFO: 3, SKIP: 4}
MIN_KERNEL_RELEASE = 27  # kernel-aurora / linux-aurora 7.3.0-27: MCA buffer bound again
LEGACY = ("speakersafetyd.service", "speakerguardd.service")
V2 = "speakerguardd-v2.service"
DAEMON_ERRORS = re.compile(r"panick|Panic!|hw_params|Out of memory|ENOMEM|Invalid sample rate|EPERM|"
                           r"Operation not permitted", re.I)
BOOTS = 8                 # earlier boots whose speaker journals go into the report
JOURNAL_LINES = 300       # per boot and unit group
UPGRADE_TIMEOUT = 900


# -- small helpers ----------------------------------------------------------

def run(cmd, timeout=30, user=None, env=None):
    """(rc, stdout+stderr) of a command; rc 127 when it is not installed,
    124 on a timeout. Never raises."""
    if user is not None:
        cmd = ["runuser", "-u", user["name"], "--", "env", "XDG_RUNTIME_DIR=%s" % user["runtime"],
               "DBUS_SESSION_BUS_ADDRESS=unix:path=%s/bus" % user["runtime"]] + list(cmd)
    if shutil.which(cmd[0]) is None:
        return 127, "%s: not installed" % cmd[0]
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                           env=env, stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or b"").decode("utf-8", "replace") + "\n(timed out after %d s)" % timeout
    except OSError as e:
        return 126, str(e)


def out(cmd, timeout=30, **kw):
    return run(cmd, timeout, **kw)[1].strip()


def read(path, limit=1 << 20, binary=False):
    try:
        with open(path, "rb") as f:
            d = f.read(limit)
    except OSError:
        return None
    return d if binary else d.decode("utf-8", "replace").strip()


def dt_strings(path):
    d = read(path, binary=True)
    return [s.decode("ascii", "replace") for s in d.split(b"\0") if s] if d else []


def unit_show(unit, *props):
    o = out(["systemctl", "show", "-p", ",".join(props), unit])
    return dict(line.split("=", 1) for line in o.splitlines() if "=" in line)


def unit_state(unit):
    s = unit_show(unit, "LoadState", "ActiveState", "SubState", "UnitFileState", "NRestarts", "Result")
    return s


# -- redaction ----------------------------------------------------------------

class Redactor:
    """Takes out the host name, user names, home paths, MAC and IP addresses
    and serial numbers before anything reaches the report."""

    MAC = re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b")
    IPV4 = re.compile(r"\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b")
    IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b")

    def __init__(self, names=(), hosts=(), secrets=()):
        self.words = []
        for h in hosts:
            if h and h not in ("localhost", "fedora", "archlinux", "nixos"):
                self.words.append((h, "<host>"))
        for n in names:
            if n and len(n) >= 2 and n not in ("root", "gdm", "sddm", "nobody"):
                self.words.append((n, "<user>"))
        for s in secrets:
            if s and len(s) >= 6:
                self.words.append((s, "<serial>"))
        # longest first, so "dj-mbp" goes before "dj"
        self.words.sort(key=lambda w: -len(w[0]))

    def __call__(self, text):
        if not isinstance(text, str):
            return text
        text = re.sub(r'("unit_id"\s*:\s*")[0-9a-fA-F]{16,}', r"\1<unit>", text)
        text = re.sub(r"/home/[^/\s:]+", "/home/<user>", text)
        text = re.sub(r"/run/user/\d+", "/run/user/<uid>", text)
        for w, rep in self.words:
            text = re.sub(r"(?<![\w-])%s(?![\w-])" % re.escape(w), rep, text)
        text = re.sub(r"[\w-]+(?:\.[\w-]+)*\.local\b", "<mdns>.local", text)
        text = self.MAC.sub("<mac>", text)
        text = self.IPV4.sub("<ip>", text)
        text = self.IPV6.sub("<ip6>", text)
        return text


NOLOGIN = ("nologin", "false", "sync", "shutdown", "halt")


def local_names():
    """Login names and real-name words of the people on this Mac. System and
    systemd DynamicUser accounts (speakersafetyd, nqptp, ...) are left alone:
    their names are unit names, not people."""
    names = []
    for p in pwd.getpwall():
        if not 1000 <= p.pw_uid < 60000 or os.path.basename(p.pw_shell or "") in NOLOGIN:
            continue
        names += [p.pw_name] + [g for g in re.split(r"[ ,]+", p.pw_gecos or "") if len(g) >= 3]
    return names


# -- distro / packages ----------------------------------------------------------

def distro():
    osr = {}
    for line in (read("/etc/os-release") or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            osr[k] = v.strip('"')
    ids = (osr.get("ID", "") + " " + osr.get("ID_LIKE", "")).split()
    kind = "nixos" if "nixos" in ids else "arch" if "arch" in ids or "archarm" in ids else \
        "fedora" if "fedora" in ids or shutil.which("rpm") else "unknown"
    return kind, osr.get("PRETTY_NAME") or osr.get("NAME") or "?"


class Packages:
    def __init__(self, kind):
        self.kind = kind
        self._nix = None

    def version(self, name):
        """Installed version of a package, or None."""
        if self.kind == "fedora":
            rc, o = run(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}\n", name])
            return o.strip().splitlines()[-1] if rc == 0 and o.strip() else None
        if self.kind == "arch":
            rc, o = run(["pacman", "-Q", name])
            return o.split()[1] if rc == 0 and len(o.split()) >= 2 else None
        if self.kind == "nixos":
            if self._nix is None:
                rc, o = run(["nix-store", "-qR", "/run/current-system"], timeout=60)
                self._nix = o.splitlines() if rc == 0 else []
            rx = re.compile(r"^/nix/store/[a-z0-9]{32}-%s-(\d[^/]*)$" % re.escape(name))
            for p in self._nix:
                m = rx.match(p)
                if m:
                    return m.group(1)
            return None
        return None

    def install_time(self, name):
        if self.kind == "fedora":
            rc, o = run(["rpm", "-q", "--qf", "%{INSTALLTIME}\n", name])
            try:
                return max(int(x) for x in o.split())
            except ValueError:
                return None
        if self.kind == "arch":
            rc, o = run(["pacman", "-Qi", name], env=dict(os.environ, LC_ALL="C"))
            m = re.search(r"^Install Date\s*:\s*(.+)$", o, re.M)
            if m:
                rc, s = run(["date", "-d", m.group(1).strip(), "+%s"])
                try:
                    return int(s.strip())
                except ValueError:
                    return None
        return None


def boot_time():
    for line in (read("/proc/stat") or "").splitlines():
        if line.startswith("btime "):
            return int(line.split()[1])
    return None


# -- ADT (read only; the same layout m1n1 documents) ----------------------------

def adt_path():
    for line in (read("/proc/mtd") or "").splitlines():
        p = line.split()
        if len(p) >= 4 and p[3].strip('"') == "adt":
            return "/dev/%sro" % p[0].rstrip(":")
    return None


def adt_parse(data, off=0, depth=0):
    if depth > 32 or off + 8 > len(data):
        raise ValueError("malformed ADT")
    nprops, nchildren = struct.unpack_from("<II", data, off)
    if nprops > 4096 or nchildren > 4096:
        raise ValueError("malformed ADT node")
    off += 8
    props = {}
    for _ in range(nprops):
        if off + 36 > len(data):
            raise ValueError("truncated ADT")
        name = data[off:off + 32].split(b"\0", 1)[0].decode("ascii", "replace")
        size = struct.unpack_from("<I", data, off + 32)[0] & 0x7FFFFFFF
        off += 36
        props[name] = data[off:off + size]
        off += (size + 3) & ~3
    children = []
    for _ in range(nchildren):
        c, off = adt_parse(data, off, depth + 1)
        children.append(c)
    return {"name": props.get("name", b"").split(b"\0", 1)[0].decode("ascii", "replace"),
            "props": props, "children": children}, off


def adt_audio():
    """{'target_type', 'acoustic_id', 'thiele_small': bool} from the ADT, or a reason."""
    p = adt_path()
    if not p:
        return None, "the boot chain exposes no ADT"
    d = read(p, 8 << 20, binary=True)
    if not d:
        return None, "cannot read %s" % p
    try:
        root, _ = adt_parse(d)
    except ValueError as e:
        return None, str(e)
    res = {"target_type": root["props"].get("target-type", b"").rstrip(b"\0").decode("ascii", "replace")}
    stack = [root]
    while stack:
        n = stack.pop()
        aid = n["props"].get("acoustic-id")
        if n["name"] == "audio" and isinstance(aid, bytes) and len(aid) == 4:
            res["acoustic_id"] = int.from_bytes(aid, "little")
            res["thiele_small"] = "speaker-thiele-small" in n["props"]
        stack += n["children"]
    return res, None


# -- the checks -----------------------------------------------------------------

class Check:
    def __init__(self, user, redact, kind):
        self.rows = []
        self.sections = []   # (title, text) appendix for the report
        self.user = user
        self.redact = redact
        self.pk = Packages(kind)
        self.kind = kind
        self.facts = {}

    def row(self, group, name, status, detail=""):
        self.rows.append({"group": group, "check": name, "status": status, "detail": self.redact(str(detail))})

    def appendix(self, title, text):
        self.sections.append((title, self.redact(text.rstrip() + "\n")))

    # (a) ------------------------------------------------------------------
    def kernel(self):
        g = "A speaker fix"
        rel = platform.release()
        self.facts["kernel"] = rel
        bt = boot_time() or 0
        if self.kind == "fedora":
            m = re.match(r"^\d+\.\d+\.\d+-(\d+)\.fc\d+\.aurora", rel)
            if m:
                n = int(m.group(1))
                self.row(g, "kernel-aurora >= -%d" % MIN_KERNEL_RELEASE, PASS if n >= MIN_KERNEL_RELEASE else FAIL,
                         "running %s" % rel + ("" if n >= MIN_KERNEL_RELEASE else ": update and reboot"))
            else:
                self.row(g, "kernel-aurora >= -%d" % MIN_KERNEL_RELEASE, WARN,
                         "running %s, not a kernel-aurora package (lab or custom build)" % rel)
            newest = self.pk.version("kernel-aurora-core") or self.pk.version("kernel-aurora")
            if newest:
                self.row(g, "kernel-aurora installed", INFO, newest)
        elif self.kind == "arch":
            v = self.pk.version("linux-aurora")
            m = re.search(r"-(\d+)(?:\.\d+)?$", v or "")
            owner = out(["pacman", "-Qoq", "/usr/lib/modules/%s" % rel])
            if not v or not m:
                self.row(g, "linux-aurora >= -%d" % MIN_KERNEL_RELEASE, WARN, "linux-aurora not installed, running %s" % rel)
            elif owner.splitlines()[:1] != ["linux-aurora"]:
                self.row(g, "linux-aurora >= -%d" % MIN_KERNEL_RELEASE, WARN,
                         "running %s is not from linux-aurora (%s)" % (rel, owner or "no owner"))
            else:
                n = int(m.group(1))
                it = self.pk.install_time("linux-aurora")
                pending = it and bt and it > bt
                st = FAIL if n < MIN_KERNEL_RELEASE else WARN if pending else PASS
                self.row(g, "linux-aurora >= -%d" % MIN_KERNEL_RELEASE, st,
                         "linux-aurora %s, running %s" % (v, rel) + (": installed after this boot, reboot" if pending else ""))
        elif self.kind == "nixos":
            booted = os.path.realpath("/run/booted-system/kernel")
            current = os.path.realpath("/run/current-system/kernel")
            self.row(g, "kernel (NixOS)", WARN if booted != current else INFO,
                     "running %s; NixOS kernels carry no release number, so the fix is checked by the "
                     "daemon rows below" % rel + ("; a newer system kernel is waiting for a reboot" if booted != current else ""))
        else:
            self.row(g, "kernel", INFO, "running %s (unknown distribution)" % rel)

    def sense_pcm(self):
        """(card number, card id, pcm device, the daemon udev starts for it)."""
        for i in range(20):
            pcms = sorted(glob.glob("/dev/snd/pcmC*D*c"))
            found = []
            for p in pcms:
                o = out(["udevadm", "info", "-q", "property", "-n", p])
                wants = re.findall(r"^SYSTEMD_WANTS=(.*)$", o, re.M)
                w = [u for u in " ".join(wants).split() if "speaker" in u]
                if w:
                    found.append((p, w))
            if found or i == 19:
                break
            time.sleep(1)
        if not found:
            # no udev hand-off: take device 2 capture, the macaudio sense PCM
            d2 = [p for p in sorted(glob.glob("/dev/snd/pcmC*D2c"))]
            found = [(d2[0], [])] if d2 else []
        if not found:
            return None
        p, w = found[0]
        m = re.search(r"pcmC(\d+)D(\d+)c", p)
        card, dev = int(m.group(1)), int(m.group(2))
        return {"card": card, "id": read("/proc/asound/card%d/id" % card), "dev": dev, "pcm": p, "wants": w}

    def speaker_fix(self):
        g = "A speaker fix"
        s = self.sense_pcm()
        self.facts["sense"] = s
        if not s:
            self.row(g, "speaker sense PCM", FAIL, "no speaker sense PCM (no macaudio sound card?)")
            return
        self.row(g, "speaker card", INFO, "%s, sense PCM %s" % (s["id"], s["pcm"]))
        st = {u: unit_state(u) for u in LEGACY + (V2,)}
        active = [u for u in LEGACY + (V2,) if st[u].get("ActiveState") == "active"]
        self.facts["units"] = st
        owner = active[0] if active else (s["wants"][0] if s["wants"] else None)
        self.facts["owner"] = owner
        label = "protection daemon active"
        if not active:
            self.row(g, label, FAIL, "none of %s is running (udev wants: %s)" % (
                ", ".join(LEGACY + (V2,)), " ".join(s["wants"]) or "nothing"))
        elif owner == V2:
            self.row(g, label, WARN, "speakerguardd v2 owns the speakers (not the published default; "
                     "the published stack runs speakersafetyd or speakerguardd 0.1)")
        else:
            self.row(g, label, PASS, "%s (%s)" % (owner, st[owner].get("SubState")))
        if owner:
            r = st[owner].get("NRestarts", "?")
            self.row(g, "%s restarts this boot" % owner.replace(".service", ""), PASS if r == "0" else FAIL, r)
            j = out(["journalctl", "-b", "-u", owner, "--no-pager", "-o", "short-monotonic"], 60)
            errs = [line for line in j.splitlines() if DAEMON_ERRORS.search(line)]
            self.row(g, "no ENOMEM / panic / hw_params error", PASS if not errs else FAIL,
                     "0 lines" if not errs else "%d lines, first: %s" % (len(errs), errs[0][-160:]))
        status = read("/proc/asound/card%d/pcm%dc/sub0/status" % (s["card"], s["dev"])) or "unreadable"
        first = status.splitlines()[0] if status else "?"
        self.row(g, "sense PCM open", PASS if first.startswith("state: RUNNING") else FAIL, first)
        k = out(["journalctl", "-k", "-b", "--no-pager", "-o", "cat"], 60)
        lines = [line for line in k.splitlines() if re.search(r"Speaker volumes (un)?locked|Speaker protection (granted|withdrawn|revoked)", line)]
        last = lines[-1] if lines else ""
        if owner == V2:
            ok = "protection granted" in last
            self.row(g, "speakers unlocked (v2 lease)", PASS if ok else FAIL, last.split(": ", 1)[-1] or "no lease line")
        else:
            ok = "Speaker volumes unlocked" in last
            self.row(g, "speaker unlock on", PASS if ok else FAIL,
                     (last.split(": ", 1)[-1] if last else "no unlock line: the kernel holds the speakers at the floor"))
        self.avc(g)
        f = out(["systemctl", "--failed", "--no-legend", "--plain"])
        failed = [line.split()[0] for line in f.splitlines() if line.strip()]
        self.row(g, "0 failed units", PASS if not failed else FAIL, "none" if not failed else " ".join(failed))
        for p in ("speakersafetyd", "speakerguardd", "aurora-audio", "asahi-audio", "speakersafetyd-aurora-profiles",
                  "pipewire", "wireplumber"):
            v = self.pk.version(p)
            if v:
                self.row(g, "package %s" % p, INFO, v)

    def avc(self, g):
        enforce = read("/sys/fs/selinux/enforce")
        if enforce is None:
            self.row(g, "0 new AVCs", SKIP, "SELinux is not enabled on this system")
            return
        mode = "Enforcing" if enforce == "1" else "Permissive"
        rc, o = run(["ausearch", "--input-logs", "-m", "AVC,USER_AVC,SELINUX_ERR", "-ts", "boot"], 60)
        if rc == 127:
            k = out(["journalctl", "-b", "--no-pager", "-o", "cat", "-g", "avc:  denied"], 60)
            n = len([x for x in k.splitlines() if "avc:" in x])
        else:
            n = len([x for x in o.splitlines() if x.startswith("type=")])
        self.row(g, "0 new AVCs", PASS if n == 0 else FAIL, "%d this boot (%s)" % (n, mode))

    # (b) ------------------------------------------------------------------
    def speaker_info(self):
        g = "B speaker info"
        comp = dt_strings("/proc/device-tree/compatible")
        self.facts["board"] = comp[0] if comp else None
        self.row(g, "board", INFO, "%s, %s" % (comp[0] if comp else "?", (read("/proc/device-tree/model") or "?").rstrip("\0")))
        snd = dt_strings("/proc/device-tree/sound/compatible")
        self.row(g, "macaudio", INFO, " ".join(snd) or "no /sound node")
        codecs = {}
        for c in glob.glob("/proc/device-tree/soc/i2c@*/codec@*/compatible") + glob.glob("/proc/device-tree/soc/*/i2c@*/codec@*/compatible"):
            for s in dt_strings(c)[:1]:
                codecs[s] = codecs.get(s, 0) + 1
        self.row(g, "speaker amps (DT)", INFO, ", ".join("%d x %s" % (n, c) for c, n in sorted(codecs.items())) or "none found")
        adt, why = adt_audio()
        if adt:
            aid = adt.get("acoustic_id")
            self.row(g, "AID (ADT acoustic-id)", INFO, ("AID%d" % aid if aid else "no audio acoustic-id") +
                     ", target %s, thiele-small %s" % (adt.get("target_type") or "?", "present" if adt.get("thiele_small") else "absent"))
            self.facts["aid"] = aid
        else:
            self.row(g, "AID (ADT acoustic-id)", INFO, "unavailable: %s" % why)
        sp = read("/proc/device-tree/chosen/asahi,speaker-id", binary=True)
        self.row(g, "speaker-id (/chosen)", INFO, str(int.from_bytes(sp, "big")) if sp and len(sp) == 4 else "not exported by this boot chain")
        for d in ("/lib/firmware/vendor/apple/audio-tuning", "/run/aurora-firmware/vendorfw/apple/audio-tuning"):
            m = read(d + "/manifest.json")
            if m:
                try:
                    j = json.loads(m)
                    self.row(g, "tuning bundle", INFO, "%s: %s board %s, macOS %s, speaker_id %s, %d files" % (
                        d, j.get("aid"), j.get("board"), j.get("macos_build"), j.get("speaker_id"), len(j.get("files") or [])))
                except ValueError:
                    self.row(g, "tuning bundle", INFO, "%s: unreadable manifest" % d)
                break
        else:
            self.row(g, "tuning bundle", INFO, "none installed (no /lib/firmware/vendor/apple/audio-tuning)")
        v2 = [p for p in ("speakerguardd-v2", "speakerguardd-v2-selinux", "aurora-speaker-upgrade") if self.pk.version(p)]
        self.row(g, "v2 packages", INFO, ", ".join("%s %s" % (p, self.pk.version(p)) for p in v2) or "none installed")
        gate = "/usr/libexec/speakerguardd-v2/speakerguardd-gate"
        if os.access(gate, os.X_OK):
            rc, o = run([gate, "explain", "--wait", "5"], 60)
            self.row(g, "speakerguardd-gate", INFO, o.strip().splitlines()[-1] if o.strip() else "rc %d" % rc)
        else:
            self.row(g, "speakerguardd-gate", INFO, "not installed")
        self.state_dirs(g)
        self.pipewire(g)
        self.journals(g)

    def state_dirs(self, g):
        txt = []
        for d in ("/var/lib/speakerguardd", "/var/lib/speakersafetyd", "/var/lib/aurora-speaker-upgrade"):
            if not os.path.isdir(d):
                continue
            txt.append(d + "/")
            for root, dirs, files in os.walk(d):
                dirs.sort()
                for f in sorted(files):
                    p = os.path.join(root, f)
                    try:
                        st = os.lstat(p)
                        txt.append("  %-40s %9d  %s" % (os.path.relpath(p, d), st.st_size,
                                                        time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))))
                    except OSError:
                        pass
        self.row(g, "speaker state dirs", INFO, "%d files (names and sizes in the report)" % len([t for t in txt if t.startswith("  ")]))
        self.appendix("speaker state directories (names and sizes only)", "\n".join(txt) or "none")

    def pipewire(self, g):
        if not self.user:
            self.row(g, "PipeWire speaker node", SKIP, "no desktop user session found")
            return
        rc, o = run(["pw-dump"], 30, user=self.user)
        try:
            objs = json.loads(o) if rc == 0 else []
        except ValueError:
            objs = []
        if not objs:
            self.row(g, "PipeWire speaker node", WARN, "pw-dump gave nothing (rc %d)" % rc)
            return
        keys = ("node.name", "node.description", "media.class", "audio.rate", "audio.format", "audio.channels",
                "audio.position", "clock.rate", "node.rate", "node.force-rate", "node.force-quantum", "node.latency",
                "node.lock-rate", "node.lock-quantum", "node.driver", "priority.driver", "resample.disable",
                "api.alsa.path", "api.alsa.period-size", "api.alsa.period-num", "api.alsa.headroom",
                "api.alsa.disable-batch", "factory.name", "filter.graph")
        txt, nodes = [], []
        for ob in objs:
            info = ob.get("info") or {}
            p = info.get("props") or ob.get("props") or {}
            name = p.get("node.name", "")
            desc = p.get("node.description", "")
            mc = p.get("media.class", "")
            if ob.get("type", "").endswith("Metadata"):
                if p.get("metadata.name") in ("settings", "default"):
                    md = {m.get("key"): m.get("value") for m in ob.get("metadata") or []
                          if str(m.get("key")).startswith(("clock.", "default.audio"))}
                    txt.append("%s metadata: %s" % (p.get("metadata.name"), json.dumps(md, sort_keys=True)))
                continue
            if not mc.startswith("Audio/Sink") and "alsa_output" not in name:
                continue
            low = (name + " " + desc).lower()
            if any(x in low for x in ("raop", "hdmi", "bluez", "headphone", "displayport")):
                continue
            if "speaker" not in low and "alsa_output" not in name and not name.startswith("audio_effect."):
                continue
            sel = {k: (p[k][:200] if isinstance(p.get(k), str) else p.get(k)) for k in keys if k in p}
            sel["state"] = info.get("state")
            sel["id"] = ob.get("id")
            fmt = [pp for pp in ((info.get("params") or {}).get("Format") or [])]
            if fmt:
                sel["format"] = fmt[0]
            nodes.append(sel)
            txt.append(json.dumps(sel, sort_keys=True))
        main = [n for n in nodes if "speaker" in (str(n.get("node.name")) + str(n.get("node.description"))).lower()
                and n.get("media.class") == "Audio/Sink"]
        self.facts["speaker_sinks"] = [n.get("node.name") for n in main]
        if main:
            n = main[0]
            self.row(g, "PipeWire speaker sink", INFO, "%s, state %s, rate %s, force-rate %s, latency %s" % (
                n.get("node.name"), n.get("state"), n.get("audio.rate") or (n.get("format") or {}).get("rate"),
                n.get("node.force-rate"), n.get("node.latency")))
        else:
            self.row(g, "PipeWire speaker sink", WARN, "no speaker sink in the session (%d output nodes)" % len(nodes))
        d = out(["wpctl", "inspect", "@DEFAULT_AUDIO_SINK@"], 15, user=self.user)
        m = re.search(r'node\.name = "([^"]+)"', d)
        self.facts["default_sink"] = m.group(1) if m else None
        self.row(g, "default output", INFO, m.group(1) if m else "unknown")
        rc, top = run(["pw-top", "-b", "-n", "6"], 20, user=self.user)
        lines = audio_top(top)
        errs = top_errors(lines)
        self.row(g, "pw-top xruns (6 s, idle)", INFO, "; ".join("%s ERR %d" % e for e in errs)[:300] if errs else "no ERR counts")
        self.appendix("PipeWire output nodes (pw-dump, filtered)", "\n".join(txt) or "none")
        self.appendix("pw-top -b -n 6 (idle)", "\n".join(lines[-80:]))
        ws = [x for x in out(["wpctl", "status"], 15, user=self.user).splitlines()
              if not re.search(r"raop|\.local\b|bluez|AirPlay", x, re.I)]
        self.appendix("wpctl status (network and Bluetooth devices left out)", "\n".join(ws))

    def journals(self, g):
        rc, o = run(["journalctl", "--list-boots", "--no-pager", "-o", "json"], 60)
        boots = []
        try:
            j = json.loads(o) if rc == 0 else []
            boots = [(b.get("index"), b.get("boot_id")) for b in j]
        except ValueError:
            for line in out(["journalctl", "--list-boots", "--no-pager"], 60).splitlines():
                f = line.split()
                if len(f) >= 2 and re.match(r"^-?\d+$", f[0]):
                    boots.append((int(f[0]), f[1]))
        boots = boots[-BOOTS:]
        self.row(g, "boots in the journal", INFO, "%d listed, the last %d go into the report" % (len(j) if rc == 0 and isinstance(j, list) else len(boots), len(boots)))
        units = []
        for u in ("speakersafetyd", "speakerguardd", "speakerguardd-v2*", "aurora-speakerguardd", "aurora-speaker-upgrade"):
            units += ["-u", u]
        summary = []
        for idx, bid in reversed(boots):
            if not bid:
                continue
            k = out(["journalctl", "-b", bid, "-k", "--no-pager", "-o", "short-monotonic", "-g",
                     "macaudio|speaker|tas27|cs42l|sn012776|mca"], 60)
            u = out(["journalctl", "-b", bid, "--no-pager", "-o", "short-monotonic"] + units, 60)
            kl, ul = k.splitlines(), u.splitlines()
            errs = len([x for x in ul if DAEMON_ERRORS.search(x)])
            v2 = len([x for x in ul if "speakerguardd-v2" in x])
            summary.append("boot %s: %d kernel speaker lines, %d daemon lines, %d error lines, %d v2 lines" % (idx, len(kl), len(ul), errs, v2))
            self.appendix("boot %s: kernel speaker lines" % idx, "\n".join(kl[-JOURNAL_LINES:]) or "none")
            self.appendix("boot %s: speaker protection units" % idx, "\n".join(ul[-JOURNAL_LINES:]) or "none")
        self.appendix("journal summary per boot", "\n".join(summary))
        v2boots = [s for s in summary if not s.endswith(" 0 v2 lines")]
        self.row(g, "boots with speakerguardd v2", INFO, "%d of %d" % (len(v2boots), len(summary)))

    # (c) ------------------------------------------------------------------
    def touchid(self):
        g = "C Touch ID"
        conf = read("/etc/aurora/touchid.conf")
        rearm = self.pk.version("aurora-touchid-rearm")
        if conf is None and not rearm:
            self.row(g, "Touch ID userspace", WARN, "aurora-touchid-rearm is not installed (no /etc/aurora/touchid.conf)")
        else:
            val = None
            for line in (conf or "").splitlines():
                m = re.match(r"^\s*require_password_after_boot\s*=\s*(\S+)", line)
                if m:
                    val = m.group(1).lower()
            eff = "no" if val == "no" else "yes"
            self.row(g, "password first after boot", PASS if eff == "yes" else WARN,
                     ("default (yes)" if val is None else "set to %s" % val) +
                     ("" if eff == "yes" else ": Touch ID is allowed at the first login (not the macOS default)"))
            self.row(g, "aurora-touchid-rearm", INFO, rearm or "version unknown")
        for u in ("aurora-touchid-boot.service", "aurora-touchid-rearm.socket", "aurora-sep.timer",
                  "aurora-sep.service", "aurora-sep-ready.service", "fprintd.service"):
            s = unit_state(u)
            if s.get("LoadState") != "loaded":
                self.row(g, u, WARN if u.startswith(("aurora-touchid-boot", "fprintd", "aurora-sep.service")) else INFO,
                         "not installed")
                continue
            failed = s.get("ActiveState") == "failed" or s.get("Result") not in (None, "", "success")
            self.row(g, u, FAIL if failed else PASS, "%s/%s, %s, restarts %s" % (
                s.get("ActiveState"), s.get("SubState"), s.get("UnitFileState") or "-", s.get("NRestarts", "-")))
        pam = sorted(os.path.basename(p) for p in glob.glob("/etc/pam.d/*") if "pam_fprintd" in (read(p) or ""))
        self.row(g, "PAM stacks with pam_fprintd", INFO, ", ".join(pam) or "none")
        for p in ("fprintd", "libfprint", "aurora-sep"):
            v = self.pk.version(p)
            if v:
                self.row(g, "package %s" % p, INFO, v)
        j = out(["journalctl", "-b", "--no-pager", "-o", "short-monotonic", "-u", "aurora-touchid-boot",
                 "-u", "aurora-sep*", "-u", "fprintd", "-u", "aurora-touchid-rearm*"], 60)
        self.appendix("Touch ID units this boot", "\n".join(j.splitlines()[-200:]) or "none")

    # (d) ------------------------------------------------------------------
    def radios(self):
        g = "D Wi-Fi / AWDL / WoL"
        for p in ("omdrop-awdl", "NetworkManager", "networkmanager", "wpa_supplicant", "wpa-supplicant", "iwd", "ethtool", "iw"):
            v = self.pk.version(p)
            if v:
                self.row(g, "package %s" % p, INFO, v)
        if not self.pk.version("omdrop-awdl"):
            self.row(g, "package omdrop-awdl", INFO, "not installed (opt-in)")
        for u in ("awdl0.service", "awdl-warmup.service"):
            s = unit_state(u)
            if s.get("LoadState") == "loaded":
                self.row(g, u, INFO, "%s, %s" % (s.get("ActiveState"), s.get("UnitFileState")))
        for n in sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []:
            if n == "lo":
                continue
            drv = os.path.basename(os.path.realpath("/sys/class/net/%s/device/driver" % n)) if os.path.exists("/sys/class/net/%s/device/driver" % n) else "virtual"
            if drv == "virtual":
                continue
            wifi = os.path.isdir("/sys/class/net/%s/wireless" % n) or os.path.exists("/sys/class/net/%s/phy80211" % n)
            if wifi:
                phy = os.path.basename(os.path.realpath("/sys/class/net/%s/phy80211" % n))
                ww = out(["iw", "phy", phy, "wowlan", "show"], 10)
                ww = "WoWLAN state not readable (driver says not supported)" if "not supported" in ww else ww
                info = out(["iw", "phy", phy, "info"], 10)
                sup = "WoWLAN support listed" if "WoWLAN support" in info else "no WoWLAN support listed"
                fw = re.search(r"firmware-version:\s*(.*)", out(["ethtool", "-i", n], 10))
                self.row(g, "Wi-Fi %s" % n, INFO, "%s, %s, %s%s" % (drv, sup, ww.splitlines()[0] if ww else "iw not installed",
                                                                   ", fw %s" % fw.group(1)[:60] if fw else ""))
            else:
                e = out(["ethtool", n], 10)
                wol = re.findall(r"(Supports Wake-on|Wake-on):\s*(\S+)", e)
                self.row(g, "Ethernet %s" % n, INFO, "%s, %s" % (drv, ", ".join("%s %s" % w for w in wol) or "no Wake-on line"))

    # (e) ------------------------------------------------------------------
    def upgrade(self):
        g = "E v2 upgrade dry run"
        exe = shutil.which("aurora-speaker-upgrade") or ("/usr/bin/aurora-speaker-upgrade" if os.access("/usr/bin/aurora-speaker-upgrade", os.X_OK) else None)
        if not exe:
            self.row(g, "aurora-speaker-upgrade", SKIP, "not installed (nothing to dry-run)")
            return
        rc, h = run([exe, "--help"], 30)
        if "--check" in h:
            jf = tempfile.NamedTemporaryFile(prefix="dj-check-upgrade-", suffix=".json", delete=False)
            jf.close()
            rc, o = run([exe, "--check", "--json", jf.name], UPGRADE_TIMEOUT)
            res = {0: (PASS, "pass"), 1: (INFO, "refused (the legacy protection stays)"),
                   2: (INFO, "cannot run here"), 3: (WARN, "internal error")}.get(rc, (WARN, "exit %d" % rc))
            last = [x for x in o.strip().splitlines() if x.strip()]
            self.row(g, "aurora-speaker-upgrade --check", res[0], "%s: %s" % (res[1], last[-1][:220] if last else ""))
            self.appendix("aurora-speaker-upgrade --check output", o + "\n" + (read(jf.name) or ""))
            os.unlink(jf.name)
        else:
            rc, o = run([exe, "status"], 60)
            self.row(g, "aurora-speaker-upgrade status", INFO, "this version has no --check; status rc %d" % rc)
            self.appendix("aurora-speaker-upgrade status", o)


# -- pw-top -------------------------------------------------------------------------

TOP_KEEP = re.compile(r"speaker|sgv2|alsa_|audio_effect|effect_|Driver|convolver|protect|voicing|pw-play|pw-cat", re.I)


def audio_top(text):
    """pw-top batch lines for the audio graph only (no network sinks, cameras, MIDI)."""
    keep = []
    for line in text.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "S" and len(f) > 1 and f[1] == "ID":
            keep.append(line)
        elif TOP_KEEP.search(f[-1]) and not re.search(r"raop|bluez|v4l2|midi", f[-1], re.I):
            keep.append(line)
    return keep


def top_errors(lines):
    """[(node, highest ERR count)] from pw-top batch lines, ERR > 0 only."""
    best = {}
    for line in lines:
        f = line.split()
        if len(f) >= 10 and f[0] in ("R", "S", "I", "C") and f[1].isdigit():
            try:
                n = int(f[8])
            except ValueError:
                continue
            if n > 0:
                best[f[-1]] = max(best.get(f[-1], 0), n)
    return sorted(best.items())


# -- user session ------------------------------------------------------------------

def find_user(name=None):
    """The desktop user whose PipeWire session plays the tone."""
    cands = [name] if name else []
    cands += [os.environ.get("SUDO_USER"), os.environ.get("PKEXEC_UID") and pwd.getpwuid(int(os.environ["PKEXEC_UID"])).pw_name]
    o = out(["loginctl", "list-sessions", "--no-legend"])
    for line in o.splitlines():
        f = line.split()
        if len(f) >= 3 and f[2] not in ("gdm", "sddm", "root", "lightdm"):
            cands.append(f[2])
    for c in cands:
        if not c or c == "root":
            continue
        try:
            p = pwd.getpwnam(c)
        except KeyError:
            continue
        rt = "/run/user/%d" % p.pw_uid
        return {"name": p.pw_name, "uid": p.pw_uid, "home": p.pw_dir, "runtime": rt,
                "session": os.path.exists(rt + "/pipewire-0")}
    return None


# -- the optional listening step ----------------------------------------------------

def tone_file(path, rate=48000, amp=0.25):
    """1 kHz, 200 Hz, then a 100 Hz-10 kHz sweep, 16 s, at -12 dBFS."""
    frames = []
    parts = [("tone", 1000, 3), ("tone", 200, 3), ("sweep", (100, 10000), 8), ("tone", 1000, 2)]
    for kind, f, secs in parts:
        n = int(rate * secs)
        ph = 0.0
        for i in range(n):
            if kind == "tone":
                fr = f
            else:
                fr = f[0] * (f[1] / f[0]) ** (i / n)
            ph += 2 * math.pi * fr / rate
            fade = min(1.0, i / (0.02 * rate), (n - i) / (0.02 * rate))
            v = int(32767 * amp * fade * math.sin(ph))
            frames.append(struct.pack("<hh", v, v))
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(frames))


class Tty:
    """The controlling terminal, for the prompts (stdin may be a pipe: curl | sh).
    A tty is not seekable, so it is opened once to read and once to write."""

    def __init__(self):
        self.r = open("/dev/tty", "r")
        self.w = open("/dev/tty", "w")

    def write(self, s):
        self.w.write(s)

    def flush(self):
        self.w.flush()

    def readline(self):
        return self.r.readline()


def ask(prompt, tty):
    if tty is None:
        return ""
    tty.write(prompt)
    tty.flush()
    line = tty.readline()
    return line.strip()


def listening(chk, tty):
    g = "Listening (manual)"
    if tty is None:
        chk.row(g, "listening step", SKIP, "no terminal to ask on")
        return
    u = chk.user
    if not u or not u["session"]:
        chk.row(g, "listening step", SKIP, "no desktop PipeWire session")
        return
    tty.write("\n=== The one manual step: listen for buzz or crackle ===\n"
              "This plays a 16 s test tone (1 kHz, 200 Hz, then a sweep) through your normal output, three times.\n"
              "1. Turn the volume down to about 10%% first. The default output now is: %s\n"
              "2. While it plays, raise the volume slowly with the keyboard keys, up to where you normally listen.\n"
              "3. Listen for buzz, crackle or clicks, especially at the start of each tone and during the sweep.\n"
              % (chk.facts.get("default_sink") or "unknown"))
    ds = chk.facts.get("default_sink") or ""
    if ds not in (chk.facts.get("speaker_sinks") or []) and "speaker" not in ds.lower():
        tty.write("NOTE: the default output does not look like the built-in speakers. Pick the speakers in the sound\n"
                  "settings first if you want to test them.\n")
    a = ask("Play the test tone now? [y/N] ", tty)
    if a.lower() not in ("y", "yes"):
        chk.row(g, "listening step", SKIP, "skipped by the user")
        return
    d = tempfile.mkdtemp(prefix="dj-check-tone-")
    os.chmod(d, 0o755)
    f = os.path.join(d, "tone.wav")
    tone_file(f)
    os.chmod(f, 0o644)
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    top = {}

    def watch():
        top["out"] = run(["pw-top", "-b", "-n", "55"], 70, user=u)[1]
    t = threading.Thread(target=watch, daemon=True)
    t.start()
    for i in range(3):
        rc, o = run(["pw-play", f], 40, user=u)
        if rc != 0:
            chk.row(g, "test tone", WARN, "pw-play failed: %s" % o.strip()[-200:])
            break
    t.join(75)
    shutil.rmtree(d, ignore_errors=True)
    heard = ask("Did you hear buzz, crackle or clicks? [y/n] ", tty).lower()
    note = ask("If yes: at roughly what volume, and during which part (1 kHz, 200 Hz, sweep)? (Enter to skip) ", tty)
    chk.row(g, "buzz or crackle heard", FAIL if heard.startswith("y") else PASS if heard.startswith("n") else INFO,
            ("yes" if heard.startswith("y") else "no" if heard.startswith("n") else "no answer") + (": " + note if note else ""))
    owner = chk.facts.get("owner")
    if owner:
        chk.appendix("protection daemon during the tone", out(["journalctl", "--since", since, "-u", owner, "--no-pager",
                                                               "-o", "short-monotonic"], 30))
    lines = audio_top(top.get("out") or "")
    errs = top_errors(lines)
    chk.row(g, "pw-top xruns during the tone", INFO, "; ".join("%s ERR %d" % e for e in errs)[:300] if errs else "no ERR counts")
    chk.appendix("pw-top during the tone", "\n".join(lines[-300:]))


# -- output ---------------------------------------------------------------------

def table(rows, width=None):
    width = width or shutil.get_terminal_size((110, 20)).columns
    w1 = min(max([len(r["check"]) for r in rows] + [10]), 40)
    lines, group = [], None
    for r in rows:
        if r["group"] != group:
            group = r["group"]
            lines.append("")
            lines.append(group)
        d = r["detail"].replace("\n", " ")
        room = max(30, width - w1 - 10)
        lines.append("  %-4s  %-*s  %s" % (r["status"], w1, r["check"][:w1], d if len(d) <= room else d[:room - 3] + "..."))
    return "\n".join(lines) + "\n"


def verdict(rows):
    gating = [r for r in rows if r["group"].startswith(("A ", "C ")) or r["group"].startswith("Listening")]
    if any(r["status"] == FAIL for r in gating):
        return FAIL
    if any(r["status"] == WARN for r in rows):
        return WARN
    return PASS


def run_check(args):
    if os.geteuid() != 0:
        print("dj-check needs root for the kernel log, the ADT and the audit log: run it with sudo", file=sys.stderr)
        return 2
    kind, pretty = distro()
    user = find_user(getattr(args, "user", None))
    host = socket.gethostname()
    serial = (read("/proc/device-tree/serial-number") or "").rstrip("\0")
    red = Redactor(local_names() + ([user["name"]] if user else []), [host, host.split(".")[0]], [serial])
    chk = Check(user, red, kind)
    t0 = time.time()
    try:
        tty = None if args.no_listen else Tty()
    except OSError:
        tty = None
    say = (lambda s: (tty.write(s), tty.flush())) if tty else (lambda s: (sys.stderr.write(s), sys.stderr.flush()))
    steps = [("speaker fix", lambda: (chk.kernel(), chk.speaker_fix())), ("speaker info", chk.speaker_info),
             ("Touch ID", chk.touchid), ("Wi-Fi / AWDL / WoL", chk.radios), ("v2 upgrade dry run", chk.upgrade)]
    for name, fn in steps:
        say("dj-check: %s ...\n" % name)
        try:
            fn()
        except Exception as e:  # a broken probe must not hide the others
            chk.row("Z internal", name, WARN, "probe error: %s: %s" % (type(e).__name__, e))
    manual = ("\nTouch ID, one manual check (no tool can do it):\n"
              "  After a restart, lock the screen (Super+L). Use your PASSWORD first: the lock screen should ask for it.\n"
              "  Then lock again and use Touch ID: it should unlock.\n")

    def render(width=None):
        rows = sorted(chk.rows, key=lambda r: r["group"])
        v = verdict(rows)
        head = ("aurora-diag dj-check %s\n%s, kernel %s, %s\nrun %s, %.0f s\nRESULT: %s\n" % (
            __version__, pretty, platform.release(), chk.facts.get("board") or "?",
            time.strftime("%Y-%m-%d %H:%M:%S %z"), time.time() - t0, v))
        return rows, v, red(head) + table(rows, width) + manual

    sys.stdout.write(render()[2])
    sys.stdout.flush()
    # The one manual listening step comes last, after the table.
    if not args.no_listen:
        listening(chk, tty)
        lst = [r for r in chk.rows if r["group"].startswith("Listening")]
        if lst:
            sys.stdout.write("\n" + table(lst))
    rows, v, full = render(width=10000)
    base = args.out or (os.path.join(user["home"], "aurora-dj-check-%s" % time.strftime("%Y%m%d-%H%M%S")) if user else
                        "/var/tmp/aurora-dj-check-%s" % time.strftime("%Y%m%d-%H%M%S"))
    rep = full + "\n\n" + "".join("\n===== %s =====\n%s" % (t, b) for t, b in chk.sections)
    for ext, data in (("txt", rep), ("json", json.dumps({"version": __version__, "result": v, "distro": pretty,
                                                           "kernel": platform.release(), "board": chk.facts.get("board"),
                                                           "rows": rows}, indent=1))):
        p = "%s.%s" % (base, ext)
        with open(p, "w") as f:
            f.write(data)
        if user and p.startswith(user["home"] + "/"):
            os.chown(p, user["uid"], -1)
    print("\nRESULT: %s" % v)
    print("Report: %s.txt (send this file; it has no user or host names, MAC or IP addresses)" % base)
    return 0 if v == PASS else 1
