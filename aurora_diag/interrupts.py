# SPDX-License-Identifier: MIT
"""Bounded, read-only /proc/interrupts evidence; no defect verdicts."""

import math
import re
import time

from . import __version__, sysfs
from .probe import timed

MAX_BYTES = 1024 * 1024
MAX_CPUS = 256
MAX_ROWS = 4096
MAX_FOCUS = 128
READ_TIMEOUT = 1.0
NOTICE = ("Rates describe only this capture; zero does not exclude an intermittent problem. "
          "A high rate does not confirm a hardware defect or the driver hypothesis in linux#2426.")


def validate(interval, samples):
    """samples counts intervals; there is one additional initial reading."""
    if not math.isfinite(interval) or not 0.1 <= interval <= 5:
        raise ValueError("interval must be finite and between 0.1 and 5 seconds")
    if isinstance(samples, bool) or not isinstance(samples, int) or not 1 <= samples <= 60:
        raise ValueError("samples must be an integer between 1 and 60")
    if interval * samples > 30:
        raise ValueError("requested sampling duration must not exceed 30 seconds")


def _unknown(reason):
    return {"status": "unknown", "reason": reason, "cpus": [], "rows": []}


def parse(text):
    """Parse one complete read. Never use a partial table as a baseline.

    Non-numeric architecture summary rows (IPI, ERR, NMI, etc.) are not IRQs.
    Unsupported or malformed numeric rows invalidate the entire reading.
    """
    if not text:
        return _unknown("empty_input")
    if len(text) > MAX_BYTES:
        return _unknown("input_limit")
    if not text.endswith("\n"):
        return _unknown("truncated_input")
    lines = text.splitlines()
    cpus = lines[0].split()
    if (not cpus or len(cpus) > MAX_CPUS or len(set(cpus)) != len(cpus)
            or any(not re.fullmatch(r"CPU[0-9]{1,5}", c) for c in cpus)):
        return _unknown("invalid_cpu_header")
    rows, seen = [], set()
    for line in lines[1:]:
        if not line.strip():
            continue
        label, sep, rest = line.partition(":")
        label = label.strip()
        if not sep:
            return _unknown("malformed_row")
        if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", label):
            continue
        if not re.fullmatch(r"[0-9]{1,10}", label):
            return _unknown("malformed_irq")
        irq = int(label)
        if irq in seen:
            return _unknown("duplicate_irq")
        seen.add(irq)
        if len(seen) > MAX_ROWS:
            return _unknown("row_limit")
        fields = rest.split()
        n = len(cpus)
        if len(fields) < n + 1:
            return _unknown("malformed_row")
        nums, tail = fields[:n], fields[n:]
        if any(not re.fullmatch(r"[0-9]{1,20}", v) or int(v) > 2**64 - 1 for v in nums):
            return _unknown("invalid_counter")
        if not any(re.fullmatch(r"[0-9a-fA-F]+\.error-handler", v) for v in tail):
            continue
        # Keep the entire suffix too: trigger/handler changes break continuity.
        if (len(tail) < 3 or len(tail) > 16
                or any(len(v) > 256 or not re.fullmatch(r"[\x21-\x7e]+", v) for v in tail)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_\-]*", tail[0])
                or not re.fullmatch(r"[0-9]{1,20}", tail[1])):
            return _unknown("invalid_identity")
        trigger = tail[2] if tail[2] in ("Level", "Edge") else None
        rows.append({"linux_irq": irq, "controller": tail[0], "hardware_irq": int(tail[1]),
                     "trigger": trigger, "handler": " ".join(tail[3:] if trigger else tail[2:]),
                     "identity_suffix": tail, "cpus": cpus,
                     "counts": [int(v) for v in nums]})
        if len(rows) > MAX_FOCUS:
            return _unknown("focus_limit")
    if not seen:
        return _unknown("no_irq_rows")
    return {"status": "ok" if rows else "absent", "reason": None if rows else "no_error_handler_rows",
            "cpus": cpus, "rows": rows}


def read_interrupts():
    try:
        with open(sysfs.root("/proc/interrupts"), "rb") as f:
            data = f.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return _unknown("missing")
    except PermissionError:
        return _unknown("permission_denied")
    except OSError:
        return _unknown("unreadable")
    if len(data) > MAX_BYTES:
        return _unknown("input_limit")
    try:
        return parse(data.decode("ascii"))
    except UnicodeDecodeError:
        return _unknown("non_ascii_input")


