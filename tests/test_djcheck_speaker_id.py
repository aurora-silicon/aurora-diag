# SPDX-License-Identifier: MIT
"""Exercise the dj-check /chosen row without probing the test host.

`tests/test_djcheck_speaker_id.py` exercises the production reader and row with
temporary synthetic `/chosen` properties and scoped permission errors. All
other `speaker_info` sources are stubbed; fixtures contain no personal host
dump and do not test physical audio or speaker safety.
"""

from contextlib import ExitStack
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from aurora_diag import djcheck  # noqa: E402


NEW = "/proc/device-tree/chosen/aurora,speaker-id"
OLD = "/proc/device-tree/chosen/asahi,speaker-id"
NOT_EXPORTED = "not exported by this boot chain"


def speaker_row(read_speaker):
    """Run the production method; only the two speaker reads reach the caller."""
    def read(path, **kwargs):
        if path in (NEW, OLD):
            return read_speaker(path, **kwargs)
        if path in ("/proc/device-tree/model",
                    "/lib/firmware/vendor/apple/audio-tuning/manifest.json",
                    "/run/aurora-firmware/vendorfw/apple/audio-tuning/manifest.json"):
            return None
        raise AssertionError("unexpected read: %s" % path)

    check = djcheck.Check(None, lambda text: text, "unknown")
    check.pk = mock.Mock(spec=djcheck.Packages)
    check.pk.version.return_value = None
    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(djcheck, "read", side_effect=read))
        stack.enter_context(mock.patch.object(djcheck, "dt_strings", return_value=[]))
        stack.enter_context(mock.patch.object(djcheck.glob, "glob", return_value=[]))
        stack.enter_context(mock.patch.object(djcheck, "adt_audio", return_value=(None, "stubbed")))
        stack.enter_context(mock.patch.object(djcheck.os, "access", return_value=False))
        stack.enter_context(mock.patch.object(djcheck, "run", side_effect=AssertionError("unexpected command")))
        for method in ("state_dirs", "pipewire", "journals"):
            stack.enter_context(mock.patch.object(check, method))
        check.speaker_info()
    rows = [row for row in check.rows if row["check"] == "speaker-id (/chosen)"]
    if len(rows) != 1:
        raise AssertionError("expected exactly one speaker-id row: %r" % rows)
    return rows[0]


class SpeakerId(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.chosen = Path(directory.name) / "chosen"
        self.chosen.mkdir()
        self.real_read = djcheck.read
        self.unreadable = set()
        self.read_speaker = mock.Mock(side_effect=self.read_property)

    def write(self, path, value):
        (self.chosen / Path(path).name).write_bytes(value)

    def read_property(self, path, **kwargs):
        fixture = self.chosen / Path(path).name
        if path in self.unreadable:
            # chmod is ineffective under root; inject a deterministic read error
            # only around the actual production reader for this property.
            with mock.patch("builtins.open", side_effect=PermissionError("synthetic fixture")):
                return self.real_read(fixture, **kwargs)
        return self.real_read(fixture, **kwargs)

    def assert_row(self, detail, fallback=False):
        self.read_speaker.reset_mock()
        self.assertEqual(speaker_row(self.read_speaker), {
            "group": "B speaker info", "check": "speaker-id (/chosen)",
            "status": "INFO", "detail": detail,
        })
        calls = [mock.call(NEW, binary=True)]
        if fallback:
            calls.append(mock.call(OLD, binary=True))
        self.assertEqual(self.read_speaker.call_args_list, calls)

    def test_old_only(self):
        self.write(OLD, b"\x01\x02\x03\x04")
        self.assert_row("16909060", fallback=True)

    def test_new_only(self):
        self.write(NEW, b"\x10\x20\x30\x40")
        self.assert_row("270544960")

    def test_new_wins_when_both_exist(self):
        self.write(NEW, b"\0\0\0\x11")
        self.write(OLD, b"\0\0\0\x17")
        self.assert_row("17")

    def test_neither(self):
        self.assert_row(NOT_EXPORTED, fallback=True)

    def test_old_zero(self):
        self.write(OLD, b"\0" * 4)
        self.assert_row("0", fallback=True)

    def test_new_zero(self):
        self.write(NEW, b"\0" * 4)
        self.assert_row("0")

    def test_new_zero_wins_over_nonzero_old(self):
        self.write(NEW, b"\0" * 4)
        self.write(OLD, b"\0\0\0\x17")
        self.assert_row("0")

    def test_big_endian_unsigned(self):
        self.write(NEW, b"\x89\xab\xcd\xef")
        self.assert_row("2309737967")

    def test_empty_or_malformed_new_does_not_fall_back(self):
        self.write(OLD, b"\0\0\0\x17")
        for value in (b"", b"\0", b"\x01\x02\x03", b"\x01\x02\x03\x04\x05"):
            with self.subTest(value=value):
                self.write(NEW, value)
                self.assert_row(NOT_EXPORTED)

    def test_empty_or_malformed_old(self):
        for value in (b"", b"\0", b"\x01\x02\x03", b"\x01\x02\x03\x04\x05"):
            with self.subTest(value=value):
                self.write(OLD, value)
                self.assert_row(NOT_EXPORTED, fallback=True)

    def test_unreadable_new_falls_back(self):
        self.write(NEW, b"\0\0\0\x11")
        self.write(OLD, b"\0\0\0\x17")
        self.unreadable.add(NEW)
        self.assert_row("23", fallback=True)

    def test_unreadable_old(self):
        self.write(OLD, b"\0" * 4)
        self.unreadable.add(OLD)
        self.assert_row(NOT_EXPORTED, fallback=True)

    def test_both_unreadable(self):
        self.write(NEW, b"\0" * 4)
        self.write(OLD, b"\0" * 4)
        self.unreadable.update((NEW, OLD))
        self.assert_row(NOT_EXPORTED, fallback=True)


if __name__ == "__main__":
    unittest.main()
