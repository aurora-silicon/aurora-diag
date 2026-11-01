# SPDX-License-Identifier: MIT
"""The dashboard (Textual).

Overview: one tile per category (Health, Thermal, CPU, GPU, Memory,
Display, Radio, Storage, Power, Audio, Engines, Sensors, System, and the
coverage table). Click a tile, or move to it and press Enter, for its
drill-down: the category's items (present, or absent and why) and every
table behind it, live, with the tile's history on top. The same
drill-downs print with `aurora-diag show NAME [--json]`.

Tabs: Overview, Sensors, Clocks, Fans, GPU, ANE, vs macOS, Subsystems,
Services, Coverage, Log.

Rendering only: the numbers come from dashboard.py, live or from a
capture. Changes go through the same policy, journal and log as the CLI
(control.apply_sysfs); on exit this session's changes are put back unless
you quit with K. Colours come from the Textual theme, so the screen reads
on light and dark terminals (d switches theme).
"""

import collections
import json

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.widgets import (DataTable, Footer, Header, Input, Label, RichLog, Sparkline, Static,
                             TabbedContent, TabPane)

from . import ane, control, dashboard, fans, log, sections, subsystems

LEVEL_STYLE = {"ok": "bold green", "pass": "bold green", "warn": "bold dark_orange", "fail": "bold red",
               "off": "dim", "skip": "dim", "absent": "dim", "active": "bold green", "failed": "bold red"}
CHIP_CLASS = {"ok": "ok", "pass": "ok", "warn": "warn", "fail": "fail", "off": "off", "skip": "off", "absent": "off"}
CTRL = __import__("re").compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_TABLES = 40


def chip(level, text=None):
    lvl = (level or "off").lower()
    return Text(" %s " % (text or lvl.upper()), style=LEVEL_STYLE.get(lvl, "dim") + " reverse")


def fmt(v, unit=None, width=70, precision=None):
    if isinstance(v, float):
        v = ("%.*f" % (precision, v)).rstrip("0").rstrip(".") if precision is not None else "%.6g" % v
    s = CTRL.sub("", v if isinstance(v, str) else json.dumps(v, default=str))
    if len(s) > width:
        s = s[:width - 3] + "..."
    return s + ((" " + unit) if unit else "")


def fill(t, rows):
    """Refresh a table without moving the view.

    Same shape: update only the cells that changed, so scroll position and
    cursor stay put. New shape: add or remove trailing rows in place.
    Clearing the table resets Textual's cursor and can schedule a later
    scroll-to-cursor.
    """
    rows = [tuple(CTRL.sub("", c) if isinstance(c, str) else c for c in r) for r in rows]
    if rows and t.row_count == len(rows) and len(t.columns) == len(rows[0]):
        for y, r in enumerate(rows):
            for x, v in enumerate(r):
                c = Coordinate(y, x)
                if t.get_cell_at(c) != v:
                    t.update_cell_at(c, v)
        return
    sx, sy, row = t.scroll_x, t.scroll_y, t.cursor_row
    if rows and len(t.columns) != len(rows[0]):
        t.clear()
    keys = list(t.rows)
    for key in reversed(keys[len(rows):]):
        t.remove_row(key)
    for y, r in enumerate(rows):
        if y >= len(keys):
            t.add_row(*r)
        else:
            for x, v in enumerate(r):
                c = Coordinate(y, x)
                if t.get_cell_at(c) != v:
                    t.update_cell_at(c, v)
    if rows:
        try:
            t.move_cursor(row=min(row, len(rows) - 1), scroll=False)
        except TypeError:   # older Textual without the scroll argument
            t.move_cursor(row=min(row, len(rows) - 1))
    t.scroll_to(sx, sy, animate=False)


def set_columns(t, cols):
    cols = [str(c) for c in cols]
    have = [str(c.label) for c in t.columns.values()]
    if have != cols:
        t.clear(columns=True)
        t.add_columns(*cols)


