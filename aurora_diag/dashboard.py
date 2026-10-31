# SPDX-License-Identifier: MIT
"""The data behind the dashboard, as one JSON-able dict.

The Textual UI only renders what collect() returns, so the same screen can
show this machine live or a capture from another one (`tui --dump FILE`
on the machine, `tui --snapshot FILE` anywhere). Fast parts are collected
every second, slow parts (subsystems, services, clocks, GPU APIs) less
often.
"""

import json
import os
import time

from . import ane, audio, check, clocks, comparison, fans, gpu, haptics, log, services, sources, subsystems
from .probe import timed

SCHEMA = "aurora-diag/dashboard"


def fast(m):
    rs = sources.collect(list(sources.FAST_SOURCES), [audio.speaker_protection])
    return {"t": time.time(), "readings": rs, "fans": fans.fans(), "hottest": fans.hottest()}


def slow(m, api=True):
    out = {"t": time.time()}
    for key, fn, to in (("subsystems", subsystems.subsystems, 10), ("services", services.all_services, 10),
                        ("clocks", clocks.all_clocks, 10), ("haptics", lambda: haptics.status(m), 5),
                        ("ane", ane.collect, 8),
                        ("check", lambda: check.run(m, api=False), 15),
                        ("gpu_devices", lambda: [dict(d, name=n) for n, d in gpu.gpu_names()], 5),
                        ("gpu_clients", gpu.per_process, 5)):
        v, err = timed(fn, to)
        out[key] = v if err is None else {"error": err}
    v, err = timed(lambda: comparison.build(m.as_dict(), sources.collect(["hwmon"]),
                                           out.get("clocks") or {}, out.get("ane") or {}), 8)
    out["comparison"] = v if err is None else {"error": err}
    if api:
        vk, err = timed(gpu.vulkan, 60)
        gl, err2 = timed(gpu.gl, 60, False)
        out["vulkan"] = vk if not err else {"error": err}
        out["gl"] = gl if not err2 else {"error": err2}
        try:
            from . import compliance
            out["compliance"] = compliance.summary(out["vulkan"], out["gl"])
        except Exception as e:
            out["compliance"] = {"error": repr(e)}
    return out


def log_tail(n=200):
    try:
        with open(log.log_path()) as f:
            return f.readlines()[-n:]
    except OSError:
        return []


def collect(m, api=True):
    d = {"schema": SCHEMA, "version": 1, "host": os.uname().nodename, "kernel": os.uname().release,
         "machine": m.as_dict()}
    d.update(fast(m))
    d["slow"] = slow(m, api)
    d["log"] = log_tail()
    return d


def dump(m, path):
    d = collect(m)
    # A few fast samples so the sparklines have something to show.
    hist = []
    for _ in range(20):
        hist.append(fast(m))
        time.sleep(0.5)
    d["history"] = [{"t": h["t"], "values": {r["id"]: r["value"] for r in h["readings"]
                                             if isinstance(r["value"], (int, float))}} for h in hist]
    with open(path, "w") as f:
        json.dump(d, f, default=str)
    return path


def load(path):
    with open(path) as f:
        d = json.load(f)
    if d.get("schema") != SCHEMA:
        raise ValueError("%s is not a dashboard capture (make one with `aurora-diag tui --dump FILE`)" % path)
    return d
