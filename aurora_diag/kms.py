# SPDX-License-Identifier: MIT
"""Read-only KMS state through the DRM ioctls, in plain Python.

Only GET ioctls are used. GETCONNECTOR is always called with room for the
modes, so the kernel never runs a forced probe on our behalf (a probe with
count_modes == 0 is what DRM masters trigger), and if opening the node
made us DRM master (no compositor running) we drop it straight away. The
result is the connectors, their properties (vrr_capable, Colorspace, HDR
output metadata, max bpc, link-status, ...), the CRTCs with their current
mode and properties (VRR_ENABLED, ACTIVE, GAMMA_LUT_SIZE) and the
encoders. Nothing here can change what is on screen.
"""

import ctypes
import fcntl
import os
import struct

from . import sysfs

_IOC_WRITE, _IOC_READ = 1, 2


def _iowr(nr, size):
    return ((_IOC_READ | _IOC_WRITE) << 30) | (size << 16) | (ord("d") << 8) | nr


def _iow(nr, size):
    return (_IOC_WRITE << 30) | (size << 16) | (ord("d") << 8) | nr


def _io(nr):
    return (ord("d") << 8) | nr


class ModeInfo(ctypes.Structure):
    _fields_ = [("clock", ctypes.c_uint32), ("hdisplay", ctypes.c_uint16), ("hsync_start", ctypes.c_uint16),
                ("hsync_end", ctypes.c_uint16), ("htotal", ctypes.c_uint16), ("hskew", ctypes.c_uint16),
                ("vdisplay", ctypes.c_uint16), ("vsync_start", ctypes.c_uint16), ("vsync_end", ctypes.c_uint16),
                ("vtotal", ctypes.c_uint16), ("vscan", ctypes.c_uint16), ("vrefresh", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("type", ctypes.c_uint32), ("name", ctypes.c_char * 32)]


class CardRes(ctypes.Structure):
    _fields_ = [("fb_id_ptr", ctypes.c_uint64), ("crtc_id_ptr", ctypes.c_uint64),
                ("connector_id_ptr", ctypes.c_uint64), ("encoder_id_ptr", ctypes.c_uint64),
                ("count_fbs", ctypes.c_uint32), ("count_crtcs", ctypes.c_uint32),
                ("count_connectors", ctypes.c_uint32), ("count_encoders", ctypes.c_uint32),
                ("min_width", ctypes.c_uint32), ("max_width", ctypes.c_uint32),
                ("min_height", ctypes.c_uint32), ("max_height", ctypes.c_uint32)]


class Crtc(ctypes.Structure):
    _fields_ = [("set_connectors_ptr", ctypes.c_uint64), ("count_connectors", ctypes.c_uint32),
                ("crtc_id", ctypes.c_uint32), ("fb_id", ctypes.c_uint32), ("x", ctypes.c_uint32),
                ("y", ctypes.c_uint32), ("gamma_size", ctypes.c_uint32), ("mode_valid", ctypes.c_uint32),
                ("mode", ModeInfo)]


class Encoder(ctypes.Structure):
    _fields_ = [("encoder_id", ctypes.c_uint32), ("encoder_type", ctypes.c_uint32), ("crtc_id", ctypes.c_uint32),
                ("possible_crtcs", ctypes.c_uint32), ("possible_clones", ctypes.c_uint32)]


class Connector(ctypes.Structure):
    _fields_ = [("encoders_ptr", ctypes.c_uint64), ("modes_ptr", ctypes.c_uint64), ("props_ptr", ctypes.c_uint64),
                ("prop_values_ptr", ctypes.c_uint64), ("count_modes", ctypes.c_uint32),
                ("count_props", ctypes.c_uint32), ("count_encoders", ctypes.c_uint32),
                ("encoder_id", ctypes.c_uint32), ("connector_id", ctypes.c_uint32),
                ("connector_type", ctypes.c_uint32), ("connector_type_id", ctypes.c_uint32),
                ("connection", ctypes.c_uint32), ("mm_width", ctypes.c_uint32), ("mm_height", ctypes.c_uint32),
                ("subpixel", ctypes.c_uint32), ("pad", ctypes.c_uint32)]


class Property(ctypes.Structure):
    _fields_ = [("values_ptr", ctypes.c_uint64), ("enum_blob_ptr", ctypes.c_uint64), ("prop_id", ctypes.c_uint32),
                ("flags", ctypes.c_uint32), ("name", ctypes.c_char * 32), ("count_values", ctypes.c_uint32),
                ("count_enum_blobs", ctypes.c_uint32)]


class PropEnum(ctypes.Structure):
    _fields_ = [("value", ctypes.c_uint64), ("name", ctypes.c_char * 32)]


class Blob(ctypes.Structure):
    _fields_ = [("blob_id", ctypes.c_uint32), ("length", ctypes.c_uint32), ("data", ctypes.c_uint64)]


class ObjProps(ctypes.Structure):
    _fields_ = [("props_ptr", ctypes.c_uint64), ("prop_values_ptr", ctypes.c_uint64), ("count_props", ctypes.c_uint32),
                ("obj_id", ctypes.c_uint32), ("obj_type", ctypes.c_uint32)]


class ClientCap(ctypes.Structure):
    _fields_ = [("capability", ctypes.c_uint64), ("value", ctypes.c_uint64)]


GETRESOURCES = _iowr(0xA0, ctypes.sizeof(CardRes))
GETCRTC = _iowr(0xA1, ctypes.sizeof(Crtc))
GETENCODER = _iowr(0xA6, ctypes.sizeof(Encoder))
GETCONNECTOR = _iowr(0xA7, ctypes.sizeof(Connector))
GETPROPERTY = _iowr(0xAA, ctypes.sizeof(Property))
GETPROPBLOB = _iowr(0xAC, ctypes.sizeof(Blob))
OBJ_GETPROPERTIES = _iowr(0xB9, ctypes.sizeof(ObjProps))
SET_CLIENT_CAP = _iow(0x0D, ctypes.sizeof(ClientCap))
DROP_MASTER = _io(0x1f)

OBJ_CRTC, OBJ_CONNECTOR = 0xcccccccc, 0xc0c0c0c0
PROP_RANGE, PROP_IMMUTABLE, PROP_ENUM, PROP_BLOB, PROP_BITMASK = 2, 4, 8, 16, 32
PROP_EXTENDED_TYPE = 0x0000ffc0
PROP_OBJECT, PROP_SIGNED_RANGE = 1 << 6, 2 << 6
CONNECTION = {1: "connected", 2: "disconnected", 3: "unknown"}
# The names the kernel uses for connectors (drm_connector_enum_list), so
# KMS and sysfs (card2-DP-1) agree.
CONNECTOR_TYPES = {0: "Unknown", 1: "VGA", 2: "DVI-I", 3: "DVI-D", 4: "DVI-A", 5: "Composite", 6: "SVIDEO",
                   7: "LVDS", 8: "Component", 9: "DIN", 10: "DP", 11: "HDMI-A", 12: "HDMI-B",
                   13: "TV", 14: "eDP", 15: "Virtual", 16: "DSI", 17: "DPI", 18: "Writeback", 19: "SPI", 20: "USB"}
MAX_MODES = 512


def _buf(n, typ=ctypes.c_uint32):
    b = (typ * max(n, 1))()
    return b, ctypes.addressof(b)


class Card:
    def __init__(self, path):
        self.path = path
        self.fd = os.open(sysfs.root(path), os.O_RDWR | os.O_CLOEXEC)
        try:
            fcntl.ioctl(self.fd, DROP_MASTER)   # only succeeds if opening made us master
        except OSError:
            pass
        for cap in (2, 3):   # UNIVERSAL_PLANES, ATOMIC: show the atomic properties too
            try:
                fcntl.ioctl(self.fd, SET_CLIENT_CAP, ClientCap(cap, 1))
            except OSError:
                pass
        self._props = {}

    def close(self):
        os.close(self.fd)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def _ioctl(self, req, st):
        fcntl.ioctl(self.fd, req, st)
        return st

    def resources(self):
        r = self._ioctl(GETRESOURCES, CardRes())
        crtcs, pc = _buf(r.count_crtcs)
        conns, pn = _buf(r.count_connectors)
        encs, pe = _buf(r.count_encoders)
        r2 = CardRes(0, pc, pn, pe, 0, r.count_crtcs, r.count_connectors, r.count_encoders)
        self._ioctl(GETRESOURCES, r2)
        return {"crtcs": list(crtcs)[:r2.count_crtcs], "connectors": list(conns)[:r2.count_connectors],
                "encoders": list(encs)[:r2.count_encoders],
                "max_width": r.max_width, "max_height": r.max_height}

    def prop_info(self, pid):
        if pid in self._props:
            return self._props[pid]
        p = self._ioctl(GETPROPERTY, Property(prop_id=pid))
        info = {"name": p.name.decode(errors="replace"), "flags": p.flags,
                "immutable": bool(p.flags & PROP_IMMUTABLE)}
        nv, ne = p.count_values, p.count_enum_blobs
        vals, pv = _buf(nv, ctypes.c_uint64)
        enums = (PropEnum * max(ne, 1))()
        p2 = Property(values_ptr=pv, enum_blob_ptr=ctypes.addressof(enums) if p.flags & (PROP_ENUM | PROP_BITMASK) else 0,
                      prop_id=pid, count_values=nv, count_enum_blobs=ne if p.flags & (PROP_ENUM | PROP_BITMASK) else 0)
        try:
            self._ioctl(GETPROPERTY, p2)
        except OSError:
            pass
        ext = p.flags & PROP_EXTENDED_TYPE
        if p.flags & PROP_RANGE or ext == PROP_SIGNED_RANGE:
            info["type"] = "range"
            info["range"] = [int(v) for v in list(vals)[:nv]]
        elif p.flags & (PROP_ENUM | PROP_BITMASK):
            info["type"] = "enum" if p.flags & PROP_ENUM else "bitmask"
            info["enum"] = {int(e.value): e.name.decode(errors="replace") for e in list(enums)[:ne]}
        elif p.flags & PROP_BLOB:
            info["type"] = "blob"
        elif ext == PROP_OBJECT:
            info["type"] = "object"
        else:
            info["type"] = "other"
        self._props[pid] = info
        return info

    def blob(self, bid):
        if not bid:
            return None
        b = self._ioctl(GETPROPBLOB, Blob(blob_id=bid))
        data = (ctypes.c_uint8 * max(b.length, 1))()
        self._ioctl(GETPROPBLOB, Blob(blob_id=bid, length=b.length, data=ctypes.addressof(data)))
        return bytes(data)[:b.length]

    def _decode(self, pid, value):
        info = self.prop_info(pid)
        v = int(value)
        out = {"raw": v, "immutable": info["immutable"], "type": info["type"]}
        if info["type"] == "enum":
            out["value"] = info["enum"].get(v, v)
            out["options"] = list(info["enum"].values())
        elif info["type"] == "bitmask":
            out["value"] = [n for b, n in info["enum"].items() if v & (1 << b)]
            out["options"] = list(info["enum"].values())
        elif info["type"] == "range":
            out["value"] = v
            out["range"] = info.get("range")
        elif info["type"] == "blob":
            out["value"] = v   # blob id; 0 means none
        else:
            out["value"] = v
        return info["name"], out

    def obj_props(self, obj_id, obj_type):
        op = self._ioctl(OBJ_GETPROPERTIES, ObjProps(obj_id=obj_id, obj_type=obj_type))
        n = op.count_props
        ids, pi = _buf(n)
        vals, pv = _buf(n, ctypes.c_uint64)
        op = self._ioctl(OBJ_GETPROPERTIES, ObjProps(pi, pv, n, obj_id, obj_type))
        out = {}
        for pid, v in zip(list(ids)[:op.count_props], list(vals)[:op.count_props]):
            try:
                name, d = self._decode(pid, v)
                out[name] = d
            except OSError:
                pass
        return out

    def connector(self, cid):
        modes = (ModeInfo * MAX_MODES)()
        props, pp = _buf(64)
        pvals, pv = _buf(64, ctypes.c_uint64)
        encs, pe = _buf(16)
        c = Connector(pe, ctypes.addressof(modes), pp, pv, MAX_MODES, 64, 16, connector_id=cid)
        self._ioctl(GETCONNECTOR, c)
        return {"id": cid, "type": CONNECTOR_TYPES.get(c.connector_type, str(c.connector_type)),
                "type_id": c.connector_type_id,
                "name": "%s-%d" % (CONNECTOR_TYPES.get(c.connector_type, "Unknown"), c.connector_type_id),
                "connection": CONNECTION.get(c.connection, str(c.connection)),
                "mm": [c.mm_width, c.mm_height], "encoder_id": c.encoder_id,
                "encoders": list(encs)[:min(c.count_encoders, 16)],
                "modes": [_mode(m) for m in list(modes)[:min(c.count_modes, MAX_MODES)]],
                "props": self.obj_props(cid, OBJ_CONNECTOR)}

    def crtc(self, cid):
        c = self._ioctl(GETCRTC, Crtc(crtc_id=cid))
        return {"id": cid, "mode": _mode(c.mode) if c.mode_valid else None, "fb": c.fb_id,
                "gamma_size": c.gamma_size, "props": self.obj_props(cid, OBJ_CRTC)}

    def encoder(self, eid):
        e = self._ioctl(GETENCODER, Encoder(encoder_id=eid))
        return {"id": eid, "crtc_id": e.crtc_id, "possible_crtcs": e.possible_crtcs}


def _mode(m):
    tot = m.htotal * m.vtotal
    hz = round(m.clock * 1000.0 / tot, 2) if tot else m.vrefresh
    return {"name": m.name.decode(errors="replace"), "w": m.hdisplay, "h": m.vdisplay, "refresh_Hz": hz,
            "clock_kHz": m.clock, "preferred": bool(m.type & (1 << 3)), "vrr_flag": None}


def card_state(path):
    """Everything KMS says about one card, or {"error": ...}."""
    try:
        card = Card(path)
    except OSError as e:
        return {"error": "%s: %s" % (path, e.strerror)}
    try:
        res = card.resources()
        crtcs = []
        for i, c in enumerate(res["crtcs"]):
            try:
                d = card.crtc(c)
                d["index"] = i
                crtcs.append(d)
            except OSError as e:
                crtcs.append({"id": c, "index": i, "error": e.strerror})
        encs = {}
        for e in res["encoders"]:
            try:
                encs[e] = card.encoder(e)
            except OSError:
                pass
        conns = []
        for c in res["connectors"]:
            try:
                d = card.connector(c)
            except OSError as e:
                conns.append({"id": c, "error": e.strerror})
                continue
            crtc_prop = (d["props"].get("CRTC_ID") or {}).get("value")
            enc = encs.get(d["encoder_id"]) or {}
            crtc_id = crtc_prop or enc.get("crtc_id") or 0
            d["crtc"] = next((x for x in crtcs if x["id"] == crtc_id), None) if crtc_id else None
            masks = 0
            for e in d["encoders"]:
                masks |= (encs.get(e) or {}).get("possible_crtcs", 0)
            d["possible_crtcs"] = [i for i in range(len(crtcs)) if masks & (1 << i)]
            edid_id = (d["props"].get("EDID") or {}).get("value")
            hdr_id = (d["props"].get("HDR_OUTPUT_METADATA") or {}).get("value")
            try:
                d["edid_blob"] = card.blob(edid_id)
            except OSError:
                d["edid_blob"] = None
            try:
                d["hdr_output_metadata"] = _hdr_meta(card.blob(hdr_id)) if hdr_id else None
            except OSError:
                d["hdr_output_metadata"] = None
            conns.append(d)
        return {"path": path, "crtcs": crtcs, "connectors": conns,
                "max_size": [res["max_width"], res["max_height"]]}
    except OSError as e:
        return {"error": "%s: %s" % (path, e.strerror)}
    finally:
        card.close()


def _hdr_meta(b):
    """struct hdr_output_metadata: u32 type, then hdr_metadata_infoframe."""
    if not b or len(b) < 30:
        return {"bytes": len(b or b"")}
    eotf, mtype = b[4], b[5]
    vals = struct.unpack_from("<12H", b, 6)
    names = {0: "SDR", 1: "HDR gamma", 2: "PQ (ST 2084)", 3: "HLG"}
    return {"eotf": names.get(eotf, eotf), "metadata_type": mtype,
            "max_display_mastering_nits": vals[8], "min_display_mastering": vals[9] / 10000.0,
            "max_cll": vals[10], "max_fall": vals[11]}


def fdt_node_order():
    """Device-tree node paths in the order the tree lists them, read from
    the flattened tree the kernel booted with (/sys/firmware/fdt, root
    only). Drivers that walk the tree (appledrm numbers its CRTCs by DCP
    node order) follow this order; sysfs listings are sorted by name."""
    try:
        with open(sysfs.root("/sys/firmware/fdt"), "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < 40 or struct.unpack_from(">I", data, 0)[0] != 0xd00dfeed:
        return None
    off = struct.unpack_from(">I", data, 8)[0]
    end = off + struct.unpack_from(">I", data, 36)[0]
    pos, stack, out = off, [], []
    while pos + 4 <= end:
        tok = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if tok == 1:
            z = data.index(b"\0", pos)
            name = data[pos:z].decode("ascii", "replace")
            pos = (z + 4) & ~3
            stack.append(name)
            out.append("/" + "/".join(s for s in stack if s))
        elif tok == 2:
            if stack:
                stack.pop()
        elif tok == 3:
            ln = struct.unpack_from(">I", data, pos)[0]
            pos = (pos + 8 + ln + 3) & ~3
        elif tok == 4:
            continue
        elif tok == 9:
            break
        else:
            break
    return out