class Tile(Vertical, can_focus=True):
    """A category on the overview: click or Enter opens its drill-down."""

    BINDINGS = [Binding("enter", "open", "Open", show=False)]

    def __init__(self, name, title, **kw):
        super().__init__(id="tile-" + name, **kw)
        self.cat, self.title_text = name, title

    def compose(self) -> ComposeResult:
        yield Label(self.title_text, classes="card-title")
        yield Static("-", classes="card-value")
        yield Sparkline([], summary_function=max)

    def set(self, value, history=None, level=None):
        self.query_one(".card-value", Static).update(value)
        for c in ("ok", "warn", "fail", "off"):
            self.remove_class(c)
        if level:
            self.add_class(CHIP_CLASS.get(level, "off"))
        sp = self.query_one(Sparkline)
        if history:
            sp.data = list(history)
            sp.display = True
        else:
            sp.display = False

    def on_click(self):
        self.action_open()

    def action_open(self):
        self.app.open_detail(self.cat)


class Detail(Screen):
    """One category's drill-down, refreshed live from the app's data."""

    BINDINGS = [Binding("escape", "back", "Back"), Binding("q", "back", "Back"),
                Binding("/", "focus_filter", "Filter rows")]

    def __init__(self, cat):
        super().__init__()
        self.cat = cat
        self.filter = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="d-head")
        yield Sparkline([], id="d-spark", summary_function=max)
        yield Input(placeholder="filter rows (substring)", id="d-filter")
        with VerticalScroll(id="d-body"):
            yield Label("Status", classes="t-title")
            yield DataTable(id="d-items", zebra_stripes=True, cursor_type="row", classes="auto")
            for i in range(MAX_TABLES):
                yield Label("", id="d-tt%d" % i, classes="t-title")
                yield DataTable(id="d-t%d" % i, zebra_stripes=True, cursor_type="row", classes="auto")
        yield Footer()

    def on_mount(self):
        self._prev_sub = self.app.sub_title
        self.query_one("#d-filter", Input).display = False
        set_columns(self.query_one("#d-items", DataTable), ("", "item", "detail"))
        self.update()

    def on_input_changed(self, ev):
        self.filter = ev.value.lower()
        self.update()

    def action_focus_filter(self):
        f = self.query_one("#d-filter", Input)
        f.display = True
        f.focus()

    def action_back(self):
        self.app.sub_title = getattr(self, "_prev_sub", self.app.sub_title)
        self.app.pop_screen()

    def _keep(self, row):
        return not self.filter or self.filter in " ".join(str(c) for c in row).lower()

    def update(self):
        s = self.app.section(self.cat)
        if not s:
            self.query_one("#d-head", Static).update("%s: collecting ..." % self.cat)
            return
        self.app.sub_title = s["title"]
        self.query_one("#d-head", Static).update(Text.assemble(
            chip(s["level"]), "  ", (s["title"], "bold"), "   ", s["headline"].replace("\n", "   ·   "),
            ("   (%s)" % s["note"]) if s.get("note") else ""))
        sp = self.query_one("#d-spark", Sparkline)
        h = self.app.hist.get("section." + self.cat)
        sp.display = bool(h)
        if h:
            sp.data = list(h)
        fill(self.query_one("#d-items", DataTable),
             [(chip(i["level"]), i["key"], fmt(i["detail"], width=150)) for i in s["items"]
              if self._keep((i["level"], i["key"], i["detail"]))])
        tabs = s.get("tables") or []
        for i in range(MAX_TABLES):
            lab = self.query_one("#d-tt%d" % i, Label)
            t = self.query_one("#d-t%d" % i, DataTable)
            if i >= len(tabs):
                lab.display = t.display = False
                continue
            tb = tabs[i]
            lab.display = t.display = True
            lab.update(tb["title"])
            set_columns(t, tb["columns"])
            rows = []
            for r in tb["rows"]:
                if not self._keep(r):
                    continue
                cells = []
                for k, c in enumerate(r[:len(tb["columns"])]):
                    if k == 0 and isinstance(c, str) and c in ("pass", "warn", "fail", "skip", "ok", "off", "absent"):
                        cells.append(chip(c))
                    else:
                        cells.append(fmt(c if c is not None else "", width=110))
                cells += [""] * (len(tb["columns"]) - len(cells))
                rows.append(tuple(cells))
            fill(t, rows)


