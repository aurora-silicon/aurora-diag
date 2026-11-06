# SPDX-License-Identifier: MIT
"""Synthetic boot pairs: numbering is noise, device identity and failures are not."""

import contextlib
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aurora_diag import check, cli, identity, services, snapshot, subsystems
from aurora_diag.machine import Machine
from test_aurora_diag import Base


RENUMBERED = [
    ("drm/card1", "drm/card2"),
    ("drm/card1-eDP-1", "drm/card2-eDP-1"),
    ("drm/card1-HDMI-A-2", "drm/card2-HDMI-A-2"),
    ("drm/renderD128", "drm/renderD129"),
    ("i2c-dev/i2c-0", "i2c-dev/i2c-3"),
    ("mtd/mtd2", "mtd/mtd3"), ("mtd/mtd2ro", "mtd/mtd3ro"),
    ("dma/dma0chan7", "dma/dma2chan7"),
    ("typec/port1", "typec/port2"),
    ("hidraw/hidraw0", "hidraw/hidraw8"),
    ("input/input2", "input/input9"), ("input/event2", "input/event9"),
    ("leds/input2::capslock", "leds/input9::capslock"),
    ("hwmon/hwmon2", "hwmon/hwmon9"),
    ("wakeup/wakeup1", "wakeup/wakeup8"),
    ("power_supply/tps6598x-source-psy-0-0038", "power_supply/tps6598x-source-psy-3-0038"),
    ("video4linux/video0", "video4linux/video2"),
    ("usbmon/usbmon1", "usbmon/usbmon3"),
    ("usbmisc/hiddev1", "usbmisc/hiddev4"),
    ("sound/controlC1", "sound/controlC0"),
    ("sound/pcmC1D2c", "sound/pcmC0D2c"),
]


def boot(devices=(), details=None, state="active"):
    s = {"kernel": {"release": "test-kernel"}, "firmware": {}, "subsystems": {"soc/display@1234": {
        "driver": "display", "level": "ok", "why": "bound", "class_devices": list(devices)}},
        "services": {"fprintd.service": state}}
    if details is not None:
        s["service_details"] = {"fprintd.service": details}
    return s


def unit(**extra):
    return dict({"unit": "fprintd.service", "active": "inactive", "sub": "dead",
                 "enabled": "static", "result": "success", "load": "loaded",
                 "bus_activatable": True, "active_sockets": []}, **extra)


def current_sub(devices=(), ids=None):
    s = {"node": "soc/display@1234", "block": "dcp", "name": "display@1234", "kind": "block",
         "driver": "display", "state": {"level": "ok", "why": "bound"}, "class_devices": list(devices)}
    if ids is not None:
        s["class_device_ids"] = ids
    return s


class TestIdentity(unittest.TestCase):
    def test_legacy_boot_pair_and_mixed_version(self):
        for old, new in RENUMBERED:
            with self.subTest(old=old):
                a, b = boot([old]), boot([new])
                self.assertEqual(snapshot.diff(a, b), [])
                b["subsystems"]["soc/display@1234"]["class_device_ids"] = {
                    new: {"path": "drm/cardN", "name": None}}
                self.assertEqual(snapshot.diff(a, b), [])
                a["subsystems"]["soc/display@1234"]["class_device_ids"] = {
                    old: {"path": identity.normalize(old), "name": None}}
                b["subsystems"]["soc/display@1234"]["class_device_ids"] = {
                    new: {"path": identity.normalize(new), "name": None}}
                self.assertEqual(snapshot.diff(a, b), [])

    def test_meaningful_numbers_are_preserved(self):
        for old, new in [("drm/card1-eDP-1", "drm/card2-eDP-2"),
                         ("dma/dma0chan7", "dma/dma2chan8"),
                         ("power_supply/tps6598x-source-psy-0-0038", "power_supply/tps6598x-source-psy-3-003a"),
                         ("sound/pcmC0D1p", "sound/pcmC1D2p"),
                         ("unknown/device1", "unknown/device2")]:
            with self.subTest(old=old):
                self.assertTrue(any(c["severity"] == "regression" for c in snapshot.diff(boot([old]), boot([new]))))

    def test_missing_device_count_and_swapped_numbers(self):
        a, b = boot(["input/event1", "input/event2"]), boot(["input/event2"])
        self.assertEqual(identity.class_delta(next(iter(a["subsystems"].values())),
                                              next(iter(b["subsystems"].values())))[0], ["input/event1"])
        self.assertTrue(any(c["severity"] == "regression" for c in snapshot.diff(a, b)))
        a, b = boot(["input/event1"]), boot(["input/event1"])
        for s, name in ((a, "keyboard"), (b, "touchpad")):
            next(iter(s["subsystems"].values()))["class_device_ids"] = {
                "input/event1": {"path": "input/inputN/eventN", "name": name}}
        self.assertTrue(any(c["severity"] == "regression" for c in snapshot.diff(a, b)))

    def test_stable_path_distinguishes_same_named_devices(self):
        a, b = boot(["hwmon/hwmon1"]), boot(["hwmon/hwmon2"])
        for s, raw, address in ((a, "hwmon/hwmon1", "38"), (b, "hwmon/hwmon2", "3a")):
            next(iter(s["subsystems"].values()))["class_device_ids"] = {
                raw: {"path": "i2c-N/N-00" + address + "/hwmon/hwmonN", "name": "sensor"}}
        self.assertTrue(any(c["severity"] == "regression" for c in snapshot.diff(a, b)))

    def test_path_normalization_preserves_hardware_addresses(self):
        self.assertEqual(identity.normalize("i2c-0/0-0038/hwmon/hwmon1"),
                         identity.normalize("i2c-3/3-0038/hwmon/hwmon8"))
        self.assertNotEqual(identity.normalize("i2c-0/0-0038/hwmon/hwmon1"),
                            identity.normalize("i2c-3/3-003a/hwmon/hwmon8"))
        self.assertNotEqual(identity.normalize("usb1/1-2/1-2:1.0"),
                            identity.normalize("usb3/3-3/3-3:1.0"))

    def test_unbound_driver_still_regresses(self):
        a, b = boot(["drm/card1"]), boot([])
        next(iter(b["subsystems"].values())).update(driver=None, level="fail")
        self.assertTrue(any(c["key"].endswith(" driver") and c["severity"] == "regression"
                            for c in snapshot.diff(a, b)))


