# SPDX-License-Identifier: MIT
"""Structured JSON-lines log that an agent can follow with `tail -F`.

One object per line. Every line carries ts (UTC, ISO 8601), mono (seconds of
CLOCK_MONOTONIC), host, pid, session and event. The events are documented in
docs/LOG.md.
"""

import json
import os
import socket
import sys
import time
import uuid

MAX_BYTES = 64 * 1024 * 1024

_session = uuid.uuid4().hex[:12]
_stdout_json = False
_path = None
_quiet = False


def log_path():
    env = os.environ.get("AURORA_DIAG_LOG")
    if env:
        return env
    if os.geteuid() == 0:
        return "/var/log/aurora-diag/aurora-diag.jsonl"
    state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(state, "aurora-diag", "aurora-diag.jsonl")


def run_dir():
    """Where the change journal and the fan lease live (tmpfs: a reboot
    resets every sysfs value, so nothing is left to restore after one)."""
    env = os.environ.get("AURORA_DIAG_RUN")
    if env:
        return env
    if os.geteuid() == 0:
        return "/run/aurora-diag"
    rt = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(rt, "aurora-diag")


def setup(stdout_json=False, quiet=False, path=None):
    global _stdout_json, _path, _quiet
    _stdout_json = stdout_json
    _quiet = quiet
    _path = path or log_path()
    try:
        os.makedirs(os.path.dirname(_path), exist_ok=True)
    except OSError:
        _path = None


def session():
    return _session


def _rotate():
    try:
        if os.path.getsize(_path) > MAX_BYTES:
            os.replace(_path, _path + ".1")
    except OSError:
        pass


def event(name, **fields):
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "mono": round(time.monotonic(), 3),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "session": _session,
        "event": name,
    }
    rec.update(fields)
    line = json.dumps(rec, separators=(",", ":"), default=str)
    if _path:
        try:
            _rotate()
            with open(_path, "a") as f:
                f.write(line + "\n")
        except OSError:
            pass
    if _stdout_json and not _quiet:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    return rec