KIND_LABEL = {"live": "live view", "bench": "quick run", "launch": "app"}
ART_STYLE = {"●": "bold green", "(": "green", ")": "green", "◆": "bold red", "•": "red", "◉": "bold",
             "N": "bold", "E": "bold", "S": "bold", "W": "bold", "█": "cyan", "░": "dim cyan", "▀": "cyan"}


def art_text(lines):
    """The demo's text drawing with a little colour (bubble, needle, laptop)."""
    t = Text()
    for i, ln in enumerate(lines):
        if i:
            t.append("\n")
        if ln.lstrip().startswith(("x ", "y ", "z ")) and "│" in ln:
            t.append(ln.replace("█", "▮"), style="magenta")
            continue
        if 1 <= i <= 11:            # the three drawings
            for ch in ln:
                t.append(ch, style=ART_STYLE.get(ch, ""))
        else:
            t.append(ln, style="bold" if i == 0 else "")
    return t


class DemosHome(Screen):
    """Every demo this machine can run, and why not where it can't."""

    BINDINGS = [Binding("escape", "back", "Back"), Binding("q", "back", "Back"), Binding("enter", "open", "Open demo")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="dh-head")
        yield DataTable(id="dh-list", zebra_stripes=True, cursor_type="row")
        yield Static("", id="dh-what")
        yield Footer()

    def on_mount(self):
        self._prev_sub = self.app.sub_title
        self.app.sub_title = "Demos"
        t = self.query_one("#dh-list", DataTable)
        t.add_columns("", "demo", "kind", "engine", "on this machine")
        self.update()
        t.focus()

    def demos(self):
        return self.app.demo_list()

    def update(self):
        ds = self.demos()
        n = sum(1 for d in ds if d["level"] == "ok")
        self.query_one("#dh-head", Static).update(Text.assemble(
            ("Demos", "bold"), "   %d of %d ready here. Enter or click opens one; nothing starts until you press s inside. "
            "No audio plays; camera and microphone demos say so and run only when you start them." % (n, len(ds))))
        fill(self.query_one("#dh-list", DataTable),
             [(chip(d["level"], {"ok": "READY", "warn": "WARN", "absent": "ABSENT"}.get(d["level"])), d["title"],
               KIND_LABEL.get(d["kind"], d["kind"]) + (" · " + ", ".join(d["uses"]) if d["uses"] else ""), d["engine"],
               fmt(d["detail"], width=90)) for d in ds])
        self.show_what()

    def on_data_table_row_highlighted(self, ev):
        self.show_what()

    def show_what(self):
        ds = self.demos()
        t = self.query_one("#dh-list", DataTable)
        if ds and 0 <= t.cursor_row < len(ds):
            d = ds[t.cursor_row]
            self.query_one("#dh-what", Static).update(Text.assemble(("What you'll see: ", "bold"), d["what"]))

    def on_data_table_row_selected(self, ev):
        self.action_open()

    def action_open(self):
        ds = self.demos()
        t = self.query_one("#dh-list", DataTable)
        if ds and 0 <= t.cursor_row < len(ds):
            self.app.push_screen(DemoScreen(ds[t.cursor_row]["name"]))

    def action_back(self):
        self.app.sub_title = self._prev_sub
        self.app.pop_screen()


