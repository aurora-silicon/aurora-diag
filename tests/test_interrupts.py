# SPDX-License-Identifier: MIT
"""Synthetic IRQ tables and fake root only; no hardware, sleep or policy bypass."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from aurora_diag import cli, interrupts as irq, sysfs


def table(counts=(10, 0), linux_irq=199, suffix="AIC2 65799 Level 24f000000.error-handler", cpus=None):
    cpus = cpus if cpus is not None else ["CPU%d" % i for i in range(len(counts))]
    return ("           " + " ".join(cpus) + "\n" +
            " %s: %s %s\n" % (linux_irq, " ".join(str(c) for c in counts), suffix))


def delta(a, b, seconds=2):
    return irq.compare(irq.parse(a), irq.parse(b), seconds)


class ParserTests(unittest.TestCase):
    def test_reported_14_cpu_layout_and_identity(self):
        doc = irq.parse(table([172630643] + [0] * 13))
        self.assertEqual(doc["status"], "ok")
        row = doc["rows"][0]
        self.assertEqual(row["counts"], [172630643] + [0] * 13)
        self.assertEqual(row["cpus"], ["CPU%d" % i for i in range(14)])
        self.assertEqual((row["linux_irq"], row["controller"], row["hardware_irq"], row["trigger"], row["handler"]),
                         (199, "AIC2", 65799, "Level", "24f000000.error-handler"))

    def test_other_counts_noncontiguous_cpus_and_special_rows(self):
        for n in (1, 2, 8, 32):
            text = table([0] * n, cpus=["CPU%d" % (i * 2) for i in range(n)])
            text += "IPI0: " + "0 " * n + "Rescheduling interrupts\nERR: 0\nNMI: 0\n"
            self.assertEqual(len(irq.parse(text)["rows"]), 1)
        self.assertEqual(irq.parse("CPU0\n0: 12 IR-IO-APIC 2-edge timer\n")['status'], 'absent')
        self.assertEqual(irq.parse(table(suffix="AIC2 65799 Level other"))["reason"], "no_error_handler_rows")

    def test_missing_or_malformed_is_not_zero(self):
        cases = {"": "empty_input", "CPU0\n": "no_irq_rows", "CPU0\nERR: 0\n": "no_irq_rows",
                 "CPU0 CPU0\n": "invalid_cpu_header", "not a header\n": "invalid_cpu_header",
                 table().rstrip(): "truncated_input", "CPU0\nbad\n": "malformed_row",
                 "CPU0\n?: 0 AIC2 1 Level a.error-handler\n": "malformed_irq",
                 table((0,)): "ok", table(("-1",)): "invalid_counter",
                 table(("NaN",)): "invalid_counter", table((2**64,)): "invalid_counter",
                 "CPU0 CPU1\n1: 0 AIC2 1 Level a.error-handler\n": "invalid_counter",
                 table(suffix="AIC2 broken Level a.error-handler"): "invalid_identity",
                 table(suffix="0 65799 Level a.error-handler"): "invalid_identity"}
        for text, expected in cases.items():
            with self.subTest(expected=expected, text=text[:60]):
                doc = irq.parse(text)
                self.assertEqual(doc["status"] if expected == "ok" else doc["reason"], expected)
                if expected != "ok":
                    self.assertEqual(doc["rows"], [])
        self.assertEqual(irq.parse(table() + table().split("\n", 1)[1])["reason"], "duplicate_irq")

    def test_bounded_input_and_output(self):
        with patch.object(irq, "MAX_BYTES", 30):
            self.assertEqual(irq.parse(table())["reason"], "input_limit")
        with patch.object(irq, "MAX_CPUS", 1):
            self.assertEqual(irq.parse(table())["reason"], "invalid_cpu_header")
        two = table() + table(linux_irq=200).split("\n", 1)[1]
        with patch.object(irq, "MAX_ROWS", 1):
            self.assertEqual(irq.parse(two)["reason"], "row_limit")
        with patch.object(irq, "MAX_FOCUS", 1):
            self.assertEqual(irq.parse(two)["reason"], "focus_limit")
        self.assertEqual(irq.parse(table(suffix="AIC2 1 Level " + "a" * 257 + ".error-handler"))["reason"],
                         "invalid_identity")


class DeltaTests(unittest.TestCase):
    def test_actual_elapsed_and_per_cpu_deltas(self):
        row = delta(table((100, 10)), table((240100, 30)), 2.5)[0]
        self.assertEqual(row["delta_counts"], [240000, 20])
        self.assertEqual(row["delta"], 240020)
        self.assertEqual(row["rate_per_s"], 96008)
        self.assertTrue(row["baseline"])
        self.assertEqual(row["state"], "ok")

    def test_zero_is_measured(self):
        row = delta(table((0,)), table((0,)))[0]
        self.assertEqual(row["rate_per_s"], 0)
        self.assertIsNone(row["reason"])

    def test_resets_even_if_total_increases(self):
        row = delta(table((10, 20)), table((100000, 19)))[0]
        self.assertEqual(row["reason"], "counter_decreased")
        self.assertIsNone(row["delta"])
        self.assertIsNone(row["rate_per_s"])
        self.assertTrue(row["baseline"])
        self.assertEqual(delta(table((100000, 19)), table((100002, 19)))[0]["rate_per_s"], 1)

    def test_identity_and_cpu_changes_rebaseline(self):
        a = table()
        for b, reason in [(table(suffix="AIC2 65800 Level 24f000000.error-handler"), "identity_changed"),
                          (table(suffix="AIC3 65799 Level 24f000000.error-handler"), "identity_changed"),
                          (table(suffix="AIC2 65799 Edge 24f000000.error-handler"), "identity_changed"),
                          (table(suffix="AIC2 65799 Level 24f100000.error-handler"), "identity_changed"),
                          (table((0,)), "cpu_columns_changed"),
                          (table(cpus=["CPU1", "CPU0"]), "cpu_columns_changed"),
                          (table(cpus=["CPU0", "CPU2"]), "cpu_columns_changed")]:
            with self.subTest(reason=reason, b=b):
                row = delta(a, b)[0]
                self.assertEqual(row["reason"], reason)
                self.assertIsNone(row["rate_per_s"])
                self.assertTrue(row["baseline"])
        moved = delta(a, table(linux_irq=222))
        self.assertEqual([r["reason"] for r in moved], ["disappeared", "appeared"])

    def test_disappearance_reappearance_and_bad_reads(self):
        a = irq.parse(table())
        absent = irq.parse(table(suffix="AIC2 1 Level other"))
        gone = irq.compare(a, absent, 1)[0]
        self.assertEqual(gone["reason"], "disappeared")
        self.assertIsNone(gone["counts"])
        self.assertFalse(gone["baseline"])
        self.assertEqual(gone["cpus"], a["cpus"])
        self.assertEqual(irq.compare(absent, a, 1)[0]["reason"], "appeared")
        bad = irq.parse("")
        self.assertEqual(irq.compare(a, bad, 1)[0]["reason"], "empty_input")
        self.assertEqual(irq.compare(bad, a, 1)[0]["reason"], "previous_reading_unknown")
        self.assertEqual(irq.compare(None, a, None)[0]["reason"], "initial_baseline")
        for seconds in (0, -1, float("nan"), float("inf")):
            self.assertEqual(irq.compare(a, a, seconds)[0]["reason"], "invalid_elapsed")


class FakeRootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.root_patch = patch.object(sysfs, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def write(self, name, data):
        p = os.path.join(self.root, name.lstrip("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode("ascii"))

    def test_reads_and_io_failures(self):
        self.assertEqual(irq.read_interrupts()["reason"], "missing")
        self.write("/proc/interrupts", table())
        self.assertEqual(irq.read_interrupts()["status"], "ok")
        for error, reason in [(PermissionError(), "permission_denied"), (OSError(), "unreadable")]:
            with patch("builtins.open", side_effect=error):
                self.assertEqual(irq.read_interrupts()["reason"], reason)
        with patch.object(irq, "MAX_BYTES", 30):
            self.assertEqual(irq.read_interrupts()["reason"], "input_limit")
        self.write("/proc/interrupts", b"\xff\n")
        self.assertEqual(irq.read_interrupts()["reason"], "non_ascii_input")

    def test_metadata_allowlist(self):
        self.write("/proc/sys/kernel/osrelease", "7.3.0-aurora\n")
        self.write("/proc/device-tree/compatible",
                   b"apple,j616s\0apple,t6040\0apple,arm-platform\0private,serial\0")
        self.write("/proc/device-tree/serial-number", "SECRET")
        with patch("builtins.open", wraps=open) as opened:
            meta = irq.metadata()
        self.assertEqual(meta, {"kernel_release": "7.3.0-aurora", "compatible":
                               ["apple,j616s", "apple,t6040", "apple,arm-platform"]})
        self.assertEqual([c.args[0] for c in opened.call_args_list],
                         [sysfs.root("/proc/sys/kernel/osrelease"), sysfs.root("/proc/device-tree/compatible")])
        self.write("/proc/sys/kernel/osrelease", "x" * 200)
        self.assertIsNone(irq.metadata()["kernel_release"])

    def test_sampling_uses_actual_clock_and_keeps_baseline(self):
        readings = iter([table((100,)), table((240100,)), table((240100,))])
        original = irq.read_interrupts
        def reader():
            self.write("/proc/interrupts", next(readings))
            return original()
        with patch.object(irq, "read_interrupts", side_effect=reader), \
                patch.object(irq.time, "sleep") as sleep, \
                patch.object(irq.time, "monotonic", side_effect=[10, 10, 10.1, 12, 12.6, 14, 14.6, 14.7]):
            doc = irq.capture(interval=1, samples=2)
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(1), unittest.mock.call(1)])
        first, second, third = doc["readings"]
        self.assertEqual(first["rows"][0]["reason"], "initial_baseline")
        self.assertAlmostEqual(second["elapsed_s"], 2.5)
        self.assertAlmostEqual(second["rows"][0]["rate_per_s"], 96000)
        self.assertAlmostEqual(second["read_duration_s"], 0.6)
        self.assertEqual(third["rows"][0]["rate_per_s"], 0)
        self.assertEqual(doc["readings_completed"], 3)
        self.assertIn("zero does not exclude", irq.render(doc))
        self.assertIn("96000.00 IRQ/s", irq.render(doc))

    def test_timeout_stops_further_reads(self):
        with patch.object(irq, "timed", side_effect=[({}, None), (None, "timed out after 1s")]) as timed, \
                patch.object(irq.time, "sleep") as sleep, patch.object(irq.time, "monotonic", return_value=1):
            doc = irq.capture()
        self.assertEqual(timed.call_count, 2)
        sleep.assert_not_called()
        self.assertEqual(doc["readings_completed"], 1)
        self.assertEqual(doc["readings"][0]["reason"], "read_timeout")

    def test_cli_text_json_and_exit_semantics(self):
        self.write("/proc/interrupts", table((0,)))
        for flags in (["--json", "irq-watch"], ["irq-watch", "--json"], ["irq-watch"]):
            out = io.StringIO()
            with patch.object(irq.time, "sleep"), \
                    patch.object(irq.time, "monotonic", side_effect=range(100)), contextlib.redirect_stdout(out):
                rc = cli.main(flags + ["--samples", "1", "--log", os.path.join(self.root, "log")])
            self.assertEqual(rc, 0)
            if "--json" in flags:
                doc = json.loads(out.getvalue())
                self.assertEqual(doc["schema"], "aurora-diag/irq-watch")
                self.assertNotIn("host", doc)
                self.assertEqual(doc["readings"][1]["rows"][0]["rate_per_s"], 0)
            else:
                self.assertIn("Linux IRQ 199 | AIC2 65799", out.getvalue())
                self.assertIn("0.00 IRQ/s", out.getvalue())
        self.write("/proc/interrupts", "")
        with patch.object(irq.time, "sleep"), patch.object(irq.time, "monotonic", side_effect=range(100)), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["irq-watch", "--json", "--samples", "1", "--log", os.path.join(self.root, "log")]), 1)
        self.assertEqual(json.loads(out.getvalue())["readings"][1]["reason"], "empty_input")

    def test_invalid_cli_parameters_fail_before_reads_or_sleep(self):
        invalid = [["--interval", v] for v in ("0", "-1", "nan", "inf", "-inf", ".01", "6")]
        invalid += [["--samples", v] for v in ("0", "-1", "1.5", "nan", "inf", "61", "1000000")]
        invalid += [["--interval", "5", "--samples", "7"]]
        for flags in invalid:
            with self.subTest(flags=flags), patch.object(irq, "read_interrupts") as read, \
                    patch.object(cli, "Machine") as machine, patch.object(irq.time, "sleep") as sleep, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ex:
                cli.main(["irq-watch"] + flags)
            self.assertEqual(ex.exception.code, 2)
            read.assert_not_called()
            machine.assert_not_called()
            sleep.assert_not_called()
        irq.validate(.5, 60)
        irq.validate(5, 6)
        for interval, count in [(0, 1), (float("nan"), 1), (1, True), (1, 1.5)]:
            with self.assertRaises(ValueError):
                irq.capture(interval, count)


if __name__ == "__main__":
    unittest.main()
