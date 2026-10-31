# SPDX-License-Identifier: MIT
"""Passive live readings beside a versioned macOS baseline export.

Telemetry never establishes equal workload/display conditions, so its delta
is descriptive only. Benchmarks are read from disk, never launched here.
"""

import json
import os

from . import ane, sysfs

SCHEMA = "aurora-diag/comparison"
BASELINE = os.path.join(os.path.dirname(__file__), "macos_baseline.json")
POWER_LABELS = {"idle.pstr": "Total System Power", "idle.phpc": "Heatpipe Power",
                "idle.pmvc": "SMC PMVC", "idle.pzc0": "SMC PZC0", "idle.pdbr": "SMC PDBR"}
MOBILENET = "mobilenetv1-1.0-224-random"


def load_baseline():
    path = os.environ.get("AURORA_DIAG_MACOS_BASELINE")
    with open(sysfs.root(path) if path else BASELINE) as f:
        raw = f.read(65537)
    if len(raw) > 65536:
        raise ValueError("baseline exceeds 64 KiB")
    b = json.loads(raw)
    if not isinstance(b, dict) or b.get("schema") != "aurora-diag/macos-baseline" or b.get("version") != 1:
        raise ValueError("unsupported macOS baseline schema/version")
    if not isinstance(b.get("machine"), str) or not isinstance(b.get("metrics"), list):
        raise ValueError("baseline needs machine and metrics")
    seen = set()
    for m in b["metrics"]:
        if not isinstance(m, dict) or not all(isinstance(m.get(k), str) for k in ("id", "label", "unit", "context")):
            raise ValueError("invalid baseline metric")
        if m["id"] in seen or type(m.get("value")) not in (int, float) or ane.number(m.get("value")) is None:
            raise ValueError("duplicate metric or invalid baseline number")
        seen.add(m["id"])
    return b


def _live(readings, clocks):
    values = {}
    for key, label in POWER_LABELS.items():
        r = next((r for r in readings if r.get("label") == label and r.get("unit") == "W"
                  and r.get("attr", "").endswith("_input") and ane.number(r.get("value")) is not None), None)
        if r:
            values[key] = (r["value"], "live", "current reading; baseline was idle, conditions unverified")
    if "idle.pstr" in values and "idle.pdbr" in values:
        n = values["idle.pstr"][0] - values["idle.pdbr"][0]
        if n >= 0:
            values["idle.non_display"] = (n, "live", "current PSTR - PDBR; baseline was idle")
    r = next((r for r in readings if r.get("label") == "CPU Die Max" and r.get("unit") == "C"
              and r.get("attr", "").endswith("_input") and ane.number(r.get("value")) is not None), None)
    if r:
        values["idle.tcmz"] = (r["value"], "live", "current temperature; baseline was idle")
    policies = [p for p in clocks.get("cpu", []) if p.get("ladder_MHz")]
    maxima = sorted({max(p["ladder_MHz"]) for p in policies})
    # Identify the two cluster types by their ladders, not CPU/policy numbers.
    if len(maxima) == 2:
        for cluster, maximum in (("e", maxima[0]), ("p", maxima[-1])):
            ladders = {tuple(p["ladder_MHz"]) for p in policies if max(p["ladder_MHz"]) == maximum}
            if len(ladders) != 1:
                continue
            ladder = next(iter(ladders))
            for field, value in (("states", len(ladder)), ("min", min(ladder)), ("max", max(ladder))):
                values["cpu.%s.%s" % (cluster, field)] = (value, "live", "advertised DVFS ladder")
    return values


def build(machine, readings, clocks, ane_data):
    try:
        b = load_baseline()
    except (OSError, ValueError, RecursionError) as e:
        return {"schema": SCHEMA, "version": 1, "error": "macOS baseline: " + str(e), "rows": []}
    compatible = machine.get("compatible") or []
    matches = b["machine"] in compatible
    live = _live(readings, clocks)
    # The smoke fixture is deliberately never substituted for MobileNet.
    benchmarks = [d["last_benchmark"] for d in ane_data.get("devices", []) if d.get("last_benchmark")]
    for batch in (1, 8):
        candidates = [x for x in benchmarks if x.get("model") == MOBILENET and x.get("batch") == batch
                      and x.get("precision") == "fp16" and x.get("ok") is True and x.get("errors", 0) == 0
                      and x.get("available") is not False]
        if len(candidates) == 1:
            x = candidates[0]
            for field, key in (("rate", "throughput_per_s"), ("p50", "p50_ms")):
                if x.get(key) is not None:
                    live["ane.b%d.%s" % (batch, field)] = (x[key], "saved", "saved benchmark at %s; not live" % x.get("generated", x["saved_at"]))
    rows = []
    for metric in b["metrics"]:
        value, measurement, reason = live.get(metric["id"], (None, "unavailable", "not exposed by live probes"))
        if metric["id"].startswith("ane.") and value is None:
            reason = "requires saved matching MobileNet benchmark; add-fixture is a different workload"
        delta = None
        level = "warn"
        if not matches:
            reason = "machine differs from baseline " + b["machine"]
        elif value is not None:
            if metric["value"]:
                delta = round(100 * (value / metric["value"] - 1), 2)
            if metric["id"].startswith("cpu."):
                level = "pass" if value == metric["value"] else "warn"
                reason = "ladder matches" if level == "pass" else "ladder differs"
        rows.append(dict(metric, linux=value, measurement=measurement, delta_pct=delta,
                         level=level, reason=reason))
    return {"schema": SCHEMA, "version": 1, "source": b.get("source"), "captured": b.get("captured"),
            "os": b.get("os"), "machine": b["machine"], "machine_matches": matches, "rows": rows}
