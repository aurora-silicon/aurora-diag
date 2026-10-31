# SPDX-License-Identifier: MIT
"""The dashboard (Textual). Tabs: Overview, Sensors, Clocks, Fans, GPU,
Subsystems, Services, Log.

Rendering only: the numbers come from dashboard.py, live or from a
capture. Changes go through the same policy, journal and log as the CLI
(control.apply_sysfs); on exit this session's changes are put back unless
you quit with K. Colours come from the Textual theme, so the screen reads
on light and dark terminals (d switches theme).
"""

import collections
import json
import time

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.coordinate import Coordinate
from textual.widgets import (DataTable, Footer, Header, Input, Label, RichLog, Sparkline, Static,
                             TabbedContent, TabPane)

from . import control, dashboard, fans, log, subsystems

LEVEL_STYLE = {"ok": "bold green", "pass": "bold green", "warn": "bold dark_orange", "fail": "bold red",
               "off": "dim", "skip": "dim", "active": "bold green", "failed": "bold red"}
CHIP_CLASS = {"ok": "ok", "pass": "ok", "warn": "warn", "fail": "fail", "off": "off", "skip": "off"}


def chip(level, text=None):
    lvl = (level or "off").lower()
    return Text(" %s " % (text or lvl.upper()), style=LEVEL_STYLE.get(lvl, "dim") + " reverse")


CTRL = __import__("re").compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def fmt(v, unit=None):
    if isinstance(v, float):
        v = ("%.2f" % v).rstrip("0").rstrip(".")
    s = CTRL.sub("", v if isinstance(v, str) else json.dumps(v, default=str))
    if len(s) > 70:
        s = s[:67] + "..."
    return s + ((" " + unit) if unit else "")


class Card(Vertical):
    """A titled metric with an optional sparkline."""

    def __init__(self, title, key=None, spark=False, **kw):
        super().__init__(**kw)
        self.title, self.key, self.spark = title, key, spark

    def compose(self) -> ComposeResult:
        yield Label(self.title, classes="card-title")
        yield Static("-", classes="card-value", id="v-" + self.id)
        if self.spark:
            yield Sparkline([], id="s-" + self.id, summary_function=max)

    def set(self, value, history=None, level=None):
        v = self.query_one(".card-value", Static)
        v.update(value)
        for c in ("ok", "warn", "fail", "off"):
            self.remove_class(c)
        if level:
            self.add_class(CHIP_CLASS.get(level, "off"))
        if self.spark and history:
            self.query_one(Sparkline).data = list(history)


class EditScreen(ModalScreen):
    def __init__(self, row):
        super().__init__()
        self.row = row

    def compose(self) -> ComposeResult:
        with Vertical(id="edit"):
            yield Label("Set %s" % self.row["id"])
            yield Label("now %s  (journalled; r restores, q quits and restores)" % fmt(self.row["value"]), classes="dim")
            yield Input(placeholder="new value", id="edit-input")

    def on_input_submitted(self, ev):
        self.dismiss(ev.value.strip() or None)

    def key_escape(self):
        self.dismiss(None)


