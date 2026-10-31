# SPDX-License-Identifier: MIT
"""Decoders for what a display says about itself.

edid():      an EDID blob (base block, CTA-861 and DisplayID extensions):
             names, size, timings, refresh range, VRR, HDR static metadata,
             colorimetry. The serial number is never decoded.
dcp_dict():  the serialized dictionaries the DCP firmware hands the kernel
             (the appledrm debugfs files DisplayAttributes, Transport,
             ColorElements): header 0xd3, then 32-bit tags of
             size:24 type:5 pad:2 last:1, 4-byte aligned.
"""

import math
import struct

INTERFACES = {1: "DVI", 2: "HDMI-a", 3: "HDMI-b", 4: "MDDI", 5: "DisplayPort"}
DEPTHS = {1: 6, 2: 8, 3: 10, 4: 12, 5: 14, 6: 16}
EOTFS = ("SDR (traditional gamma)", "HDR (traditional gamma)", "PQ (SMPTE ST 2084)", "HLG")
COLORIMETRY = ("xvYCC601", "xvYCC709", "sYCC601", "opYCC601", "opRGB", "BT2020cYCC", "BT2020YCC", "BT2020RGB")


def _pnp(b):
    v = (b[0] << 8) | b[1]
    return "".join(chr(((v >> s) & 0x1f) + 64) for s in (10, 5, 0))


def _text(d):
    return d[5:18].split(b"\n")[0].decode("ascii", "replace").strip()


def _dtd(d):
    clock = struct.unpack("<H", d[0:2])[0] * 10  # kHz
    ha = d[2] | ((d[4] >> 4) << 8)
    hb = d[3] | ((d[4] & 0xf) << 8)
    va = d[5] | ((d[7] >> 4) << 8)
    vb = d[6] | ((d[7] & 0xf) << 8)
    tot = (ha + hb) * (va + vb)
    return {"width": ha, "height": va, "pixel_clock_kHz": clock,
            "refresh_Hz": round(clock * 1000.0 / tot, 2) if tot else None,
            "interlaced": bool(d[17] & 0x80)}


def _cta(ext, out):
    out["cta_revision"] = ext[1]
    end = ext[2] if 4 <= ext[2] <= 127 else 4
    out["underscan"] = bool(ext[3] & 0x80)
    out["basic_audio"] = bool(ext[3] & 0x40)
    i = 4
    while i < end:
        tag, n = ext[i] >> 5, ext[i] & 0x1f
        p = ext[i + 1:i + 1 + n]
        if tag == 1:
            out["audio_formats"] = out.get("audio_formats", 0) + n // 3
        elif tag == 2:
            out["video_codes"] = out.get("video_codes", 0) + n
        elif tag == 3 and len(p) >= 3:
            oui = p[0] | (p[1] << 8) | (p[2] << 16)
            if oui == 0x000c03:
                out.setdefault("vendor_blocks", []).append("HDMI 1.4")
            elif oui == 0xc45dd8:
                out.setdefault("vendor_blocks", []).append("HDMI Forum")
                if len(p) >= 11:
                    vmin, vmax = p[9] & 0x3f, ((p[9] & 0xc0) << 2) | p[10]
                    if vmin or vmax:
                        out["hdmi_vrr_Hz"] = [vmin, vmax]
                if len(p) >= 8:
                    out["hdmi_allm"] = bool(p[7] & 0x02) if len(p) > 7 else None
            elif oui == 0x00001a:
                out.setdefault("vendor_blocks", []).append("AMD FreeSync")
                if len(p) >= 7:
                    out["freesync_Hz"] = [p[5], p[6]]
            else:
                out.setdefault("vendor_blocks", []).append("%06x" % oui)
        elif tag == 7 and p:
            et = p[0]
            if et == 6 and len(p) >= 3:
                eotf = [EOTFS[b] for b in range(4) if p[1] & (1 << b)]
                h = {"eotfs": eotf, "static_metadata_type1": bool(p[2] & 1)}
                if len(p) >= 4 and p[3]:
                    h["max_luminance_nits"] = round(50 * 2 ** (p[3] / 32.0), 1)
                if len(p) >= 5 and p[4]:
                    h["max_frame_avg_nits"] = round(50 * 2 ** (p[4] / 32.0), 1)
                if len(p) >= 6 and p[5] and h.get("max_luminance_nits"):
                    h["min_luminance_nits"] = round(h["max_luminance_nits"] * (p[5] / 255.0) ** 2 / 100, 4)
                out["hdr_static_metadata"] = h
            elif et == 5 and len(p) >= 2:
                out["colorimetry"] = [COLORIMETRY[b] for b in range(8) if p[1] & (1 << b)]
                if len(p) >= 3 and p[2] & 0x80:
                    out["colorimetry"].append("DCI-P3")
            elif et == 7:
                out.setdefault("extended_blocks", []).append("HDR dynamic metadata")
            elif et in (14, 15):
                out["ycbcr420"] = True
        i += 1 + n
    j = end
    while j + 18 <= 127 and ext[j] | ext[j + 1]:
        out.setdefault("extra_timings", []).append(_dtd(ext[j:j + 18]))
        j += 18


