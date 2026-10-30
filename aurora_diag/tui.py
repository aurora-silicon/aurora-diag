# SPDX-License-Identifier: MIT
"""Interactive view for humans (curses, standard library only).

Keys: Left/Right or Tab switch pages, Up/Down/PgUp/PgDn move, / filters,
e edits the selected value (writable ones, through the same policy and
journal as the CLI), h sets the click strength, f sets a fan, a hands the
fans back, r restores this session's changes, q quits and restores them,
K quits and keeps them (fans are handed back either way).
"""

import curses
import time

from . import audio, control, fans, haptics, log, sources

PAGES = [
    ("Overview", None),
    ("Thermal", {"temp", "thermal", "cooling", "fan"}),
    ("Power", {"power", "battery", "regulator"}),
    ("CPU", {"cpufreq", "devfreq", "debugfs"}),
    ("GPU", {"gpu", "vulkan", "gl"}),
    ("Display", {"display", "backlight", "leds"}),
    ("Input", {"input", "haptic"}),
    ("Audio", {"speaker", "audio"}),
    ("Devices", {"typec", "usb", "radio", "net", "bt", "v4l2", "driver", "coproc", "sep", "iio"}),
    ("Params", {"modparam"}),
    ("Changes", "journal"),
]
SLOW = ("haptic", "typec", "usb", "input", "radio", "v4l2", "regulator", "remoteproc", "driver", "modparam", "sep", "drm", "debugfs")


class State:
    def __init__(self, args, m):
        self.args, self.m = args, m
        self.page, self.sel, self.top = 0, 0, 0
        self.filter = ""
        self.fast, self.slow = [], []
        self.api = None  # Vulkan and GL: run once, the first time the GPU page shows
        self.slow_at = 0
        self.msg = "q quits (restores), K keeps changes, e edits, / filters, f fan, h haptic, a fans auto"

    def refresh(self):
        self.fast = sources.collect(list(sources.FAST_SOURCES), [audio.speaker_protection])
        if time.monotonic() - self.slow_at > 10:
            self.slow = sources.collect(list(SLOW), [audio.alsa_controls])
            self.slow_at = time.monotonic()
        if any(e.get("session") == log.session() for e in fans.fan_entries()):
            lease = fans.read_lease() or {}
            fans.write_lease(15, lease.get("max_temp", fans.DEFAULT_MAX_TEMP))

    def rows(self):
        name, cats = PAGES[self.page]
        if isinstance(cats, set) and "vulkan" in cats and self.api is None:
            self.api = sources.collect(["api"])
        allr = self.fast + self.slow + (self.api or [])
        if cats is None:
            return overview_rows(allr, self.m)
        if cats == "journal":
            return [{"id": "%s %s" % (e["kind"], e["key"]), "value": "%s -> %s" % (e["original"], e.get("current")),
                     "unit": None, "label": "session %s" % e["session"], "writable": False, "path": None}
                    for e in control.journal_entries()] or [{"id": "no pending changes", "value": "", "unit": None, "label": "", "writable": False, "path": None}]
        rs = [r for r in allr if r["cat"] in cats]
        if self.filter:
            f = self.filter.lower()
            rs = [r for r in rs if f in r["id"].lower() or f in (r.get("label") or "").lower()]
        return rs


def overview_rows(rs, m):
    out = []

    def row(k, v, unit=None):
        out.append({"id": k, "value": v, "unit": unit, "label": "", "writable": False, "path": None})

    row("machine", "%s (%s, %s)" % (m.model, m.board, m.soc or "unknown SoC"))
    row("control", "enabled" if m.known else "read-only (not an Apple silicon device tree)")
    name, t = fans.hottest()
    if t is not None:
        row("hottest die", "%.1f C  %s" % (t, name))
    for r in rs:
        if r["cat"] == "iio" and r.get("unit") in ("deg", "lux"):
            row(r.get("label") or r["id"], r["value"], r["unit"])
    fl = fans.fans()
    if not fl:
        row("fans", "none (fanless)")
    for f in fl:
        row(f["label"], "%s rpm (target %s, %s-%s)%s" % (f["rpm"], f["target"], f["min"], f["max"],
                                                         "" if f["target_writable"] else " read-only"))
    for r in rs:
        if r["cat"] == "battery" and r["attr"] in ("capacity", "status", "power_now"):
            row("battery " + r["attr"], r["value"], r.get("unit"))
        if r["cat"] == "power" and r.get("label") in ("Total System Power", "AC Input Power", "Heatpipe Power"):
            row(r["label"], r["value"], r.get("unit"))
        if r["cat"] == "cpufreq" and r["attr"] == "scaling_cur_freq":
            row("cpufreq " + r["dev"], r["value"], r.get("unit"))
        if r["cat"] == "backlight" and r["attr"] == "brightness":
            row("backlight " + r["dev"], r["value"])
        if r["cat"] == "speaker" and r["attr"] in ("service", "armed"):
            row(r["id"], r["value"])
        if r["cat"] == "speaker" and r["attr"] in ("t_coil_C", "ff_coil_C"):
            row(r["id"].replace("speaker.speakerguardd.", "coil "), r["value"], "C")
    st = haptics.status(m)
    if st.get("supported") and "press" in st:
        row("click strength", "press %#x release %#x" % (st["press"], st["release"]))
    return out


