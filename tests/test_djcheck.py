# SPDX-License-Identifier: MIT
"""dj-check helpers: redaction, pw-top parsing, the ADT reader, the verdict."""

import os
import struct
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


if __name__ == "__main__":
    unittest.main()
