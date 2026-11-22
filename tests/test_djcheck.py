# SPDX-License-Identifier: MIT
"""dj-check helpers: redaction, pw-top parsing, the ADT reader, the verdict."""

import io
import os
import shutil
import stat
import struct
import subprocess
import tarfile
import sys
import tempfile
import unittest
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from aurora_diag import djcheck  # noqa: E402


def adt_node(props, children=()):
    out = struct.pack("<II", len(props), len(children))
    for k, v in props.items():
        out += k.encode().ljust(32, b"\0") + struct.pack("<I", len(v)) + v + b"\0" * ((4 - len(v) % 4) % 4)
    for c in children:
        out += c
    return out


class Redaction(unittest.TestCase):
    def test_names_hosts_addresses(self):
        r = djcheck.Redactor(["dj"], ["dj-mbp"], ["C02ABCDEF"])
        t = r("dj-mbp speakersafetyd[1]: user dj home /home/dj/x mac aa:bb:cc:dd:ee:ff ip 192.168.1.20 "
              "serial C02ABCDEF raop_sink.Djs-iMac.local.192.168.1.3.7000 /run/user/1000/bus")
        for leak in ("dj-mbp", " dj ", "/home/dj", "aa:bb", "192.168", "C02ABCDEF", "Djs-iMac", "/run/user/1000"):
            self.assertNotIn(leak, t)
        self.assertIn("<host>", t)
        self.assertIn("speakersafetyd", t)
        self.assertNotIn("017d60b8", r('{"unit_id": "017d60b8840af2880379094cf29c852015043228dbd6352584faf7a4846a26d1"}'))

    def test_keeps_words_that_contain_a_name(self):
        r = djcheck.Redactor(["dj"], [], [])
        self.assertEqual(r("aurora-diag dj-check djx"), "aurora-diag dj-check djx")


class PwTop(unittest.TestCase):
    TOP = """S   ID  QUANT   RATE    WAIT    BUSY   W/Q   B/Q  ERR FORMAT           NAME
R   35    512  48000 949.0us   2.4us  0.09  0.00    4   S24_32 4 48000 sgv2.speaker.j413
R   42      0      0   0.7us   2.1us  0.00  0.00    1    F32P 12 48000  + alsa_output.platform-sound.RawSpeakers
R   42      0      0   0.7us   2.1us  0.00  0.00    3    F32P 12 48000  + alsa_output.platform-sound.RawSpeakers
S   98      0      0    ---     ---   ---   ---     0                  raop_sink.Living-Room.local.10.0.0.2.7000
S   85      0      0    ---     ---   ---   ---     0                  Midi-Bridge
"""

    def test_filter_and_errors(self):
        lines = djcheck.audio_top(self.TOP)
        self.assertEqual(len(lines), 4)
        self.assertFalse(any("raop" in x for x in lines))
        self.assertEqual(djcheck.top_errors(lines),
                         [("alsa_output.platform-sound.RawSpeakers", 3), ("sgv2.speaker.j413", 4)])


class Adt(unittest.TestCase):
    def test_acoustic_id(self):
        audio = adt_node({"name": b"audio\0", "acoustic-id": struct.pack("<I", 13), "speaker-thiele-small": b"x" * 8})
        arm = adt_node({"name": b"arm-io\0"}, [audio])
        root = adt_node({"name": b"device-tree\0", "target-type": b"J414s\0"}, [arm])
        node, end = djcheck.adt_parse(root)
        self.assertEqual(end, len(root))
        self.assertEqual(node["children"][0]["children"][0]["props"]["acoustic-id"], struct.pack("<I", 13))
        with self.assertRaises(ValueError):
            djcheck.adt_parse(root[:20])


class Verdict(unittest.TestCase):
    def test_levels(self):
        row = lambda g, s: {"group": g, "check": "x", "status": s, "detail": ""}  # noqa: E731
        self.assertEqual(djcheck.verdict([row("A speaker fix", "PASS"), row("B speaker info", "INFO")]), "PASS")
        self.assertEqual(djcheck.verdict([row("A speaker fix", "FAIL")]), "FAIL")
        self.assertEqual(djcheck.verdict([row("D Wi-Fi", "WARN")]), "WARN")
        self.assertEqual(djcheck.verdict([row("B speaker info", "FAIL")]), "PASS")


