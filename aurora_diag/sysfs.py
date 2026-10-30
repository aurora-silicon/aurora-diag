# SPDX-License-Identifier: MIT
"""Small, defensive helpers for reading the kernel's pseudo filesystems.

Every path goes through root(): tests point AURORA_DIAG_ROOT at a fake tree.
Reads never raise; they return None when a file is missing, unreadable or
does not answer in time (some debugfs and SMC-backed files can block).
"""

import os
import stat
import threading

ROOT = os.environ.get("AURORA_DIAG_ROOT", "/")


def root(path):
    """Map an absolute system path into the configured root."""
    if ROOT == "/":
        return path
    return os.path.join(ROOT, path.lstrip("/"))


def unroot(path):
    """Inverse of root(): the system path a rooted path stands for."""
    if ROOT == "/":
        return path
    rel = os.path.relpath(path, os.path.realpath(ROOT))
    return "/" + rel


def exists(path):
    return os.path.exists(root(path))


def listdir(path):
    try:
        return sorted(os.listdir(root(path)))
    except OSError:
        return []


def isdir(path):
    return os.path.isdir(root(path))


def read(path, limit=65536):
    """The file's text, stripped, or None."""
    try:
        with open(root(path), "rb") as f:
            data = f.read(limit)
    except OSError:
        return None
    return data.decode("utf-8", "replace").strip()


def read_error(path, limit=65536):
    """(text, None) or (None, 'why'): like read() but says why it failed."""
    try:
        with open(root(path), "rb") as f:
            data = f.read(limit)
    except OSError as e:
        return None, e.strerror or str(e)
    return data.decode("utf-8", "replace").strip(), None


def read_timeout(path, timeout=2.0, limit=65536):
    """read() in a worker thread so a blocking file cannot hang the tool."""
    result = [None]

    def work():
        result[0] = read(path, limit)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    return result[0] if not t.is_alive() else None


def read_int(path):
    v = read(path)
    if v is None:
        return None
    try:
        return int(v, 0)
    except ValueError:
        try:
            return int(float(v))
        except ValueError:
            return None


def read_float(path):
    v = read(path)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def mode(path):
    try:
        return stat.S_IMODE(os.stat(root(path)).st_mode)
    except OSError:
        return None


def writable(path):
    """Whether the kernel offers a store for this attribute (any w bit)."""
    m = mode(path)
    return m is not None and bool(m & 0o222)


def write(path, value):
    """Write a value the way `echo value > path` does. Raises OSError."""
    with open(root(path), "w") as f:
        f.write(str(value))


def realpath(path):
    return unroot(os.path.realpath(root(path)))


def basename_of_link(path):
    try:
        return os.path.basename(os.readlink(root(path)))
    except OSError:
        return None


def dt_strings(path):
    """A device-tree string-list property (NUL separated)."""
    try:
        with open(root(path), "rb") as f:
            data = f.read(4096)
    except OSError:
        return []
    return [s.decode("ascii", "replace") for s in data.split(b"\0") if s]


def uevent(path):
    out = {}
    text = read(path + "/uevent") or ""
    for line in text.splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def dt_value(path):
    """A device-tree property as text: a string list joined by spaces when
    it is printable text, else its big-endian 32-bit cells joined by dots
    (apple,firmware-version = <26 6 2> reads "26.6.2")."""
    try:
        with open(root(path), "rb") as f:
            data = f.read(4096)
    except OSError:
        return None
    if not data:
        return ""
    parts = data.rstrip(b"\0").split(b"\0")
    if data.endswith(b"\0") and all(p and all(32 <= c < 127 for c in p) for p in parts):
        return " ".join(p.decode("ascii") for p in parts)
    if len(data) % 4 == 0:
        return ".".join(str(int.from_bytes(data[i:i + 4], "big")) for i in range(0, len(data), 4))
    return data.hex()