def metadata():
    """Only kernel release and allowlisted board/SoC compatibles, never DT dumps."""
    release = sysfs.read("/proc/sys/kernel/osrelease", limit=129)
    if not release or not re.fullmatch(r"[A-Za-z0-9._+\-]{1,128}", release):
        release = None
    compatible = sysfs.dt_strings("/proc/device-tree/compatible")
    return {"kernel_release": release,
            "compatible": [c for c in compatible
                           if re.fullmatch(r"apple,(?:j[0-9]{3,5}[a-z]{0,2}|t[0-9]{4}|arm-platform)", c)][:8]}


def compare(previous, current, elapsed):
    """Only adjacent compatible readings yield deltas; current counts rebaseline."""
    old = {r["linux_irq"]: r for r in previous["rows"]} if previous else {}
    new = {r["linux_irq"]: r for r in current["rows"]}
    result = []
    for irq in sorted(old.keys() | new.keys()):
        a, b = old.get(irq), new.get(irq)
        reason = None
        if not b:
            reason = current["reason"] if current["status"] == "unknown" else "disappeared"
        elif previous is None:
            reason = "initial_baseline"
        elif previous["status"] == "unknown":
            reason = "previous_reading_unknown"
        elif previous["cpus"] != current["cpus"]:
            reason = "cpu_columns_changed"
        elif not a:
            reason = "appeared"
        elif a["identity_suffix"] != b["identity_suffix"]:
            reason = "identity_changed"
        elif not math.isfinite(elapsed) or elapsed <= 0:
            reason = "invalid_elapsed"
        elif any(y < x for x, y in zip(a["counts"], b["counts"])):
            reason = "counter_decreased"
        row = dict(b or a)
        row.update(state="unknown" if reason else "ok", reason=reason,
                   baseline=b is not None, counts=b["counts"] if b else None,
                   delta_counts=None, delta=None, rate_per_s=None)
        if not reason:
            row["delta_counts"] = [y - x for x, y in zip(a["counts"], b["counts"])]
            row["delta"] = sum(row["delta_counts"])
            row["rate_per_s"] = row["delta"] / elapsed
        result.append(row)
    return result


def capture(interval=1.0, samples=3):
    validate(interval, samples)
    meta, error = timed(metadata, READ_TIMEOUT)
    start = time.monotonic()
    readings, previous, last = [], None, None
    for index in range(samples + 1):
        if index:
            time.sleep(interval)
        began = time.monotonic()
        current, read_error = timed(read_interrupts, READ_TIMEOUT)
        now = time.monotonic()
        if read_error:
            current = _unknown("read_timeout" if "timed out" in read_error else "read_failed")
        elapsed = now - last if last is not None else None
        readings.append(dict(current, index=index, offset_s=now - start, elapsed_s=elapsed,
                             read_duration_s=now - began, rows=compare(previous, current, elapsed)))
        previous, last = current, now
        # Do not accumulate abandoned readers if a pseudo file hangs.
        if read_error:
            break
    return {"schema": "aurora-diag/irq-watch", "version": 1, "tool_version": __version__,
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "metadata": meta, "metadata_status": "unknown" if error else "ok",
            "focus": "address.error-handler", "interval_s": interval, "requested_intervals": samples,
            "readings_completed": len(readings), "elapsed_s": time.monotonic() - start,
            "notice": NOTICE, "readings": readings}


def render(doc):
    lines = ["IRQ watch: %d readings; %.3fs elapsed; focus address.error-handler" %
             (doc["readings_completed"], doc["elapsed_s"])]
    meta = doc["metadata"] or {}
    lines.append("Kernel: %s; compatible: %s" % (meta.get("kernel_release") or "unknown",
                                               ", ".join(meta.get("compatible", [])) or "unknown"))
    for s in doc["readings"]:
        lines.append("[%d] +%.3fs %s; CPUs: %s%s" %
                     (s["index"], s["offset_s"], s["status"], " ".join(s["cpus"]) or "unknown",
                      ("; " + s["reason"]) if s["reason"] else ""))
        for r in s["rows"]:
            value = ("delta=%d, %.2f IRQ/s over %.6fs" %
                     (r["delta"], r["rate_per_s"], s["elapsed_s"])) if r["state"] == "ok" else "unknown: " + r["reason"]
            lines.append("  Linux IRQ %d | %s %d %s | %s" %
                         (r["linux_irq"], r["controller"], r["hardware_irq"], r["handler"], value))
    return "\n".join(lines + [doc["notice"]])
