# SPDX-License-Identifier: MIT
"""ANE inventory and comparison tests: synthetic sysfs, no accelerator access."""

import json
import os
import struct
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aurora_diag import ane, check, comparison, dashboard, snapshot, sources, sysfs
from aurora_diag.machine import Machine
from test_aurora_diag import Base, build, w


def accelerator(root, name="accel0", chip="t6000", driver="ane"):
    os.makedirs(sysfs.root("/proc"), exist_ok=True)
    platform = "285c04000.ane" if name == "accel0" else "385c04000.ane"
    dev = "/sys/devices/platform/" + platform
    w(root, dev + "/of_node/compatible", "apple,%s-ane\0apple,ane\0" % chip)
    with open(sysfs.root(dev + "/of_node/reg"), "wb") as f:
        f.write(struct.pack(">4I", 2, 0x85c04000, 0, 0x4000))
    w(root, dev + "/ane_stats", "busy_ns 2000\njobs 20\ntimeouts 0\nfaults 0\n")
    w(root, dev + "/wedged", "0")
    w(root, dev + "/power/runtime_status", "suspended")
    w(root, dev + "/performance_level", "3")
    w(root, dev + "/firmware_version", "1.2.3")
    w(root, "/dev/accel/" + name, "")
    classdir = sysfs.root("/sys/class/accel/" + name)
    os.makedirs(classdir, exist_ok=True)
    os.symlink(sysfs.root(dev), classdir + "/device")
    if driver:
        driverdir = sysfs.root("/sys/bus/platform/drivers/" + driver)
        os.makedirs(driverdir, exist_ok=True)
        os.symlink(driverdir, sysfs.root(dev + "/driver"))
    return dev


class AneBase(Base):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("AURORA_DIAG_ANE_BENCHMARK", None)
        os.environ.pop("AURORA_DIAG_MACOS_BASELINE", None)
        self.addCleanup(self.env.stop)

    def save(self, b, path=ane.BENCHMARK):
        w(self.root, path, json.dumps(b))


