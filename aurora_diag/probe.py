# SPDX-License-Identifier: MIT
"""Run a probe with a deadline so one stuck sysfs read or a hung
vulkaninfo cannot stall a whole run."""

import threading


def timed(fn, timeout, *args):
    """(value, None) or (None, reason). A probe that overruns keeps running
    in a daemon thread and its result is dropped."""
    box = {}

    def work():
        try:
            box["v"] = fn(*args)
        except Exception as e:  # a broken probe must not take the run down
            box["e"] = "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, "timed out after %gs" % timeout
    if "e" in box:
        return None, box["e"]
    return box.get("v"), None
