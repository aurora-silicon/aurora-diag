# SPDX-License-Identifier: MIT
"""Read the panic log that iBoot keeps for the next OS (read only).

When macOS panics, XNU writes an embedded panic record (magic 'FUNK') into
the firmware's panic region in DRAM. On the next boot iBoot copies that
region into a small namespace of the internal Apple NVMe drive, and macOS
reads it from there. Linux enumerates the same namespace as an ordinary
block device (on the MacBook Neo it is /dev/nvme0n3, 3 MiB), so the last
panic can be read from Linux without any driver support.

Layers, outermost first:

  1. iBoot's container (as seen in our own captures): a 4-byte magic
     'PEEP', a u32 entry count, then that many 24-byte entries
     {u32 type, u32 status flags, u64 offset, u64 length}. Entry type 0 holds
     the panic region copy. Other types are listed but not interpreted. A
     namespace may also hold a bare 'FUNK' record at offset 0.
  2. The embedded panic header, struct embedded_panic_header in the public
     XNU source (osfmk/kern/debug.h): magic, CRC-32 of everything from the
     version field to the end of the record, version, flags, then
     offset/length pairs for the panic text, the stackshot and the
     "other" log. Version 6 adds the OS strings and boot-session UUID,
     version 8 the device target/model strings. Offsets are always taken
     from the header, never from sizeof(): macOS 26.6 writes a version 8
     header whose panic text starts at byte 801.
  3. The panic text (plain text) and the other log (plain text). The
     stackshot is binary and only reported by size.

Nothing here ever writes to the device: it is opened O_RDONLY, and the tool
never issues an unmap, a trim or any NVMe command. Reading does not consume
the record; macOS clears it when macOS next boots.
"""

import datetime
import glob
import hashlib
import os
import re
import struct
import zlib

from . import sysfs

CONTAINER_MAGIC = b"PEEP"
FUNK = 0x46554E4B
MAX_ENTRIES = 16
MAX_NAMESPACE = 16 << 20

# struct embedded_panic_header (public XNU, osfmk/kern/debug.h)
HDR_FMT = "<IIIQIIIIII"
HDR_V6_SIZE = 177
HDR_V8_SIZE = 225

FLAGS = {
    0x01: "coredump-complete",
    0x02: "stackshot-succeeded",
    0x04: "stackshot-failed-debuggersync",
    0x08: "stackshot-failed-error",
    0x10: "stackshot-failed-incomplete",
    0x20: "stackshot-failed-nested",
    0x40: "nested-panic",
    0x80: "button-reset-panic",
    0x100: "companion-proc-initiated",
    0x200: "coredump-failed",
    0x400: "compress-failed",
    0x800: "stackshot-data-compressed",
    0x1000: "encrypted-coredump-skipped",
    0x2000: "coredump-skipped-exclude-regions-unavailable",
    0x4000: "corefile-unlinked",
    0x8000: "incoherent-paniclog",
    0x10000: "exclave-panic",
    0x20000: "userspace-initiated",
    0x40000: "integrated-coproc-initiated",
}


class Invalid(ValueError):
    pass


def _cstr(b):
    return b.split(b"\0", 1)[0].decode("utf-8", "replace")


def flag_names(flags):
    names = [n for bit, n in sorted(FLAGS.items()) if flags & bit]
    rest = flags & ~sum(FLAGS)
    if rest:
        names.append("0x%x" % rest)
    return names


def _segment(rec, off, length, name, hdr_end):
    if length == 0:
        return None
    if off < hdr_end or off + length > len(rec):
        raise Invalid("%s segment %d+%d outside the record (%d bytes, header %d)" % (name, off, length, len(rec), hdr_end))
    return (off, length)