class TestExpect(Base):
    def run_expect(self, old, sub=None, units=()):
        old.update(schema=snapshot.SCHEMA, version=1)
        path = os.path.join(self.root, "baseline.json")
        with open(path, "w") as f:
            json.dump(old, f)
        with contextlib.ExitStack() as stack:
            for target, value in [("subsystems.subsystems", [sub] if sub else []),
                                  ("services.system_units", list(units)), ("services.speakerguardd", {}),
                                  ("subsystems.dmesg", []), ("snapshot._sensor_ids", []),
                                  ("gpu.gpu_names", []), ("ane.collect", {"devices": []}), ("ane.checks", [])]:
                stack.enter_context(patch("aurora_diag.check." + target, return_value=value))
            return check.run(Machine(), expect=path, api=False, secs={})["checks"]

    def test_expect_uses_same_identity_and_counts(self):
        for old, new in RENUMBERED:
            with self.subTest(old=old):
                rows = self.run_expect(boot([old]), current_sub([new]))
                self.assertEqual(next(r for r in rows if r["group"] == "subsys")["level"], "pass")
        rows = self.run_expect(boot(["drm/card1-eDP-1"]), current_sub(["drm/card2-eDP-2"]))
        self.assertEqual(next(r for r in rows if r["group"] == "subsys")["level"], "fail")

    def test_expect_stable_identity_loss_even_with_same_raw_name(self):
        a = boot(["hwmon/hwmon1"])
        next(iter(a["subsystems"].values()))["class_device_ids"] = {
            "hwmon/hwmon1": {"path": "hwmon/hwmonN", "name": "sensor-a"}}
        rows = self.run_expect(a, current_sub(["hwmon/hwmon1"], {
            "hwmon/hwmon1": {"path": "hwmon/hwmonN", "name": "sensor-b"}}))
        self.assertEqual(next(r for r in rows if r["group"] == "subsys")["level"], "fail")

    def test_expect_idle_service_and_failure(self):
        for changed, expected in [({}, "pass"), ({"active": "failed"}, "fail"),
                                  ({"load": "not-found"}, "fail"), ({"enabled": "masked"}, "fail"),
                                  ({"result": "exit-code"}, "fail"), ({"bus_activatable": False}, "fail")]:
            with self.subTest(changed=changed):
                u = unit(); u.update(changed)
                rows = self.run_expect(boot(), units=[u])
                self.assertEqual(next(r for r in rows if r["group"] == "service")["level"], expected)

    def test_expect_missing_service_and_lost_activation(self):
        for state in ("active", "inactive"):
            old = boot(details=unit(), state=state)
            rows = self.run_expect(old, units=[])
            self.assertEqual(next(r for r in rows if r["group"] == "service")["level"], "fail")
            u = unit(); u["bus_activatable"] = False
            rows = self.run_expect(old, units=[u])
            self.assertEqual(next(r for r in rows if r["group"] == "service")["level"], "fail")

    def test_cli_exit_for_renumber_and_real_loss(self):
        a, b = boot(["mtd/mtd2ro"]), boot(["mtd/mtd3ro"])
        for s, name in ((a, "old"), (b, "new")):
            s.update(schema=snapshot.SCHEMA, version=1)
            with open(os.path.join(self.root, name), "w") as f:
                json.dump(s, f)
        args = SimpleNamespace(old=os.path.join(self.root, "old"), new=os.path.join(self.root, "new"), quiet=True, json=True)
        with patch("aurora_diag.cli.log.event"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_diff(args, None), 0)
            next(iter(b["subsystems"].values()))["class_devices"] = []
            with open(args.new, "w") as f:
                json.dump(b, f)
            self.assertEqual(cli.cmd_diff(args, None), 2)

    def test_collect_from_sysfs_symlinks(self):
        parent = "/sys/devices/platform/display@1234"
        a, b = {}, {}
        for number, result in ((1, a), (2, b)):
            real = parent + "/drm/card%d/card%d-eDP-1" % (number, number)
            os.makedirs(os.path.join(self.root, real.lstrip("/")))
            directory = os.path.join(self.root, "sys/class/drm")
            os.makedirs(directory, exist_ok=True)
            os.symlink(os.path.join(self.root, real.lstrip("/")), os.path.join(directory, "card%d-eDP-1" % number))
            result.update(identity.collect(subsystems.class_devices(), parent))
            os.unlink(os.path.join(directory, "card%d-eDP-1" % number))
        self.assertEqual(list(a.values()), list(b.values()))
        self.assertEqual(list(a.values())[0]["path"], "drm/cardN/cardN-eDP-1")


