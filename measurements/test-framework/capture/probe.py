"""Read-only step-0 profiling, imported only by measured test workers."""
import atexit
import cProfile
import fcntl
import json
import os
from pathlib import Path
import sys
import select
import subprocess
import threading
from threading import local as thread_local
import time
import unittest

started = time.perf_counter()
output_directory = Path(os.environ['TF_PROFILE_DIR'])
counts = {"processes": 0, "git": 0, "sockets": 0}
pending = thread_local()

def audit(event, args):
    if event == "subprocess.Popen":
        pending.argv = args[1]
    elif event == "os.posix_spawn" and getattr(pending, "argv", None) == args[1]:
        pending.argv = None
        return
    if event in {"subprocess.Popen", "os.posix_spawn", "os.spawn", "os.exec", "os.fork", "os.forkpty", "os.system"}:
        counts["processes"] += 1
        executable = os.fsdecode(args[0]) if args and isinstance(args[0], (str, bytes)) else ""
        if os.path.basename(executable) == "git":
            counts["git"] += 1
    elif event in {"socket.connect", "socket.bind"}:
        counts["sockets"] += 1

sys.addaudithook(audit)
phases = {"setup": 0., "body": 0., "teardown": 0.}
calls = {}

def timed(owner, name, category, predicate=None):
    original = getattr(owner, name)
    def measured(*args, **kwargs):
        if predicate is not None and not predicate(args, kwargs):
            return original(*args, **kwargs)
        before = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            elapsed = time.perf_counter() - before
            row = calls.setdefault(category, {"count": 0, "seconds": 0.})
            row["count"] += 1
            row["seconds"] += elapsed
    setattr(owner, name, measured)

timed(time, 'sleep', 'sleep')
timed(threading.Condition, 'wait', 'condition_wait')
timed(select, 'select', 'select')
timed(os, 'fsync', 'fsync')
timed(fcntl, 'fcntl', 'fullfsync', lambda a, k: len(a) > 1 and a[1] == 51 and sys.platform == 'darwin')
timed(subprocess, 'run', 'git_run', lambda a, k: isinstance(a[0] if a else k.get('args'), (list, tuple)) and
      os.path.basename(os.fsdecode((a[0] if a else k['args'])[0])) == 'git')
for name, phase in [("_callSetUp", "setup"), ("_callTestMethod", "body"),
                    ("_callTearDown", "teardown"), ("doCleanups", "teardown")]:
    original = getattr(unittest.TestCase, name)
    def measured(self, *args, _original=original, _phase=phase, **kwargs):
        before = time.perf_counter()
        try:
            return _original(self, *args, **kwargs)
        finally:
            phases[_phase] += time.perf_counter() - before
    setattr(unittest.TestCase, name, measured)

profiler = cProfile.Profile()
profiler.enable()

def finish():
    profiler.disable()
    rows = []
    for entry in profiler.getstats():
        code = entry.code
        if isinstance(code, str):
            label = code
        else:
            label = f"{code.co_filename}:{code.co_firstlineno}:{code.co_name}"
        rows.append({"function": label, "calls": entry.callcount,
                     "self_seconds": entry.inlinetime, "inclusive_seconds": entry.totaltime})
    target = output_directory / (str(os.getpid()) + ".json")
    target.write_text(json.dumps({"cwd": os.getcwd(), "names": sys.argv[1:],
        "seconds": time.perf_counter() - started, "counts": counts, "phases": phases, "calls": calls,
        "functions": sorted(rows, key=lambda row: -row["inclusive_seconds"])}, indent=1) + "\n")

atexit.register(finish)