class TestANE(AneBase):
    def test_inventory_zero_is_not_unknown_and_read_only(self):
        accelerator(self.root)
        d = ane.collect()["devices"][0]
        self.assertEqual(d["generation"], "H13")
        self.assertEqual(d["firmware_version"], "1.2.3")
        self.assertEqual(d["power_state"], "suspended")
        self.assertEqual(d["performance_level"], "3")
        self.assertEqual(d["client_count"], 0)
        self.assertEqual(d["wedged"], 0)
        self.assertEqual(d["counters"]["jobs"], 20)
        self.assertEqual(d["error_counters"], {"timeouts": 0, "faults": 0})
        rows = sources.collect(["ane"])
        self.assertIn("ane.accel0.generation", {r["id"] for r in rows})
        self.assertTrue(all(r["writable"] is False for r in rows))
        self.assertEqual([r for r in ane.checks(ane.collect()) if r["name"].endswith("health")][0]["level"], "pass")

    def test_generation_h13_to_h17_and_unknown(self):
        for gen in range(13, 18):
            self.assertEqual(ane.generation(["apple,h%d-ane" % gen]), "H%d" % gen)
            self.assertEqual(ane.generation([], str(gen)), "H%d" % gen)
        for chip, gen in (("t8103", "H13"), ("t6021", "H14"), ("t6031", "H15"), ("t6041", "H16")):
            self.assertEqual(ane.generation(["apple,%s-ane" % chip]), gen)
        self.assertIsNone(ane.generation(["apple,t9999-ane"]))
        self.assertIsNone(ane.generation([], "18"))

    def test_no_ane_and_other_accelerators(self):
        self.assertEqual(ane.collect()["devices"], [])
        accelerator(self.root, driver="other", chip="unknown")
        w(self.root, "/sys/class/accel/accel0/device/of_node/compatible", "vendor,other\0")
        self.assertEqual(ane.collect()["devices"], [])
        self.assertEqual(ane.checks(ane.collect())[0]["level"], "warn")

    def test_missing_unbound_and_malformed_attributes(self):
        dev = accelerator(self.root, chip="t9999", driver=None)
        for field in ("firmware_version", "performance_level", "wedged", "ane_stats"):
            os.unlink(sysfs.root(dev + "/" + field))
        w(self.root, dev + "/clients", "not a count")
        d = ane.collect()["devices"][0]
        self.assertIsNone(d["generation"])
        self.assertIsNone(d["firmware_version"])
        self.assertIsNone(d["wedged"])
        self.assertEqual(d["error_counters"], {})
        self.assertEqual(ane.checks(ane.collect())[0]["level"], "fail")

    def test_dt_firmware_is_labelled_and_perf_alias(self):
        dev = accelerator(self.root, chip="t6021", driver="ane_t6021")
        os.unlink(sysfs.root(dev + "/firmware_version"))
        w(self.root, dev + "/of_node/apple,firmware-version", "26.1\0")
        os.unlink(sysfs.root(dev + "/performance_level"))
        w(self.root, dev + "/perf_level", "0")
        d = ane.collect()["devices"][0]
        self.assertEqual(d["generation"], "H14")
        self.assertEqual(d["firmware_version"], "26.1")
        self.assertIn("of_node", d["firmware_source"])
        self.assertEqual(d["performance_level"], "0")

    def test_clients_deduplicate_file_descriptions_per_device(self):
        accelerator(self.root)
        accelerator(self.root, "accel1", "t6021", "ane_t6021")
        for pid, fd, node, client in ((10, 3, "accel0", 8), (11, 4, "accel0", 8), (12, 3, "accel0", 9),
                                      (12, 4, "accel1", 8)):
            w(self.root, "/proc/%d/fdinfo/%d" % (pid, fd), "drm-client-id: %d\n" % client)
            p = sysfs.root("/proc/%d/fd" % pid)
            os.makedirs(p, exist_ok=True)
            os.symlink(sysfs.root("/dev/accel/" + node), p + "/" + str(fd))
        ds = ane.collect()["devices"]
        self.assertEqual([d["client_count"] for d in ds], [2, 1])
        self.assertTrue(all(d["clients_complete"] for d in ds))

    def test_proc_permissions_are_not_zero_clients(self):
        accelerator(self.root)
        w(self.root, "/proc/10/comm", "private")
        real = os.listdir

        def listing(path):
            if path == sysfs.root("/proc/10/fd"):
                raise PermissionError("restricted")
            return real(path)

        with patch.object(os, "listdir", side_effect=listing):
            d = ane.collect()["devices"][0]
        self.assertIsNone(d["client_count"])
        self.assertFalse(d["clients_complete"])

    def test_kernel_client_attribute(self):
        dev = accelerator(self.root)
        w(self.root, dev + "/client_count", "4")
        self.assertEqual(ane.collect()["devices"][0]["client_count"], 4)

    def test_wedged_fail_and_cumulative_errors_warn(self):
        dev = accelerator(self.root)
        w(self.root, dev + "/ane_stats", "jobs 100\nbusy_ns 20000\ntimeouts 2\ninvalid data\n")
        health = lambda: next(r for r in ane.checks(ane.collect()) if r["name"].endswith("health"))
        self.assertEqual(health()["level"], "warn")
        w(self.root, dev + "/wedged", "1")
        self.assertEqual(health()["level"], "fail")
        w(self.root, dev + "/wedged", "0")
        w(self.root, dev + "/power/runtime_status", "error")
        self.assertEqual(health()["level"], "fail")

    def test_saved_smoke_is_associated_and_private_fields_removed(self):
        accelerator(self.root)
        self.save({"name": "add-fixture", "chip": "t6000", "die": 0, "available": True,
                   "errors": 0, "min_ms": 0.03, "median_ms": 0.04, "secret": "PRIVATE"})
        data = ane.collect()
        self.assertEqual(data["devices"][0]["last_benchmark"]["median_ms"], 0.04)
        self.assertNotIn("PRIVATE", json.dumps(data))
        rows = ane.checks(data)
        self.assertEqual(rows[-1]["level"], "pass")

    def test_multidie_smoke_association_uses_dt_reg(self):
        accelerator(self.root)
        dev = accelerator(self.root, "accel1")
        with open(sysfs.root(dev + "/of_node/reg"), "wb") as f:
            f.write(struct.pack(">4I", 0x22, 0x85c04000, 0, 0x4000))
        self.save({"chip": "t6000", "die": 1, "median_ms": 0.04})
        ds = ane.collect()["devices"]
        self.assertIsNone(ds[0]["last_benchmark"])
        self.assertEqual(ds[1]["last_benchmark"]["die"], 1)

    def test_snapshot_ignores_volatile_status_and_detects_loss(self):
        dev = accelerator(self.root)
        a = snapshot.build(Machine(), api=False)
        w(self.root, dev + "/performance_level", "7")
        w(self.root, dev + "/ane_stats", "jobs 100\nbusy_ns 99999")
        b = snapshot.build(Machine(), api=False)
        self.assertEqual(a["ane"], b["ane"])
        os.unlink(sysfs.root(dev + "/driver"))
        b = snapshot.build(Machine(), api=False)
        self.assertTrue(any(r["section"] == "ane" and r["severity"] == "regression" for r in snapshot.diff(a, b)))
        baseline = os.path.join(self.root, "baseline.json")
        with open(baseline, "w") as f:
            json.dump(a, f)
        result = check.run(Machine(), only="ane", api=False, expect=baseline)
        self.assertTrue(any("baseline" in r["reason"] and r["level"] == "fail" for r in result["checks"]))

    def test_mismatched_and_ambiguous_benchmark_rejected(self):
        accelerator(self.root)
        self.save({"device": "/dev/accel/accel8", "median_ms": 1})
        self.assertIn("identify exactly one", ane.collect()["benchmark_error"])
        accelerator(self.root, "accel1", "t6000")
        self.save({"chip": "t6000", "median_ms": 1})
        self.assertIsNotNone(ane.collect()["benchmark_error"])

    def test_bad_saved_results_are_bounded_and_do_not_crash(self):
        accelerator(self.root)
        for raw in ("{", "[]", "x" * 65537, '{"chip":"t6000","median_ms":NaN}',
                    '{"chip":"t6000","errors":true}', '{"chip":"t6000","median_ms":-1}'):
            w(self.root, ane.BENCHMARK, raw)
            data = ane.collect()
            self.assertIsNotNone(data["benchmark_error"])
            self.assertEqual(len(data["devices"]), 1)

    def test_override_and_failed_saved_correctness(self):
        accelerator(self.root)
        os.environ["AURORA_DIAG_ANE_BENCHMARK"] = "/run/test-result.json"
        self.save({"device": "accel0", "median_ms": 0.04, "errors": 1}, "/run/test-result.json")
        self.assertEqual(ane.checks(ane.collect())[-1]["level"], "fail")

    def test_agent_check_only_and_timeout(self):
        accelerator(self.root)
        result = check.run(Machine(), only="ane", api=False)
        self.assertTrue(result["checks"])
        self.assertTrue(all(r["group"] == "ane" for r in result["checks"]))
        original = check.timed

        def timed(fn, timeout, *args):
            return (None, "timed out") if fn == ane.collect else original(fn, timeout, *args)

        with patch.object(check, "timed", side_effect=timed):
            result = check.run(Machine(), only="ane", api=False)
        self.assertEqual(result["exit"], 2)


