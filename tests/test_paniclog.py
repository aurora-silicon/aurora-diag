# SPDX-License-Identifier: MIT
"""aurora-diag panic-log: synthetic panic records only (no real dumps:
real ones carry serial numbers and ECIDs)."""

import io
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from aurora_diag import cli, paniclog, sysfs  # noqa: E402

UUID = "0F0E0D0C-0B0A-4908-8706-050403020100"
TEXT = ("panic(cpu 3 caller 0xfffffe0012345678): synthetic test panic\n"
        "Debugger message: panic\n"
        "OS version: 99A1\n"
        "Epoch Time:        sec       usec\n"
        "  Boot    : 0x6aeff800 0x00000000\n"
        "  Calendar: 0x6aeff8b2 0x000f0ba0\n"
        "last line\n")


def record(length=0x4000, version=8, text=TEXT, text_at=None, flags=0x802, stackshot=b"\x01\x02" * 50,
           other="** Stackshot Succeeded **\n", os_version="99A1 (test)", corrupt_crc=False):
    """An embedded panic record as XNU lays it out (public osfmk/kern/debug.h)."""
    rec = bytearray(length)
    hdr = {6: 177, 8: 225}.get(version, 177)
    po = text_at if text_at is not None else hdr
    t = text.encode()
    so = po + len(t)
    oo = so + len(stackshot)
    struct.pack_into("<IIIQIIIIII", rec, 0, paniclog.FUNK, 0, version, flags, po, len(t),
                     so if stackshot else 0, len(stackshot), oo, len(other))
    rec[52:84] = os_version.encode().ljust(32, b"\0")
    rec[116:153] = UUID.encode().ljust(37, b"\0")
    if version >= 8:
        rec[177:193] = b"J000AP".ljust(16, b"\0")
        rec[193:225] = b"Mac00,0".ljust(32, b"\0")
    rec[po:po + len(t)] = t
    rec[so:so + len(stackshot)] = stackshot
    rec[oo:oo + len(other)] = other.encode()
    crc = zlib.crc32(bytes(rec[8:])) & 0xFFFFFFFF
    struct.pack_into("<I", rec, 4, crc ^ (1 if corrupt_crc else 0))
    return bytes(rec)


def container(rec, extra=((4, 2, b"\0" * 11), (2, 2, b"\0" * 11)), size=0x10000, count=None):
    entries = [(0, 1, rec)] + list(extra)
    off = 8 + 24 * len(entries)
    off = max(off, 0x50)
    buf = bytearray(size)
    buf[0:4] = paniclog.CONTAINER_MAGIC
    struct.pack_into("<I", buf, 4, len(entries) if count is None else count)
    for i, (t, f, data) in enumerate(entries):
        struct.pack_into("<IIQQ", buf, 8 + 24 * i, t, f, off, len(data))
        buf[off:off + len(data)] = data
        off += len(data)
    return bytes(buf)


def put(path, data):
    with open(path, "wb" if isinstance(data, bytes) else "w") as f:
        f.write(data)


def get(path):
    with open(path, "rb") as f:
        return f.read()


class TestRecord(unittest.TestCase):
    def test_v8_record_fields(self):
        r = paniclog.parse_record(record())
        self.assertEqual(r["version"], 8)
        self.assertTrue(r["crc_ok"])
        self.assertEqual(r["os_version"], "99A1 (test)")
        self.assertEqual(r["boot_session_uuid"], UUID)
        self.assertEqual(r["device_target"], "J000AP")
        self.assertEqual(r["flag_names"], ["stackshot-succeeded", "stackshot-data-compressed"])
        self.assertEqual(r["stackshot_bytes"], 100)
        self.assertEqual(r["other_text"], "** Stackshot Succeeded **\n")
        self.assertEqual(r["time"], "2026-11-08T04:21:06Z")
        self.assertTrue(r["summary"].startswith("panic(cpu 3"))

    def test_v6_and_offsets_come_from_the_header(self):
        r = paniclog.parse_record(record(version=6))
        self.assertEqual(r["version"], 6)
        self.assertIsNone(r["device_target"])
        # macOS 26.6 writes a v8 header with its panic text at byte 801
        r = paniclog.parse_record(record(text_at=801))
        self.assertEqual(r["segments"]["panic"][0], 801)
        self.assertTrue(r["panic_text"].startswith("panic("))

    def test_crc_mismatch_is_reported_not_hidden(self):
        r = paniclog.parse_record(record(corrupt_crc=True))
        self.assertFalse(r["crc_ok"])

    def test_bad_records(self):
        good = record()
        with self.assertRaises(paniclog.Invalid):
            paniclog.parse_record(b"\0" * 64)
        with self.assertRaises(paniclog.Invalid):
            paniclog.parse_record(good[:40])
        bad = bytearray(good)
        struct.pack_into("<I", bad, 24, 0x7FFFFFFF)          # panic length past the end
        with self.assertRaises(paniclog.Invalid):
            paniclog.parse_record(bytes(bad))
        bad = bytearray(good)
        struct.pack_into("<I", bad, 20, 10)                   # panic text inside the header
        with self.assertRaises(paniclog.Invalid):
            paniclog.parse_record(bytes(bad))
        bad = bytearray(good)
        po, pl = struct.unpack_from("<II", bad, 20)
        struct.pack_into("<I", bad, 28, po + 1)               # stackshot overlaps the text
        with self.assertRaises(paniclog.Invalid):
            paniclog.parse_record(bytes(bad))
        bad = bytearray(good)
        struct.pack_into("<I", bad, 24, 0)                    # no panic text
        with self.assertRaises(paniclog.Invalid):
            paniclog.parse_record(bytes(bad))


