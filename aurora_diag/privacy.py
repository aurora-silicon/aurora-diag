# SPDX-License-Identifier: MIT
"""Takes personal data out of anything meant to leave the machine.

`report` and `dj-check` write files that people attach to bugs. Before
they are written, every string in them goes through a Redactor: the host
name, the login and real names of the people on the Mac, home paths, MAC
and IP addresses, the serial number, Wi-Fi network names and (for
`report`) file system and partition UUIDs. Hardware facts, versions and
readings stay as they are.
"""

import os
import pwd
import re
import shutil
import socket
import subprocess

from . import sysfs


class Redactor:
    """Takes out the host name, user names, home paths, MAC and IP addresses
    and serial numbers before anything reaches the report. With uuids=True
    it also replaces UUIDs (root=UUID=..., partition and boot session ids)."""

    MAC = re.compile(r"\b[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\b")
    IPV4 = re.compile(r"\b(?!127\.0\.0\.1\b)(?:\d{1,3}\.){3}\d{1,3}\b")
    IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){3,7}[0-9a-fA-F]{1,4}\b")
    UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

    def __init__(self, names=(), hosts=(), secrets=(), ssids=(), uuids=False):
        self.words = []
        self.uuids = uuids
        for h in hosts:
            if h and h not in ("localhost", "fedora", "archlinux", "nixos"):
                self.words.append((h, "<host>"))
        for n in names:
            if n and len(n) >= 2 and n not in ("root", "gdm", "sddm", "nobody"):
                self.words.append((n, "<user>"))
        for s in secrets:
            if s and len(s) >= 6:
                self.words.append((s, "<serial>"))
        for s in ssids:
            if s and len(s) >= 2:
                self.words.append((s, "<ssid>"))
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
        if self.uuids:
            text = self.UUID.sub("<uuid>", text)
        text = self.MAC.sub("<mac>", text)
        text = self.IPV4.sub("<ip>", text)
        text = self.IPV6.sub("<ip6>", text)
        return text

    def tree(self, obj):
        """The same document with every string (keys too) redacted."""
        if isinstance(obj, dict):
            return {self(k) if isinstance(k, str) else k: self.tree(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.tree(v) for v in obj]
        return self(obj)


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


def _out(cmd, timeout=4):
    if not shutil.which(cmd[0]):
        return ""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def wifi_names():
    """The network the Mac is on and the networks NetworkManager remembers."""
    names = set()
    for line in _out(["iw", "dev"]).splitlines():
        m = re.match(r"\s*ssid (.+)$", line)
        if m:
            names.add(m.group(1).strip())
    for line in _out(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"]).splitlines():
        name, _, typ = line.rpartition(":")
        if typ.endswith("wireless") and name:
            names.add(name.replace("\\:", ":"))
    return sorted(names)


def serial():
    return (sysfs.read("/proc/device-tree/serial-number") or "").rstrip("\0").strip()


def for_report():
    """A Redactor for files that leave the machine."""
    host = socket.gethostname()
    return Redactor(local_names(), [host, host.split(".")[0]], [serial()], wifi_names(), uuids=True)