def comparison_fixture():
    machine = {"compatible": ["apple,j616s", "apple,t6041", "apple,arm-platform"]}
    readings = [dict(label="Total System Power", value=10.0, unit="W", attr="power1_input"),
                dict(label="SMC PDBR", value=2.0, unit="W", attr="power2_input"),
                dict(label="Heatpipe Power", value=3.64, unit="W", attr="power3_input")]
    clocks = {"cpu": [dict(ladder_MHz=[1020, 2592]), dict(ladder_MHz=[1260, 4512])]}
    return machine, readings, clocks, {"devices": []}


class TestMacOSComparison(AneBase):
    def rows(self, args=None):
        return {r["id"]: r for r in comparison.build(*(args or comparison_fixture()))["rows"]}

    def test_baseline_values_and_live_deltas(self):
        rows = self.rows()
        self.assertEqual(rows["ane.b1.rate"]["value"], 3309)
        self.assertEqual(rows["ane.b8.p50"]["value"], 2.108)
        self.assertEqual(rows["idle.phpc"]["delta_pct"], 39.46)
        self.assertEqual(rows["idle.non_display"]["linux"], 8)
        self.assertEqual(rows["idle.phpc"]["level"], "warn")
        self.assertIn("conditions unverified", rows["idle.phpc"]["reason"])
        self.assertEqual(rows["cpu.p.max"]["level"], "pass")
        self.assertEqual(rows["cpu.p.states"]["level"], "warn")

    def test_machine_mismatch_has_no_delta(self):
        args = list(comparison_fixture())
        args[0] = {"compatible": ["apple,j316s"]}
        self.assertTrue(all(r["delta_pct"] is None for r in self.rows(args).values()))

    def test_missing_values_and_single_cluster_are_unknown(self):
        args = list(comparison_fixture())
        args[1] = []
        args[2] = {"cpu": [{"ladder_MHz": [600, 2064]}]}
        self.assertTrue(all(r["linux"] is None for r in self.rows(args).values()))

    def test_smoke_never_compares_to_mobilenet(self):
        accelerator(self.root)
        self.save({"chip": "t6000", "name": "add-fixture", "median_ms": 0.04, "errors": 0})
        args = list(comparison_fixture())
        args[3] = ane.collect()
        self.assertIsNone(self.rows(args)["ane.b1.p50"]["linux"])

    def test_matching_model_export_is_saved_not_live(self):
        accelerator(self.root, chip="t6041")
        self.save({"device": "accel0", "model": comparison.MOBILENET, "precision": "fp16", "batch": 1,
                   "ok": True, "p50_ms": 0.4, "throughput_per_s": 2500, "generated": "2026-10-31T00:00:00Z"})
        args = list(comparison_fixture())
        args[3] = ane.collect()
        rows = self.rows(args)
        self.assertEqual(rows["ane.b1.rate"]["linux"], 2500)
        self.assertEqual(rows["ane.b1.p50"]["measurement"], "saved")
        self.assertIsNone(rows["ane.b8.rate"]["linux"])

    def test_bad_baseline_and_override(self):
        os.environ["AURORA_DIAG_MACOS_BASELINE"] = "/etc/aurora-diag/baseline.json"
        for b in ({}, {"schema": "aurora-diag/macos-baseline", "version": 99}):
            w(self.root, os.environ["AURORA_DIAG_MACOS_BASELINE"], json.dumps(b))
            result = comparison.build(*comparison_fixture())
            self.assertIn("error", result)
            self.assertEqual(result["rows"], [])
        os.environ.pop("AURORA_DIAG_MACOS_BASELINE")
        baseline = comparison.load_baseline()
        baseline["metrics"][0]["value"] = float("nan")
        os.environ["AURORA_DIAG_MACOS_BASELINE"] = "/etc/aurora-diag/baseline.json"
        w(self.root, os.environ["AURORA_DIAG_MACOS_BASELINE"], json.dumps(baseline))
        self.assertIn("error", comparison.build(*comparison_fixture()))
        baseline["metrics"][0]["value"] = "19"
        w(self.root, os.environ["AURORA_DIAG_MACOS_BASELINE"], json.dumps(baseline))
        self.assertIn("error", comparison.build(*comparison_fixture()))

    def test_dashboard_collects_new_data_without_api(self):
        build(self.root)
        accelerator(self.root)
        result = dashboard.collect(Machine(), api=False)
        self.assertEqual(result["slow"]["ane"]["devices"][0]["generation"], "H13")
        self.assertIn("rows", result["slow"]["comparison"])
        json.dumps(result)

    def test_cli_documents_are_single_json_objects(self):
        accelerator(self.root)
        for cmd in ("ane", "compare"):
            result = subprocess.run([sys.executable, "-m", "aurora_diag", cmd, "--json"],
                                    env=dict(os.environ, AURORA_DIAG_ROOT=self.root), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["schema"], "aurora-diag/" + ("comparison" if cmd == "compare" else cmd))


if __name__ == "__main__":
    unittest.main()
