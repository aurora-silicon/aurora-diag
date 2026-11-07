# SPDX-License-Identifier: MIT
"""aurora-se-probe: decoding the kernel report, and redaction."""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from aurora_diag import se_probe  # noqa: E402

# The MacBook Neo's report, 2026-11-08 (fleet27, firmware 37010160).
NEO = """stockholm-se-probe 1 mode=apdu
ident nci=1.0 manufacturer=04 firmware=37010160 rf-interfaces=0
cmd nfcee-discover rsp=0001
ntf gid=2 oid=00 data=0101010100
nfcee expected=1 reported=1
cmd nfcee-discover-stop rsp=0001
apdu-nfcee none
result ok=0 parked=yes
"""

# A made-up part with a direct APDU NFCEE (no real card data).
CPLC = ("9f7f2a" + "4790" + "d321" + "4791" + "1234" + "0100" + "5001" + "deadbeef" + "aa55" +
        "4812" + "5002" + "4813" + "5003" + "0000" + "5004" + "11223344" + "0000" + "5005" + "55667788")
PPSE_FCI = ("6f2c840e325041592e5359532e4444463031a51abf0c17"
            "6115" "4f07a0000000031010" "500a56495341204445424954" "8701 01".replace(" ", ""))
FAKE = """stockholm-se-probe 1 mode=apdu
ident nci=1.0 manufacturer=04 firmware=12345678 rf-interfaces=0
cmd nfcee-discover rsp=0001
ntf gid=2 oid=00 data=c001020001 00
nfcee expected=1 reported=1
cmd nfcee-discover-stop rsp=0001
apdu-nfcee id=c0 status=01
cmd nfcee-enable rsp=00
cmd conn-create rsp=00ff0103
conn id=3 max-payload=255 credits=1
apdu name=isd-default cmd=00a4040000 sw=9000 resp=6f108408a000000151000000a5049f6501ff
apdu name=cplc cmd=80ca9f7f00 sw=9000 resp=%s
apdu name=card-data cmd=80ca006600 sw=9000 resp=6626732406072a864886fc6b01600c060a2a864886fc6b02020201640b06092a864886fc6b040200
apdu name=key-info cmd=80ca00e000 sw=9000 resp=e012c00401018810c00402018810c00403018810
apdu name=ppse cmd=00a404000e325041592e5359532e444446303100 sw=9000 resp=%s
apdu name=visa cmd=00a4040007a000000003101000 sw=6a82 resp=
apdu name=piv cmd=00a404000ba00000030800001000010000 error=-110
cmd conn-close rsp=00
cmd nfcee-restore-disabled rsp=00
result ok=0 parked=yes
""" % (CPLC, PPSE_FCI)


class TestSeProbe(unittest.TestCase):
    def test_neo_hci_only(self):
        s = se_probe.summarize(se_probe.parse_report(NEO))
        self.assertEqual(s["controller"]["firmware"], "37010160")
        self.assertEqual(len(s["nfcee"]), 1)
        e = s["nfcee"][0]
        self.assertEqual(e["id"], "0x01")
        self.assertEqual(e["status"], "disabled")
        self.assertEqual(e["protocols"], ["HCI access"])
        self.assertIsNone(s["apdu_path"])
        self.assertEqual(s["applets"], [])
        text = se_probe.text_view(s)
        self.assertIn("HCI only", text)
        self.assertIn("parked=yes", text)

    def test_decode_and_redact(self):
        rep = se_probe.parse_report(FAKE.replace("c001020001 00", "c00102000100"))
        s = se_probe.summarize(rep)
        out = json.dumps(s)
        # Serial, batch, equipment and production dates never in public output.
        for secret in ("deadbeef", "aa55", "11223344", "55667788", "5001", "5005", "VISA DEBIT", "56495341"):
            self.assertNotIn(secret, out, secret)
        a = {x["probe"]: x for x in s["applets"]}
        self.assertEqual(a["cplc"]["cplc"]["ic_fabricator"], "4790 (NXP)")
        self.assertEqual(a["cplc"]["cplc"]["ic_serial_number"], "<redacted>")
        self.assertTrue(any("SCP02" in v for v in a["card-data"]["card_data"]))
        self.assertTrue(any("GP 2.2.1" in v for v in a["card-data"]["card_data"]))
        self.assertEqual(len(a["key-info"]["keys"]), 3)
        self.assertTrue(a["ppse"]["present"])
        self.assertTrue(any("a0000000031010" in v for v in a["ppse"]["fci"]))
        self.assertFalse(a["visa"]["present"])
        self.assertEqual(a["visa"]["status"], "not found")
        self.assertEqual(a["piv"]["error"], "-110")
        self.assertEqual(s["nfcee"][0]["protocols"], ["APDU", "HCI access"])

    def test_private_view(self):
        s = se_probe.summarize(se_probe.parse_report(FAKE.replace("c001020001 00", "c00102000100")), private=True)
        out = json.dumps(s)
        self.assertIn("deadbeef", out)
        self.assertIn("VISA DEBIT", out)

    def test_unavailable_reason(self):
        self.assertTrue(se_probe.why_unavailable())


if __name__ == "__main__":
    unittest.main()
