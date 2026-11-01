# SPDX-License-Identifier: MIT
"""Tests for the categories (sections.py), the EDID and DCP dictionary
decoders, the flattened-tree walker and the inventory side of check and
snapshot diff, against a fake tree."""

import json
import os
import struct
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from aurora_diag import check, edid, kms, sections, snapshot, subsystems, sysfs  # noqa: E402
from test_aurora_diag import Base, build, w  # noqa: E402
from test_bringup import dt_node  # noqa: E402


def make_edid(name="Test Mon", vrange=(48, 165), hdr=True):
    b = bytearray(256)
    b[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    v = ((ord("A") - 64) << 10) | ((ord("B") - 64) << 5) | (ord("C") - 64)
    b[8:10] = struct.pack(">H", v)
    b[10:12] = struct.pack("<H", 0x1234)
    b[12:16] = b"SERI"
    b[17] = 34
    b[18], b[19] = 1, 4
    b[20] = 0xb5          # digital, 10 bpc, DisplayPort
    b[21], b[22] = 60, 34
    # detailed timing: 2560x1440, 241.5 MHz
    d = bytearray(18)
    d[0:2] = struct.pack("<H", 24150)
    d[2], d[3], d[4] = 2560 & 0xff, 160, ((2560 >> 8) << 4) | 0
    d[5], d[6], d[7] = 1440 & 0xff, 41, ((1440 >> 8) << 4) | 0
    b[54:72] = d
    b[72:90] = b"\x00\x00\x00\xfc\x00" + name.encode().ljust(13, b"\n")[:13]
    b[90:108] = bytes([0, 0, 0, 0xfd, 0, vrange[0], vrange[1], 30, 160, 60]) + b"\n" + b" " * 7
    b[108:126] = b"\x00\x00\x00\xff\x00" + b"SECRETSERIAL\n"
    b[126] = 1
    ext = bytearray(128)
    ext[0], ext[1] = 0x02, 3
    blocks = bytearray()
    if hdr:
        blocks += bytes([(7 << 5) | 6, 6, 0x05, 0x01, 0x80, 0x60, 0x20])   # HDR: SDR + PQ, type 1
        blocks += bytes([(7 << 5) | 3, 5, 0x80, 0x00])                     # colorimetry BT2020RGB
    ext[4:4 + len(blocks)] = blocks
    ext[2] = 4 + len(blocks)
    b[128:256] = ext
    return bytes(b)


def dcp_blob(d):
    def tag(size, typ, last=False):
        return struct.pack("<I", size | (typ << 24) | ((1 << 31) if last else 0))

    out = bytearray(struct.pack("<I", 0xd3))

    def emit(v):
        nonlocal out
        while len(out) % 4:
            out += b"\0"
        if isinstance(v, dict):
            out += tag(len(v), 1)
            for k, x in v.items():
                emit(k)
                emit(x)
        elif isinstance(v, bool):
            out += tag(int(v), 11)
        elif isinstance(v, int):
            out += tag(64, 4) + struct.pack("<q", v)
        elif isinstance(v, str):
            s = v.encode() + b"\0"
            out += tag(len(s), 9) + s
    emit(d)
    return bytes(out)


class TestDecoders(unittest.TestCase):
    def test_edid(self):
        e = edid.edid(make_edid())
        self.assertEqual(e["manufacturer"], "ABC")
        self.assertEqual(e["name"], "Test Mon")
        self.assertEqual(e["range_vertical_Hz"], [48, 165])
        self.assertEqual(e["bpc"], 10)
        self.assertEqual(e["interface"], "DisplayPort")
        self.assertTrue(e["hdr"])
        self.assertIn("PQ (SMPTE ST 2084)", e["hdr_static_metadata"]["eotfs"])
        self.assertEqual(e["colorimetry"], ["BT2020RGB"])
        self.assertEqual(e["preferred"]["width"], 2560)
        self.assertNotIn("SECRET", json.dumps(e))
        self.assertNotIn("SERI", json.dumps(e))

    def test_edid_rejects_garbage(self):
        self.assertIn("error", edid.edid(b"\0" * 128))
        self.assertIn("error", edid.edid(b""))

    def test_dcp_dict(self):
        blob = dcp_blob({"MinimumVariableRefreshRate": 48 << 16, "MaximumVariableRefreshRate": 280 << 16,
                         "SupportsVariableRefreshRate": True, "SupportsPQEOTF": True,
                         "ProductAttributes": {"ProductName": "MPG", "YearOfManufacture": 2024}})
        d = edid.dcp_dict(blob)
        a = edid.display_attributes(d)
        self.assertEqual(a["product_name"], "MPG")
        self.assertEqual((a["vrr_min_Hz"], a["vrr_max_Hz"]), (48.0, 280.0))
        self.assertTrue(a["SupportsVariableRefreshRate"])
        self.assertIsNone(edid.dcp_dict(b"\x01\x02\x03\x04\x05\x06\x07\x08"))

    def test_fdt_order(self):
        # header (40 bytes) + struct block: / { dcp@2 {} dcp@1 { x = <1>; } }
        strings = b"x\0"
        st = bytearray()

        def node(n):
            nb = n.encode() + b"\0"
            nb += b"\0" * ((4 - len(nb) % 4) % 4)
            return struct.pack(">I", 1) + nb
        st += node("") + node("dcp@2") + struct.pack(">I", 2) + node("dcp@1")
        st += struct.pack(">III", 3, 4, 0) + struct.pack(">I", 1) + struct.pack(">I", 2) + struct.pack(">I", 2) + struct.pack(">I", 9)
        off = 40
        hdr = struct.pack(">10I", 0xd00dfeed, 40 + len(st) + len(strings), off, off + len(st), 0, 17, 16, 0, len(strings), len(st))
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            p = os.path.join(t, "sys/firmware")
            os.makedirs(p)
            with open(os.path.join(p, "fdt"), "wb") as f:
                f.write(hdr + st + strings)
            old = sysfs.ROOT
            sysfs.ROOT = t
            try:
                self.assertEqual(kms.fdt_node_order(), ["/", "/dcp@2", "/dcp@1"])
            finally:
                sysfs.ROOT = old


class TestSections(Base):
    def setUp(self):
        super().setUp()
        build(self.root)
        subsystems._dmesg_cache.clear()
        for k in ("emerg,alert,crit,err", "warn", "emerg,alert,crit,err,warn,notice,info"):
            subsystems._dmesg_cache[k] = []
        sections._MODALIAS.clear()
        sections._cache.clear()
        dt_node(self.root, "aop@1", "apple,aop")
        dt_node(self.root, "aop@1/als", "apple,aop-als")
        dt_node(self.root, "dcp@2", "apple,dcp", extra={"apple,notch-height": struct.pack(">I", 74)})
        w(self.root, "/proc/stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 100 0 100 800 0 0 0 0 0 0\n")
        w(self.root, "/proc/meminfo", "MemTotal: 16000000 kB\nMemAvailable: 8000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
        w(self.root, "/proc/uptime", "100.0 50.0\n")
        os.makedirs(os.path.join(self.root, "proc/1"), exist_ok=True)
        w(self.root, "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "2000000\n")
        c = "/sys/class/drm/card1-eDP-1"
        w(self.root, c + "/status", "connected\n")
        w(self.root, c + "/enabled", "enabled\n")
        w(self.root, c + "/modes", "3456x2234\n")
        with open(os.path.join(self.root, c.lstrip("/"), "edid"), "wb") as f:
            f.write(make_edid("Apple Panel", (24, 120)))
        os.makedirs(os.path.join(self.root, "sys/class/drm/card1/device"), exist_ok=True)

    def test_motion_absent_says_why(self):
        s = sections.sensors()
        acc = next(i for i in s["items"] if i["key"] == "accelerometer")
        self.assertEqual(acc["level"], "absent")
        self.assertIn("no accel node under /aop@1", acc["detail"])
        self.assertIn("no Apple motion driver", acc["detail"])

    def test_motion_node_without_calibration(self):
        dt_node(self.root, "aop@1/accel", "apple,aop-accel")
        s = sections.sensors()
        acc = next(i for i in s["items"] if i["key"] == "accelerometer")
        self.assertEqual(acc["level"], "warn")
        self.assertIn("no calibration", acc["detail"])

    def test_motion_present(self):
        d = "/sys/bus/iio/devices/iio:device5"
        w(self.root, d + "/name", "aop-sensors-accel\n")
        w(self.root, d + "/in_accel_x_raw", "65536\n")
        w(self.root, d + "/in_accel_scale", "0.000149638\n")
        s = sections.sensors()
        acc = next(i for i in s["items"] if i["key"] == "accelerometer")
        self.assertEqual(acc["level"], "ok")
        self.assertAlmostEqual(acc["values"]["in_accel_x_raw"], 9.80667, places=3)

    def test_display_from_sysfs(self):
        s = sections.display()
        e = next(i for i in s["items"] if i["key"] == "eDP-1")
        self.assertIn("Apple Panel", e["detail"])
        notch = next(i for i in s["items"] if i["key"] == "notch")
        self.assertIn("74", notch["detail"])
        self.assertEqual(s["facts"]["connectors"]["eDP-1"]["edid_name"], "Apple Panel")

    def test_every_category_renders(self):
        secs = sections.collect([n for n in sections.ORDER if n != "health"], timeout=30)
        for n, s in secs.items():
            for k in ("name", "title", "level", "headline", "items", "tables", "facts"):
                self.assertIn(k, s, n)
            self.assertNotIn("probe failed", s["headline"], n)
            sections.render(s)
            json.dumps(s, default=str)
        rows = sections.coverage(secs)
        self.assertTrue(any(r["level"] == "absent" and r["detail"] for r in rows))

    def test_check_inventory_against_baseline(self):
        secs = {"sensors": sections.section("sensors", "S", [sections.item("accelerometer", "absent", "no node")]),
                "radio": sections.section("radio", "R", [sections.item("Wi-Fi wlan0", "ok", "brcmfmac")])}
        base = {"inventory_items": {"sensors/accelerometer": "absent", "radio/Wi-Fi wlan0": "ok",
                                    "engines/ave": "ok"}}
        rows = {r["name"]: r for r in check.inventory_rows(secs, base)}
        self.assertEqual(rows["sensors/accelerometer"]["level"], "skip")
        self.assertEqual(rows["radio/Wi-Fi wlan0"]["level"], "pass")
        self.assertNotIn("engines/ave", rows)     # category not collected: not judged
        secs["radio"]["items"][0]["level"] = "absent"
        rows = {r["name"]: r for r in check.inventory_rows(secs, base)}
        self.assertEqual(rows["radio/Wi-Fi wlan0"]["level"], "fail")

    def test_diff_inventory(self):
        a = {"kernel": {}, "firmware": {}, "subsystems": {}, "inventory_items": {"display/eDP-1": "ok", "sensors/gyroscope": "absent"},
             "inventory": {"radio": {"wifi": {"wlan0": {"driver": "brcmfmac", "firmware": "1"}}}}}
        b = {"kernel": {}, "firmware": {}, "subsystems": {}, "inventory_items": {"display/eDP-1": "warn", "sensors/gyroscope": "ok"},
             "inventory": {"radio": {"wifi": {"wlan0": {"driver": None, "firmware": "2"}}}}}
        ch = {(c["key"]): c["severity"] for c in snapshot.diff(a, b)}
        self.assertEqual(ch["display/eDP-1"], "regression")
        self.assertEqual(ch["sensors/gyroscope"], "improvement")
        self.assertEqual(ch["radio.wifi.wlan0.driver"], "regression")
        self.assertEqual(ch["radio.wifi.wlan0.firmware"], "change")

    def test_show_cli_json(self):
        env = dict(os.environ, AURORA_DIAG_ROOT=self.root)
        r = subprocess.run([sys.executable, "-m", "aurora_diag", "show", "memory", "--json"], capture_output=True, text=True,
                           env=env, cwd=os.path.dirname(HERE), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.loads(r.stdout)
        self.assertEqual(d["name"], "memory")
        self.assertTrue(d["items"])


if __name__ == "__main__":
    unittest.main()


class TestKnownWarnings(Base):
    def test_baseline_warning_passes(self):
        build(self.root)
        import tempfile
        from aurora_diag.machine import Machine
        res = check.run(Machine(), api=False, secs={})
        warns = ["%s/%s" % (r["group"], r["name"]) for r in res["checks"] if r["level"] == "warn"]
        base = {"schema": snapshot.SCHEMA, "version": 1, "subsystems": {}, "services": {}, "sensors": [],
                "warnings": warns, "dmesg_readable": False}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(base, f)
        res2 = check.run(Machine(), api=False, secs={}, expect=f.name)
        os.unlink(f.name)
        known = [r for r in res2["checks"] if r["reason"].startswith("known in the baseline")]
        self.assertEqual(len(known), len([w for w in warns if not w.startswith("sensors/")]) or len(known))
        self.assertFalse(any(r["level"] == "warn" and "%s/%s" % (r["group"], r["name"]) in warns for r in res2["checks"]))


class TestColourTemperatureABI(Base):
    def test_either_name(self):
        build(self.root)
        d = "/sys/bus/iio/devices/iio:device0"
        w(self.root, d + "/in_cct_input", "5100\n")
        s = sections.sensors()
        self.assertEqual(next(i for i in s["items"] if i["key"] == "colour temperature")["level"], "ok")
        w(self.root, d + "/in_colortemp_input", "5200\n")
        s = sections.sensors()
        self.assertIn("5200", next(i for i in s["items"] if i["key"] == "colour temperature")["detail"])
        self.assertEqual(snapshot.sensor_id("iio/aop-sensors-als/in_cct_input"), "iio/aop-sensors-als/in_colortemp_input")
        a = {"kernel": {}, "firmware": {}, "subsystems": {}, "sensors": ["iio/aop-sensors-als/in_cct_input"]}
        b = {"kernel": {}, "firmware": {}, "subsystems": {}, "sensors": ["iio/aop-sensors-als/in_colortemp_input"]}
        self.assertFalse([c for c in snapshot.diff(a, b) if c["section"] == "sensors"])


class TestGpuStats(Base):
    """agx_stats as the aurora driver writes it: G13/G14 (busy_ns, pstate) and
    G16S/G17P (firmware utilisation records, stale while the GPU is idle)."""

    def gpu(self, stats):
        build(self.root)
        dev = "/sys/devices/platform/soc/206400000.gpu"
        w(self.root, dev + "/agx_stats", stats)
        os.makedirs(os.path.join(self.root, "sys/bus/platform/drivers/aurora"), exist_ok=True)
        os.symlink(os.path.join(self.root, "sys/bus/platform/drivers/aurora"), os.path.join(self.root, dev.lstrip("/"), "driver"))
        node = dt_node(self.root, "soc/gpu@206400000", "apple,agx-g14g", extra={"operating-points-v2": struct.pack(">I", 0x18)})
        os.symlink(node, os.path.join(self.root, dev.lstrip("/"), "of_node"))
        table = dt_node(self.root, "opp-table-gpu", "operating-points-v2", extra={"phandle": struct.pack(">I", 0x18)})
        for i, hz in enumerate((0, 444000000, 612000000)):
            os.makedirs(os.path.join(table, "opp%02d" % i))
            with open(os.path.join(table, "opp%02d" % i, "opp-hz"), "wb") as f:
                f.write(struct.pack(">Q", hz))
        cls = os.path.join(self.root, "sys/class/drm")
        os.makedirs(cls, exist_ok=True)
        for n in ("card1", "renderD128"):
            os.makedirs(os.path.join(cls, n), exist_ok=True)
            os.symlink(os.path.join(self.root, dev.lstrip("/")), os.path.join(cls, n, "device"))
        return {i["key"]: i for i in sections.gpu()["items"]}

    def test_g14_pstate_frequency_from_dt(self):
        items = self.gpu("busy_ns 1000\njobs 5\npstate 1\npower_mw 0\nutil1 0\n")
        self.assertEqual(items["frequency"]["level"], "ok")
        self.assertIn("444 MHz requested (pstate 1", items["frequency"]["detail"])

    def test_g17_valid_utilisation(self):
        items = self.gpu("utilisation_status valid\nutil_device_percent 55\npstate_status unavailable\npstate unsupported\n"
                         "achieved_frequency_hz unsupported\nbusy_ns unsupported\n")
        self.assertEqual(items["utilisation"]["level"], "ok")
        self.assertIn("55% busy", items["utilisation"]["detail"])
        self.assertEqual(items["frequency"]["level"], "absent")

    def test_g17_idle_is_not_absent(self):
        items = self.gpu("utilisation_status stale\nutil_device_percent unsupported\npstate_status unavailable\n"
                         "pstate unsupported\nachieved_frequency_hz unsupported\nbusy_ns unsupported\n")
        self.assertEqual(items["utilisation"]["level"], "ok")
        self.assertIn("idle", items["utilisation"]["detail"])