class DemoScreen(Screen):
    """One demo: what you will see, start (s), stop (x), its live view or output."""

    BINDINGS = [Binding("s", "start", "Start"), Binding("x", "stop", "Stop"), Binding("z", "zero", "Re-zero"),
                Binding("t", "toggle", "Toggle setting"),
                Binding("escape", "back", "Back"), Binding("q", "back", "Back")]

    def __init__(self, name):
        super().__init__()
        self.dname = name
        self.proc = None
        self.running = False
        self.state = None
        self.samples = None

    def info(self):
        return next((d for d in self.app.demo_list() if d["name"] == self.dname), None)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="dm-head")
        yield Static("", id="dm-what")
        yield Static("", id="dm-result")
        yield Static("", id="dm-art")
        yield DataTable(id="dm-table", zebra_stripes=True, cursor_type="row")
        yield RichLog(id="dm-log", wrap=True, highlight=False, markup=False)
        yield Footer()

    def on_mount(self):
        from . import demos
        self._prev_sub = self.app.sub_title
        d = self.info() or {}
        self.app.sub_title = "Demo: " + d.get("title", self.dname)
        self.query_one("#dm-table", DataTable).display = d.get("name") == "speakers"
        self.query_one("#dm-art", Static).display = d.get("name") in ("motion", "whitepoint")
        self.wp = demos.WhitePointState()
        self.query_one("#dm-log", RichLog).display = d.get("kind") in ("bench", "launch")
        self.state = demos.MotionState()
        self.head("ready, press s to start" if d.get("level") == "ok" else "not available here")
        self.query_one("#dm-what", Static).update(Text.assemble(("What you'll see: ", "bold"), d.get("what", ""),
                                                                "\n", ("On this machine: ", "bold"), d.get("detail", ""),
                                                                ("\nCommand: " + " ".join(d["command"])) if d.get("command") else ""))

    def head(self, status, level=None):
        d = self.info() or {}
        self.query_one("#dm-head", Static).update(Text.assemble(
            chip(level or d.get("level")), "  ", (d.get("title", self.dname), "bold"), "   ", status))

    def action_start(self):
        d = self.info()
        if not d or self.running:
            return
        if d["level"] not in ("ok", "warn"):
            self.app.notify("not available here: " + d["detail"], severity="warning")
            return
        if d["kind"] == "live":
            self.running = True
            self.head("running, live (x stops)", "ok")
            if self.app.capture:
                key = "whitepoint_samples" if d["name"] == "whitepoint" else "motion_samples"
                self.samples = list(self.app.capture.get(key) or [])
            self._timer = self.set_interval(0.1 if d["name"] == "motion" else 1.0, self.tick)
            self.tick()
            return
        if self.app.capture:
            self.app.notify("this is a capture: demos run only live", severity="warning")
            return
        self.running = True
        self.head("running (x stops)", "ok")
        self.query_one("#dm-log", RichLog).clear()
        self.run_proc()

    @work(thread=True, exclusive=True, group="demo")
    def run_proc(self):
        from . import demos
        demo = demos.get(self.dname)
        cmd = demo.command()
        if not cmd:
            self.app.call_from_thread(self.head, "no command to run", "warn")
            self.running = False
            return
        self.proc = demos.spawn(demo, cmd)
        out = []
        for line in self.proc.stdout:
            out.append(line)
            self.app.call_from_thread(self.query_one("#dm-log", RichLog).write, line.rstrip("\n"))
        rc = self.proc.wait()
        text = "".join(out)
        res, _ = demos.result_line(demo, text)
        self.running = False
        self.app.call_from_thread(self.finished, rc, res)

    def finished(self, rc, res):
        self.head("finished (exit %s)" % rc, "ok" if rc == 0 else "warn")
        self.query_one("#dm-result", Static).update(Text.assemble(("Result: ", "bold"), res or "see the output below"))

    def tick(self):
        from . import demos, sections
        if not self.running:
            return
        if self.dname == "motion":
            if self.samples is not None:
                if not self.samples:
                    return
                s = self.samples.pop(0)
                if not self.samples:
                    self.samples = [s]
            else:
                s = demos.motion_sample()
            self.state.feed(s)
            self.query_one("#dm-art", Static).update(art_text(demos.motion_art(s, self.state)))
        elif self.dname == "whitepoint":
            if self.samples is not None:
                for s in self.samples:
                    self.wp.feed(s)
                if self.samples:
                    self.show_whitepoint(self.samples[-1])
                self.samples = []
            elif not getattr(self, "_wp_busy", False):
                self._wp_busy = True
                self.sample_whitepoint()
        elif self.dname == "speakers":
            sec = self.app.section("audio") if self.app.capture else sections.audio_section()
            tb = next((t for t in (sec or {}).get("tables", []) if t["title"].startswith("Speakers")), None)
            t = self.query_one("#dm-table", DataTable)
            if tb:
                set_columns(t, tb["columns"])
                fill(t, [tuple(fmt(c) for c in r) for r in tb["rows"]])
            self.query_one("#dm-result", Static).update((sec or {}).get("headline", "").replace("\n", "   ·   "))

    @work(thread=True, exclusive=True, group="whitepoint")
    def sample_whitepoint(self):
        from . import demos
        try:
            s = demos.whitepoint_sample()
        finally:
            self._wp_busy = False
        self.app.call_from_thread(self.show_whitepoint, s, True)

    def show_whitepoint(self, s, feed=False):
        from . import demos
        if feed:
            self.wp.feed(s)
        t = Text()
        for i, segs in enumerate(demos.whitepoint_art(s, self.wp)):
            if i:
                t.append("\n")
            for text, style in segs:
                t.append(text, style=style or "")
        self.query_one("#dm-art", Static).update(t)

    def action_toggle(self):
        if self.dname != "whitepoint":
            return
        if self.app.capture:
            self.app.notify("this is a capture: nothing to change", severity="warning")
            return
        self.toggle_whitepoint()

    @work(thread=True, group="whitepoint-set")
    def toggle_whitepoint(self):
        from . import demos
        try:
            cur = demos.whitepoint_setting()
            before, after = demos.whitepoint_set(not cur)
            self.app.call_from_thread(self.app.notify, "ambient white point %s (was %s)" % ("on" if after else "off", "on" if before else "off"))
        except Exception as e:
            self.app.call_from_thread(self.app.notify, "not changed: %s" % e, severity="warning")

    def action_zero(self):
        if self.dname == "whitepoint":
            self.wp.reset()
            return
        if self.state:
            self.state.reset()
            self.app.notify("level and heading re-zeroed")

    def action_stop(self):
        from . import demos
        if not self.running and not (self.proc and self.proc.poll() is None):
            return
        self.running = False
        if getattr(self, "_timer", None):
            self._timer.stop()
        if self.proc is not None:
            demo = demos.get(self.dname)
            self.run_stop(demo)
        self.head("stopped")

    @work(thread=True, group="demo-stop")
    def run_stop(self, demo):
        from . import demos
        demos.stop(demo, self.proc)

    def action_back(self):
        self.action_stop()
        self.app.sub_title = self._prev_sub
        self.app.pop_screen()



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


