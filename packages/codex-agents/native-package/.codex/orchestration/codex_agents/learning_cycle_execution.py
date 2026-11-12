"""Private blocking calculation worker primitive; deliberately unselected.

Broad-read Seatbelt is experimental, not native read containment. Enrollment,
memory policy, custody image, descendants, fairness and scheduling remain gates.
Unknown custody retains the actual context; it never authorizes release/retry.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import selectors
import struct
import subprocess
import sys
import tempfile
import threading
import time

from .process_tree import process_record
from .learning_cycle_process import (
    LEARNING_PROCESS_READY, READY_BYTES, MAX_INPUT_BYTES, MAX_OUTPUT_BYTES,
    MAX_CPU_SECONDS, _ENVIRONMENT, LearningProcessError, decode_calculation_reply,
)

_HELPER_ROOT = Path(__file__).resolve().parent.parent
_PROFILE = '(version 1) (allow default) (deny network*) (deny file-write*) (deny process-fork)'
_BOOTSTRAP = '''import sys
sys.path.insert(0,sys.argv[1])
allowed={"codex_agents","codex_agents.contracts","codex_agents.learning_contracts",
         "codex_agents.learning_analysis","codex_agents.learning_cycle_core",
         "codex_agents.learning_cycle_process"}
class FixedImports:
    def find_spec(self,name,path=None,target=None):
        root=name.partition(".")[0]
        if (root in {"sqlite3","_sqlite3"}
                or (root=="codex_agents" and name not in allowed)
                or (root!="codex_agents" and root not in sys.stdlib_module_names)):
            raise RuntimeError("learning helper dependency refused")
sys.meta_path.insert(0,FixedImports())
from codex_agents.learning_cycle_process import main
raise SystemExit(main(sys.argv[2:]))
'''


class _Stop(Exception):
    def __init__(self, suffix):
        self.reason = "learning_execution_" + suffix
        super().__init__(self.reason)


class LearningExecutionUnconfirmed(RuntimeError):
    def __init__(self, context):
        self.context = context
        super().__init__("learning_execution_unconfirmed")


@dataclass(frozen=True, repr=False)
class LearningExecutionResult:
    reason: str
    result: dict | None
    finished_ns: int | None
    roots_reaped: int
    no_process_started: bool


class _Context:
    def __init__(self, raw, operation, binding_digest, deadline_ns, cancelled, poll_ns, cleanup_ns):
        self.raw = raw
        self.operation = operation
        self.binding_digest = binding_digest
        self.deadline_ns = deadline_ns
        self.cancelled = cancelled
        self.poll_ns = poll_ns
        self.cleanup_ns = cleanup_ns
        self.last_ns = None
        self.scratch = None
        self.process = None
        self.birth = None
        self.spawn_ambiguous = False
        self.custody_lost = False
        self.guarded = False
        self.reaped = False
        self.roots_reaped = 0
        self.output = bytearray()
        self.offset = 0

    def sample(self):
        try:
            now = time.monotonic_ns()
        except BaseException:
            raise _Stop("clock_failure") from None
        if (type(now) is not int or now < 0
                or (self.last_ns is not None and now < self.last_ns)):
            raise _Stop("clock_failure")
        self.last_ns = now
        if self.cancelled.is_set():
            raise _Stop("cancelled")
        if now >= self.deadline_ns:
            raise _Stop("expired")
        return now

    def record(self):
        try:
            record = process_record(self.process.pid)
            if (record is None or record[0] != os.getpid()
                    or (self.birth is not None and record[2] != self.birth)
                    or type(record[2]) is not str or not record[2]):
                raise ValueError()
            return record
        except BaseException:
            self.custody_lost = True
            raise _Stop("birth_unavailable") from None


def _exchange(context):
    context.sample()
    command = [
        "/usr/bin/sandbox-exec", "-p", _PROFILE,
        str(Path(sys.executable).resolve()), "-I", "-B", "-S", "-c", _BOOTSTRAP,
        str(_HELPER_ROOT), context.operation, str(MAX_CPU_SECONDS),
    ]
    context.sample()
    context.spawn_ambiguous = True
    context.process = subprocess.Popen(
        command, cwd=context.scratch, env=dict(_ENVIRONMENT),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, start_new_session=True, bufsize=0,
    )
    context.spawn_ambiguous = False
    process = context.process
    context.birth = context.record()[2]
    context.record()  # Fresh parent/birth check on the original unreaped handle.
    mode = b"S" if context.operation == "select_mining" else b"P"
    marker = LEARNING_PROCESS_READY + mode + struct.pack(">Q", process.pid)
    cap = READY_BYTES + MAX_OUTPUT_BYTES
    out_eof = err_eof = False
    for stream in (process.stdin, process.stdout, process.stderr):
        os.set_blocking(stream.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        selector.register(process.stderr, selectors.EVENT_READ)
        while True:
            now = context.sample()
            record = context.record()
            if record[3] and out_eof and err_eof:
                break
            delay = min(context.poll_ns, context.deadline_ns - now) / 1_000_000_000
            events = selector.select(delay)
            context.sample()
            for key, _ in events:
                context.sample()
                stream = key.fileobj
                if stream is process.stdin:
                    try:
                        count = os.write(stream.fileno(), memoryview(context.raw)[
                            context.offset:context.offset + 65_536])
                    except BlockingIOError:
                        context.sample()
                        continue
                    except BrokenPipeError:
                        raise _Stop("invalid_output") from None
                    context.sample()
                    if count <= 0:
                        raise _Stop("invalid_output")
                    context.offset += count
                    if context.offset == len(context.raw):
                        selector.unregister(stream)
                        stream.close()
                        context.sample()
                    continue
                # Read at most the marker until it is complete. No input is
                # registered until exact phase/PID and fresh custody match.
                maximum = (READY_BYTES - len(context.output) if not context.guarded
                           else min(65_536, cap + 1 - len(context.output)))
                try:
                    part = os.read(stream.fileno(), 1 if stream is process.stderr else maximum)
                except BlockingIOError:
                    context.sample()
                    continue
                context.sample()
                if stream is process.stderr:
                    if part:
                        # Do not retain stderr, even in private custody.
                        raise _Stop("worker_failed")
                    err_eof = True
                    selector.unregister(stream)
                    continue
                if not part:
                    out_eof = True
                    selector.unregister(stream)
                    if not context.guarded:
                        raise _Stop("invalid_output")
                    continue
                context.output.extend(part)
                if len(context.output) > cap:
                    raise _Stop("output_limit")
                if not context.guarded and len(context.output) == READY_BYTES:
                    if bytes(context.output) != marker:
                        raise _Stop("invalid_output")
                    context.record()
                    context.sample()
                    context.guarded = True
                    selector.register(process.stdin, selectors.EVENT_WRITE)
    context.sample()
    if not context.guarded or context.offset != len(context.raw):
        raise _Stop("invalid_output")
    # EOF is insufficient. A fresh original-child zombie observation must
    # precede this nonblocking owned wait. Never poll/reap before that check.
    if not context.record()[3]:
        raise _Stop("birth_unavailable")
    code = process.wait(timeout=0)
    context.reaped = True
    context.sample()
    if type(code) is not int or code != 0:
        raise _Stop("worker_failed")


def _cleanup(context):
    """One bounded owned wait; any ambiguity keeps all actual custody."""
    try:
        if context.spawn_ambiguous:
            return False
        process = context.process
        if process is not None:
            if not context.reaped:
                # No signal without a fresh original parent/birth record.
                if context.birth is not None and not context.custody_lost:
                    record = context.record()
                    if not record[3]:
                        process.kill()
                process.wait(timeout=context.cleanup_ns / 1_000_000_000)
                context.reaped = True
            if (context.birth is None or context.custody_lost or not context.guarded
                    or type(process.returncode) is not int):
                return False
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            context.roots_reaped = 1
        # Only the actual fresh empty owned directory, after confirmed cleanup.
        # rmdir refuses unexpected contents rather than deleting arbitrary files.
        if context.scratch is not None:
            os.rmdir(context.scratch)
            context.scratch = None
        return True
    except BaseException:
        return False


def _execute_learning_job(raw, *, operation, binding_digest, deadline_ns, cancelled,
                          poll_ns=50_000_000, cleanup_ns=1_000_000_000):
    """Internal trusted-worker call; never a writer, health or API operation."""
    if (type(raw) is not bytes or not 0 < len(raw) <= MAX_INPUT_BYTES
            or type(operation) is not str or operation not in {"select_mining", "plan"}
            or type(binding_digest) is not str or len(binding_digest) != 64
            or any(char not in "0123456789abcdef" for char in binding_digest)
            or type(deadline_ns) is not int or not 1 <= deadline_ns <= (1 << 63) - 1
            or type(cancelled) is not threading.Event
            or type(poll_ns) is not int or not 1_000_000 <= poll_ns <= 100_000_000
            or type(cleanup_ns) is not int or not 1_000_000_000 <= cleanup_ns <= 5_000_000_000
            or sys.platform != "darwin"):
        raise ValueError("learning_execution_invalid_input")
    context = _Context(raw, operation, binding_digest, deadline_ns, cancelled, poll_ns, cleanup_ns)
    reason = "learning_execution_completed"
    result = None
    try:
        context.sample()
        context.scratch = Path(tempfile.mkdtemp(prefix="learning-execution-"))
        _exchange(context)
        context.sample()
        try:
            result = decode_calculation_reply(
                bytes(context.output[READY_BYTES:]),
                input_digest=hashlib.sha256(raw).hexdigest(),
                operation=operation, binding_digest=binding_digest,
            )
        except LearningProcessError:
            raise _Stop("invalid_output") from None
        if type(result) is not dict:
            raise _Stop("invalid_output")
        context.sample()
    except _Stop as stopped:
        reason = stopped.reason
        result = None
    except BaseException:
        # Never propagate a payload-bearing exception or its message.
        reason = "learning_execution_internal_failure"
        result = None
    if not _cleanup(context):
        raise LearningExecutionUnconfirmed(context) from None
    try:
        finished = context.sample()
    except _Stop as stopped:
        reason, result, finished = stopped.reason, None, context.last_ns
    except BaseException:
        reason, result, finished = "learning_execution_internal_failure", None, context.last_ns
    return LearningExecutionResult(reason, result, finished, context.roots_reaped, context.process is None)
