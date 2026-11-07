# SPDX-License-Identifier: MIT
"""aurora-se-probe: a read-only look inside the Mac's embedded Secure Element.

Every Apple silicon Mac carries an NXP NFC controller ("Stockholm") with an
embedded Secure Element (eSE) behind it, the chip Apple Pay keeps its cards
in. The Macs have no NFC antenna, and the card manager keys are Apple's, so
Linux cannot install or run anything on it. What it can do is look.

The apple-stockholm kernel driver, loaded with se_probe=1, offers
/sys/kernel/debug/apple-stockholm/<dev>/se_probe. Opening that file powers
the controller up, lists the execution environments it reports (NFCEE
discovery), and if one of them takes ISO 7816 APDUs, sends a fixed table of
SELECT and GET DATA commands that the kernel compiled in and checks against
an allowlist. Then the controller is parked off again. The file's text is
the raw report; this module decodes it.

Nothing here sends a command. Only the kernel does, and only from its table.

Privacy: CPLC holds the chip's serial number, batch and production dates,
and payment applets carry labels. By default the decoded output redacts
those (public-safe, fine to paste in an issue); --show-private prints them.
Raw reports go only where --raw-out points, written 0600.
"""

import argparse
import glob
import json
import os
import re
import sys

try:
    from . import sysfs
except ImportError:  # run as a single file (python3 -I se_probe.py)
    class sysfs:  # noqa: N801
        root = staticmethod(lambda path: path)

DEBUGFS = "/sys/kernel/debug/apple-stockholm"
PARAM = "/sys/module/apple_stockholm/parameters/se_probe"

NFCEE_STATUS = {0x00: "enabled", 0x01: "disabled", 0x02: "removed"}
NFCEE_PROTOCOLS = {0x00: "APDU", 0x01: "HCI access", 0x02: "Type 3 tag commands", 0x03: "transparent"}

SW_TEXT = {
    0x9000: "OK",
    0x6283: "selected file deactivated",
    0x6700: "wrong length",
    0x6982: "security status not satisfied",
    0x6985: "conditions of use not satisfied",
    0x6999: "applet selection failed",
    0x6a80: "incorrect data",
    0x6a81: "function not supported",
    0x6a82: "not found",
    0x6a86: "incorrect P1/P2",
    0x6a88: "referenced data not found",
    0x6d00: "instruction not supported",
    0x6e00: "class not supported",
}

# What each probe means, for the summary.
PROBE_TEXT = {
    "isd-default": "card manager (default selection)",
    "cplc": "Card Production Life Cycle data",
    "card-data": "GlobalPlatform card recognition data",
    "key-info": "card manager key information",
    "isd-gp": "GlobalPlatform card manager AID",
    "isd-op": "Open Platform card manager AID",
    "gp-crs": "GlobalPlatform Contactless Registry Service",
    "ppse": "payment directory, contactless (2PAY.SYS.DDF01)",
    "pse": "payment directory, contact (1PAY.SYS.DDF01)",
    "visa": "Visa", "mastercard": "Mastercard", "maestro": "Maestro", "amex": "American Express",
    "discover": "Discover", "jcb": "JCB", "unionpay": "UnionPay", "interac": "Interac",
    "fido-u2f": "FIDO U2F", "piv": "PIV", "openpgp": "OpenPGP card", "ndef": "NFC Forum NDEF tag",
    "ccc-digital-key": "CCC digital key (car key)",
}

# TLV tags whose values are public by nature: AIDs, priorities, versions,
# structure. Everything else is shown as a length unless --show-private.
PUBLIC_TAGS = {"6F", "84", "A5", "BF0C", "61", "4F", "87", "73", "06", "60", "63", "64", "65", "66", "67",
               "E0", "C0", "9F65", "5F2D", "9F11", "9F38", "88"}
# Tags that must never be printed in public output even inside public parents.
NEVER_PUBLIC = {"5A", "57", "5F20", "9F1F", "9F6B", "56", "9F0B", "5F24", "5F34", "9F4D"}

CPLC_FIELDS = [
    # name, length, public
    ("ic_fabricator", 2, True),
    ("ic_type", 2, True),
    ("os_id", 2, True),
    ("os_release_date", 2, True),
    ("os_release_level", 2, True),
    ("ic_fabrication_date", 2, False),
    ("ic_serial_number", 4, False),
    ("ic_batch_identifier", 2, False),
    ("ic_module_fabricator", 2, True),
    ("ic_module_packaging_date", 2, False),
    ("icc_manufacturer", 2, True),
    ("ic_embedding_date", 2, False),
    ("ic_prepersonalizer", 2, True),
    ("ic_prepersonalization_date", 2, False),
    ("ic_prepersonalization_equipment", 4, False),
    ("ic_personalizer", 2, True),
    ("ic_personalization_date", 2, False),
    ("ic_personalization_equipment", 4, False),
]