def parse_record(rec):
    """Parse one embedded panic record (bytes starting at the 'FUNK' magic,
    as long as the region it was copied from). Raises Invalid."""
    if len(rec) < 44:
        raise Invalid("record shorter than the fixed header")
    (magic, crc, version, flags, p_off, p_len, s_off, s_len, o_off, o_len) = struct.unpack_from(HDR_FMT, rec, 0)
    if magic != FUNK:
        raise Invalid("no FUNK magic")
    if version < 1 or version > 64:
        raise Invalid("implausible header version %d" % version)
    hdr_end = 44
    out = {"version": version, "flags": flags, "flag_names": flag_names(flags), "crc": crc,
           "crc_ok": (zlib.crc32(rec[8:]) & 0xFFFFFFFF) == crc, "length": len(rec),
           "os_version": None, "macos_version": None, "boot_session_uuid": None,
           "device_target": None, "device_model": None}
    if version >= 2 and len(rec) >= HDR_V6_SIZE:
        out["os_version"] = _cstr(rec[52:84]) or None
        out["macos_version"] = _cstr(rec[84:116]) or None
        out["boot_session_uuid"] = _cstr(rec[116:153]) or None
        hdr_end = HDR_V6_SIZE
    if version >= 8 and len(rec) >= HDR_V8_SIZE:
        out["device_target"] = _cstr(rec[177:193]) or None
        out["device_model"] = _cstr(rec[193:225]) or None
        hdr_end = HDR_V8_SIZE
    segs = {}
    for name, off, length in (("panic", p_off, p_len), ("stackshot", s_off, s_len), ("other", o_off, o_len)):
        segs[name] = _segment(rec, off, length, name, hdr_end)
    if version >= 6 and len(rec) >= HDR_V6_SIZE:
        for name, at in (("extended", 161), ("initiator", 169)):
            off, length = struct.unpack_from("<II", rec, at)
            segs[name] = _segment(rec, off, length, name, hdr_end)
    used = sorted(v for v in segs.values() if v)
    for (a_off, a_len), (b_off, _) in zip(used, used[1:]):
        if a_off + a_len > b_off:
            raise Invalid("overlapping segments")
    if not segs["panic"]:
        raise Invalid("record has no panic text")
    text = lambda s: rec[s[0]:s[0] + s[1]].decode("utf-8", "replace") if s else ""
    out["panic_text"] = text(segs["panic"])
    out["other_text"] = text(segs["other"])
    out["stackshot_bytes"] = segs["stackshot"][1] if segs["stackshot"] else 0
    out["segments"] = {k: list(v) for k, v in segs.items() if v}
    out["time"] = panic_time(out["panic_text"])
    lines = out["panic_text"].splitlines()
    out["summary"] = lines[0] if lines else ""
    return out