def prompt(scr, text):
    h, w = scr.getmaxyx()
    curses.echo()
    curses.curs_set(1)
    scr.move(h - 1, 0)
    scr.clrtoeol()
    scr.addstr(h - 1, 0, text[:w - 1])
    try:
        v = scr.getstr(h - 1, min(len(text), w - 2), 80).decode()
    finally:
        curses.noecho()
        curses.curs_set(0)
    return v.strip()


def draw(scr, st):
    scr.erase()
    h, w = scr.getmaxyx()
    x = 0
    for i, (name, _) in enumerate(PAGES):
        attr = curses.A_REVERSE if i == st.page else curses.A_NORMAL
        if x + len(name) + 2 < w:
            scr.addstr(0, x, " %s " % name, attr)
        x += len(name) + 2
    rows = st.rows()
    st.sel = max(0, min(st.sel, len(rows) - 1))
    body = h - 3
    if st.sel < st.top:
        st.top = st.sel
    if st.sel >= st.top + body:
        st.top = st.sel - body + 1
    idw = min(max([len(r["id"]) for r in rows] + [10]), w // 2)
    for i, r in enumerate(rows[st.top:st.top + body]):
        y = i + 1
        v = r["value"]
        if isinstance(v, float):
            v = ("%.3f" % v).rstrip("0").rstrip(".")
        v = str(v).split("\n")[0]
        line = "%-*s %s %s%s  %s" % (idw, r["id"][:idw], "w" if r.get("writable") else " ", v,
                                     (" " + r["unit"]) if r.get("unit") else "", r.get("label") or "")
        attr = curses.A_REVERSE if st.top + i == st.sel else curses.A_NORMAL
        scr.addstr(y, 0, line[:w - 1], attr)
    status = "[%s] %s" % (("filter: " + st.filter) if st.filter else time.strftime("%H:%M:%S"), st.msg)
    scr.addstr(h - 1, 0, status[:w - 1], curses.A_BOLD)
    scr.refresh()


def edit(scr, st):
    rows = st.rows()
    if not rows:
        return
    r = rows[st.sel]
    if not r.get("writable") or not r.get("path"):
        st.msg = "%s is read-only" % r["id"]
        return
    v = prompt(scr, "new value for %s (now %s): " % (r["id"], r["value"]))
    if not v:
        return
    try:
        if r["path"].startswith("alsa:"):
            _, card, numid = r["path"].split(":")
            control.apply_alsa(card, numid, r["attr"], v, st.m, st.args.allow_unknown)
        else:
            control.apply_sysfs(r["path"], v, st.m, st.args.allow_unknown)
        st.msg = "set %s = %s (r restores)" % (r["id"], v)
    except (control.Refused, OSError) as e:
        st.msg = "refused: %s" % e
    st.slow_at = 0


def run(args, m):
    st = State(args, m)
    keep = {"v": False}

    def main(scr):
        curses.curs_set(0)
        scr.timeout(1000)
        last = 0
        while True:
            if time.monotonic() - last >= 1:
                st.refresh()
                last = time.monotonic()
            draw(scr, st)
            k = scr.getch()
            if k == -1:
                continue
            if k in (ord("q"),):
                return
            if k == ord("K"):
                keep["v"] = True
                return
            if k in (curses.KEY_RIGHT, 9):
                st.page = (st.page + 1) % len(PAGES)
                st.sel = st.top = 0
            elif k in (curses.KEY_LEFT, curses.KEY_BTAB):
                st.page = (st.page - 1) % len(PAGES)
                st.sel = st.top = 0
            elif k == curses.KEY_DOWN:
                st.sel += 1
            elif k == curses.KEY_UP:
                st.sel -= 1
            elif k == curses.KEY_NPAGE:
                st.sel += scr.getmaxyx()[0] - 3
            elif k == curses.KEY_PPAGE:
                st.sel -= scr.getmaxyx()[0] - 3
            elif k == ord("/"):
                st.filter = prompt(scr, "filter: ")
                st.sel = st.top = 0
            elif k == ord("e"):
                edit(scr, st)
            elif k == ord("f"):
                v = prompt(scr, "fan N RPM (0 = auto): ").split()
                try:
                    fans.set_fan(m, int(v[0]), int(v[1]), lease=15, allow_unknown=args.allow_unknown)
                    st.msg = "fan %s -> %s rpm under the guard" % (v[0], v[1])
                except (control.Refused, ValueError, IndexError, OSError) as e:
                    st.msg = "refused: %s" % e
            elif k == ord("a"):
                fans.auto(reason="tui")
                st.msg = "fans handed back to the SMC"
            elif k == ord("h"):
                v = prompt(scr, "click strength PRESS RELEASE (hex or dec): ").split()
                try:
                    haptics.set_strength(m, int(v[0], 0), int(v[1], 0), args.allow_unknown)
                    st.msg = "click strength set (r restores)"
                except (control.Refused, ValueError, IndexError, OSError) as e:
                    st.msg = "refused: %s" % e
            elif k == ord("r"):
                rs, fl = control.restore(session=log.session(), reason="tui restore")
                st.msg = "restored %d, failed %d" % (len(rs), len(fl))

    log.event("tui_start")
    try:
        curses.wrapper(main)
    finally:
        fans.auto(reason="tui exit")
        if not keep["v"]:
            control.restore(session=log.session(), reason="tui exit")
        log.event("tui_exit", kept=keep["v"])