class TestNamespace(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(paniclog.parse(b"\0" * 4096)["state"], "empty")

    def test_container(self):
        res = paniclog.parse(container(record()))
        self.assertEqual(res["state"], "record")
        self.assertTrue(res["container"])
        self.assertEqual([e["type"] for e in res["entries"]], [0, 4, 2])
        self.assertEqual(res["records"][0]["entry"], 0)

    def test_bare_record(self):
        res = paniclog.parse(record() + b"\0" * 1024)
        self.assertEqual(res["state"], "record")
        self.assertFalse(res["container"])

    def test_garbage_and_bad_containers(self):
        self.assertEqual(paniclog.parse(b"\x30\x83" + b"\x11" * 100)["state"], "unknown")
        res = paniclog.parse(container(record(), count=200))
        self.assertEqual(res["state"], "unknown")
        self.assertTrue(res["errors"])
        buf = bytearray(container(record()))
        struct.pack_into("<Q", buf, 8 + 16, 1 << 40)        # entry 0 length past the namespace
        res = paniclog.parse(bytes(buf))
        self.assertEqual(res["state"], "unknown")
        self.assertIn("error", res["entries"][0])

    def test_no_panic_time_without_calendar(self):
        r = paniclog.parse_record(record(text="panic(cpu 0 caller 0x0): x\n"))
        self.assertIsNone(r["time"])


class TestDevice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self._root = sysfs.ROOT
        sysfs.ROOT = self.root

    def tearDown(self):
        sysfs.ROOT = self._root
        self.tmp.cleanup()

    def ns(self, name, nsid, data, parts=(), driver="nvme-apple"):
        blk = os.path.join(self.root, "sys/block", name)
        os.makedirs(blk)
        put(os.path.join(blk, "nsid"), "%d\n" % nsid)
        put(os.path.join(blk, "size"), "%d\n" % (len(data) // 512))
        for p in parts:
            os.makedirs(os.path.join(blk, p))
        drv = os.path.join(self.root, "sys/bus/platform/drivers", driver)
        os.makedirs(drv, exist_ok=True)
        os.makedirs(os.path.join(blk, "device/device"))
        os.symlink(drv, os.path.join(blk, "device/device/driver"))
        dev = os.path.join(self.root, "dev")
        os.makedirs(dev, exist_ok=True)
        put(os.path.join(dev, name), data)

    def test_discovery_picks_the_namespace_with_a_record(self):
        self.ns("nvme0n1", 1, b"\0" * 8192, parts=("nvme0n1p1",))
        self.ns("nvme0n2", 2, b"\x30\x83" + b"\x11" * 8190)
        self.ns("nvme0n3", 3, container(record()))
        self.ns("nvme0n4", 4, b"\0" * (32 << 20))            # too big
        self.assertEqual([c["device"] for c in paniclog.candidates()], ["/dev/nvme0n2", "/dev/nvme0n3"])
        res = paniclog.find()
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["device"], "/dev/nvme0n3")
        self.assertEqual(res[0]["records"][0]["boot_session_uuid"], UUID)

    def test_other_controllers_are_ignored(self):
        self.ns("nvme1n2", 2, container(record()), driver="nvme")
        self.assertEqual(paniclog.candidates(), [])

    def test_empty_namespace_is_reported(self):
        self.ns("nvme0n3", 3, b"\0" * 8192)
        res = paniclog.find()
        self.assertEqual(res[0]["state"], "empty")
        self.assertIn("empty", paniclog.describe(res[0])[0])

    def test_read_is_read_only(self):
        self.ns("nvme0n3", 3, container(record()))
        path = os.path.join(self.root, "dev/nvme0n3")
        os.chmod(path, 0o444)
        before = get(path)
        with mock.patch("os.open", wraps=os.open) as op:
            paniclog.find()
        for call in op.call_args_list:
            flags = call.args[1]
            self.assertEqual(flags & (os.O_WRONLY | os.O_RDWR), 0)
        self.assertEqual(get(path), before)

    def test_cli_text_json_and_save(self):
        self.ns("nvme0n3", 3, container(record()))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["--log", os.path.join(self.root, "log.jsonl"), "panic-log", "--lines", "2"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("99A1 (test)", text)
        self.assertIn("2026-11-08T04:21:06Z", text)
        self.assertIn(UUID, text)
        self.assertIn("| panic(cpu 3", text)
        self.assertNotIn("last line", text)
        out = io.StringIO()
        save = os.path.join(self.root, "saved")
        with redirect_stdout(out):
            rc = cli.main(["--json", "--log", os.path.join(self.root, "log.jsonl"), "panic-log", "--save", save])
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["schema"], "aurora-diag/panic-log")
        rec = doc["results"][0]["records"][0]
        self.assertEqual(rec["version"], 8)
        self.assertEqual(len(doc["saved"]), 2)
        for p in doc["saved"]:
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(get(doc["saved"][0]).decode(), TEXT)

    def test_cli_nothing_found(self):
        os.makedirs(os.path.join(self.root, "sys/block"))
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.main(["--log", os.path.join(self.root, "log.jsonl"), "panic-log"])
        self.assertEqual(rc, 1)
        self.assertIn("no panic-log namespace", out.getvalue())


if __name__ == "__main__":
    unittest.main()