class Dash(App):
    TITLE = "aurora-diag"
    CSS = """
    Screen { background: $background; }
    #overview { grid-size: 4; grid-gutter: 1 2; padding: 1 2; height: auto; }
    Card { border: round $panel-lighten-2; padding: 0 1; height: 7; }
    Card.ok { border: round $success; }
    Card.warn { border: round $warning; }
    Card.fail { border: round $error; }
    .card-title { color: $text-muted; text-style: bold; }
    .card-value { text-style: bold; height: 2; }
    Sparkline { height: 2; }
    DataTable { height: 1fr; }
    #banner { padding: 0 2; height: auto; color: $text-muted; }
    #edit { width: 70; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    .dim { color: $text-muted; }
    Input#filter { margin: 0 1; }
    """
    BINDINGS = [
        Binding("q", "quit_restore", "Quit"),
        Binding("K", "quit_keep", "Quit, keep changes"),
        Binding("r", "restore", "Restore"),
        Binding("e", "edit", "Edit value"),
        Binding("a", "fans_auto", "Fans auto"),
        Binding("d", "toggle_theme", "Light/dark"),
        Binding("/", "focus_filter", "Filter"),
    ]

    def __init__(self, machine=None, capture=None, args=None):
        super().__init__()
        self.m, self.capture, self.args = machine, capture, args
        self.data = None
        self.hist = collections.defaultdict(lambda: collections.deque(maxlen=120))
        self.filter = ""

    # -- layout ----------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="banner")
        with TabbedContent(initial="tab-overview"):
            with TabPane("Overview", id="tab-overview"):
                with VerticalScroll():
                    with Grid(id="overview"):
                        for cid, title, spark in (
                                ("health", "Health check", False), ("die", "Hottest die", True),
                                ("cpu", "CPU clocks", True), ("gpu", "GPU", True),
                                ("power", "System power", True), ("fans", "Fans", True),
                                ("lid", "Lid angle", True), ("als", "Ambient light", True),
                                ("battery", "Battery", False), ("speaker", "Speaker protection", True),
                                ("vulkan", "Vulkan", False), ("gl", "OpenGL / GLES", False)):
                            yield Card(title, id=cid, spark=spark)
            with TabPane("Sensors", id="tab-sensors"):
                yield Input(placeholder="filter (regex-free substring)", id="filter")
                yield DataTable(id="t-sensors", zebra_stripes=True, cursor_type="row")
            with TabPane("Clocks", id="tab-clocks"):
                yield DataTable(id="t-clocks", zebra_stripes=True, cursor_type="row")
            with TabPane("Fans", id="tab-fans"):
                yield Static("", id="fan-note", classes="dim")
                yield DataTable(id="t-fans", zebra_stripes=True, cursor_type="row")
            with TabPane("GPU", id="tab-gpu"):
                yield DataTable(id="t-gpu", zebra_stripes=True, cursor_type="row")
            with TabPane("Subsystems", id="tab-subsys"):
                yield DataTable(id="t-subsys", zebra_stripes=True, cursor_type="row")
            with TabPane("Services", id="tab-services"):
                yield DataTable(id="t-services", zebra_stripes=True, cursor_type="row")
            with TabPane("Log", id="tab-log"):
                yield RichLog(id="t-log", wrap=False, highlight=True, markup=False)
        yield Footer()

    def on_mount(self):
        cols = {"t-sensors": ("", "id", "value", "label"), "t-clocks": ("what", "now", "min", "max", "ladder"),
                "t-fans": ("fan", "rpm", "target", "min", "max", "control"),
                "t-gpu": ("section", "item", "value"),
                "t-subsys": ("state", "block", "node", "driver", "firmware", "devices", "why"),
                "t-services": ("state", "unit", "detail")}
        for tid, c in cols.items():
            self.query_one("#" + tid, DataTable).add_columns(*c)
        if self.capture:
            self.data = self.capture
            for h in self.capture.get("history", []):
                for k, v in h["values"].items():
                    self.hist[k].append(v)
            self.render_all()
            self.sub_title = "capture from %s (read only)" % self.capture.get("host")
        else:
            self.sub_title = "live, %s" % (self.m.model or self.m.board)
            log.event("tui_start", ui="textual")
            self.load_slow()
            self.set_interval(1.0, self.load_fast)
            self.set_interval(15.0, self.load_slow)

    # -- data ---------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="fast")
    def load_fast(self):
        f = dashboard.fast(self.m)
        f["log"] = dashboard.log_tail()
        self.call_from_thread(self.apply_fast, f)

    @work(thread=True, exclusive=True, group="slow")
    def load_slow(self):
        first = self.data is None or "slow" not in self.data
        s = dashboard.slow(self.m, api=first)
        self.call_from_thread(self.apply_slow, s)

    def apply_fast(self, f):
        if self.data is None:
            self.data = {"machine": self.m.as_dict(), "host": "", "kernel": "", "slow": {}}
        self.data.update(f)
        for r in f["readings"]:
            if isinstance(r["value"], (int, float)) and not isinstance(r["value"], bool):
                self.hist[r["id"]].append(r["value"])
        self.render_all()

    def apply_slow(self, s):
        if self.data is None:
            self.data = {"machine": self.m.as_dict(), "readings": [], "fans": [], "hottest": [None, None]}
        old = self.data.get("slow") or {}
        for k in ("vulkan", "gl", "compliance"):
            if k not in s and k in old:
                s[k] = old[k]
        self.data["slow"] = s
        self.render_all()

    # -- rendering -------------------------------------------------------------------

    def readings(self):
        return (self.data or {}).get("readings") or []

    def render_all(self):
        if not self.data:
            return
        md = self.data.get("machine") or {}
        fw = md.get("firmware") or {}
        self.query_one("#banner", Static).update(
            "%s  ·  %s  ·  kernel %s  ·  stage2 %s  ·  system firmware %s" % (
                md.get("model"), md.get("soc"), self.data.get("kernel") or "", fw.get("asahi,m1n1-stage2-version", "?"),
                fw.get("asahi,system-fw-version", "?")))
        self.render_overview()
        self.render_sensors()
        self.render_clocks()
        self.render_fans()
        self.render_gpu()
        self.render_subsys()
        self.render_services()
        self.render_log()

    def card(self, cid, value, hist_key=None, level=None):
        try:
            c = self.query_one("#" + cid, Card)
        except Exception:
            return
        c.set(value, self.hist.get(hist_key) if hist_key else None, level)

    def render_overview(self):
        rs = self.readings()
        slow = self.data.get("slow") or {}
        ck = slow.get("check") or {}
        if "counts" in ck:
            c = ck["counts"]
            self.card("health", "%s\n%d pass  %d warn  %d fail" % (ck["result"].upper(), c["pass"], c["warn"], c["fail"]),
                      level=ck["result"])
        name, t = (self.data.get("hottest") or [None, None])
        if t is not None:
            key = next((r["id"] for r in rs if r["cat"] == "thermal" and r["attr"] == "temp"
                        and ("zone:" + (r.get("label") or "")) == name), None)
            self.card("die", "%.1f °C\n%s" % (t, (name or "").replace("zone:", "")), key,
                      "fail" if t >= 95 else "warn" if t >= 85 else "ok")
        cur = [r for r in rs if r["cat"] == "cpufreq" and r["attr"] == "scaling_cur_freq"]
        if cur:
            self.card("cpu", "  ".join("%s %.0f" % (r["dev"].replace("policy", "p"), r["value"]) for r in cur if isinstance(r["value"], (int, float))) + " MHz",
                      cur[-1]["id"])
        g = [r for r in rs if r["cat"] == "gpu"]
        util = next((r for r in g if r["attr"] == "util"), None)
        mem = next((r for r in g if r["attr"] == "gpu_memory/user_resident"), None)
        pw = next((r for r in g if r["attr"] in ("cores_powered",)), None)
        parts = []
        if util:
            parts.append("busy %s%%" % util["value"])
        if mem:
            parts.append("%.0f MiB" % (mem["value"] / 2**20))
        if pw:
            parts.append("cores %s" % ("on" if pw["value"] in (True, "true", 1) else "off"))
        self.card("gpu", "  ".join(parts) or "-", util["id"] if util else (mem["id"] if mem else None))
        tot = next((r for r in rs if r.get("label") == "Total System Power" and r["attr"].endswith("_input")), None)
        if tot:
            self.card("power", "%.2f W" % tot["value"], tot["id"])
        fl = self.data.get("fans") or []
        if fl:
            self.card("fans", "  ".join("%s %s rpm" % (f["label"], f["rpm"]) for f in fl),
                      next((r["id"] for r in rs if r["cat"] == "fan" and r["attr"].endswith("_input")), None))
        else:
            self.card("fans", "fanless", level="off")
        for cid, unit in (("lid", "deg"), ("als", "lux")):
            r = next((r for r in rs if r["cat"] == "iio" and r.get("unit") == unit), None)
            if r is None:
                r = next((r for r in rs if r["cat"] == "iio" and ("angl" if cid == "lid" else "illuminance") in r["attr"]), None)
            if r is None:
                self.card(cid, "not present", level="off")
            elif isinstance(r["value"], (int, float)):
                self.card(cid, "%s %s" % (fmt(r["value"]), "°" if cid == "lid" else "lux"), r["id"], "ok")
            else:
                self.card(cid, str(r["value"]), level="warn")
        bat = {r["attr"]: r["value"] for r in rs if r["cat"] == "battery"}
        if bat:
            self.card("battery", "%s%%  %s\n%s W" % (bat.get("capacity"), bat.get("status"), fmt(bat.get("power_now"))))
        sp = {r["attr"]: r for r in rs if r["cat"] == "speaker"}
        coils = [r for r in rs if r["cat"] == "speaker" and r["attr"] in ("t_coil_C", "ff_coil_C") and isinstance(r["value"], float)]
        svc = (sp.get("service") or {}).get("value")
        hot = max(coils, key=lambda r: r["value"]) if coils else None
        self.card("speaker", "%s, armed %s\nhottest coil %s" % (
            next((r["value"] for r in rs if r["id"] == "speaker.speakerguardd.service"), "?"),
            next((r["value"] for r in rs if r["id"] == "speaker.speakerguardd.armed"), "?"),
            ("%.1f °C" % hot["value"]) if hot else "-"), hot["id"] if hot else None,
            "ok" if next((r["value"] for r in rs if r["id"] == "speaker.speakerguardd.service"), "") == "active" else "warn")
        vk = slow.get("vulkan") or {}
        dev = next((d for d in vk.get("devices", []) if "CPU" not in (d.get("deviceType") or "")), None)
        if dev:
            cp = (slow.get("compliance") or {}).get("vulkan:%s" % dev.get("deviceName")) or {}
            self.card("vulkan", "%s %s, Vulkan %s\n%d ext, conformance %s%s" % (
                dev.get("driverName"), dev.get("driverVersion"), dev.get("apiVersion"), dev.get("extension_count", 0),
                dev.get("conformanceVersion"), (", %d required missing" % len(cp["missing"])) if cp.get("missing") else ""),
                level="warn" if cp.get("missing") else "ok")
        elif "error" in vk:
            self.card("vulkan", vk["error"][:60], level="fail")
        gl = slow.get("gl") or {}
        if gl.get("gl_core") or gl.get("gles"):
            c, e = gl.get("gl_core") or {}, gl.get("gles") or {}
            self.card("gl", "GL %s · ES %s\n%s" % ((c.get("version") or "-").split(" ")[0],
                                                  (e.get("version") or "-").replace("OpenGL ES ", "").split(" ")[0],
                                                  c.get("path") or e.get("path")),
                      level="ok" if (c.get("path") or e.get("path")) == "native" else "warn")

    def _fill(self, tid, rows):
        """Refresh a table without moving the view.

        Same shape: update only the cells that changed, so scroll position and cursor stay put.
        New shape: rebuild, then restore the scroll offset and the cursor without scrolling to it.
        """
        rows = [tuple(CTRL.sub("", c) if isinstance(c, str) else c for c in r) for r in rows]
        t = self.query_one("#" + tid, DataTable)
        if rows and t.row_count == len(rows) and len(t.columns) == len(rows[0]):
            for y, r in enumerate(rows):
                for x, v in enumerate(r):
                    c = Coordinate(y, x)
                    if t.get_cell_at(c) != v:
                        t.update_cell_at(c, v)
            return
        sx, sy, row = t.scroll_x, t.scroll_y, t.cursor_row
        t.clear()
        for r in rows:
            t.add_row(*r)
        if rows:
            try:
                t.move_cursor(row=min(row, len(rows) - 1), scroll=False)
            except TypeError:   # older Textual without the scroll argument
                t.move_cursor(row=min(row, len(rows) - 1))
        t.scroll_to(sx, sy, animate=False)

    def sensor_rows(self):
        cats = ("temp", "power", "iio", "thermal", "cooling", "battery", "fan", "speaker", "gpu", "backlight", "leds")
        out = []
        for r in self.readings():
            if r["cat"] not in cats:
                continue
            if self.filter and self.filter.lower() not in ("%s %s" % (r["id"], r.get("label") or "")).lower():
                continue
            out.append(r)
        return out

    def render_sensors(self):
        self._rows_sensors = self.sensor_rows()
        rows = []
        for r in self._rows_sensors:
            w = Text(" w ", style="bold reverse cyan") if r.get("writable") else Text("   ")
            rows.append((w, r["id"], fmt(r["value"], r.get("unit")), r.get("label") or ""))
        self._fill("t-sensors", rows)

    def render_clocks(self):
        c = (self.data.get("slow") or {}).get("clocks") or {}
        rows = []
        live = {r["dev"]: r["value"] for r in self.readings() if r["cat"] == "cpufreq" and r["attr"] == "scaling_cur_freq"}
        for p in c.get("cpu", []):
            ladder = p.get("ladder_MHz") or []
            now = live.get(p["policy"], p.get("cur_MHz"))
            bar = "".join("█" if (now or 0) >= f else "░" for f in ladder)
            rows.append(("CPU %s (cpus %s, %s)" % (p["policy"], p.get("cpus"), p.get("governor")),
                         fmt(now, "MHz"), fmt(p.get("min_MHz")), fmt(p.get("max_MHz")), "%s %d steps" % (bar, len(ladder))))
        for name, t in sorted((c.get("dt_ladders") or {}).items()):
            mhz = [o["MHz"] for o in t["opps"] if o.get("MHz") is not None]
            rows.append(("DT %s" % name, "", fmt(min(mhz)) if mhz else "", fmt(max(mhz)) if mhz else "",
                         "%s (used by %s)" % (", ".join("%.0f" % x for x in mhz), ", ".join(t["used_by"]) or "-")))
        for d in c.get("devfreq", []):
            rows.append(("devfreq %s" % d["device"], fmt(d["cur_MHz"], "MHz"), fmt(d["min_MHz"]), fmt(d["max_MHz"]), ""))
        m = c.get("memory") or {}
        if m:
            rows.append(("Memory", "%.1f GiB" % ((m.get("size_bytes") or 0) / 2**30), "", "",
                         m["speed"] if isinstance(m.get("speed"), str) else json.dumps(m.get("speed"))))
        self._fill("t-clocks", rows)

    def render_fans(self):
        fl = self.data.get("fans") or []
        self.query_one("#fan-note", Static).update(
            "Fan speeds change with `aurora-diag fan set N RPM` (guarded, journalled, auto-restored) once the "
            "kernel allows it (macsmc_hwmon fan_control). Writes from here: select a fan target on Sensors and press e."
            if fl else "No fans: this machine is fanless.")
        self._fill("t-fans", [(f["label"], fmt(f["rpm"], "rpm"), fmt(f["target"]), fmt(f["min"]), fmt(f["max"]),
                               chip("ok", "writable") if f["target_writable"] else chip("off", "read only")) for f in fl])

    def render_gpu(self):
        slow = self.data.get("slow") or {}
        rows = []
        for d in slow.get("gpu_devices") or [] if isinstance(slow.get("gpu_devices"), list) else []:
            kd = d.get("kernel_driver") or {}
            rows.append(("kernel", d.get("name"), "%s %s · %s" % (kd.get("name", d.get("driver")), kd.get("version", ""), " ".join(d["nodes"]))))
        for r in self.readings():
            if r["cat"] == "gpu" and r["attr"] not in ("driver", "nodes"):
                v, u = r["value"], r.get("unit")
                if r["attr"].startswith("gpu_memory/") and isinstance(v, int) and v > 1 << 20:
                    v, u = round(v / 2**20, 1), "MiB"
                rows.append(("live", r["id"].split(".", 2)[-1], fmt(v, u)))
        vk = slow.get("vulkan") or {}
        for d in vk.get("devices", []):
            if "CPU" in (d.get("deviceType") or ""):
                continue
            for k in ("deviceName", "driverName", "driverID", "driverInfo", "apiVersion", "conformanceVersion",
                      "extension_count", "features_enabled", "features_total", "format_count"):
                rows.append(("vulkan", k, fmt(d.get(k))))
            for h in d.get("memory_heaps", []):
                rows.append(("vulkan", "heap%d" % h["index"], "%.2f GiB, budget %.2f GiB" % (h["size"] / 2**30, (h.get("budget") or 0) / 2**30)))
        gl = slow.get("gl") or {}
        for api in ("gl_core", "gl_compat", "gles"):
            i = gl.get(api)
            if i:
                rows.append((api, "renderer", "%s (%s)" % (i["renderer"], i["path"])))
                rows.append((api, "version", "%s · GLSL %s · %s" % (i["version"], i["glsl_version"], i["profile"])))
                rows.append((api, "extensions", str(i["extension_count"])))
        for k, v in (slow.get("compliance") or {}).items():
            if isinstance(v, dict):
                lvl = "warn" if v.get("missing") else "ok"
                rows.append(("compliance", k, Text.assemble(chip(lvl), " %d checked, missing: %s" % (
                    v.get("required_checked", v.get("checked", 0)), ", ".join(v.get("missing") or []) or "none"))))
        for p in slow.get("gpu_clients") or [] if isinstance(slow.get("gpu_clients"), list) else []:
            rows.append(("client", "%s[%d]" % (p["comm"], p["pid"]), "%.1f MiB resident, %d clients" % (p["resident_KiB"] / 1024, p["clients"])))
        self._fill("t-gpu", rows)

    def render_subsys(self):
        subs = (self.data.get("slow") or {}).get("subsystems")
        if not isinstance(subs, list):
            return
        order = {"fail": 0, "warn": 1, "ok": 2, "off": 3}
        rows = []
        for s in sorted((s for s in subs if s["kind"] == "block"), key=lambda s: (order[s["state"]["level"]], s.get("title") or "")):
            rows.append((chip(s["state"]["level"]), s.get("title") or s["block"], s["name"], s.get("driver") or "-",
                         subsystems.firmware_short(s), ", ".join(s.get("class_devices") or [])[:50],
                         s["state"]["why"][:90]))
        self._fill("t-subsys", rows)

    def render_services(self):
        sv = (self.data.get("slow") or {}).get("services")
        if not isinstance(sv, dict):
            return
        rows = []
        for u in sv.get("system", []):
            lvl = {"active": "ok", "failed": "fail"}.get(u["active"], "off")
            rows.append((chip(lvl, u["active"]), u["unit"], "%s · %s%s" % (u["sub"], u["enabled"], (" · %d restarts" % u["restarts"]) if u.get("restarts") else "")))
        for u in sv.get("user", []):
            rows.append((chip("ok" if u["active"] == "active" else "warn", u["active"]), "%s (user %s)" % (u["unit"], u["user"]), ""))
        t = sv.get("touchid") or {}
        fu = t.get("fprint_users")
        rows.append((chip("ok" if fu else "warn", "touch id"), "enrolled prints", json.dumps(fu)))
        rows.append((chip("ok" if t.get("sep_state_files") else "warn", "sep"), "SEP host state files", str(len(t.get("sep_state_files") or []))))
        rows.append((chip("ok" if t.get("pam_fprintd_in") else "off", "pam"), "pam_fprintd in", ", ".join(t.get("pam_fprintd_in") or []) or "none"))
        sg = sv.get("speakerguardd") or {}
        rows.append((chip("ok" if sg.get("service") == "active" else "warn", "speakers"), "speakerguardd",
                     "armed %s · governor %s · hottest coil %s °C" % (sg.get("armed"), sg.get("governor"), fmt(sg.get("hottest_coil_C")))))
        self._fill("t-services", rows)

    def render_log(self):
        lg = self.query_one("#t-log", RichLog)
        # Follow new lines only while the reader is already at the bottom.
        lg.auto_scroll = getattr(lg, "is_vertical_scroll_end", True) or lg.max_scroll_y == 0
        lines = self.data.get("log") or []
        n = getattr(self, "_log_n", 0)
        if len(lines) < n:
            n = 0
        for line in lines[n:]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("event") in ("reading", "sample"):
                continue
            rest = {k: v for k, v in rec.items() if k not in ("ts", "mono", "host", "pid", "session", "event")}
            lg.write("%s %-10s %s" % (rec.get("ts", "")[11:19], rec.get("event"), json.dumps(rest, default=str)[:200]))
        self._log_n = len(lines)

    # -- actions ------------------------------------------------------------------------

    def on_input_changed(self, ev):
        if ev.input.id == "filter":
            self.filter = ev.value
            self.render_sensors()

    def action_focus_filter(self):
        self.query_one(TabbedContent).active = "tab-sensors"
        self.query_one("#filter", Input).focus()

    def action_toggle_theme(self):
        self.theme = "textual-light" if self.theme != "textual-light" else "textual-dark"

    def action_edit(self):
        if self.capture:
            self.notify("this is a capture: nothing to change", severity="warning")
            return
        if self.query_one(TabbedContent).active != "tab-sensors":
            self.notify("edit works on the Sensors tab", severity="information")
            return
        t = self.query_one("#t-sensors", DataTable)
        rows = getattr(self, "_rows_sensors", [])
        if not rows or t.cursor_row >= len(rows):
            return
        r = rows[t.cursor_row]
        if not r.get("writable"):
            self.notify("%s is read only" % r["id"], severity="warning")
            return

        def done(v):
            if v is None:
                return
            try:
                control.apply_sysfs(r["path"], v, self.m, getattr(self.args, "allow_unknown", False))
                self.notify("set %s = %s (r restores)" % (r["id"], v))
            except (control.Refused, OSError) as e:
                self.notify("refused: %s" % e, severity="error")

        self.push_screen(EditScreen(r), done)

    def action_restore(self):
        if self.capture:
            return
        rs, fl = control.restore(session=log.session(), reason="tui restore")
        self.notify("restored %d, failed %d" % (len(rs), len(fl)))

    def action_fans_auto(self):
        if self.capture:
            return
        fans.auto(reason="tui")
        self.notify("fans handed back to the SMC")

    def _finish(self, keep):
        if not self.capture:
            fans.auto(reason="tui exit")
            if not keep:
                control.restore(session=log.session(), reason="tui exit")
            log.event("tui_exit", kept=keep, ui="textual")
        self.exit()

    def action_quit_restore(self):
        self._finish(False)

    def action_quit_keep(self):
        self._finish(True)