def panic_time(text):
    """The panic's wall-clock time, from the 'Calendar:' line of XNU's
    'Epoch Time' block (seconds and microseconds in hex), as UTC ISO 8601."""
    m = re.search(r"^\s*Calendar\s*:\s*0x([0-9a-fA-F]+)\s+0x([0-9a-fA-F]+)", text, re.M)
    if not m:
        return None
    sec = int(m.group(1), 16)
    if not sec:
        return None
    return datetime.datetime.fromtimestamp(sec, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(buf):
    """Parse a whole namespace. Returns {"state": "empty"|"record"|"unknown",
    "entries": [...], "records": [...]}; never raises on garbage."""
    out = {"state": "unknown", "container": False, "entries": [], "records": [], "errors": []}
    if not buf.strip(b"\0"):
        out["state"] = "empty"
        return out
    if buf[:4] == CONTAINER_MAGIC:
        out["container"] = True
        count = struct.unpack_from("<I", buf, 4)[0] if len(buf) >= 8 else 0
        if not 1 <= count <= MAX_ENTRIES or 8 + 24 * count > len(buf):
            out["errors"].append("bad container entry count %d" % count)
            return out
        for i in range(count):
            etype, eflags, off, length = struct.unpack_from("<IIQQ", buf, 8 + 24 * i)
            ent = {"index": i, "type": etype, "status": eflags, "offset": off, "length": length}
            out["entries"].append(ent)
            if off < 8 + 24 * count or off + length > len(buf) or length == 0:
                ent["error"] = "outside the namespace"
                continue
            if etype == 0:
                try:
                    rec = parse_record(buf[off:off + length])
                    rec["entry"] = i
                    out["records"].append(rec)
                except Invalid as e:
                    ent["error"] = str(e)
                    out["errors"].append("entry %d: %s" % (i, e))
    elif struct.unpack_from("<I", buf, 0)[0] == FUNK:
        try:
            out["records"].append(parse_record(buf))
        except Invalid as e:
            out["errors"].append(str(e))
    else:
        return out
    out["state"] = "record" if out["records"] else "unknown"
    return out


def candidates():
    """Small, unpartitioned namespaces on an Apple NVMe controller other
    than namespace 1 (the main drive). The panic log's NSID is not fixed, so
    it is not hard-coded; the contents decide (see find())."""
    found = []
    for blk in sorted(glob.glob(sysfs.root("/sys/block/nvme*n*"))):
        name = os.path.basename(blk)
        nsid = sysfs.read("/sys/block/%s/nsid" % name)
        size = sysfs.read("/sys/block/%s/size" % name)
        if nsid is None or size is None or nsid.strip() == "1":
            continue
        nbytes = int(size) * 512
        if not 0 < nbytes <= MAX_NAMESPACE:
            continue
        if any(p.startswith(name + "p") for p in sysfs.listdir("/sys/block/%s" % name)):
            continue
        drv = os.path.basename(os.path.realpath(sysfs.root("/sys/block/%s/device/device/driver" % name)))
        if sysfs.exists("/sys/block/%s/device/device/driver" % name) and "apple" not in drv:
            continue
        found.append({"device": "/dev/" + name, "nsid": int(nsid), "bytes": nbytes})
    return found


def read_device(path, limit=MAX_NAMESPACE):
    fd = os.open(sysfs.root(path), os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        chunks, total = [], 0
        while total < limit:
            b = os.read(fd, min(1 << 20, limit - total))
            if not b:
                break
            chunks.append(b)
            total += len(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def find(device=None):
    """Read the panic-log namespace. Returns a result dict for the device
    that holds a record (or, if none does, every empty candidate)."""
    devs = [{"device": device, "nsid": None, "bytes": None}] if device else candidates()
    results = []
    for d in devs:
        res = dict(d)
        try:
            buf = read_device(d["device"])
        except OSError as e:
            res.update(state="error", error="%s: %s" % (d["device"], e.strerror or e))
            results.append(res)
            continue
        res["bytes"] = len(buf)
        res["sha256"] = hashlib.sha256(buf).hexdigest()
        res.update(parse(buf))
        results.append(res)
    with_rec = [r for r in results if r.get("state") == "record"]
    if with_rec:
        return with_rec
    return [r for r in results if r.get("state") in ("empty", "error")] or results


def save(rec, directory, raw=None):
    """Write the panic text (and the other log) of one record; raw=bytes
    also writes the whole namespace image. Files are 0600: panic text names
    the machine (serial-like identifiers, kernel addresses)."""
    os.makedirs(directory, exist_ok=True)
    tag = rec.get("boot_session_uuid") or "crc-%08x" % rec["crc"]
    written = []

    def put(name, data):
        path = os.path.join(directory, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        written.append(path)

    put("panic-%s.txt" % tag, rec["panic_text"].encode())
    if rec.get("other_text"):
        put("panic-%s-other.txt" % tag, rec["other_text"].encode())
    if raw is not None:
        put("panic-%s-namespace.bin" % tag, raw)
    return written


def describe(res, lines=12):
    """Human-readable lines for one find() result."""
    out = []
    dev = res.get("device")
    if res.get("state") == "error":
        return ["%s: cannot read (%s); reading the panic log needs root" % (dev, res.get("error"))]
    head = "%s (%s bytes%s)" % (dev, res.get("bytes"), ", NSID %s" % res["nsid"] if res.get("nsid") else "")
    if res.get("state") == "empty":
        return [head + ": empty, no panic since macOS last read it"]
    if res.get("state") != "record":
        return [head + ": not a panic log" + ("" if not res.get("errors") else " (%s)" % "; ".join(res["errors"]))]
    out.append(head + ": %d record(s)%s" % (len(res["records"]), " in an iBoot container" if res.get("container") else ""))
    for rec in res["records"]:
        out.append("  OS:            %s" % (rec.get("os_version") or "unknown"))
        out.append("  Time (UTC):    %s" % (rec.get("time") or "unknown"))
        out.append("  Boot session:  %s" % (rec.get("boot_session_uuid") or "unknown"))
        out.append("  Header:        v%d, flags %s, CRC %s" % (
            rec["version"], ",".join(rec["flag_names"]) or "none", "ok" if rec["crc_ok"] else "MISMATCH"))
        out.append("  Panic text:    %d bytes; stackshot %d bytes; other log %d bytes" % (
            len(rec["panic_text"]), rec["stackshot_bytes"], len(rec["other_text"])))
        for ln in rec["panic_text"].splitlines()[:lines]:
            out.append("  | " + ln)
    for e in res.get("entries", []):
        if e["type"] != 0 or e.get("error"):
            out.append("  container entry %d: type %d, status 0x%x, %d bytes%s" % (
                e["index"], e["type"], e["status"], e["length"], (", " + e["error"]) if e.get("error") else ""))
    return out
