# SPDX-License-Identifier: MIT
"""Demos: discovery, the motion view, result parsing, against a fake tree."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from aurora_diag import demos, sections, subsystems  # noqa: E402
from test_aurora_diag import Base, build, w  # noqa: E402


class TestDemos(Base):
    def setUp(self):
        super().setUp()
        build(self.root)
        subsystems._dmesg_cache.clear()
        for k in ("emerg,alert,crit,err", "warn", "emerg,alert,crit,err,warn,notice,info"):
            subsystems._dmesg_cache[k] = []
        sections._MODALIAS.clear()
        demos.DEMOS = None

    def add_motion(self):
        a = "/sys/bus/iio/devices/iio:device7"
        w(self.root, a + "/name", "aop-sensors-accel\n")
        for ax, v in zip("xyz", (-926, 284, -64458)):
            w(self.root, a + "/in_accel_%s_raw" % ax, "%d\n" % v)
        w(self.root, a + "/in_accel_scale", "0.000149638\n")
        g = "/sys/bus/iio/devices/iio:device8"
        w(self.root, g + "/name", "aop-sensors-gyro\n")
        for ax, v in zip("xyz", (65536, 0, -131072)):
            w(self.root, g + "/in_anglvel_%s_raw" % ax, "%d\n" % v)
        w(self.root, g + "/in_anglvel_scale", "0.000000266316\n")

    def test_motion_sample_units(self):
        self.add_motion()
        s = demos.motion_sample()
        self.assertAlmostEqual(s["accel"][2], -64458 * 0.000149638, places=4)
        self.assertAlmostEqual(s["gyro"][0], 1.0, places=2)    # raw / 65536 deg/s
        self.assertAlmostEqual(s["gyro"][2], -2.0, places=2)
        st = demos.MotionState()
        for i in range(12):
            s["t"] = i * 0.1
            st.feed(s)
        lines = demos.motion_art(s, st)
        self.assertTrue(any("Spirit level" in ln for ln in lines))
        self.assertTrue(any("heading" in ln for ln in lines))
        self.assertEqual(demos.get("motion").probe()[0], "ok")

    def test_absent_with_reasons(self):
        inv = {d["name"]: d for d in demos.inventory()}
        self.assertEqual(inv["motion"]["level"], "absent")
        self.assertEqual(inv["ave"]["level"], "absent")
        self.assertIn("AVE", inv["ave"]["detail"])
        self.assertEqual(inv["avd"]["level"], "absent")
        for d in inv.values():
            self.assertTrue(d["detail"])
            self.assertTrue(d["what"])
        json.dumps(list(inv.values()), default=str)

    def test_parsers(self):
        r = demos.ave_parse("noise\nRESULT codec=h264 1920x1080 session=0 frames=300 secs=3.0 fps=99.8 out_MB=12.1\n")
        self.assertEqual(r["fps"], 99.8)
        r = demos.avd_parse('{"status":"pass","bitexact":true,"fps":320.5,"codec":"h264","height":1080}\n{"status":"pass","bitexact":true,"best_fps":334.0}\n')
        self.assertEqual(r["fps"], 334.0)
        self.assertTrue(r["bitexact"])
        self.assertEqual(demos.gpu_parse("...\nglmark2 Score: 4123\n")["score"], 4123)


class TestWhitePoint(Base):
    def setUp(self):
        super().setUp()
        build(self.root)
        demos.DEMOS = None

    def test_gvariant(self):
        g = demos._gvariant_props("({'NightLightActive': <false>, 'Temperature': <uint32 5600>, "
                                  "'LightLevel': <214.40209899999999>, 'LightLevelUnit': <'lux'>},)")
        self.assertEqual(g, {"NightLightActive": False, "Temperature": 5600, "LightLevel": 214.402099, "LightLevelUnit": "lux"})

    def test_model_and_factors(self):
        self.assertAlmostEqual(demos.white_point_model(6500, 300), 6500)
        self.assertEqual(demos.white_point_model(2700, 300), 5000)       # clamped
        self.assertAlmostEqual(demos.white_point_model(2700, 10), 6500)  # off at 10 lux or less
        self.assertAlmostEqual(demos.white_point_model(2700, 12.5), 5750)  # half way between 10 and 15 lux
        self.assertEqual(demos.white_point_model(2700, 15), 5000)       # full strength from 15 lux
        k = demos.white_point_model(5000, 300)
        self.assertTrue(5600 < k < 5800)                                  # half the shift in mired
        self.assertEqual([round(x, 3) for x in demos.blackbody(6500)], [1.0, 1.0, 1.0])
        r, g, b = demos.blackbody(5000)
        self.assertTrue(r == 1.0 and b < g < 1.0)

    def test_probe_degrades(self):
        lvl, why = demos.whitepoint_probe()
        self.assertEqual(lvl, "absent")
        w(self.root, "/sys/bus/iio/devices/iio:device0/in_cct_input", "5100.5\n")
        lvl, why = demos.whitepoint_probe()
        self.assertEqual(lvl, "ok")
        self.assertIn("5100", why)
        s = demos.whitepoint_sample()
        lines = demos.whitepoint_plain(demos.whitepoint_art(s, demos.WhitePointState()))
        self.assertTrue(any("colour 5100 K" in ln for ln in lines))
        self.assertTrue(any("absent:" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()
