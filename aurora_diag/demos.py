# SPDX-License-Identifier: MIT
"""Demos: one place to see and start every demo this machine can run.

Each demo says what you will see, whether this machine can run it (and why
not), and how it starts and stops. Nothing starts by itself: a demo runs
only when someone presses its key, clicks it, or runs `aurora-diag demo
NAME`. Nothing here plays audio. The camera and the microphone are used
only by the demos that say so, and only after an explicit start.

Kinds:
    live    drawn here from sysfs, read only (motion, speaker protection)
    bench   runs a short command, shows its output and the result
            (fps, ms), next to a reference number where one exists
    launch  starts an app (a camera preview, a window) that runs until
            stopped; stopping also runs the demo's own "off" command

Programs are found, never assumed: PATH, the desktop user's
~/.local/bin, ~/aurora-try-dev and the bench kits under /var/tmp and
/root. A demo whose program or device is missing says which.
"""

import csv
import glob
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time

from . import log, sysfs

# Reference numbers measured on macOS on the same machine (AVE: the
# engines report, 2026-10-31; AVD: avd-bench's macos.csv when it is
# installed). Keyed by board compatible.
AVE_MACOS = {"apple,j616s": {"h264-1080": 235.6, "hevc-1080": 223.5, "h264-4k": 62.1, "hevc-4k": 59.0}}
AVE_SOURCE = "macOS VideoToolbox on the same machine (engines report 2026-10-31, COMPARE.md)"


# -- discovery helpers ----------------------------------------------------------

def desktop_user():
    from . import sections
    return sections._desktop_user()


def homes():
    out = []
    u = desktop_user()
    if u:
        out.append(u.pw_dir)
    if os.geteuid() != 0:
        out.append(os.path.expanduser("~"))
    return [h for i, h in enumerate(out) if h and h not in out[:i]]


def find_bin(names, globs=()):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    for h in homes():
        for n in names:
            p = os.path.join(h, ".local/bin", n)
            if os.access(p, os.X_OK):
                return p
    for g in globs:
        for p in sorted(glob.glob(g)):
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


def aurora_try(group=None):
    """The aurora-try CLI: installed, or a development checkout."""
    p = find_bin(["aurora-try"])
    if p:
        return p
    for h in homes():
        for sub in (["aurora-try-int"] + (["aurora-try-" + group] if group else [])):
            c = os.path.join(h, "aurora-try-dev", sub, "bin", "aurora-try")
            if os.access(c, os.X_OK):
                return c
    return None


def v4l2_device(name_rx):
    """(/dev/videoN, platform device, its /dev/mediaN) for the V4L2 node
    whose name matches name_rx."""
    for v in sysfs.listdir("/sys/class/video4linux"):
        d = "/sys/class/video4linux/" + v
        if re.search(name_rx, sysfs.read(d + "/name") or ""):
            dev = sysfs.realpath(d + "/device")
            media = None
            for base in ("/sys/bus/media/devices", "/sys/class/media"):
                for m in sysfs.listdir(base):
                    here = sysfs.realpath("%s/%s" % (base, m))
                    if media is None and dev in (os.path.dirname(here), sysfs.realpath(here + "/device")):
                        media = "/dev/" + m
            return "/dev/" + v, os.path.basename(dev), media
    return None, None, None


def session_ok():
    u = desktop_user()
    if not u:
        return False, "no desktop session (a demo with a window needs a logged-in session)"
    if not glob.glob("/run/user/%d/wayland-*" % u.pw_uid) and not os.path.exists("/tmp/.X11-unix"):
        return False, "the desktop user has no Wayland or X11 display"
    return True, u.pw_name


# -- motion -----------------------------------------------------------------------

def _iio(name):
    for d in sysfs.listdir("/sys/bus/iio/devices"):
        p = "/sys/bus/iio/devices/" + d
        if sysfs.read(p + "/name") == name:
            return p
    return None


def _axes(p, kind):
    scale = sysfs.read_float("%s/in_%s_scale" % (p, kind))
    out, err = [], None
    for ax in "xyz":
        raw, why = sysfs.read_error("%s/in_%s_%s_raw" % (p, kind, ax))
        try:
            out.append(int(raw) * (scale if scale else 1.0))
        except (TypeError, ValueError):
            out.append(None)
            err = why or "unreadable"
    return out, err