class Tone(unittest.TestCase):
    def test_tone_level(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "t.wav")
            djcheck.tone_file(p)
            with wave.open(p) as w:
                self.assertEqual((w.getnchannels(), w.getframerate()), (2, 48000))
                self.assertAlmostEqual(w.getnframes() / 48000, 16, places=1)
                data = w.readframes(w.getnframes())
            peak = max(abs(v) for v in struct.unpack("<%dh" % (len(data) // 2), data))
            self.assertLessEqual(peak, int(32767 * 0.25) + 1)


class Report(unittest.TestCase):
    """The report is created by root, maybe in the tester's home."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = os.path.join(self.tmp.name, "aurora-dj-check-20261123-120000")

    def tearDown(self):
        self.tmp.cleanup()

    def test_new_files_mode_0600(self):
        b = djcheck.write_report(self.base, {"txt": "report", "json": "{}"}, uid=os.getuid())
        self.assertEqual(b, self.base)
        for ext, data in (("txt", "report"), ("json", "{}")):
            p = "%s.%s" % (b, ext)
            st = os.lstat(p)
            self.assertTrue(stat.S_ISREG(st.st_mode))
            self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
            self.assertEqual(st.st_uid, os.getuid())
            with open(p) as f:
                self.assertEqual(f.read(), data)

    def test_a_planted_symlink_or_file_is_left_alone(self):
        target = os.path.join(self.tmp.name, "target")
        with open(target, "w") as f:
            f.write("keep")
        os.symlink(target, self.base + ".txt")
        with open(self.base + "-2.json", "w") as f:
            f.write("keep")
        b = djcheck.write_report(self.base, {"txt": "report", "json": "{}"})
        self.assertEqual(b, self.base + "-3")
        with open(target) as f:
            self.assertEqual(f.read(), "keep")
        with open(self.base + "-2.json") as f:
            self.assertEqual(f.read(), "keep")
        self.assertTrue(os.path.islink(self.base + ".txt"))
        self.assertFalse(os.path.exists(self.base + ".json"))
        self.assertFalse(os.path.exists(self.base + "-2.txt"))
        self.assertEqual(stat.S_IMODE(os.lstat(b + ".txt").st_mode), 0o600)


class OneLiner(unittest.TestCase):
    """dj-check.sh downloads and unpacks only after sudo, as root."""

    def setUp(self):
        self.sh, tar = shutil.which("sh"), shutil.which("tar")
        if not (self.sh and tar):
            self.skipTest("needs sh and tar")
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = self.tmp.name
        bindir, self.log = os.path.join(t, "bin"), os.path.join(t, "log")
        os.makedirs(bindir)
        tgz = os.path.join(t, "main.tar.gz")
        with tarfile.open(tgz, "w:gz") as tf:
            data = b"#!/bin/sh\n"
            ti = tarfile.TarInfo("aurora-diag-main/bin/aurora-diag")
            ti.size, ti.mode = len(data), 0o755
            tf.addfile(ti, io.BytesIO(data))
        # Stubs: sudo marks what runs under it; nothing reaches the network.
        stubs = {
            "id": 'echo 1000',
            "sudo": 'FAKE_ROOT=1 exec "$@"',
            "curl": 'echo "curl root=${FAKE_ROOT:-0}" >> "$LOG"; cat "$TGZ"',
            "wget": 'echo "wget root=${FAKE_ROOT:-0}" >> "$LOG"; cat "$TGZ"',
            "tar": 'echo "tar root=${FAKE_ROOT:-0}" >> "$LOG"; exec %s "$@"' % tar,
            "python3": 'echo "python root=${FAKE_ROOT:-0} $*" >> "$LOG"; test -f "$2"',
        }
        for name, body in stubs.items():
            with open(os.path.join(bindir, name), "w") as f:
                f.write("#!%s\n%s\n" % (self.sh, body))
            os.chmod(os.path.join(bindir, name), 0o755)
        open(self.log, "w").close()
        self.env = {"PATH": bindir + ":/usr/bin:/bin", "LOG": self.log, "TGZ": tgz, "TMPDIR": t}

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args, **env):
        return subprocess.run([self.sh, os.path.join(os.path.dirname(HERE), "dj-check.sh")] + list(args),
                              env=dict(self.env, **env), capture_output=True, text=True, timeout=60)

    def ran(self):
        with open(self.log) as f:
            return f.read().splitlines()

    def test_download_and_unpack_run_as_root(self):
        r = self.run_script("--no-listen")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.ran()
        self.assertTrue(lines, "nothing ran")
        for ln in lines:
            self.assertIn("root=1", ln, lines)
        self.assertTrue(lines[-1].startswith("python root=1 -I "), lines)
        self.assertTrue(lines[-1].endswith("/bin/aurora-diag dj-check --no-listen"), lines)
        # The unpacked tree is gone afterwards.
        self.assertEqual(sorted(os.listdir(self.t)), ["bin", "log", "main.tar.gz"])

    def test_refuses_a_ref_that_is_not_a_name(self):
        r = self.run_script(AURORA_DIAG_REF="../x")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.ran(), [])

if __name__ == "__main__":
    unittest.main()
