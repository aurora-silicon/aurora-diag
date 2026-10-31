# SPDX-License-Identifier: MIT
"""Read-only ANE status fixtures, including devices that never probed."""

import builtins
import errno
import grp
import os
import unittest
from unittest.mock import patch

from aurora_diag import ane, sysfs
from test_ane import AneBase, accelerator
from test_aurora_diag import w


class TestANEStatus(AneBase):
    def tree(self, chip, status="okay", bound=False):
        dt = "/sys/firmware/devicetree/base/soc/ane@400000000"
        w(self.root, dt + "/compatible", "apple,%s-ane\0apple,ane\0" % chip)
        if status is not None:
            w(self.root, dt + "/status", status + "\0")
        if bound:
            dev = accelerator(self.root, chip=chip)
            # Real sysfs uses of_node symlinks into the live DT tree.
            for name in ("compatible", "reg"):
                os.unlink(sysfs.root(dev + "/of_node/" + name))
            os.rmdir(sysfs.root(dev + "/of_node"))
            os.symlink(sysfs.root(dt), sysfs.root(dev + "/of_node"))
            bus = sysfs.root("/sys/bus/platform/devices")
            os.makedirs(bus, exist_ok=True)
            os.symlink(sysfs.root(dev), bus + "/285c04000.ane")
            return dt, dev
        return dt, None

    def reasons(self):
        return " ".join(c["reason"] for c in ane.checks(ane.collect()))

    def test_generation_fixtures_and_live_dt_deduplication(self):
        for chip, generation in (("t6000", "H13"), ("t6021", "H14"),
                                 ("t6041", "H16"), ("t8140", "H17")):
            with self.subTest(chip=chip):
                # A separate fixture root for every generation.
                old = sysfs.ROOT
                old_root = self.root
                self.root = sysfs.ROOT = os.path.join(self.root, chip)
                try:
                    dt, dev = self.tree(chip, bound=True)
                    data = ane.collect()
                    self.assertEqual(len(data["dt_nodes"]), 1)
                    d = data["devices"][0]
                    self.assertEqual(d["generation"], generation)
                    self.assertEqual(d["compatible"][0], "apple,%s-ane" % chip)
                    self.assertEqual(d["dt_status"], "okay")
                    self.assertEqual(data["dt_nodes"][0]["driver"], "ane")
                    self.assertEqual(d["counters"], {"busy_ns": 2000, "jobs": 20, "timeouts": 0, "faults": 0})
                    self.assertEqual(d["power_state"], "suspended")
                finally:
                    sysfs.ROOT = old
                    self.root = old_root

    def test_dt_only_disabled_and_unbound_generations(self):
        dt, _ = self.tree("t8140", "disabled")
        data = ane.collect()
        self.assertEqual(data["devices"], [])
        self.assertEqual(data["dt_nodes"][0]["generation"], "H17")
        self.assertIn("status is disabled", self.reasons())
        self.assertIn("ownership provider", self.reasons())
        w(self.root, dt + "/status", "okay\0")
        self.assertIn("driver is unbound", self.reasons())
        self.assertIn("firmware reservations", self.reasons())
        w(self.root, dt + "/compatible", "apple,t6021-ane\0")
        self.assertIn("omarchy-ane-firmware-fetch", self.reasons())
        self.assertIn("NixOS", self.reasons())

    def test_absent_status_means_okay_and_unknown_chip_stays_unknown(self):
        self.tree("t9999", None)
        n = ane.collect()["dt_nodes"][0]
        self.assertEqual(n["status"], "okay")
        self.assertIsNone(n["generation"])

    def test_missing_compatible_is_not_a_missing_device(self):
        dt, _ = self.tree("t6000")
        os.unlink(sysfs.root(dt + "/compatible"))
        n = ane.collect()["dt_nodes"][0]
        self.assertEqual(n["path"], dt)
        self.assertIsNone(n["generation"])
        self.assertIn("DT compatible is missing", self.reasons())

    def test_platform_binding_without_accelerator_class(self):
        dt, dev = self.tree("t6041", bound=True)
        os.unlink(sysfs.root("/sys/class/accel/accel0/device"))
        os.unlink(sysfs.root("/dev/accel/accel0"))
        data = ane.collect()
        self.assertEqual(data["devices"], [])
        self.assertEqual(data["dt_nodes"][0]["driver"], "ane")

    def test_new_performance_abi_zero_unsupported_and_error(self):
        dev = accelerator(self.root)
        for level, vote, error in (("5", "1000 2000", "0"), ("0", "0 0", "0"),
                                   ("unsupported", "unsupported", "-5")):
            with self.subTest(level=level):
                for field, value in (("performance_level", level), ("memory_vote", vote), ("performance_error", error)):
                    w(self.root, dev + "/" + field, value)
                data = ane.collect()
                d = data["devices"][0]
                self.assertEqual((d["performance_level"], d["memory_vote"], d["performance_error"]), (level, vote, error))
                health = next(c for c in ane.checks(data) if c["name"].endswith("health"))
                self.assertEqual(health["level"], "fail" if error != "0" else "pass")
                if error != "0":
                    self.assertIn("errno -5", health["reason"])
        self.assertIn("ane.accel0.memory_vote", {r["id"] for r in ane.readings()})

    def test_old_kernel_and_stats_disabled_are_explicit(self):
        dev = accelerator(self.root)
        os.unlink(sysfs.root(dev + "/performance_level"))
        os.unlink(sysfs.root(dev + "/ane_stats"))
        d = ane.collect()["devices"][0]
        self.assertIsNone(d["memory_vote"])
        self.assertIsNone(d["performance_error"])
        self.assertEqual(d["counters"], {})
        self.assertTrue(any("ane_stats" in reason for reason in d["issues"]))
        self.assertFalse(any("memory_vote" in reason for reason in d["issues"]))

    def test_zero_counters_and_missing_required_attributes(self):
        dev = accelerator(self.root)
        w(self.root, dev + "/ane_stats", "busy_ns 0\njobs 0\n")
        self.assertEqual(ane.collect()["devices"][0]["counters"], {"busy_ns": 0, "jobs": 0})
        for field in ("wedged", "power/runtime_status"):
            os.unlink(sysfs.root(dev + "/" + field))
        d = ane.collect()["devices"][0]
        self.assertIsNone(d["wedged"])
        self.assertIsNone(d["power_state"])
        self.assertTrue(any("runtime_status is not exposed" in s for s in d["issues"]))

    def test_permissions_name_actual_group_without_opening_device(self):
        dt, dev = self.tree("t6041", bound=True)
        real_open = builtins.open
        real_access = os.access
        for field in ("memory_vote", "performance_error"):
            w(self.root, dev + "/" + field, "0")
        group = grp.getgrgid(os.stat(sysfs.root(dev + "/wedged")).st_gid).gr_name
        for code in (errno.EACCES, errno.EPERM):
            def reader(path, *args, **kwargs):
                if str(path).startswith(sysfs.root("/dev/")):
                    self.fail("collector opened an accelerator device")
                denied = [dev + "/" + f for f in ("wedged", "ane_stats", "power/runtime_status", "performance_level", "memory_vote", "performance_error")]
                denied.append(dt + "/status")
                if os.path.realpath(path) in [os.path.realpath(sysfs.root(p)) for p in denied]:
                    raise OSError(code, "Permission denied")
                self.assertNotIn("w", args[0] if args else kwargs.get("mode", "r"))
                return real_open(path, *args, **kwargs)
            with patch("builtins.open", side_effect=reader), patch.object(os, "access", side_effect=lambda p, m: False if p == sysfs.root("/dev/accel/accel0") else real_access(p, m)):
                data = ane.collect()
            self.assertIsNone(data["dt_nodes"][0]["status"])
            self.assertIsNone(data["devices"][0]["wedged"])
            self.assertIsNone(data["devices"][0]["memory_vote"])
            self.assertIsNone(data["devices"][0]["performance_error"])
            reasons = " ".join(c["reason"] for c in ane.checks(data))
            self.assertIn("%s group" % group, reasons)
            self.assertIn("Permission denied", reasons)

    def test_absent_everything_and_malformed_data(self):
        self.assertIn("ANE node, DARTs and power domains", self.reasons())
        dev = accelerator(self.root)
        for field in ("ane_stats", "wedged", "memory_vote", "performance_error"):
            w(self.root, dev + "/" + field, "invalid")
        d = ane.collect()["devices"][0]
        self.assertEqual(d["counters"], {})
        self.assertIsNone(d["wedged"])
        self.assertTrue(any("Malformed ANE memory_vote" in s for s in d["issues"]))
        self.assertTrue(any("Malformed ANE performance_error" in s for s in d["issues"]))

    def test_wrong_driver_is_not_ane_binding(self):
        accelerator(self.root, driver="other")
        data = ane.collect()
        self.assertFalse(data["devices"][0]["driver_bound"])
        self.assertIn("bound to other", " ".join(c["reason"] for c in ane.checks(data)))


if __name__ == "__main__":
    unittest.main()