class TestActivation(Base):
    def test_bus_registration_and_systemctl_metadata(self):
        from test_aurora_diag import w
        w(self.root, "/usr/share/dbus-1/system-services/fprint.service",
          "[D-BUS Service]\nName=net.example.Fprint\nSystemdService=fprintd.service\n")
        self.assertEqual(services._bus_names(), {"net.example.Fprint"})
        out = ("Id=fprintd.service\nActiveState=inactive\nSubState=dead\nUnitFileState=static\n"
               "LoadState=loaded\nType=dbus\nBusName=net.example.Fprint\nResult=success\n\n")
        with patch.object(services, "_run", return_value=(0, out)):
            self.assertTrue(services.idle_activatable(services._show_units(["systemctl"], ["fprintd.service"])[0]))
        with patch.object(services, "_run", return_value=(0, out.replace("net.example.Fprint", "net.missing.Service"))):
            self.assertFalse(services.idle_activatable(services._show_units(["systemctl"], ["fprintd.service"])[0]))

    def test_live_socket_required(self):
        service = ("Id=pipewire.service\nActiveState=inactive\nLoadState=loaded\n"
                   "UnitFileState=disabled\nResult=success\nTriggeredBy=pipewire.socket\n\n")
        for active in ("active", "inactive", "failed"):
            sock = "Id=pipewire.socket\nLoadState=loaded\nActiveState=" + active + "\n"
            with patch.object(services, "_run", side_effect=[(0, service), (0, sock)]):
                u = services._show_units(["systemctl", "--user"], ["pipewire.service"], bus_names=set())[0]
                self.assertEqual(services.idle_activatable(u), active == "active")

    def test_snapshot_activation_and_failure(self):
        a = boot(details=unit(), state="active")
        for changed, regression in [({}, False), ({"active": "failed"}, True),
                                    ({"enabled": "masked"}, True), ({"load": "not-found"}, True),
                                    ({"result": "exit-code"}, True), ({"bus_activatable": False}, True)]:
            with self.subTest(changed=changed):
                u = unit(); u.update(changed)
                b = boot(details=u, state=u["active"])
                self.assertEqual(any(c["severity"] == "regression" for c in snapshot.diff(a, b)), regression)
        # Losing activation is a regression even when both snapshots say inactive.
        a["services"]["fprintd.service"] = "inactive"
        self.assertTrue(any(c["severity"] == "regression" for c in snapshot.diff(a, b)))

    def test_legacy_fprint_only_exception(self):
        self.assertFalse(any(c["severity"] == "regression" for c in snapshot.diff(boot(), boot(state="inactive"))))
        for new in (None, "failed", "unknown"):
            self.assertTrue(any(c["severity"] == "regression" for c in snapshot.diff(boot(), boot(state=new))))
        a, b = boot(), boot()
        a["services"], b["services"] = {"ordinary.service": "active"}, {"ordinary.service": "inactive"}
        self.assertTrue(any(c["severity"] == "regression" for c in snapshot.diff(a, b)))


if __name__ == "__main__":
    unittest.main()