TITLES = {"health": "Health check", "thermal": "Thermal", "cpu": "CPU", "gpu": "GPU", "memory": "Memory",
          "display": "Display", "radio": "Radio (Wi-Fi, Bluetooth)", "storage": "Storage", "power": "Power & battery",
          "audio": "Audio & speaker protection", "engines": "Engines (ANE, AVE, ISP, SEP, ...)",
          "sensors": "Sensors & motion", "system": "System (PCIe, USB, suspend)"}


class Dash(App):
    TITLE = "aurora-diag"
    CSS = """
    Screen { background: $background; }
    #overview { grid-size: 4; grid-gutter: 1 2; padding: 1 2; height: auto; }
    Tile { border: round $panel-lighten-2; padding: 0 1; height: 7; }
    Tile:hover { background: $boost; }
    Tile:focus { border: double $accent; }
    Tile.ok { border: round $success; }
    Tile.warn { border: round $warning; }
    Tile.fail { border: round $error; }
    Tile.ok:focus, Tile.warn:focus, Tile.fail:focus, Tile.off:focus { border: double $accent; }
    .card-title { color: $text-muted; text-style: bold; }
    .card-value { text-style: bold; height: 2; }
    Sparkline { height: 2; }
    #d-spark { height: 3; margin: 0 2; }
    DataTable { height: 1fr; }
    DataTable.auto { height: auto; max-height: 60; margin: 0 1 1 1; }
    .t-title { color: $accent; text-style: bold; margin: 0 1; }
    #d-head { padding: 0 2; height: auto; }
    #dh-head, #dm-head { padding: 0 2; height: auto; }
    #dh-list { height: 1fr; margin: 1 1; }
    #dh-what, #dm-what, #dm-result { padding: 0 2; height: auto; color: $text; }
    #dm-what { color: $text-muted; margin: 1 0; }
    #dm-art { padding: 0 1; height: auto; color: $accent; }
    #dm-table { height: auto; margin: 1 1; }
    #dm-log { height: 1fr; margin: 0 1; border: round $panel-lighten-2; }
    #banner { padding: 0 2; height: auto; color: $text-muted; }
    #hint { padding: 0 2; color: $text-muted; }
    #edit { width: 70; height: auto; padding: 1 2; border: thick $accent; background: $surface; }
    .dim { color: $text-muted; }
    Input#filter, Input#d-filter { margin: 0 1; }
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
                    yield Static("Click a tile, or Tab to it and press Enter, for the details. Esc goes back.", id="hint")
                    with Grid(id="overview"):
                        yield Tile("demos", "Demos")
                        for n in sections.ORDER:
                            yield Tile(n, TITLES.get(n, n))
                        yield Tile("coverage", "Coverage (present / absent)")
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
            with TabPane("ANE", id="tab-ane"):
                yield DataTable(id="t-ane", zebra_stripes=True, cursor_type="row")
            with TabPane("vs macOS", id="tab-compare"):
                yield Static("", id="compare-note", classes="dim")
                yield DataTable(id="t-compare", zebra_stripes=True, cursor_type="row")
            with TabPane("Subsystems", id="tab-subsys"):
                yield DataTable(id="t-subsys", zebra_stripes=True, cursor_type="row")
            with TabPane("Services", id="tab-services"):
                yield DataTable(id="t-services", zebra_stripes=True, cursor_type="row")
            with TabPane("Coverage", id="tab-coverage"):
                yield DataTable(id="t-coverage", zebra_stripes=True, cursor_type="row")
            with TabPane("Log", id="tab-log"):
                yield RichLog(id="t-log", wrap=False, highlight=True, markup=False)
        yield Footer()

    def on_mount(self):
        cols = {"t-sensors": ("", "id", "value", "label"), "t-clocks": ("what", "now", "min", "max", "ladder"),
                "t-fans": ("fan", "rpm", "target", "min", "max", "control"),
                "t-gpu": ("section", "item", "value"),
                "t-ane": ("device", "item", "value"),
                "t-compare": ("state", "metric", "Linux", "macOS", "delta", "sample", "context"),
                "t-subsys": ("state", "block", "node", "driver", "firmware", "devices", "why"),
                "t-services": ("state", "unit", "detail"),
                "t-coverage": ("state", "category", "item", "detail")}
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
        f = dashboard.fast(self.m, (self.data or {}).get("slow"))
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
        secs = dict(self.data.get("sections") or {})
        secs.update(f.pop("sections", {}))
        self.data.update(f)
        self.data["sections"] = secs
        for r in f["readings"]:
            if isinstance(r["value"], (int, float)) and not isinstance(r["value"], bool):
                self.hist[r["id"]].append(r["value"])
        for n, s in secs.items():
            if n in sections.LIVE and isinstance(s.get("spark"), (int, float)):
                self.hist["section." + n].append(s["spark"])
        self.render_all()

    def apply_slow(self, s):
        if self.data is None:
            self.data = {"machine": self.m.as_dict(), "readings": [], "fans": [], "hottest": [None, None]}
        old = self.data.get("slow") or {}
        for k in ("vulkan", "gl", "compliance"):
            if k not in s and k in old:
                s[k] = old[k]
        self.data["slow"] = s
        secs = dict(self.data.get("sections") or {})
        for n, v in (s.get("sections") or {}).items():
            if n not in sections.LIVE or n not in secs:
                secs[n] = v
            if n not in sections.LIVE and isinstance(v.get("spark"), (int, float)):
                self.hist["section." + n].append(v["spark"])
        self.data["sections"] = secs
        self.render_all()

    def section(self, name):
        if name == "coverage":
            return self.coverage_section()
        return ((self.data or {}).get("sections") or {}).get(name)

    def coverage_section(self):
        secs = (self.data or {}).get("sections") or {}
        rows = sections.coverage(secs)
        n = collections.Counter(r["level"] for r in rows)
        return sections.section("coverage", "Coverage", [], [sections.table(
            "Every category item: present, or absent and why", ("state", "category", "item", "detail"),
            [[r["level"], r["section"], r["item"], r["detail"]] for r in rows])],
            headline="%d present, %d absent\n%d warn, %d fail" % (n["ok"], n["absent"] + n["off"], n["warn"], n["fail"]),
            level="fail" if n["fail"] else "warn" if n["warn"] else "ok")

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
        self.render_ane()
        self.render_comparison()
        self.render_subsys()
        self.render_services()
        self.render_coverage()
        self.render_log()
        if isinstance(self.screen, Detail):
            self.screen.update()

    def demo_list(self):
        slow = (self.data or {}).get("slow") or {}
        if self.capture:
            return self.capture.get("demos") or slow.get("demos") or []
        if isinstance(slow.get("demos"), list):
            return slow["demos"]
        from . import demos
        return demos.inventory()

    def render_overview(self):
        try:
            slow = (self.data or {}).get("slow") or {}
            ds = slow.get("demos") if isinstance(slow.get("demos"), list) else (self.capture or {}).get("demos")
            t = self.query_one("#tile-demos", Tile)
            if ds is None:
                t.set("press Enter to see the demos", level="off")
            else:
                ready = [d["name"] for d in ds if d["level"] == "ok"]
                t.set("%d ready here\n%s" % (len(ready), ", ".join(ready)[:44]), level="ok" if ready else "off")
        except Exception:
            pass
        for n in list(sections.ORDER) + ["coverage"]:
            s = self.section(n)
            try:
                t = self.query_one("#tile-" + n, Tile)
            except Exception:
                continue
            if not s:
                t.set("collecting ...", level="off")
                continue
            t.set(s["headline"] or "-", self.hist.get("section." + n), s["level"])

    def open_detail(self, name):
        if name == "demos":
            self.push_screen(DemosHome())
        else:
            self.push_screen(Detail(name))

    def render_coverage(self):
        s = self.coverage_section()
        fill(self.query_one("#t-coverage", DataTable),
             [(chip(r[0]), r[1], r[2], fmt(r[3], width=140)) for r in s["tables"][0]["rows"]])

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
        fill(self.query_one("#t-sensors", DataTable), rows)

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
        fill(self.query_one("#t-clocks", DataTable), rows)

    def render_fans(self):
        fl = self.data.get("fans") or []
        self.query_one("#fan-note", Static).update(
            "Fan speeds change with `aurora-diag fan set N RPM` (guarded, journalled, auto-restored) once the "
            "kernel allows it (macsmc_hwmon fan_control). Writes from here: select a fan target on Sensors and press e."
            if fl else "No fans: this machine is fanless.")
        fill(self.query_one("#t-fans", DataTable),
             [(f["label"], fmt(f["rpm"], "rpm"), fmt(f["target"]), fmt(f["min"]), fmt(f["max"]),
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
        fill(self.query_one("#t-gpu", DataTable), rows)

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
        fill(self.query_one("#t-subsys", DataTable), rows)

    def render_ane(self):
        data = (self.data.get("slow") or {}).get("ane")
        t = self.query_one("#t-ane", DataTable)
        if not isinstance(data, dict):
            fill(t, [("ANE", "status", "not collected in this capture")])
            return
        if data.get("error"):
            fill(t, [("ANE", "probe", data["error"])])
            return
        rows = []
        for d in data.get("devices", []):
            for key in ("driver", "generation", "firmware_version", "firmware_source", "client_count", "clients_complete",
                        "performance_level", "power_state", "wedged"):
                rows.append((d["node"], key, fmt(d.get(key)) if d.get(key) is not None else "unknown / not exposed"))
            for section in ("counters", "error_counters"):
                if not d.get(section):
                    rows.append((d["node"], section, "not exposed"))
                for key, value in sorted((d.get(section) or {}).items()):
                    rows.append((d["node"], section + "." + key, fmt(value)))
            for key, value in (d.get("last_benchmark") or {}).items():
                rows.append((d["node"], "last benchmark: " + key, fmt(value, precision=4)))
        for c in ane.checks(data):
            rows.append((c.get("device", "ANE"), c["name"], Text.assemble(chip(c["level"]), " " + c["reason"])))
        fill(t, rows)

    def render_comparison(self):
        data = (self.data.get("slow") or {}).get("comparison") or {}
        note = data.get("error") or (
            "%s · %s · %s · refreshed with status probes (15 s). Deltas describe readings; workload conditions are unverified."
            % (data.get("machine", "baseline not collected"), data.get("os", ""), data.get("captured", "")))
        self.query_one("#compare-note", Static).update(Text(note))
        rows = []
        for r in data.get("rows", []):
            delta = r.get("delta_pct")
            rows.append((chip(r["level"]), r["label"],
                         fmt(r["linux"], r["unit"], precision=4) if r.get("linux") is not None else "unavailable",
                         fmt(r["value"], r["unit"], precision=4), "%+.2f%%" % delta if delta is not None else "—",
                         r["measurement"], r["reason"] + "; " + r["context"]))
        fill(self.query_one("#t-compare", DataTable), rows)

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
        fill(self.query_one("#t-services", DataTable), rows)

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


TABS = ["overview", "sensors", "clocks", "fans", "gpu", "ane", "compare", "subsys", "services", "coverage", "log"]


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
    """Render tabs and drill-downs (show-NAME) of a capture headlessly to
    SVG (Textual's own screenshot) and optionally PNG. Returns the files
    written."""
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
                if t == "demos" or t.startswith("demo-"):
                    app.push_screen(DemosHome())
                    await pilot.pause(0.5)
                    if t.startswith("demo-"):
                        scr = DemoScreen(t[5:])
                        app.push_screen(scr)
                        await pilot.pause(0.4)
                        scr.action_start()
                        await pilot.pause(2.5)
                    name = "%s-%s.svg" % (prefix, t)
                    app.save_screenshot(name, outdir)
                    if t.startswith("demo-"):
                        scr.running = False
                        app.pop_screen()
                    app.pop_screen()
                    await pilot.pause(0.2)
                elif t.startswith("show-"):
                    app.open_detail(t[5:])
                    await pilot.pause(0.6)
                    name = "%s-%s.svg" % (prefix, t)
                    app.save_screenshot(name, outdir)
                    app.pop_screen()
                    await pilot.pause(0.2)
                else:
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
