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

from . import ane, audio, check, clocks, comparison, fans, gpu, haptics, log, sections, services, sources, subsystems
from .probe import timed

SCHEMA = "aurora-diag/dashboard"


def fast(m, slow=None):
    rs = sources.collect(list(sources.FAST_SOURCES), [audio.speaker_protection])
    sp = [r for r in rs if r["cat"] == "speaker"]
    secs = {}
    for name, fn in (("thermal", lambda: sections.thermal(rs)), ("cpu", sections.cpu),
                     ("gpu", lambda: sections.gpu(slow)), ("memory", sections.memory),
                     ("power", lambda: sections.power(rs)), ("audio", lambda: sections.audio_section(sp)),
                     ("sensors", lambda: sections.sensors(rs))):
        v, err = timed(fn, 5)
        secs[name] = v if err is None else sections.section(name, name.title(), [sections.item(name, "warn", "probe failed: " + err)],
                                                            headline="probe failed", level="warn")
    return {"t": time.time(), "readings": rs, "fans": fans.fans(), "hottest": fans.hottest(), "sections": secs}


def slow(m, api=True):
    out = {"t": time.time()}
    for key, fn, to in (("subsystems", subsystems.subsystems, 10), ("services", services.all_services, 10),
                        ("clocks", clocks.all_clocks, 10), ("haptics", lambda: haptics.status(m), 5),
                        ("ane", ane.collect, 8),
                        ("gpu_devices", lambda: [dict(d, name=n) for n, d in gpu.gpu_names()], 5),
                        ("gpu_clients", gpu.per_process, 5)):
        v, err = timed(fn, to)
        out[key] = v if err is None else {"error": err}
    v, err = timed(lambda: comparison.build(m.as_dict(), sources.collect(["hwmon"]),
                                           out.get("clocks") or {}, out.get("ane") or {}), 8)
    out["comparison"] = v if err is None else {"error": err}
    from . import demos
    v, err = timed(demos.inventory, 15)
    out["demos"] = v if err is None else {"error": err}
    subs = out["subsystems"] if isinstance(out["subsystems"], list) else None
    secs = sections.collect([n for n in sections.ORDER if n != "health"], subs=subs)
    v, err = timed(lambda: check.run(m, api=False, secs=secs), 20)
    out["check"] = v if err is None else {"error": err}
    if err is None:
        secs["health"] = sections.health(check_result=v)
    out["sections"] = secs
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
    d["slow"] = slow(m, api)
    sections.cpu()   # prime the CPU deltas so the first live sample is a real one
    time.sleep(0.5)
    d.update(fast(m, d["slow"]))
    d["log"] = log_tail()
    return d


def dump(m, path):
    d = collect(m)
    # A few fast samples so the sparklines have something to show.
    hist = []
    for _ in range(20):
        hist.append(fast(m, d["slow"]))
        time.sleep(0.5)
    d["history"] = [{"t": h["t"], "values": dict({r["id"]: r["value"] for r in h["readings"]
                                                  if isinstance(r["value"], (int, float))},
                                                 **{"section." + n: s.get("spark") for n, s in h["sections"].items()
                                                    if isinstance(s.get("spark"), (int, float))})} for h in hist]
    d.update({k: hist[-1][k] for k in ("readings", "fans", "hottest", "sections", "t")})
    d["sections"] = dict(d["slow"].get("sections") or {}, **hist[-1]["sections"])
    # Two seconds of motion at 20 Hz, so a capture can replay the motion demo.
    from . import demos
    if any(x["name"] == "motion" and x["level"] in ("ok", "warn") for x in d["slow"].get("demos") or [] if isinstance(x, dict)):
        ms = []
        for _ in range(60):
            ms.append(demos.motion_sample())
            time.sleep(0.05)
        d["motion_samples"] = ms
    if any(x["name"] == "whitepoint" and x["level"] == "ok" for x in d["slow"].get("demos") or [] if isinstance(x, dict)):
        ws = []
        for _ in range(8):
            ws.append(demos.whitepoint_sample())
            time.sleep(0.5)
        d["whitepoint_samples"] = ws
    with open(path, "w") as f:
        json.dump(d, f, default=str)
    return path


MAC = __import__("re").compile(r"\b[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}\b")


def redact(d, host=None):
    """A copy of a capture safe to publish: Wi-Fi network names, MAC and
    Bluetooth addresses and the host name replaced. Screenshots use it."""
    ssids = set()

    def find(o):
        if isinstance(o, dict):
            if str(o.get("title") or "").startswith("Wi-Fi") and isinstance(o.get("rows"), list):
                for r in o["rows"]:
                    if len(r) >= 2 and r[0] == "SSID" and isinstance(r[1], str) and r[1] != "not connected":
                        ssids.add(r[1])
            for v in o.values():
                find(v)
        elif isinstance(o, list):
            for v in o:
                find(v)
    find(d)
    names = sorted(ssids, key=len, reverse=True)
    old_host = d.get("host")

    def clean(o):
        if isinstance(o, str):
            for n in names:
                o = o.replace(n, "<wifi>")
            o = MAC.sub("xx:xx:xx:xx:xx:xx", o)
            if host and old_host and old_host != host:
                o = o.replace(old_host, host)
            return o
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [clean(v) for v in o]
        return o
    out = clean(d)
    if host:
        out["host"] = host
    return out


def load(path):
    with open(path) as f:
        d = json.load(f)
    if d.get("schema") != SCHEMA:
        raise ValueError("%s is not a dashboard capture (make one with `aurora-diag tui --dump FILE`)" % path)
    return d
