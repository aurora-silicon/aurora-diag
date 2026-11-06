# SPDX-License-Identifier: MIT
"""Deterministic version of scrolltest.py, including ANE and vs macOS."""

import copy
import json
import unittest

try:
    from textual.coordinate import Coordinate
    from textual.widgets import DataTable, TabbedContent
    from aurora_diag.tui_textual import Dash
except ImportError:
    Dash = None

from aurora_diag import comparison


def capture():
    rows = [dict(id="temp.test.%d" % i, cat="temp", dev="test", attr="temp_input", value=40 + i / 10,
                 unit="C", label="sensor %d" % i, writable=False) for i in range(100)]
    devices = [dict(node="/dev/accel/accel%d" % i, name="accel%d" % i, driver="ane", node_present=True,
                    generation="H13", firmware_version="1.2.3", firmware_source="firmware_version",
                    client_count=0, clients_complete=True, performance_level="0", power_state="suspended",
                    wedged=0, counters={"jobs": 20, "busy_ns": 2000}, error_counters={"faults": 0},
                    last_benchmark=None) for i in range(4)]
    return dict(schema="aurora-diag/dashboard", version=1, host="synthetic", kernel="test",
                machine=dict(model="Synthetic Mac", soc="test", compatible=["apple,j616s"]),
                readings=rows, fans=[], hottest=[None, None], log=[],
                slow=dict(ane={"devices": devices},
                          comparison=comparison.build({"compatible": ["apple,j616s"]}, [], {}, {"devices": []})))


@unittest.skipIf(Dash is None, "Textual is optional; install it to run dashboard regressions")
class TestDashboardScroll(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_preserves_scroll_cursor_and_rows(self):
        app = Dash(capture=capture())
        async with app.run_test(size=(140, 20)) as pilot:
            for tab, table in (("sensors", "t-sensors"), ("ane", "t-ane"), ("compare", "t-compare")):
                app.query_one(TabbedContent).active = "tab-" + tab
                await pilot.pause(0.1)
                t = app.query_one("#" + table, DataTable)
                if tab == "compare":
                    self.assertEqual(t.get_cell_at(Coordinate(13, 3)), "0.302 ms")
                t.move_cursor(row=4, scroll=False)
                t.scroll_to(0, max(1, t.max_scroll_y // 2), animate=False)
                await pilot.pause(0.1)
                before = (t.scroll_y, t.cursor_row, t.row_count)
                self.assertGreater(before[0], 0, tab)
                for i in range(3):
                    slow = copy.deepcopy(app.data["slow"])
                    slow["ane"]["devices"][0]["performance_level"] = str(i)
                    slow["comparison"]["rows"][0]["linux"] = i
                    app.apply_slow(slow)
                    app.apply_fast(dict(readings=copy.deepcopy(app.data["readings"]), fans=[], hottest=[None, None]))
                    await pilot.pause(0.05)
                self.assertEqual((t.scroll_y, t.cursor_row, t.row_count), before, tab)

    async def test_device_added_preserves_view_and_old_capture_works(self):
        app = Dash(capture=capture())
        async with app.run_test(size=(140, 20)) as pilot:
            app.query_one(TabbedContent).active = "tab-ane"
            await pilot.pause(0.1)
            t = app.query_one("#t-ane", DataTable)
            t.move_cursor(row=5, scroll=False)
            t.scroll_to(0, 10, animate=False)
            await pilot.pause(0.1)
            before = (t.scroll_y, t.cursor_row)
            slow = copy.deepcopy(app.data["slow"])
            dev = copy.deepcopy(slow["ane"]["devices"][-1])
            dev.update(node="/dev/accel/accel9", name="accel9")
            slow["ane"]["devices"].append(dev)
            app.apply_slow(slow)
            await pilot.pause(0.1)
            self.assertEqual((t.scroll_y, t.cursor_row), before)
            app.apply_slow({})  # pre-ANE dashboard capture
            await pilot.pause(0.1)
            self.assertEqual(t.row_count, 1)


if __name__ == "__main__":
    unittest.main()


class TestRedact(unittest.TestCase):
    def test_redact_ssid_mac_host(self):
        from aurora_diag import dashboard
        cap = {"host": "labhost", "sections": {"radio": {"headline": "MyLabNet -50 dBm", "tables": [
            {"title": "Wi-Fi wlan0", "columns": ["field", "value"], "rows": [["SSID", "MyLabNet"], ["bss", "aa:bb:cc:dd:ee:ff"]]}]}},
               "log": ["labhost says hi"]}
        r = dashboard.redact(cap, "mbp")
        text = json.dumps(r)
        self.assertNotIn("MyLabNet", text)
        self.assertNotIn("aa:bb:cc", text)
        self.assertNotIn("labhost", text)
        self.assertIn("<wifi> -50 dBm", text)
