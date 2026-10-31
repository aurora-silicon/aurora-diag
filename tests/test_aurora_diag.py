# SPDX-License-Identifier: MIT
"""Tests against a fake /sys tree: no hardware, no root needed.

    python3 -m unittest discover -s tests
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from aurora_diag import audio, control, display, fans, log, sources, sysfs  # noqa: E402
from aurora_diag.machine import Machine  # noqa: E402


def w(root, path, text, mode=0o644):
    p = os.path.join(root, path.lstrip("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        os.chmod(p, 0o644)
    with open(p, "w") as f:
        f.write(text)
    os.chmod(p, mode)


def build(root, compat=b"apple,j316s\0apple,t6000\0apple,arm-platform\0", fans_present=True, die=50000):
    p = os.path.join(root, "proc/device-tree")
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "compatible"), "wb") as f:
        f.write(compat)
    with open(os.path.join(p, "model"), "wb") as f:
        f.write(b"Apple Test\0")
    h = "/sys/class/hwmon/hwmon2"
    w(root, h + "/name", "macsmc_hwmon\n", 0o444)
    w(root, h + "/temp1_input", "41250\n", 0o444)
    w(root, h + "/temp1_label", "NAND Flash Temperature\n", 0o444)
    w(root, h + "/power1_input", "5300000\n", 0o444)
    w(root, h + "/power1_label", "Total System Power\n", 0o444)
    if fans_present:
        for n in (1, 2):
            w(root, h + "/fan%d_input" % n, "0\n", 0o444)
            w(root, h + "/fan%d_label" % n, "Fan %d\n" % n, 0o444)
            w(root, h + "/fan%d_min" % n, "2317\n", 0o444)
            w(root, h + "/fan%d_max" % n, "5779\n", 0o444)
            w(root, h + "/fan%d_target" % n, "0\n", 0o644)
    for n in (5, 6):
        w(root, "/sys/class/hwmon/hwmon%d/name" % n, "tas2764\n", 0o444)
        w(root, "/sys/class/hwmon/hwmon%d/temp1_input" % n, "30000\n", 0o444)
    z = "/sys/class/thermal/thermal_zone1"
    w(root, z + "/type", "cpu-die-thermal\n", 0o444)
    w(root, z + "/temp", "%d\n" % die, 0o444)
    w(root, z + "/mode", "enabled\n")
    w(root, z + "/policy", "power_allocator\n")
    w(root, z + "/trip_point_0_type", "passive\n", 0o444)
    w(root, z + "/trip_point_0_temp", "95000\n")
    w(root, z + "/trip_point_1_type", "critical\n", 0o444)
    w(root, z + "/trip_point_1_temp", "110000\n")
    w(root, "/sys/class/thermal/cooling_device0/type", "cpufreq-cpu0\n", 0o444)
    w(root, "/sys/class/thermal/cooling_device0/cur_state", "0\n")
    w(root, "/sys/class/thermal/cooling_device0/max_state", "16\n", 0o444)
    b = "/sys/class/power_supply/macsmc-battery"
    w(root, b + "/type", "Battery\n", 0o444)
    w(root, b + "/capacity", "99\n", 0o444)
    w(root, b + "/voltage_now", "12749000\n", 0o444)
    w(root, b + "/constant_charge_voltage", "12918000\n")
    w(root, b + "/charge_control_end_threshold", "100\n")
    w(root, b + "/serial_number", "SECRET\n", 0o444)
    w(root, "/sys/bus/iio/devices/iio:device1/name", "aop-sensors-las\n", 0o444)
    w(root, "/sys/bus/iio/devices/iio:device1/in_angl_raw", "112\n", 0o444)
    w(root, "/sys/bus/iio/devices/iio:device0/name", "aop-sensors-als\n", 0o444)
    w(root, "/sys/bus/iio/devices/iio:device0/in_illuminance_input", "19.14\n", 0o444)
    w(root, "/sys/class/backlight/apple-panel-bl/brightness", "156\n")
    w(root, "/sys/class/backlight/apple-panel-bl/max_brightness", "509\n", 0o444)
    w(root, "/sys/class/leds/kbd_backlight/brightness", "0\n")
    w(root, "/sys/class/leds/kbd_backlight/max_brightness", "255\n", 0o444)
    w(root, "/sys/class/leds/kbd_backlight/trigger", "[none] usb-gadget kbd-capslock\n")
    w(root, "/sys/module/appledrm/parameters/show_notch", "N\n")
    w(root, "/sys/module/macsmc_hwmon/parameters/fan_control", "Y\n")
    w(root, "/sys/module/spi_apple_qspi/parameters/read_only", "Y\n")
    w(root, "/sys/module/snd_soc_macaudio/parameters/please_blow_up_my_speakers", "0\n")
    w(root, "/sys/class/drm/card1-eDP-1/status", "connected\n")
    w(root, "/sys/class/drm/card1-eDP-1/enabled", "enabled\n", 0o444)
    w(root, "/sys/class/drm/card1-eDP-1/dpms", "On\n", 0o444)
    w(root, "/sys/class/drm/card1-eDP-1/modes", "3024x1890\n", 0o444)
    w(root, "/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq", "1704000\n", 0o444)
    w(root, "/sys/devices/system/cpu/cpufreq/policy0/scaling_max_freq", "2064000\n")
    w(root, "/sys/kernel/debug/apple-pmc/x/status", "static agents: 0xb\n", 0o444)
    w(root, "/sys/kernel/debug/aurora-g17/capacity", "pool 0\n", 0o444)
    w(root, "/sys/kernel/debug/asahi-g16/stats", "jobs 0\n", 0o444)
    w(root, "/sys/kernel/debug/tracing/trace_pipe", "never read\n", 0o444)
    w(root, "/run/speakerguardd/state",
      "speakerguardd-state 2\nboottime_ns 9\ncovered_ns 9\narmed 1\nreleased 0\n"
      "# speaker name t_coil_C t_magnet_C mean_1s_W mean_60s_W v_rms_V reduction_dB hard t_peak_C\n"
      "speaker Left_Woofer 51.5 50.0 0.1 0.0 0.002 0.000 0 51.5\n"
      "# unified name ff_coil_C ff_magnet_C vi_coil_C vi_magnet_C reduction_dB mean_1s_W mean_60s_W hard t_peak_C\n"
      "unified Left_Woofer 52.0 50.0 50.5 50.0 0.0 0.0 0.0 0 52.0\nend\n", 0o600)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "root")
        os.makedirs(self.root)
        self.run = os.path.join(self.tmp.name, "run")
        os.environ["AURORA_DIAG_RUN"] = self.run
        os.environ["AURORA_DIAG_LOG"] = os.path.join(self.tmp.name, "log.jsonl")
        os.environ["AURORA_DIAG_ROOT"] = self.root
        sysfs.ROOT = self.root
        log.setup(path=os.environ["AURORA_DIAG_LOG"])

    def tearDown(self):
        sysfs.ROOT = "/"
        self.tmp.cleanup()

    def logs(self):
        with open(os.environ["AURORA_DIAG_LOG"]) as f:
            return [json.loads(x) for x in f]


class TestRead(Base):
    def test_generic_enumeration(self):
        build(self.root)
        rs = sources.collect()
        ids = {r["id"]: r for r in rs}
        self.assertEqual(ids["hwmon.macsmc_hwmon.temp1_input"]["value"], 41.25)
        self.assertEqual(ids["hwmon.macsmc_hwmon.temp1_input"]["label"], "NAND Flash Temperature")
        self.assertEqual(ids["hwmon.macsmc_hwmon.power1_input"]["unit"], "W")
        self.assertIn("hwmon.tas2764-1.temp1_input", ids)
        self.assertIn("hwmon.tas2764-2.temp1_input", ids)
        self.assertEqual(ids["iio.aop-sensors-las.in_angl_raw"]["label"], "lid angle")
        self.assertEqual(ids["iio.aop-sensors-als.in_illuminance_input"]["unit"], "lux")
        self.assertEqual(ids["thermal.thermal_zone1.temp"]["value"], 50.0)
        self.assertEqual(ids["psy.macsmc-battery.voltage_now"]["value"], 12.749)
        self.assertNotIn("psy.macsmc-battery.serial_number", ids)
        self.assertEqual(ids["leds.kbd_backlight.trigger"]["value"], "none")
        self.assertIn("debugfs.apple-pmc/x/status", ids)
        self.assertIn("debugfs.aurora-g17/capacity", ids)
        self.assertIn("debugfs.asahi-g16/stats", ids)
        self.assertFalse(any("tracing" in i for i in ids))

    def test_speakerguardd_state(self):
        build(self.root)
        rs = {r["id"]: r for r in audio.speaker_protection()}
        self.assertEqual(rs["speaker.speakerguardd.speaker.Left_Woofer.t_coil_C"]["value"], 51.5)
        self.assertEqual(rs["speaker.speakerguardd.unified.Left_Woofer.vi_coil_C"]["value"], 50.5)
        self.assertEqual(rs["speaker.speakerguardd.armed"]["value"], 1)
        self.assertTrue(all(not r["writable"] for r in rs.values()))

    def test_machine(self):
        build(self.root)
        m = Machine()
        self.assertTrue(m.known)
        self.assertEqual(m.soc, "apple,t6000")
        self.assertEqual(m.board, "apple,j316s")

    def test_unknown_machine_is_read_only(self):
        build(self.root, compat=b"acme,board\0acme,soc\0")
        m = Machine()
        self.assertFalse(m.known)
        with self.assertRaises(control.Refused):
            control.apply_sysfs("/sys/class/hwmon/hwmon2/fan1_target", "3000", m)


class TestPolicy(Base):
    def setUp(self):
        super().setUp()
        build(self.root)
        self.m = Machine()

    def refused(self, path, value="1"):
        with self.assertRaises(control.Refused):
            control.apply_sysfs(path, value, self.m)

    def test_safety_exclusions(self):
        self.refused("/sys/class/power_supply/macsmc-battery/constant_charge_voltage", "13000000")
        self.refused("/sys/class/thermal/thermal_zone1/mode", "disabled")
        self.refused("/sys/class/thermal/thermal_zone1/trip_point_1_temp", "130000")
        self.refused("/sys/module/spi_apple_qspi/parameters/read_only", "N")
        self.refused("/sys/module/snd_soc_macaudio/parameters/please_blow_up_my_speakers")
        self.refused("/sys/module/macsmc_hwmon/parameters/fan_control", "Y")
        self.refused("/sys/kernel/debug/apple-pmc/x/status")
        self.refused("/run/speakerguardd/state", "x")
        self.refused("/sys/class/drm/card1-eDP-1/status", "off")
        self.assertIsNotNone(control.alsa_refusal("Speaker Volume Unlock"))
        self.assertIsNotNone(control.alsa_refusal("Left Woofer VSENSE Switch"))
        self.assertIsNone(control.alsa_refusal("Headphone Playback Volume"))
        self.assertIsNone(control.alsa_refusal("Jack DAC Playback Volume"))
        self.assertIsNotNone(control.alsa_refusal("Left Tweeter Amp Gain Volume"))
        self.assertIsNotNone(control.alsa_refusal("Speaker Playback Mux"))

    def test_unknown_machine_is_read_only(self):
        with open(os.path.join(self.root, "proc/device-tree/compatible"), "wb") as f:
            f.write(b"vendor,board\0vendor,soc\0")
        m = Machine()
        with self.assertRaises(control.Refused):
            control.apply_sysfs("/sys/class/backlight/apple-panel-bl/brightness", "10", m)
        control.apply_sysfs("/sys/class/backlight/apple-panel-bl/brightness", "10", m, allow_unknown=True)

    def test_set_and_restore(self):
        bl = "/sys/class/backlight/apple-panel-bl/brightness"
        control.apply_sysfs(bl, "10", self.m)
        control.apply_sysfs(bl, "20", self.m)
        control.apply_sysfs("/sys/class/leds/kbd_backlight/trigger", "kbd-capslock", self.m)
        control.apply_sysfs("/sys/class/thermal/thermal_zone1/trip_point_0_temp", "90000", self.m)
        self.assertEqual(sysfs.read(bl), "20")
        self.assertEqual(len(control.journal_entries()), 3)
        restored, failed = control.restore()
        self.assertEqual(failed, [])
        self.assertEqual(sysfs.read(bl), "156")
        self.assertEqual(sysfs.read("/sys/class/leds/kbd_backlight/trigger"), "none")
        self.assertEqual(sysfs.read("/sys/class/thermal/thermal_zone1/trip_point_0_temp"), "95000")
        self.assertEqual(control.journal_entries(), [])
        ev = [e["event"] for e in self.logs()]
        self.assertIn("set", ev)
        self.assertIn("restored", ev)

    def test_notch_reports_not_live(self):
        res = display.set_notch(self.m, True)
        self.assertFalse(res["live"])
        self.assertEqual(sysfs.read(display.NOTCH_PARAM), "Y")
        control.restore()
        self.assertEqual(sysfs.read(display.NOTCH_PARAM), "N")


class TestFans(Base):
    def test_fanless_refused(self):
        build(self.root, compat=b"apple,j700\0apple,t8140\0apple,arm-platform\0", fans_present=False)
        with self.assertRaisesRegex(control.Refused, "fanless"):
            fans.set_fan(Machine(), 1, 3000, start_guard=False)

    def test_limits_and_hot_refused(self):
        build(self.root, die=95000)
        m = Machine()
        with self.assertRaisesRegex(control.Refused, "minimum"):
            fans.set_fan(m, 1, 100, start_guard=False)
        with self.assertRaisesRegex(control.Refused, "limit"):
            fans.set_fan(m, 1, 3000, start_guard=False)

    def test_readonly_target_explains(self):
        build(self.root)
        os.chmod(os.path.join(self.root, "sys/class/hwmon/hwmon2/fan1_target"), 0o444)
        with self.assertRaisesRegex(control.Refused, "fan_control=1"):
            fans.set_fan(Machine(), 1, 3000, start_guard=False)

    def _guard(self, max_temp=90):
        env = dict(os.environ)
        code = ("import sys; sys.path.insert(0, %r); from aurora_diag import fans, log, sysfs; "
                "sysfs.ROOT=%r; log.setup(); fans.run_guard(%r, interval=0.1, blind_limit=0.5)"
                % (os.path.dirname(HERE), self.root, max_temp))
        return subprocess.Popen([sys.executable, "-c", code], env=env)

    def _wait(self, p, t=10):
        p.wait(timeout=t)

    def test_guard_temperature_trip(self):
        build(self.root)
        m = Machine()
        fans.set_fan(m, 1, 3000, lease=60, start_guard=False)
        self.assertEqual(sysfs.read("/sys/class/hwmon/hwmon2/fan1_target"), "3000")
        p = self._guard()
        time.sleep(0.5)
        self.assertIsNone(p.poll())
        w(self.root, "/sys/class/thermal/thermal_zone1/temp", "91000\n", 0o644)
        self._wait(p)
        self.assertEqual(sysfs.read("/sys/class/hwmon/hwmon2/fan1_target"), "0")
        self.assertEqual(fans.fan_entries(), [])
        self.assertTrue(any(e["event"] == "guard_exit" and "passed" in e["reason"] for e in self.logs()))

    def test_guard_lease_expiry(self):
        build(self.root)
        fans.set_fan(Machine(), 2, 4000, lease=1, start_guard=False)
        p = self._guard()
        self._wait(p)
        self.assertEqual(sysfs.read("/sys/class/hwmon/hwmon2/fan2_target"), "0")

    def test_guard_blind_fails_closed(self):
        build(self.root)
        fans.set_fan(Machine(), 1, 3000, lease=60, start_guard=False)
        p = self._guard()
        time.sleep(0.3)
        os.unlink(os.path.join(self.root, "sys/class/thermal/thermal_zone1/temp"))
        self._wait(p)
        self.assertEqual(sysfs.read("/sys/class/hwmon/hwmon2/fan1_target"), "0")

    def test_guard_sigkill_is_bounded_by_restore(self):
        build(self.root)
        fans.set_fan(Machine(), 1, 3000, lease=60, start_guard=False)
        p = self._guard()
        time.sleep(0.3)
        p.terminate()
        self._wait(p)
        self.assertEqual(sysfs.read("/sys/class/hwmon/hwmon2/fan1_target"), "0")


if __name__ == "__main__":
    unittest.main()
