# SPDX-License-Identifier: MIT
"""Tests for the bring-up side (subsystems, clocks, check, snapshot/diff,
GPU parsing, compliance) against a fake tree."""

import copy
import json
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from aurora_diag import check, clocks, compliance, gpu, snapshot, subsystems, sysfs  # noqa: E402
from aurora_diag.machine import Machine  # noqa: E402
from test_aurora_diag import Base, build, w  # noqa: E402

DT = "/sys/firmware/devicetree/base"


def dt_node(root, rel, compat, status=None, extra=None):
    d = os.path.join(root, DT.lstrip("/"), rel)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "compatible"), "wb") as f:
        f.write(compat.encode() + b"\0")
    if status:
        with open(os.path.join(d, "status"), "wb") as f:
            f.write(status.encode() + b"\0")
    for k, v in (extra or {}).items():
        with open(os.path.join(d, k), "wb") as f:
            f.write(v)
    return d


def platform_dev(root, name, dt_rel, driver=None):
    dev = os.path.join(root, "sys/devices/platform", name)
    os.makedirs(dev, exist_ok=True)
    os.symlink(os.path.join(root, DT.lstrip("/"), dt_rel), os.path.join(dev, "of_node"))
    bus = os.path.join(root, "sys/bus/platform/devices")
    os.makedirs(bus, exist_ok=True)
    os.symlink(dev, os.path.join(bus, name))
    if driver:
        dd = os.path.join(root, "sys/bus/platform/drivers", driver)
        os.makedirs(dd, exist_ok=True)
        os.symlink(dd, os.path.join(dev, "driver"))
    return dev


class TestSubsystems(Base):
    def setUp(self):
        super().setUp()
        build(self.root)
        subsystems._dmesg_cache.clear()
        subsystems._dmesg_cache["emerg,alert,crit,err"] = []
        subsystems._dmesg_cache["warn"] = []
        subsystems._dmesg_cache["emerg,alert,crit,err,warn,notice,info"] = ["apple-dcp 1.dcp: firmware version 13.5"]
        dt_node(self.root, "dcp@1", "apple,dcp")
        dt_node(self.root, "isp@2", "apple,isp")
        dt_node(self.root, "ane@3", "apple,ane", status="disabled")
        dt_node(self.root, "gpu@4", "apple,agx", extra={"apple,firmware-version": struct.pack(">4I", 13, 5, 2, 0)})
        dt_node(self.root, "iommu@5", "apple,dart")
        dcp = platform_dev(self.root, "1.dcp", "dcp@1", "apple-dcp")
        platform_dev(self.root, "2.isp", "isp@2")
        platform_dev(self.root, "5.iommu", "iommu@5", "apple-dart")
        w(self.root, "/sys/class/drm/card1-eDP-1/uevent", "")
        # card1-eDP-1 lives under the DCP device
        os.makedirs(os.path.join(dcp, "drm"), exist_ok=True)

    def by_block(self):
        return {s["block"]: s for s in subsystems.subsystems()}

    def test_states(self):
        s = self.by_block()
        self.assertEqual(s["dcp"]["state"]["level"], "ok")
        self.assertEqual(s["dcp"]["firmware_log"], "apple-dcp 1.dcp: firmware version 13.5")
        self.assertEqual(s["isp"]["state"]["level"], "fail")
        self.assertEqual(s["ane"]["state"]["level"], "off")
        self.assertEqual(s["gpu"]["state"]["level"], "warn")  # no Linux device
        self.assertEqual(s["gpu"]["dt_firmware"], "13.5.2.0")
        self.assertEqual(s["iommu"]["kind"], "infra")

    def test_check_and_expect(self):
        m = Machine()
        res = check.run(m, api=False)
        sub = {r["name"]: r for r in res["checks"] if r["group"] == "subsys"}
        self.assertEqual(sub["ISP (camera) isp@2"]["level"], "fail")
        self.assertEqual(res["exit"], 2)
        snap = snapshot.build(m, api=False)
        base = os.path.join(self.tmp.name, "base.json")
        with open(base, "w") as f:
            json.dump(snap, f)
        # Lose the DCP's driver: with the baseline that is a failure.
        os.unlink(os.path.join(self.root, "sys/devices/platform/1.dcp/driver"))
        res = check.run(m, api=False, expect=base)
        sub = {r["name"]: r for r in res["checks"] if r["group"] == "subsys"}
        self.assertEqual(sub["DCP (internal display) dcp@1"]["level"], "fail")
        self.assertIn("baseline", sub["DCP (internal display) dcp@1"]["reason"])
        text = check.render(res)
        self.assertIn("FAIL", text)
        self.assertIn("infrastructure", text)

    def test_diff(self):
        m = Machine()
        a = snapshot.build(m, api=False)
        b = copy.deepcopy(a)
        b["subsystems"]["dcp@1"]["driver"] = None
        b["subsystems"]["dcp@1"]["level"] = "fail"
        b["dmesg_errors"] = ["apple-dcp N.dcp: crashed"]
        b["dmesg_readable"] = a["dmesg_readable"] = True
        b["sensors"] = a["sensors"][1:]
        ch = snapshot.diff(a, b)
        reg = [c for c in ch if c["severity"] == "regression"]
        keys = {c["section"] for c in reg}
        self.assertTrue({"subsystems", "dmesg", "sensors"} <= keys)
        self.assertIn("regressions", snapshot.render_diff(ch))
        self.assertEqual(snapshot.diff(a, copy.deepcopy(a)), [])


