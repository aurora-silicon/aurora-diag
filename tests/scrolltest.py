# Live check: scroll the sensors table, let refreshes run, the view must not move.
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from aurora_diag import machine, tui_textual
from textual.widgets import DataTable, TabbedContent

async def main():
    app = tui_textual.Dash(machine.detect() if hasattr(machine, "detect") else machine.Machine(), None, None)
    async with app.run_test(size=(140, 30)) as pilot:
        await pilot.pause(3)
        app.query_one(TabbedContent).active = "tab-sensors"
        await pilot.pause(2)
        t = app.query_one("#t-sensors", DataTable)
        t.scroll_to(0, max(0, t.max_scroll_y // 2), animate=False)
        await pilot.pause(0.3)
        before = (t.scroll_y, t.cursor_row, t.row_count)
        await pilot.pause(4.5)          # four 1 s refreshes
        after = (t.scroll_y, t.cursor_row, t.row_count)
        print("rows", t.row_count, "max_scroll", t.max_scroll_y, "before", before, "after", after)
        print("PASS" if before[0] == after[0] and before[0] > 0 else "FAIL")
asyncio.run(main())
