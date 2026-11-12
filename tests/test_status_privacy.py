# SPDX-License-Identifier: MIT
"""status (what works, fleet, firmware), report redaction and the audio
category under speakerguardd v2, against a fake /sys tree."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

from test_aurora_diag import Base, build, w  # noqa: E402
from aurora_diag import privacy, report, sections, status  # noqa: E402
from aurora_diag.machine import Machine  # noqa: E402


class Fleet(unittest.TestCase):
    def test_fleet_tag_from_release(self):
        self.assertEqual(status.fleet_tag("7.3.0-aurora-fleet67"), "fleet67")
        self.assertEqual(status.fleet_tag("7.3.0-aurora-fleet45w2+"), "fleet45w2")

    def test_fleet_tag_from_package(self):
        self.assertEqual(status.fleet_tag("7.3.0-27.aurora.fc44.aarch64", "kernel-aurora-7.3.0-27.fleet70.fc44"), "fleet70")

    def test_distro_kernel_has_no_fleet(self):
        self.assertIsNone(status.fleet_tag("7.3.0-400.asahi.fc44.aarch64", "kernel-16k-7.3.0-400.asahi.fc44"))
        self.assertIsNone(status.fleet_tag("6.18.2-1-ARCH"))


class Privacy(unittest.TestCase):
    def test_report_tree_is_redacted(self):
        red = privacy.Redactor(["alice", "Alice"], ["alice-mba"], ["M24Q34P403"], ["Home Wi-Fi 5G"], uuids=True)
        rep = {"host": "alice-mba", "kernel": {"cmdline": "root=UUID=58d61ca5-eb6c-4c7c-aefe-e01e3339b333 rw quiet"},
               "dmesg": {"last": ["brcmfmac: connected to Home Wi-Fi 5G aa:bb:cc:dd:ee:ff", "wld0: 10.1.1.20",
                                  "serial M24Q34P403 on alice-mba", "/home/alice/.config"]},
               "machine": {"board": "apple,j413", "model": "Apple MacBook Air (13-inch, M2, 2022)"},
               "readings": {"alice-mba temp": 41.5}, "generated": "2026-11-12T00:00:00Z", "version": "0"}
        out = report.redact(rep, red)
        text = json.dumps(out)
        for leak in ("alice", "M24Q34P403", "Home Wi-Fi 5G", "aa:bb:cc", "10.1.1.20", "58d61ca5"):
            self.assertNotIn(leak, text)
        self.assertEqual(out["host"], "<host>")
        self.assertTrue(out["redacted"])
        self.assertEqual(out["machine"]["board"], "apple,j413")
        self.assertIn("<host> temp", out["readings"])
        self.assertEqual(out["readings"]["<host> temp"], 41.5)
        self.assertIn("root=UUID=<uuid>", out["kernel"]["cmdline"])

    def test_djcheck_keeps_uuids(self):
        r = privacy.Redactor([], [], [])
        self.assertIn("58d61ca5-eb6c-4c7c-aefe-e01e3339b333", r("os_uuid 58d61ca5-eb6c-4c7c-aefe-e01e3339b333"))

    def test_write_names_files_by_board_with_private_modes(self):
        rep = {"host": "<host>", "generated": "2026-11-12T01:02:03Z", "machine": {"board": "apple,j413"},
               "version": "x", "redacted": True}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(report, "text", lambda r: "text\n"):
            js, txt = report.write(rep, d)
            self.assertEqual(os.path.basename(js), "aurora-diag-report-apple-j413-2026-11-12T010203Z.json")
            for p in (js, txt):
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_wifi_names_from_iw_and_nmcli(self):
        outs = {"iw": "phy#0\n\tInterface wld0\n\t\tssid Home Net\n", "nmcli": "Home Net:802-11-wireless\nOffice\\:5G:802-11-wireless\nWired:802-3-ethernet\n"}
        with mock.patch.object(privacy, "_out", lambda cmd, timeout=4: outs.get(cmd[0], "")):
            self.assertEqual(privacy.wifi_names(), ["Home Net", "Office:5G"])


class StatusAndAudio(Base):
    def test_audio_category_reports_speakerguardd_v2(self):
        build(self.root)
        w(self.root, "/run/speakerguardd-graph/state", "result=pass\nuser=someone\n")
        rs = [{"id": "speaker.speakerguardd-v2.service", "attr": "service", "value": "active", "unit": None},
              {"id": "speaker.speakerguardd.service", "attr": "service", "value": "inactive", "unit": None},
              {"id": "speaker.speakerguardd-v2.graph.result", "attr": "result", "value": "pass", "unit": None},
              {"id": "speaker.speakerguardd-v2.session.1000.amps", "attr": "amps", "value": "1", "unit": None}]
        with mock.patch.object(sections, "_desktop_user", lambda: None):
            s = sections.audio_section(rs)
        prot = [i for i in s["items"] if i["key"] == "speakerguardd"][0]
        self.assertEqual(prot["level"], "ok")
        self.assertIn("v2 active", prot["detail"])
        self.assertEqual(s["level"], "ok")
        self.assertEqual(s["facts"]["speakerguardd"], "v2 active")

    def test_audio_category_v2_failed_is_a_failure(self):
        build(self.root)
        rs = [{"id": "speaker.speakerguardd-v2.service", "attr": "service", "value": "failed", "unit": None}]
        with mock.patch.object(sections, "_desktop_user", lambda: None):
            s = sections.audio_section(rs)
        self.assertEqual(s["level"], "fail")

    def test_hardware_rows_reported_vs_working(self):
        secs = {
            "engines": sections.section("engines", "Engines", [sections.item("ane", "ok", "1/1 bound (ane)"),
                                                               sections.item("isp", "fail", "not bound"),
                                                               sections.item("prores", "absent", "no PRORES node")]),
            "radio": sections.section("radio", "Radio", [sections.item("Wi-Fi wld0", "ok", "brcmfmac"),
                                                         sections.item("other radios", "absent", "none")],
                                      facts={"wifi": {"wld0": {"firmware": "20.96.31"}}}),
            "storage": sections.section("storage", "Storage", [sections.item("NVMe nvme0", "warn", "hot")],
                                        facts={"nvme": {"nvme0": {"firmware": "561.100."}}}),
        }
        subs = [{"block": "ane", "driver": "ane", "dt_firmware": "17.1"}]
        rows = {r["key"]: r for r in status.hardware(secs, subs)}
        self.assertEqual(rows["ane"]["block"], "ANE (Neural Engine)")
        self.assertTrue(rows["ane"]["reported"] and rows["ane"]["working"])
        self.assertEqual(rows["ane"]["firmware"], "17.1")
        self.assertEqual(rows["isp"]["state"], "broken")
        self.assertTrue(rows["isp"]["reported"])
        self.assertFalse(rows["isp"]["working"])
        self.assertFalse(rows["prores"]["reported"])
        self.assertEqual(rows["Wi-Fi wld0"]["firmware"], "20.96.31")
        self.assertNotIn("other radios", rows)
        self.assertEqual(rows["NVMe nvme0"]["state"], "partial")
        self.assertEqual(rows["NVMe nvme0"]["firmware"], "561.100")

    def test_status_document_on_a_fake_machine(self):
        build(self.root)
        w(self.root, "/proc/device-tree/chosen/asahi,m1n1-stage2-version", "v1.4.21\0")
        doc = status.build(Machine(), api=False, timeout=10)
        self.assertEqual(doc["schema"], "aurora-diag/status")
        self.assertEqual(doc["software"]["boot_chain"].get("asahi,m1n1-stage2-version"), "v1.4.21")
        self.assertIn("Touch ID", [r["block"] for r in doc["hardware"]])
        self.assertTrue(all(set(r) >= {"block", "reported", "working", "state", "firmware"} for r in doc["hardware"]))
        text = status.text(doc)
        self.assertIn("Hardware", text)
        self.assertIn("fleet", text)
        self.assertIn(status.exit_code(doc), (0, 1, 2))


if __name__ == "__main__":
    unittest.main()