def _displayid(sec, out):
    """DisplayID Adaptive-Sync data block (tag 0x2b): refresh range per
    6-byte descriptor (flags, duration, min Hz, max Hz low, max Hz high)."""
    if len(sec) < 5:
        return
    i, stop = 4, min(4 + sec[1], len(sec))
    while i + 3 <= stop:
        tag, length = sec[i], sec[i + 2]
        if tag == 0 and length == 0:
            break
        p = sec[i + 3:i + 3 + length]
        if tag == 0x2b:
            for k in range(0, len(p) - 5, 6):
                d = p[k:k + 6]
                out.setdefault("adaptive_sync", []).append({"min_Hz": d[2], "max_Hz": d[3] | ((d[4] & 3) << 8)})
        i += 3 + length


def edid(blob):
    """A dict describing the display, or {"error": ...}. No serial numbers."""
    if not blob:
        return {"error": "no EDID"}
    if len(blob) < 128 or blob[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return {"error": "not an EDID (%d bytes)" % len(blob)}
    b = blob
    out = {"bytes": len(b), "manufacturer": _pnp(b[8:10]),
           "product_code": "%04x" % struct.unpack("<H", b[10:12])[0],
           "version": "%d.%d" % (b[18], b[19])}
    if b[17]:
        out["year"] = 1990 + b[17] if b[16] != 0xff else 1990 + b[17]
        if b[16] == 0xff:
            out["model_year"] = True
    digital = bool(b[20] & 0x80)
    out["input"] = "digital" if digital else "analog"
    if digital:
        out["bpc"] = DEPTHS.get((b[20] >> 4) & 7)
        out["interface"] = INTERFACES.get(b[20] & 0xf)
    if b[21] and b[22]:
        out["size_cm"] = [b[21], b[22]]
        out["diagonal_in"] = round(math.hypot(b[21], b[22]) / 2.54, 1)
    timings = []
    for o in (54, 72, 90, 108):
        d = b[o:o + 18]
        if d[0] | d[1]:
            timings.append(_dtd(d))
            continue
        tag = d[3]
        if tag == 0xfc:
            out["name"] = _text(d)
        elif tag == 0xfe:
            out.setdefault("text", []).append(_text(d))
        elif tag == 0xfd:
            vmin = d[5] + (255 if d[4] & 1 else 0)
            vmax = d[6] + (255 if d[4] & 2 else 0)
            out["range_vertical_Hz"] = [vmin, vmax]
            out["range_horizontal_kHz"] = [d[7] + (255 if d[4] & 4 else 0), d[8] + (255 if d[4] & 8 else 0)]
            if d[9]:
                out["max_pixel_clock_MHz"] = d[9] * 10
    if timings:
        out["preferred"] = timings[0]
        if len(timings) > 1:
            out["other_timings"] = timings[1:]
    nexts = b[126]
    out["extensions"] = []
    for k in range(1, nexts + 1):
        ext = b[128 * k:128 * (k + 1)]
        if len(ext) < 128:
            break
        if ext[0] == 0x02:
            out["extensions"].append("CTA-861")
            _cta(ext, out)
        elif ext[0] == 0x70:
            out["extensions"].append("DisplayID")
            _displayid(ext[1:], out)
        else:
            out["extensions"].append("%#04x" % ext[0])
    hdr = out.get("hdr_static_metadata") or {}
    out["hdr"] = any(e.startswith(("PQ", "HLG")) for e in hdr.get("eotfs", []))
    vr = out.get("range_vertical_Hz")
    out["vrr_hint"] = bool(out.get("hdmi_vrr_Hz") or out.get("freesync_Hz") or out.get("adaptive_sync")
                           or (vr and vr[1] - vr[0] >= 20 and b[24] & 0x01 and digital))
    return out


def edid_file(path):
    from . import sysfs
    try:
        with open(sysfs.root(path), "rb") as f:
            return f.read()
    except OSError:
        return None


# -- DCP dictionaries ----------------------------------------------------------

DICT, ARRAY, INT64, STRING, BLOB, BOOL = 1, 2, 4, 9, 10, 11


class _P:
    def __init__(self, data):
        self.d, self.pos = data, 0

    def tag(self):
        self.pos = (self.pos + 3) & ~3
        if self.pos + 4 > len(self.d):
            raise ValueError("truncated")
        v = struct.unpack_from("<I", self.d, self.pos)[0]
        self.pos += 4
        return v & 0xffffff, (v >> 24) & 0x1f, (v >> 29) & 3

    def take(self, n):
        if self.pos + n > len(self.d):
            raise ValueError("truncated")
        b = self.d[self.pos:self.pos + n]
        self.pos += n
        return b

    def value(self, depth=0):
        if depth > 32:
            raise ValueError("too deep")
        size, typ, pad = self.tag()
        if pad:
            raise ValueError("bad tag")
        if typ == DICT:
            out = {}
            for _ in range(size):
                k = self.value(depth + 1)
                out[k if isinstance(k, str) else str(k)] = self.value(depth + 1)
            return out
        if typ == ARRAY:
            return [self.value(depth + 1) for _ in range(size)]
        if typ == INT64:
            return struct.unpack("<q", self.take(8))[0]
        if typ == STRING:
            return self.take(size).split(b"\0")[0].decode("utf-8", "replace")
        if typ == BLOB:
            return {"blob_bytes": size, "_": self.take(size)[:0]}
        if typ == BOOL:
            return bool(size)
        raise ValueError("unknown type %d" % typ)


def dcp_dict(data):
    """The firmware dictionary, or None if the data is not one."""
    if not data or len(data) < 8 or struct.unpack_from("<I", data, 0)[0] != 0xd3:
        return None
    p = _P(data)
    p.pos = 4
    try:
        return p.value()
    except (ValueError, struct.error):
        return None


def _fx(v):
    """16.16 fixed point."""
    return round(v / 65536.0, 2) if isinstance(v, int) else v


def display_attributes(d):
    """The useful part of DisplayAttributes, flattened."""
    if not isinstance(d, dict):
        return {}
    pa = d.get("ProductAttributes") or {}
    lum = d.get("Luminance") or {}
    out = {}
    if pa.get("ProductName"):
        out["product_name"] = pa["ProductName"]
    if pa.get("ManufacturerID"):
        out["manufacturer_id"] = pa["ManufacturerID"]
    if pa.get("YearOfManufacture"):
        out["year"] = pa["YearOfManufacture"]
    for k, n in (("MinimumRefreshRate", "min_refresh_Hz"), ("MaximumRefreshRate", "max_refresh_Hz")):
        if k in d:
            out[n] = d[k]
    for k, n in (("MinimumVariableRefreshRate", "vrr_min_Hz"), ("MaximumVariableRefreshRate", "vrr_max_Hz")):
        if k in d:
            out[n] = _fx(d[k])
    for k in ("SupportsVariableRefreshRate", "SupportsPQEOTF", "SupportsHLGEOTF", "SupportsHDRGammaEOTF",
              "SupportsHDRStaticMetadataType1", "DefaultColorSpaceIsSRGB", "HasHDMILegacyEDID"):
        if k in d:
            out[k] = bool(d[k])
    for k in ("SupportsBT2020RGB", "SupportsBT2020YCC"):
        if k in d:
            out[k] = d[k]
    if lum:
        out["luminance_nits"] = {k: _fx(v) for k, v in lum.items() if isinstance(v, int)}
    if "NativeFormatHorizontalPixels" in d:
        out["native"] = "%sx%s" % (d.get("NativeFormatHorizontalPixels"), d.get("NativeFormatVerticalPixels"))
    if "MaxHorizontalImageSize" in d:
        out["image_size_cm"] = [d.get("MaxHorizontalImageSize"), d.get("MaxVerticalImageSize")]
    return out


def transport(d):
    if not isinstance(d, dict):
        return None
    return {k: v for k, v in d.items() if isinstance(v, (str, int, bool))}