class TestClocks(Base):
    def test_dt_ladders_and_policies(self):
        build(self.root)
        t = os.path.join(self.root, "proc/device-tree/opp-table-gpu")
        for i, hz in enumerate((0, 338000000, 1398000000)):
            os.makedirs(os.path.join(t, "opp%02d" % i))
            with open(os.path.join(t, "opp%02d" % i, "opp-hz"), "wb") as f:
                f.write(struct.pack(">Q", hz))
        with open(os.path.join(t, "phandle"), "wb") as f:
            f.write(struct.pack(">I", 7))
        g = os.path.join(self.root, "proc/device-tree/gpu@1")
        os.makedirs(g)
        with open(os.path.join(g, "operating-points-v2"), "wb") as f:
            f.write(struct.pack(">I", 7))
        w(self.root, "/sys/devices/system/cpu/cpufreq/policy0/scaling_available_frequencies", "600000 2064000\n", 0o444)
        c = clocks.all_clocks()
        lad = c["dt_ladders"]["opp-table-gpu"]
        self.assertEqual([o["MHz"] for o in lad["opps"]], [0.0, 338.0, 1398.0])
        self.assertEqual(lad["used_by"], ["gpu@1"])
        self.assertEqual(c["cpu"][0]["ladder_MHz"], [600.0, 2064.0])
        self.assertIn("not exposed", c["memory"]["speed"])


class TestGpuParsing(unittest.TestCase):
    def test_kv(self):
        kv = gpu._kv("version=1 crashed=0\nphysical_qids used=4 total=128\nbusy_ns 5\nwords=[1, 2]")
        self.assertEqual(kv["physical_qids.used"], 4)
        self.assertEqual(kv["crashed"], 0)
        self.assertEqual(kv["busy_ns"], 5)

    def test_summary_and_heaps(self):
        text = ("Vulkan Instance Version: 1.4.341\n\nDevices:\n========\nGPU0:\n\tapiVersion         = 1.4.363\n"
                "\tdeviceType         = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU\n\tdeviceName         = Apple M2 (G14G B0)\n"
                "\tdriverName         = Borealis\n\tconformanceVersion = 1.4.6.2\n")
        inst, devs = gpu._parse_summary(text)
        self.assertEqual(inst, "1.4.341")
        self.assertEqual(devs[0]["driverName"], "Borealis")
        full = ("x\nVkPhysicalDeviceMemoryProperties:\n=================================\nmemoryHeaps: count = 1\n"
                "\tmemoryHeaps[0]:\n\t\tsize   = 100 (0x64) (100 B)\n\t\tbudget = 90 (0x5a) (90 B)\n"
                "\t\tusage  = 0 (0x00000000) (0.00 B)\n\t\tflags: count = 1\n\t\t\tMEMORY_HEAP_DEVICE_LOCAL_BIT\n"
                "memoryTypes: count = 2\n\tmemoryTypes[0]:\n\tmemoryTypes[1]:\n")
        h = gpu._parse_heaps(full)
        self.assertEqual(h[0]["heaps"][0]["size"], 100)
        self.assertEqual(h[0]["heaps"][0]["flags"], ["MEMORY_HEAP_DEVICE_LOCAL_BIT"])
        self.assertEqual(h[0]["type_count"], 2)


class TestCompliance(unittest.TestCase):
    def test_vulkan_required(self):
        feats = {n: True for names in compliance.REQUIRED_VK.values() for n in names}
        feats["maintenance5"] = False
        r = compliance.vulkan_device({"apiVersion": "1.4.363", "feature_values": feats, "conformance": "0.0.0.0"})
        self.assertEqual(r["missing"], ["maintenance5"])
        self.assertFalse(r["conformance_submitted"])
        r = compliance.vulkan_device({"apiVersion": "1.3.0", "feature_values": feats, "conformance": "1.3.8.0"})
        self.assertEqual(r["missing"], [])

    def test_gl_minimums(self):
        lim = dict(compliance.GL46_MIN)
        lim["MAX_SAMPLES"] = 2
        r = compliance.gl_context({"version": "4.6 (Core Profile) Mesa", "limits": lim})
        self.assertEqual(r["missing"], ["MAX_SAMPLES 2 < 4"])
        r = compliance.gl_context({"version": "OpenGL ES 3.2 Mesa", "limits": dict(compliance.GLES32_MIN)}, es=True)
        self.assertEqual(r["missing"], [])


if __name__ == "__main__":
    unittest.main()