def motion_sample():
    """One reading of every motion and environment sensor there is."""
    s = {"t": time.monotonic()}
    acc = _iio("aop-sensors-accel") or next((p for p in ("/sys/bus/iio/devices/" + d for d in sysfs.listdir("/sys/bus/iio/devices"))
                                             if glob.glob(sysfs.root(p) + "/in_accel_x_raw")), None)
    gyr = _iio("aop-sensors-gyro") or next((p for p in ("/sys/bus/iio/devices/" + d for d in sysfs.listdir("/sys/bus/iio/devices"))
                                            if glob.glob(sysfs.root(p) + "/in_anglvel_x_raw")), None)
    if acc:
        s["accel"], s["accel_error"] = _axes(acc, "accel")            # m/s^2
    if gyr:
        rad, s["gyro_error"] = _axes(gyr, "anglvel")                  # rad/s
        s["gyro"] = [math.degrees(v) if v is not None else None for v in rad]  # deg/s (raw / 65536 on the AOP)
    las = _iio("aop-sensors-las")
    if las:
        raw = sysfs.read_float(las + "/in_angl_raw")
        sc = sysfs.read_float(las + "/in_angl_scale")
        if raw is not None:
            s["lid_deg"] = math.degrees(raw * sc) if sc else raw
    als = _iio("aop-sensors-als")
    if als:
        s["lux"] = sysfs.read_float(als + "/in_illuminance_input")
        s["cct"] = sysfs.read_float(als + "/in_colortemp_input")
    return s