TABS = ["overview", "sensors", "clocks", "fans", "gpu", "subsys", "services", "log"]


def _svg_to_png(svg, png):
    """Convert with whatever this system has: rsvg-convert, cairosvg, or
    Quick Look plus sips on macOS (then crop the square padding)."""
    import os
    import re
    import shutil
    import subprocess
    import tempfile
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-w", "2000", "-o", png, svg], check=True)
        return
    try:
        import cairosvg
        cairosvg.svg2png(url=svg, write_to=png, output_width=2000)
        return
    except ImportError:
        pass
    if shutil.which("qlmanage") and shutil.which("sips"):
        with open(svg) as f:
            m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', f.read(2000))
        w, h = (float(m.group(1)), float(m.group(2))) if m else (1970.0, 1172.4)
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["qlmanage", "-t", "-s", "2000", "-o", tmp, svg], check=True, capture_output=True)
            shutil.move(os.path.join(tmp, os.path.basename(svg) + ".png"), png)
        subprocess.run(["sips", "-c", str(round(2000 * h / w)), "2000", png], check=True, capture_output=True)
        return
    raise RuntimeError("no SVG converter: install rsvg-convert (librsvg) or python cairosvg")


def screenshots(capture, outdir, prefix, tabs=None, theme="textual-dark", png=False, size=(160, 46)):
    """Render tabs of a capture headlessly to SVG (Textual's own
    screenshot) and optionally PNG. Returns the files written."""
    import asyncio
    import os
    os.makedirs(outdir, exist_ok=True)
    written = []

    async def go():
        app = Dash(None, capture, None)
        async with app.run_test(size=size) as pilot:
            app.theme = theme
            await pilot.pause(0.5)
            for t in tabs or TABS:
                app.query_one(TabbedContent).active = "tab-" + t
                await pilot.pause(0.4)
                name = "%s-%s.svg" % (prefix, t)
                app.save_screenshot(name, outdir)
                written.append(os.path.join(outdir, name))

    asyncio.run(go())
    if png:
        for svg in list(written):
            out = svg[:-4] + ".png"
            _svg_to_png(svg, out)
            written.append(out)
    return written


def run(args, m):
    cap = dashboard.load(args.snapshot) if getattr(args, "snapshot", None) else None
    Dash(m, cap, args).run()
