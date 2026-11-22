# SPDX-License-Identifier: MIT
"""Demos: discovery, the motion view, result parsing, against a fake tree."""

import json
import os
import sys
import unittest
from unittest import mock

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



class TestRootPrograms(unittest.TestCase):
    """As root, the AVE/AVD benches (not as_user demos) never take a program
    or a clip another user can replace: /var/tmp kits, ~/.local/bin."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        self.kit = os.path.join(self.tmp.name, "vartmp", "kit")
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        os.makedirs(self.kit)
        for d in (self.home, self.kit):
            for n in ("avebench", "avd-bench"):
                self.exe(os.path.join(d, ".local/bin" if d == self.home else "", n))
        with open(os.path.join(self.kit, "h264-1080.mp4"), "wb") as f:
            f.write(b"clip")
        kit = self.kit

        def fake_glob(pattern):
            # The /var/tmp globs land in this user-owned kit.
            if pattern.startswith("/var/tmp/"):
                hit = os.path.join(kit, os.path.basename(pattern))
                return [hit] if os.path.exists(hit) else []
            return []
        self.patches = [mock.patch.object(demos.shutil, "which", return_value=None),
                        mock.patch.object(demos.glob, "glob", side_effect=fake_glob),
                        mock.patch.object(demos, "homes", return_value=[self.home])]
        for x in self.patches:
            x.start()

    def tearDown(self):
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    @staticmethod
    def exe(path):
        with open(path, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(path, 0o755)

    def as_root(self):
        return mock.patch.object(demos.os, "geteuid", return_value=0)

    def test_root_skips_user_owned_benches_and_clips(self):
        if os.getuid() == 0:
            self.skipTest("the kit must be owned by a normal user")
        with self.as_root():
            self.assertIsNone(demos._avebench())
            self.assertIsNone(demos._avdbench())
            self.assertIsNone(demos._avd_sample())
            self.assertIsNone(demos.ave_command())
            self.assertIsNone(demos.avd_command())

    def test_root_skips_a_user_owned_var_tmp_kit(self):
        if os.getuid() == 0:
            self.skipTest("the kit must be owned by a normal user")
        for n in ("avebench", "avd-bench"):
            os.remove(os.path.join(self.home, ".local/bin", n))
        with self.as_root():
            self.assertIsNone(demos._avebench())
            self.assertIsNone(demos._avdbench())
            self.assertIsNone(demos._avd_sample())

    def test_root_skips_the_desktop_users_local_bin(self):
        if os.getuid() == 0:
            self.skipTest("the home must be owned by a normal user")
        os.remove(os.path.join(self.kit, "avebench"))
        with self.as_root():
            self.assertIsNone(demos._avebench())

    def test_normal_user_still_finds_their_benches(self):
        with mock.patch.object(demos.os, "geteuid", return_value=os.getuid() or 1000):
            self.assertEqual(demos._avebench(), os.path.join(self.home, ".local/bin/avebench"))
            self.assertEqual(demos._avd_sample(), os.path.join(self.kit, "h264-1080.mp4"))

    def test_as_user_lookups_are_unchanged_as_root(self):
        self.exe(os.path.join(self.home, ".local/bin/vkcube"))
        with self.as_root():
            self.assertEqual(demos.find_bin(["vkcube"]), os.path.join(self.home, ".local/bin/vkcube"))

    def test_root_only(self):
        me = (0, os.getuid())
        f = os.path.join(self.kit, "avebench")
        real = os.path.realpath(f)
        os.chmod(self.kit, 0o755)
        self.assertEqual(demos.root_only(f, owners=me), real)
        self.assertIsNone(demos.root_only(f))                 # owned by this user, not root
        os.chmod(self.kit, 0o1777)                            # sticky, like /var/tmp
        self.assertEqual(demos.root_only(f, owners=me), real)
        os.chmod(self.kit, 0o777)                             # anyone can rename the entry
        self.assertIsNone(demos.root_only(f, owners=me))
        os.chmod(self.kit, 0o755)
        os.chmod(f, 0o775)                                    # group writable file
        self.assertIsNone(demos.root_only(f, owners=me))
        os.chmod(f, 0o755)
        self.assertIsNone(demos.root_only(os.path.join(self.kit, "missing"), owners=me))

    def test_root_only_links(self):
        me = (0, os.getuid())
        f = os.path.join(self.kit, "avebench")
        real = os.path.realpath(f)
        os.chmod(self.kit, 0o755)
        loose = os.path.join(self.tmp.name, "loose")
        os.makedirs(loose)
        os.chmod(loose, 0o777)
        # A link in a writable directory to a safe file: the link can be
        # repointed, so it is refused although its target is fine.
        os.symlink(f, os.path.join(loose, "link"))
        self.assertIsNone(demos.root_only(os.path.join(loose, "link"), owners=me))
        # The reverse: a link in a safe directory to a file in a writable one.
        self.exe(os.path.join(loose, "target"))
        os.symlink(os.path.join(loose, "target"), os.path.join(self.kit, "via"))
        self.assertIsNone(demos.root_only(os.path.join(self.kit, "via"), owners=me))
        # A chain through a link in a writable directory is refused too.
        os.symlink(os.path.join(loose, "link"), os.path.join(self.kit, "chain"))
        self.assertIsNone(demos.root_only(os.path.join(self.kit, "chain"), owners=me))
        # Safe link to a safe file (relative): the resolved file is returned.
        os.symlink("avebench", os.path.join(self.kit, "safe"))
        self.assertEqual(demos.root_only(os.path.join(self.kit, "safe"), owners=me), real)
        os.symlink("safe", os.path.join(self.kit, "safe2"))
        self.assertEqual(demos.root_only(os.path.join(self.kit, "safe2"), owners=me), real)
        loop = os.path.join(self.kit, "loop")
        os.symlink("loop", loop)
        self.assertIsNone(demos.root_only(loop, owners=me))

    def root_program(self):
        for c in ("/usr/bin/true", "/bin/true", "/usr/bin/env"):
            r = os.path.realpath(c)
            if os.path.isfile(r) and demos.root_only(r):
                return r
        self.skipTest("no root-owned program in /usr/bin")

    def test_root_skips_a_user_link_to_a_root_program(self):
        if os.getuid() == 0:
            self.skipTest("the links must be owned by a normal user")
        target = self.root_program()
        for n in ("avebench", "avd-bench"):
            os.remove(os.path.join(self.home, ".local/bin", n))
            os.remove(os.path.join(self.kit, n))
        # ~/.local/bin link to a root program: the user picks what root runs.
        os.symlink(target, os.path.join(self.home, ".local/bin", "avebench"))
        # /var/tmp/<dir> kit link to a root program.
        os.symlink(target, os.path.join(self.kit, "avd-bench"))
        os.remove(os.path.join(self.kit, "h264-1080.mp4"))
        os.symlink(target, os.path.join(self.kit, "h264-1080.mp4"))
        with self.as_root():
            self.assertIsNone(demos._avebench())
            self.assertIsNone(demos._avdbench())
            self.assertIsNone(demos._avd_sample())

    def test_root_gets_the_resolved_path_it_checked(self):
        target = self.root_program()
        # /bin is a root-owned link to usr/bin on merged-/usr systems.
        link = os.path.join("/bin", os.path.basename(target))
        if not os.path.islink("/bin") or os.path.realpath(link) != target:
            self.skipTest("no /bin -> usr/bin link here")
        with self.as_root(), mock.patch.object(demos.shutil, "which", return_value=link), \
                mock.patch.object(demos, "homes", return_value=[]):
            self.assertEqual(demos.find_bin(["true"], as_root=True), target)
        self.assertEqual(demos.root_only(link), target)

if __name__ == "__main__":
    unittest.main()