IC_FABRICATORS = {"4790": "NXP", "4180": "Atmel", "4250": "Samsung", "4090": "Infineon", "3060": "Renesas",
                  "4070": "Oberthur", "2050": "STMicroelectronics"}

GP_OIDS = {
    "2a864886fc6b01": "GlobalPlatform card recognition data",
    "2a864886fc6b02": "GlobalPlatform card management type and version",
    "2a864886fc6b03": "card identification scheme",
    "2a864886fc6b04": "secure channel protocol",
}


# -- report parsing ---------------------------------------------------------------

def parse_report(text):
    """The kernel's report: one record per line, 'kind key=value ...'."""
    rep = {"header": None, "ident": None, "nfcee": [], "ntf": [], "cmds": [], "apdus": [], "apdu_nfcee": None,
           "conn": None, "result": None, "lines": 0}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        rep["lines"] += 1
        kind, _, rest = line.partition(" ")
        kv = dict(re.findall(r"([\w-]+)=(\S+)", rest))
        if kind == "stockholm-se-probe":
            rep["header"] = rest
        elif kind == "ident":
            rep["ident"] = kv
        elif kind == "ntf":
            rep["ntf"].append(kv)
            if kv.get("gid") == "2" and kv.get("oid") == "00":
                rep["nfcee"].append(parse_nfcee(bytes.fromhex(kv.get("data", ""))))
        elif kind == "cmd":
            step, _, r = rest.partition(" ")
            rep["cmds"].append(dict(dict(re.findall(r"([\w-]+)=(\S+)", r)), step=step))
        elif kind == "apdu-nfcee":
            rep["apdu_nfcee"] = kv
        elif kind == "conn":
            rep["conn"] = kv
        elif kind == "apdu":
            a = {"name": kv.get("name"), "cmd": kv.get("cmd"), "error": kv.get("error")}
            if "sw" in kv:
                a["sw"] = int(kv["sw"], 16)
                a["data"] = bytes.fromhex(kv.get("resp", ""))
            rep["apdus"].append(a)
        elif kind == "result":
            rep["result"] = rest
    return rep


def parse_nfcee(b):
    """NCI 1.x NFCEE_DISCOVER_NTF: ID, status, protocols, TLVs."""
    out = {"raw": b.hex()}
    if len(b) < 3:
        out["error"] = "short"
        return out
    out["id"] = b[0]
    out["status"] = NFCEE_STATUS.get(b[1], "0x%02x" % b[1])
    n = b[2]
    protos = list(b[3:3 + n])
    out["protocols"] = [NFCEE_PROTOCOLS.get(p, "0x%02x" % p) for p in protos]
    i = 3 + n
    tlvs = []
    if i < len(b):
        count = b[i]
        i += 1
        for _ in range(count):
            if i + 2 > len(b):
                break
            t, ln = b[i], b[i + 1]
            tlvs.append({"type": t, "value": b[i + 2:i + 2 + ln].hex()})
            i += 2 + ln
    out["tlvs"] = tlvs
    return out


# -- BER-TLV --------------------------------------------------------------------------

def tlv_parse(b):
    """BER-TLV list of (tag_hex, value_bytes, children or None)."""
    out = []
    i = 0
    while i < len(b):
        if b[i] in (0x00, 0xff):
            i += 1
            continue
        start = i
        first = b[i]
        i += 1
        if first & 0x1f == 0x1f:
            while i < len(b) and b[i] & 0x80:
                i += 1
            i += 1
        tag = b[start:i].hex().upper()
        if i >= len(b):
            break
        ln = b[i]
        i += 1
        if ln & 0x80:
            k = ln & 0x7f
            ln = int.from_bytes(b[i:i + k], "big")
            i += k
        val = b[i:i + ln]
        i += ln
        kids = tlv_parse(val) if first & 0x20 else None
        out.append((tag, val, kids))
    return out


def tlv_lines(tlvs, private, depth=0):
    lines = []
    for tag, val, kids in tlvs:
        pad = "  " * depth
        if kids is not None:
            lines.append("%s%s (%d bytes)" % (pad, tag, len(val)))
            lines += tlv_lines(kids, private, depth + 1)
            continue
        if private or (tag in PUBLIC_TAGS and tag not in NEVER_PUBLIC):
            shown = val.hex()
            if tag in ("50", "9F12") or (tag == "84" and _printable(val)):
                shown += " %r" % val.decode("ascii", "replace")
            lines.append("%s%s %s" % (pad, tag, shown))
        else:
            lines.append("%s%s <%d bytes, redacted>" % (pad, tag, len(val)))
    return lines