class MotionState:
    """Heading from the integrated gyro z rate, with the gyro bias and the
    level taken from the first samples (z re-zeroes both)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.heading = 0.0
        self.bias = None
        self._bias_acc = []
        self.level = None
        self.last_t = None
        self.peak = [1.0, 1.0, 1.0]

    def feed(self, s):
        g, a = s.get("gyro"), s.get("accel")
        if g and None not in g:
            if self.bias is None:
                self._bias_acc.append(g)
                if len(self._bias_acc) >= 10:
                    self.bias = [sum(x[i] for x in self._bias_acc) / len(self._bias_acc) for i in range(3)]
            else:
                if self.last_t is not None:
                    dt = min(s["t"] - self.last_t, 0.5)
                    self.heading = (self.heading + (g[2] - self.bias[2]) * dt) % 360
                for i in range(3):
                    self.peak[i] = max(self.peak[i] * 0.995, abs(g[i] - self.bias[i]), 1.0)
            self.last_t = s["t"]
        if a and None not in a and self.level is None:
            self.level = tilt(a)

    def rates(self, s):
        g = s.get("gyro")
        if not g or None in g:
            return None
        b = self.bias or [0.0, 0.0, 0.0]
        return [g[i] - b[i] for i in range(3)]


def tilt(a):
    """(pitch, roll) in degrees from gravity, whichever way the sensor faces."""
    ax, ay, az = a
    s = -1.0 if az < 0 else 1.0
    roll = math.degrees(math.atan2(ay, s * az))
    pitch = math.degrees(math.atan2(-ax, math.hypot(ay, az)))
    return pitch, roll


def _grid(w, h):
    return [[" "] * w for _ in range(h)]


def _line(g, x0, y0, x1, y1, ch):
    n = int(max(abs(x1 - x0), abs(y1 - y0))) or 1
    for i in range(n + 1):
        x = int(round(x0 + (x1 - x0) * i / n))
        y = int(round(y0 + (y1 - y0) * i / n))
        if 0 <= y < len(g) and 0 <= x < len(g[0]):
            g[y][x] = ch


def motion_art(s, st, width=96):
    """The motion demo as lines of text: a spirit level and a tilting
    horizon from gravity, a compass from the integrated gyro, rate bars,
    lid angle and ambient light. Returns (lines, styles) where styles maps
    a character to a colour name for the TUI."""
    a = s.get("accel")
    lines = []
    have_a = a and None not in a
    pitch, roll = tilt(a) if have_a else (0.0, 0.0)
    if have_a and st.level:
        pitch -= st.level[0]
        roll -= st.level[1]
    # spirit level: 25 x 11, bubble moves against the tilt, +-30 deg to the edge
    W, H = 25, 11
    lv = _grid(W, H)
    for x in range(W):
        lv[0][x] = lv[H - 1][x] = "─"
    for y in range(H):
        lv[y][0] = lv[y][W - 1] = "│"
    lv[0][0], lv[0][W - 1], lv[H - 1][0], lv[H - 1][W - 1] = "╭", "╮", "╰", "╯"
    cx, cy = W // 2, H // 2
    for y in range(1, H - 1):
        lv[y][cx] = "┊"
    for x in range(1, W - 1):
        lv[cy][x] = "┈"
    lv[cy][cx] = "┼"
    lv[cy - 2][cx - 4] = lv[cy - 2][cx + 4] = lv[cy + 2][cx - 4] = lv[cy + 2][cx + 4] = "·"
    if have_a:
        bx = cx + max(-1, min(1, -roll / 30.0)) * (cx - 2)
        by = cy + max(-1, min(1, pitch / 30.0)) * (cy - 1)
        bx, by = int(round(bx)), int(round(by))
        for dx, ch in ((-1, "("), (0, "●"), (1, ")")):
            if 0 < bx + dx < W - 1:
                lv[by][bx + dx] = ch
    # horizon: the laptop seen from the front, tilting with roll and
    # nodding with pitch; rasterised by rotating each cell back into the
    # laptop's frame (columns are half as wide as rows are tall)
    HW, HH = 25, 11
    hz = _grid(HW, HH)
    r = math.radians(roll) if have_a else 0.0
    hx, hy = HW / 2.0, 8.5 + (max(-2.0, min(2.0, pitch / 15.0)) if have_a else 0.0)
    for y in range(HH):
        for x in range(HW):
            dx, dy = (x - hx) / 2.0, (hy - y)
            u = dx * math.cos(r) - dy * math.sin(r)
            v = dx * math.sin(r) + dy * math.cos(r)
            if abs(u) <= 5.6 and -0.5 < v <= 0.5:
                hz[y][x] = "▀"
            elif abs(u) <= 4.6 and 0.5 < v <= 5.5:
                hz[y][x] = "█" if (abs(u) > 3.9 or v > 4.8) else "░"
    for x in range(HW):
        if hz[HH - 1][x] == " ":
            hz[HH - 1][x] = "─"
    # compass: circle radius 5 rows (10 columns), needle at the heading
    CW, CH = 25, 11
    cp = _grid(CW, CH)
    ox, oy = CW // 2, CH // 2
    for deg in range(0, 360, 10):
        t = math.radians(deg)
        x, y = int(round(ox + 10 * math.sin(t))), int(round(oy - 5 * math.cos(t)))
        cp[y][x] = "·"
    for lab, deg in (("N", 0), ("E", 90), ("S", 180), ("W", 270)):
        t = math.radians(deg)
        cp[int(round(oy - 5 * math.cos(t)))][int(round(ox + 10 * math.sin(t)))] = lab
    t = math.radians(st.heading)
    _line(cp, ox, oy, ox + 8 * math.sin(t), oy - 4 * math.cos(t), "•")
    cp[int(round(oy - 4 * math.cos(t)))][int(round(ox + 8 * math.sin(t)))] = "◆"
    cp[oy][ox] = "◉"
    head = ["  Spirit level (gravity)", "  Horizon (roll %+.1f°)" % roll if have_a else "  Horizon",
            "  Heading (gyro z, integrated)"]
    lines.append("".join(h.ljust(30) for h in head))
    for y in range(11):
        lines.append("  " + "".join(lv[y]) + "     " + "".join(hz[y]) + "     " + "".join(cp[y]))
    if have_a:
        lines.append("  pitch %+6.1f°  roll %+6.1f°  |g| %.2f m/s²%s     heading %5.1f°%s" % (
            pitch, roll, math.sqrt(sum(v * v for v in a)), "".ljust(1), 0.0 if st.heading >= 359.95 else st.heading,
            "  (calibrating gyro bias...)" if st.bias is None and s.get("gyro") else ""))
    else:
        lines.append("  accelerometer: %s" % (s.get("accel_error") or "absent"))
    lines.append("")
    rates = st.rates(s)
    lines.append("  Rotation rate (deg/s, bias removed)")
    if rates:
        for i, ax in enumerate("xyz"):
            v = rates[i]
            scale = max(st.peak[i], 20.0)
            n = int(round(min(abs(v) / scale, 1.0) * 30))
            bar = ("█" * n).rjust(30) + "│" + " " * 30 if v < 0 else " " * 30 + "│" + ("█" * n).ljust(30)
            lines.append("  %s %s %+8.2f" % (ax, bar, v))
    else:
        lines.append("  gyroscope: %s" % (s.get("gyro_error") or "absent"))
    if have_a:
        lines.append("  accel  x %+7.3f  y %+7.3f  z %+7.3f m/s²" % tuple(a))
    env = []
    if s.get("lid_deg") is not None:
        env.append("lid %.1f°" % s["lid_deg"])
    if s.get("lux") is not None:
        env.append("light %.0f lux" % s["lux"])
    if s.get("cct") is not None:
        env.append("colour %.0f K" % s["cct"])
    lines.append("")
    lines.append("  " + ("   ".join(env) if env else "no lid angle or light sensor"))
    return lines


def motion_probe():
    from . import sections
    items, found = sections.motion()
    acc = next((i for i in items if i["key"] == "accelerometer"), None)
    gyr = next((i for i in items if i["key"] == "gyroscope"), None)
    if found.get("accelerometer") or found.get("gyroscope"):
        s = motion_sample()
        if s.get("accel_error") and s.get("gyro_error"):
            return "warn", "registered but not reporting: %s" % s["accel_error"]
        return "ok", "aop-sensors-accel and aop-sensors-gyro reporting"
    return "absent", (acc or gyr or {}).get("detail", "no accelerometer or gyroscope")


# -- the demo table -----------------------------------------------------------------

class Demo:
    def __init__(self, name, title, kind, engine, what, probe, command=None, stop=None, uses=(), as_user=False,
                 parse=None, reference=None, timeout=120):
        self.name, self.title, self.kind, self.engine, self.what = name, title, kind, engine, what
        self._probe, self._command, self._stop = probe, command, stop
        self.uses, self.as_user, self.parse, self.reference, self.timeout = tuple(uses), as_user, parse, reference, timeout

    def probe(self):
        try:
            return self._probe()
        except Exception as e:
            return "warn", "probe failed: %s" % e

    def command(self):
        return self._command() if self._command else None

    def stop_command(self):
        return self._stop() if self._stop else None

    def as_dict(self, probe=True):
        d = {"name": self.name, "title": self.title, "kind": self.kind, "engine": self.engine, "what": self.what,
             "uses": list(self.uses)}
        if probe:
            d["level"], d["detail"] = self.probe()
            try:
                c = self.command()
            except Exception:
                c = None
            d["command"] = c
            d["reference"] = self.reference() if self.reference else None
        return d


def _need(*checks):
    """The first failing (level, why) of the checks, or None."""
    for c in checks:
        r = c()
        if r:
            return r
    return None


def _avebench():
    return find_bin(["avebench"], ["/var/tmp/*/avebench", "/root/*/avebench", "/root/avebench/avebench", "/root/avebench"])


def ave_probe():
    dev, pdev, _ = v4l2_device(r"apple-ave|\bave\b")
    nodes = [n for n in sysfs.listdir("/sys/firmware/devicetree/base") if n.startswith(("video-codec@", "ave@"))]
    if not dev:
        if not nodes:
            return "absent", "no AVE node in the live device tree"
        st = sysfs.dt_value("/sys/firmware/devicetree/base/%s/status" % nodes[0]) or "okay"
        return "absent", "AVE node /%s is %s, no apple-ave V4L2 device" % (nodes[0], st)
    b = _avebench()
    if not b:
        return "absent", "%s is ready, but avebench is not installed (build it from the AVE kit: cc -O2 -o avebench avebench.c)" % dev
    return "ok", "%s (%s), %s" % (dev, pdev, b)


def ave_command():
    dev, _, _ = v4l2_device(r"apple-ave|\bave\b")
    b = _avebench()
    return [b, dev, "1920", "1080", "h264", "300"] if dev and b else None


def ave_parse(text):
    m = re.findall(r"RESULT codec=(\S+) (\d+)x(\d+).*?fps=([\d.]+)", text)
    if not m:
        return None
    codec, w, h, fps = m[-1]
    return {"fps": float(fps), "label": "%s %sx%s" % (codec, w, h), "key": "%s-%s" % (codec, "1080" if h == "1080" else "4k")}


def _board():
    return (sysfs.dt_strings("/proc/device-tree/compatible") or [None])[0]


def ave_reference():
    r = AVE_MACOS.get(_board() or "")
    return {"value": r["h264-1080"], "unit": "fps", "what": "H.264 1080p", "source": AVE_SOURCE} if r else None


def _avdbench():
    return find_bin(["avd-bench"], ["/var/tmp/*/avd-bench/avd-bench", "/var/tmp/*/avd-bench", "/root/*/avd-bench"])


def _avd_sample():
    b = _avdbench()
    roots = [os.path.dirname(b), os.path.dirname(os.path.dirname(b))] if b else []
    for r in roots:
        for p in ("h264-1080.mp4", "samples/h264-1080.mp4", "media/h264-1080.mp4", "mediakit/samples/h264-1080.mp4"):
            if os.path.isfile(os.path.join(r, p)):
                return os.path.join(r, p)
    for g in ("/var/tmp/*/h264-1080.mp4", "/var/tmp/*/*/h264-1080.mp4", "/var/tmp/*/*/*/h264-1080.mp4",
              "/var/tmp/avd-demo/demo-h264-4k30.mp4"):
        hit = sorted(glob.glob(g))
        if hit:
            return hit[0]
    return None


def avd_probe():
    dev, pdev, media = v4l2_device(r"^avd$|apple-avd")
    if not dev:
        return "absent", "no AVD V4L2 device (driver not bound or no AVD node)"
    if not media:
        return "warn", "%s has no media device (the request API needs one)" % dev
    b = _avdbench()
    if not b:
        return "absent", "%s ready, but avd-bench is not installed (the AVD bench kit)" % dev
    s = _avd_sample()
    if not s:
        return "absent", "avd-bench found, but no sample clip (h264-1080.mp4) next to it"
    return "ok", "%s + %s, %s" % (dev, media, os.path.basename(s))


def avd_command():
    dev, _, media = v4l2_device(r"^avd$|apple-avd")
    b, s = _avdbench(), _avd_sample()
    if not (dev and media and b and s):
        return None
    return [b, "--input", s, "--video", dev, "--media", media, "--queue", "4", "--passes", "2"]


def avd_parse(text):
    best = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        fps = d.get("best_fps") or d.get("fps")
        if isinstance(fps, (int, float)) and (best is None or fps > best["fps"]):
            best = {"fps": float(fps), "bitexact": d.get("bitexact"), "status": d.get("status"),
                    "label": "%s %s" % (d.get("codec", ""), d.get("height", "") or "")}
    if best is None:
        m = re.findall(r"fps[=: ]+([\d.]+)", text)
        if m:
            best = {"fps": float(m[-1]), "bitexact": "bitexact=y" in text or '"bitexact": true' in text}
    return best


def avd_reference():
    b = _avdbench()
    if not b:
        return None
    board = (_board() or "").split(",")[-1]
    for p in (os.path.join(os.path.dirname(b), "macos.csv"), os.path.join(os.path.dirname(os.path.dirname(b)), "macos.csv")):
        try:
            with open(p) as f:
                for row in csv.DictReader(f):
                    if row.get("machine") == board and row.get("codec") == "h264" and row.get("height") == "1080":
                        return {"value": float(row["fps"]), "unit": "fps", "what": "H.264 1080p", "source": "avd-bench macos.csv (%s)" % row.get("source")}
        except OSError:
            continue
    return None


def ane_probe():
    accel = sysfs.listdir("/sys/class/accel")
    if not accel:
        nodes = [n for n in sysfs.listdir("/sys/firmware/devicetree/base") if n.startswith("ane@")]
        st = sysfs.dt_value("/sys/firmware/devicetree/base/%s/status" % nodes[0]) if nodes else None
        return "absent", ("ANE node /%s is %s" % (nodes[0], st)) if nodes and st not in (None, "okay") else "no /dev/accel device (ANE driver not bound)"
    t = aurora_try("classify")
    if not t:
        return "absent", "%s present, but aurora-try (classify) is not installed for the desktop user" % accel[0]
    return "ok", "%s, %s classify" % (accel[0], t)


def try_cmd(*args, group=None):
    def f():
        t = aurora_try(group)
        return [t] + list(args) if t else None
    return f


def try_probe(needs=(), group=None, window=False, camera=False, mic=False):
    def f():
        t = aurora_try(group)
        if not t:
            return "absent", "aurora-try is not installed for the desktop user (see aurora-try's TRY.md)"
        if camera:
            dev, _, _ = v4l2_device(r"isp|uvc|camera")
            if not dev:
                return "absent", "no camera V4L2 device (apple-isp not bound)"
        if mic and not sysfs.exists("/proc/asound/cards"):
            return "absent", "no sound card"
        if window:
            ok, why = session_ok()
            if not ok:
                return "absent", why
        return "ok", t
    return f


def gpu_probe():
    ok, why = session_ok()
    if not ok:
        return "absent", why
    g = find_bin(["glmark2-wayland", "glmark2"])
    if not g:
        return "absent", "glmark2 is not installed"
    return "ok", "%s (off-screen, in %s's session)" % (os.path.basename(g), why)


def gpu_command():
    g = find_bin(["glmark2-wayland", "glmark2"])
    if not g:
        return None
    return [g, "--off-screen", "-s", "1280x720", "-b", "build:use-vbo=true:duration=3", "-b", "texture:duration=3",
            "-b", "shading:shading=phong:duration=3", "-b", "refract:duration=3"]


def gpu_parse(text):
    m = re.search(r"glmark2 Score:\s*(\d+)", text)
    fps = [float(x) for x in re.findall(r"FPS:\s*([\d.]+)", text)]
    if not m and not fps:
        return None
    return {"score": int(m.group(1)) if m else None, "label": "glmark2 off-screen 720p, %d scenes" % len(fps)}


def vkcube_probe():
    ok, why = session_ok()
    if not ok:
        return "absent", why
    return ("ok", "vkcube in %s's session" % why) if find_bin(["vkcube"]) else ("absent", "vkcube is not installed (vulkan-tools)")


def classify_parse(text):
    ms = re.findall(r"([\d.]+)\s*ms", text)
    return {"ms": float(ms[-1]), "label": "last timing in the output"} if ms else None


def speaker_probe():
    from . import audio
    rs = audio.speaker_protection()
    svc = next((r["value"] for r in rs if r["id"] == "speaker.speakerguardd.service"), None)
    if any(r["attr"] == "t_coil_C" for r in rs):
        return "ok", "speakerguardd %s, live model readable (read only, nothing plays)" % svc
    if svc == "active":
        return "warn", "speakerguardd active but its state is unreadable (run as root)"
    return "absent", "speakerguardd is %s on this machine" % (svc or "not installed")


def build():
    sample = lambda: next((p for h in homes() for p in [os.path.join(h, "aurora-try-dev/data/demo-30s.wav")] if os.path.isfile(p)), None)
    return [
        Demo("motion", "Motion", "live", "AOP (accelerometer, gyroscope, lid, light)",
             "a spirit level and a tilting horizon from gravity, a compass that turns as you rotate the laptop "
             "(integrated gyro z), live rotation-rate bars, the lid angle and the ambient light. Tilt or turn the "
             "machine. z re-zeroes the level and the heading.",
             motion_probe),
        Demo("speakers", "Speaker protection", "live", "speakerguardd (read only)",
             "each speaker's modelled coil and magnet temperature, power and measured voltage, live. Nothing plays.",
             speaker_probe),
        Demo("ave", "Hardware encode (AVE)", "bench", "AVE",
             "300 frames of 1080p H.264 through the hardware encoder (avebench), the fps it reached, and the macOS "
             "number on the same machine where one was measured.",
             ave_probe, ave_command, parse=ave_parse, reference=ave_reference, timeout=120),
        Demo("avd", "Hardware decode (AVD)", "bench", "AVD",
             "a 1080p H.264 clip decoded by the hardware decoder, every frame checked bit for bit against software "
             "decode (avd-bench), the fps, and the macOS number where one was measured.",
             avd_probe, avd_command, parse=avd_parse, reference=avd_reference, timeout=240),
        Demo("ane", "Neural Engine classify", "bench", "ANE vs CPU",
             "photos classified by MobileNetV1 on the Neural Engine and on the CPU: the label for each photo and "
             "the milliseconds each engine took.",
             ane_probe, try_cmd("classify", group="classify"), as_user=True, parse=classify_parse, timeout=300),
        Demo("gpu", "GPU benchmark", "bench", "GPU (aurora + Borealis/Mesa)",
             "glmark2 off-screen at 720p for a few seconds per scene and its score. Nothing appears on screen.",
             gpu_probe, gpu_command, as_user=True, parse=gpu_parse, timeout=120),
        Demo("vkcube", "Spinning cube (Vulkan)", "launch", "GPU (Vulkan)",
             "a window with the spinning Vulkan cube until you stop it.",
             vkcube_probe, lambda: [find_bin(["vkcube"])] if find_bin(["vkcube"]) else None, as_user=True),
        Demo("camera-blur", "Camera background blur", "launch", "ANE/CPU (aurora-try)",
             "a live camera preview with the background blurred; apps can pick the camera 'try'. Uses the camera.",
             try_probe(window=True, camera=True), try_cmd("blur"), try_cmd("camera-off"), uses=("camera",), as_user=True),
        Demo("camera-frame", "Camera Center Stage framing", "launch", "ANE/CPU (aurora-try)",
             "a live preview whose crop follows your face. Uses the camera.",
             try_probe(window=True, camera=True), try_cmd("frame"), try_cmd("camera-off"), uses=("camera",), as_user=True),
        Demo("camera-denoise", "Camera denoise", "launch", "ANE/CPU (aurora-try)",
             "raw camera on the left, denoised on the right (best in low light). Uses the camera.",
             try_probe(window=True, camera=True), try_cmd("denoise"), try_cmd("camera-off"), uses=("camera",), as_user=True),
        Demo("captions-file", "Captions from a file", "bench", "GPU/CPU (whisper.cpp)",
             "a 30 s speech clip transcribed to text, faster than real time. No microphone.",
             try_probe(), lambda: ([aurora_try(), "transcribe", sample()] if aurora_try() and sample() else None),
             as_user=True, timeout=300),
        Demo("captions-live", "Live captions", "launch", "GPU/CPU (whisper.cpp)",
             "a caption line from the microphone, refreshed every 2 s. Uses the microphone.",
             try_probe(mic=True), try_cmd("captions"), uses=("microphone",), as_user=True),
        Demo("mic-denoise", "Noise-suppressed microphone", "launch", "CPU (DeepFilterNet3)",
             "a new microphone 'Aurora Microphone (df3)' that apps can pick; it does not become the default. "
             "Uses the microphone.",
             try_probe(mic=True), try_cmd("mic-denoise", "on"), try_cmd("mic-denoise", "off"), uses=("microphone",), as_user=True),
        Demo("photos", "Photo search", "bench", "CPU (CLIP-class model)",
             "your ~/Pictures searched for 'a dog on a beach': ranked matches and an HTML gallery.",
             try_probe(), try_cmd("photos", "search", "a dog on a beach"), as_user=True, timeout=300),
        Demo("upscale", "Photo upscale 4x", "bench", "GPU (ncnn Vulkan)",
             "a photo upscaled 4x with a before/after page to open; prints the time it took.",
             try_probe(), try_cmd("upscale", "demo", "--x4"), as_user=True, timeout=300),
        Demo("web", "Camera in a browser", "launch", "camera + effects (aurora-try web)",
             "a URL to open on your phone with the live camera and effect buttons. Uses the camera.",
             try_probe(camera=True), try_cmd("web", "--lan"), uses=("camera",), as_user=True),
    ]


DEMOS = None


def all_demos():
    global DEMOS
    if DEMOS is None:
        DEMOS = build()
    return DEMOS


def get(name):
    for d in all_demos():
        if d.name == name:
            return d
    raise KeyError("no demo %r (one of: %s)" % (name, ", ".join(d.name for d in all_demos())))


def inventory():
    return [d.as_dict() for d in all_demos()]


def try_status():
    t = aurora_try()
    if not t:
        return None
    from . import sections
    u = desktop_user()
    rc, out = sections._run([t, "status"], user=u, timeout=10)
    return out.strip() if out else None


# -- running ---------------------------------------------------------------------------

def _user_env(u):
    env = {"XDG_RUNTIME_DIR": "/run/user/%d" % u.pw_uid, "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/%d/bus" % u.pw_uid,
           "HOME": u.pw_dir, "USER": u.pw_name, "LANG": os.environ.get("LANG", "C.UTF-8"),
           "PATH": "%s/.local/bin:/usr/local/bin:/usr/bin:/bin" % u.pw_dir}
    way = sorted(glob.glob("/run/user/%d/wayland-[0-9]" % u.pw_uid))
    if way:
        env["WAYLAND_DISPLAY"] = os.path.basename(way[0])
    if os.path.exists("/tmp/.X11-unix/X0"):
        env["DISPLAY"] = ":0"
    return env


def spawn(demo, cmd):
    """Start cmd for demo (as the desktop user when the demo needs their
    session) in its own process group. Returns the Popen."""
    u = desktop_user() if demo.as_user else None
    if u is not None and os.geteuid() == 0 and u.pw_uid != 0:
        env = _user_env(u)
        full = ["runuser", "-u", u.pw_name, "--", "env"] + ["%s=%s" % kv for kv in env.items()] + cmd
        envp = None
    elif u is not None:
        envp = dict(os.environ, **_user_env(u))
        full = cmd
    else:
        envp, full = None, cmd
    log.event("demo_start", demo=demo.name, command=cmd, user=u.pw_name if u else None)
    return subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                            start_new_session=True, env=envp, cwd=(u.pw_dir if u else "/"))


def stop(demo, proc):
    """Stop a running demo: SIGINT its process group (like Ctrl-C), then
    SIGTERM, then SIGKILL; then the demo's own off command."""
    if proc is not None and proc.poll() is None:
        for sig, wait in ((signal.SIGINT, 3), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                break
            try:
                proc.wait(wait)
                break
            except subprocess.TimeoutExpired:
                continue
    off = demo.stop_command()
    res = None
    if off:
        p = spawn(demo, off)
        try:
            res = p.communicate(timeout=30)[0]
        except subprocess.TimeoutExpired:
            p.kill()
    log.event("demo_stop", demo=demo.name, off=off)
    return res


def result_line(demo, text):
    """The result as one line, with the reference where there is one."""
    r = demo.parse(text) if demo.parse else None
    if not r:
        return None, None
    ref = demo.reference() if demo.reference else None
    parts = []
    if r.get("fps") is not None:
        parts.append("%.1f fps" % r["fps"])
    if r.get("score") is not None:
        parts.append("score %d" % r["score"])
    if r.get("ms") is not None:
        parts.append("%.2f ms" % r["ms"])
    if r.get("bitexact") is not None:
        parts.append("bit-exact" if r["bitexact"] in (True, "y", "true") else "NOT bit-exact")
    if r.get("label"):
        parts.append(r["label"])
    if ref and r.get("fps"):
        parts.append("macOS %.1f fps (%.0f%%)" % (ref["value"], 100.0 * r["fps"] / ref["value"]))
    log.event("demo_result", demo=demo.name, result=r, reference=ref)
    return ", ".join(parts), r