def _printable(b):
    return all(0x20 <= c < 0x7f for c in b)


# -- decoders -------------------------------------------------------------------------

def decode_cplc(data, private):
    tl = tlv_parse(data)
    if tl and tl[0][0] == "9F7F":
        data = tl[0][1]
    fields = {}
    i = 0
    for name, ln, public in CPLC_FIELDS:
        v = data[i:i + ln]
        i += ln
        if len(v) < ln:
            break
        fields[name] = v.hex() if (public or private) else "<redacted>"
    if "ic_fabricator" in fields:
        fab = IC_FABRICATORS.get(fields["ic_fabricator"])
        if fab:
            fields["ic_fabricator"] += " (%s)" % fab
    return fields


def decode_card_data(data):
    """GP card recognition data (tag 66/73): which GP version and SCP."""
    out = []

    def walk(tlvs):
        for tag, val, kids in tlvs:
            if kids is not None:
                walk(kids)
            elif tag == "06":
                h = val.hex()
                for prefix, what in GP_OIDS.items():
                    if h.startswith(prefix):
                        rest = val[len(prefix) // 2:]
                        detail = ".".join(str(x) for x in rest)
                        if prefix.endswith("04") and len(rest) >= 2:
                            detail = "SCP%02x i=%02x" % (rest[0], rest[1])
                        elif prefix.endswith("02") and rest:
                            detail = "GP " + detail
                        out.append("%s: %s" % (what, detail))
                        break
                else:
                    out.append("OID " + h)
    walk(tlv_parse(data))
    return out


def decode_key_info(data):
    """Key information template E0: C0 entries (ID, version, type, length)."""
    keys = []

    def walk(tlvs):
        for tag, val, kids in tlvs:
            if kids is not None:
                walk(kids)
            elif tag == "C0" and len(val) >= 4:
                keys.append({"id": val[0], "version": val[1], "type": "0x%02x" % val[2], "length": val[3]})
    walk(tlv_parse(data))
    return keys


def sw_text(sw):
    if sw in SW_TEXT:
        return SW_TEXT[sw]
    if sw >> 8 == 0x61:
        return "more data"
    if sw >> 8 == 0x63:
        return "warning 63%02x" % (sw & 0xff)
    return "status %04x" % sw


def summarize(rep, private=False):
    """The decoded, public-safe view (unless private)."""
    s = {"schema": "aurora-se-probe", "version": 1, "redacted": not private,
         "controller": rep["ident"], "result": rep["result"], "nfcee": [], "apdu_path": None, "applets": []}
    for e in rep["nfcee"]:
        d = {k: e.get(k) for k in ("id", "status", "protocols")}
        if isinstance(d.get("id"), int):
            d["id"] = "0x%02x" % d["id"]
        d["tlvs"] = e.get("tlvs") if private else [{"type": t["type"], "bytes": len(t["value"]) // 2}
                                                    for t in e.get("tlvs", [])]
        s["nfcee"].append(d)
    if rep["apdu_nfcee"]:
        s["apdu_path"] = {"nfcee": rep["apdu_nfcee"], "connection": rep["conn"]}
    for a in rep["apdus"]:
        r = {"probe": a["name"], "what": PROBE_TEXT.get(a["name"], a["name"])}
        if a.get("error"):
            r["error"] = a["error"]
        else:
            r["sw"] = "%04x" % a["sw"]
            r["status"] = sw_text(a["sw"])
            r["present"] = a["sw"] == 0x9000 or a["sw"] >> 8 in (0x61, 0x62, 0x63)
            data = a["data"]
            if data:
                if a["name"] == "cplc":
                    r["cplc"] = decode_cplc(data, private)
                elif a["name"] == "card-data":
                    r["card_data"] = decode_card_data(data)
                elif a["name"] == "key-info":
                    r["keys"] = decode_key_info(data)
                else:
                    r["fci"] = tlv_lines(tlv_parse(data), private)
        s["applets"].append(r)
    return s


def text_view(s):
    lines = []
    c = s.get("controller") or {}
    lines.append("Secure Element probe (read only)%s" % ("" if s["redacted"] else "  [PRIVATE: unredacted]"))
    if c:
        lines.append("  controller   NCI %s, manufacturer 0x%s, firmware %s, %s RF interfaces" % (
            c.get("nci"), c.get("manufacturer"), c.get("firmware"), c.get("rf-interfaces")))
    if not s["nfcee"]:
        lines.append("  NFCEEs       none reported")
    for e in s["nfcee"]:
        lines.append("  NFCEE %s   %s, speaks %s" % (e.get("id"), e.get("status"), ", ".join(e.get("protocols") or []) or "?"))
    if s["apdu_path"]:
        n = s["apdu_path"]["nfcee"]
        lines.append("  APDU path    NFCEE %s (found %s)" % (n.get("id"), {"00": "enabled", "01": "disabled, enabled for the probe and disabled again"}.get(n.get("status"), n.get("status"))))
    elif s["nfcee"]:
        lines.append("  APDU path    none: no NFCEE takes APDUs directly (HCI only), so no applet was asked anything")
    for a in s["applets"]:
        if "error" in a:
            lines.append("  %-16s %-46s error %s" % (a["probe"], a["what"], a["error"]))
            continue
        mark = "PRESENT" if a["present"] else "-"
        lines.append("  %-16s %-46s %-8s %s %s" % (a["probe"], a["what"], mark, a["sw"], a["status"]))
        for k, v in (a.get("cplc") or {}).items():
            lines.append("      %-34s %s" % (k, v))
        for v in a.get("card_data") or []:
            lines.append("      " + v)
        for k in a.get("keys") or []:
            lines.append("      key id %(id)d version %(version)d type %(type)s length %(length)d" % k)
        for v in a.get("fci") or []:
            lines.append("      " + v)
    lines.append("  result       %s" % s.get("result"))
    return "\n".join(lines)


# -- running ----------------------------------------------------------------------------

def find_node(name):
    nodes = sorted(glob.glob(os.path.join(sysfs.root(DEBUGFS), "*", name)))
    return nodes[0] if nodes else None


def why_unavailable():
    if os.geteuid() != 0:
        return "needs root (debugfs, CAP_SYS_ADMIN)"
    if not os.path.isdir(sysfs.root("/sys/module/apple_stockholm")):
        return "the apple_stockholm driver is not loaded (no Stockholm node in this device tree, or no module)"
    try:
        with open(sysfs.root(PARAM)) as f:
            on = f.read().strip() in ("Y", "1")
    except OSError:
        on = False
    if not on:
        return ("the driver was loaded without se_probe=1. Reload it for this boot only:\n"
                "    modprobe -r apple_stockholm && modprobe apple_stockholm se_probe=1")
    return "no debugfs node under %s (is debugfs mounted?)" % DEBUGFS


def availability():
    """(level, detail) for the demo list, without touching the hardware."""
    if find_node("se_probe"):
        return "ok", "apple-stockholm se_probe=1: NFCEE discovery and a read-only command table, then parked"
    if not os.path.isdir(sysfs.root("/sys/module/apple_stockholm")):
        return "absent", why_unavailable()
    return "absent", why_unavailable().split("\n")[0]


def run_kernel_probe(nfcee_only=False):
    node = find_node("nfcee" if nfcee_only else "se_probe")
    if not node:
        raise RuntimeError(why_unavailable())
    with open(node) as f:
        return f.read()


def save_raw(text, outdir):
    os.makedirs(outdir, mode=0o700, exist_ok=True)
    import time
    path = os.path.join(outdir, "se-probe-%s-%s.txt" % (os.uname().nodename, time.strftime("%Y%m%d-%H%M%S")))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aurora-se-probe",
                                 description="Read-only look inside the Mac's embedded Secure Element "
                                             "(apple-stockholm se_probe=1). Sends nothing itself.")
    ap.add_argument("--nfcee-only", action="store_true",
                    help="only list the execution environments; no command reaches the Secure Element")
    ap.add_argument("--input", metavar="FILE", help="decode a saved raw report instead of probing")
    ap.add_argument("--raw-out", metavar="DIR", help="also save the raw kernel report (0600) in DIR; keep it private")
    ap.add_argument("--show-private", action="store_true",
                    help="print serial numbers, dates and labels (default: redacted, safe to share)")
    ap.add_argument("--json", action="store_true", help="JSON instead of text")
    ap.add_argument("--allowlist", action="store_true", help="print the kernel's command table and exit")
    args = ap.parse_args(argv)

    try:
        if args.allowlist:
            node = find_node("apdu_allowlist")
            if not node:
                raise RuntimeError(why_unavailable())
            with open(node) as f:
                sys.stdout.write(f.read())
            return 0
        if args.input:
            with open(args.input) as f:
                raw = f.read()
        else:
            raw = run_kernel_probe(args.nfcee_only)
    except (OSError, RuntimeError) as e:
        print("aurora-se-probe: %s" % e, file=sys.stderr)
        return 2

    if args.raw_out:
        print("raw report: %s (private, do not publish)" % save_raw(raw, args.raw_out), file=sys.stderr)

    s = summarize(parse_report(raw), private=args.show_private)
    if args.json:
        print(json.dumps(s, indent=1, default=str))
    else:
        print(text_view(s))
    return 0 if (s.get("result") or "").startswith("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
