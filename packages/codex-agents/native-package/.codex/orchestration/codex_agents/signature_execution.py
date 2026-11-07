"""Fixed blocking execution for a prestarted trusted signature worker.

No service operation or scheduler invokes this primitive directly. Actual owned
handles, birth checks and fixed no-fork helper channels remain separate from
caller authentication and fresh admission. Unconfirmed errors retain process,
scratch and input custody; they never authorize capacity release or replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import struct

from .process_tree import ProcessTree, process_record
from .signature_input_wire import (
    SignatureWireLimits, decode_signature_inputs, encode_signature_inputs,
)
from .signature_inputs import SignatureInputSnapshot
from .signature_payloads import decode_signature_payloads, signature_payload_response_limit
from .signature_process import SIGNATURE_PROCESS_READY, SIGNATURE_PROCESS_READY_BYTES

_HELPER_ROOT = Path(__file__).resolve().parent.parent
_BOOTSTRAP = '''import sys
sys.path.insert(0,sys.argv[1])
allowed={"codex_agents.contracts","codex_agents.learning_contracts","codex_agents.owner_record_payload",
         "codex_agents.owner_key_contracts","codex_agents.signature_inputs","codex_agents.signature_input_wire",
         "codex_agents.signature_payloads","codex_agents.signature_process"}
class FixedImports:
    def find_spec(self,name,path=None,target=None):
        if (name.partition(".")[0] in {"sqlite3","_sqlite3"}
                or (name.startswith("codex_agents.") and name not in allowed)):
            raise RuntimeError("signature helper dependency refused")
sys.meta_path.insert(0,FixedImports())
from codex_agents.signature_process import main
raise SystemExit(main(sys.argv[2:]))
'''


class _Stop(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class SignatureExecutionUnconfirmed(RuntimeError):
    def __init__(self, context):
        self.context = context
        super().__init__('signature_execution_unconfirmed')


@dataclass(frozen=True, repr=False)
class SignatureExecutionResult:
    reason: str
    indices: tuple[int, ...]
    finished_ns: int | None
    roots_reaped: int
    no_process_started: bool


class _Context:
    def __init__(self, inputs, limits, deadline_ns, cancelled, poll_ns, cleanup_ns):
        self.inputs = inputs
        self.limits = limits
        self.deadline_ns = deadline_ns
        self.cancelled = cancelled
        self.poll_ns = poll_ns
        self.cleanup_ns = cleanup_ns
        self.last_ns = None
        self.scratch = None
        self.process = None
        self.tree = None
        self.spawn_ambiguous = False
        self.guarded = False
        self.reaped = False
        self.roots_reaped = 0
        self.wire = None
        self.batch = None

    def sample(self, *, stopping=True):
        now = time.monotonic_ns()
        if type(now) is not int or now < 0 or (self.last_ns is not None and now < self.last_ns):
            raise _Stop('signature_execution_clock_failure')
        self.last_ns = now
        if stopping:
            if self.cancelled.is_set():
                raise _Stop('signature_execution_cancelled')
            if now >= self.deadline_ns:
                raise _Stop('signature_execution_expired')
        return now


def _profile(scratch):
    # Experimental broad-read guard only; the narrower policy is stopped and
    # unselected. No OS read-isolation or native-caller authority is claimed.
    return ('(version 1) (allow default) (deny network*) (deny file-write*) (deny process-fork) '
            '(allow file-write* (subpath ' + json.dumps(str(scratch)) + ')) '
            '(allow file-write* (literal "/dev/null"))')


def _close_streams(process):
    for stream in (process.stdin, process.stdout):
        if stream is not None and not stream.closed:
            stream.close()


def _cleanup_process(context):
    try:
        return _cleanup_process_checked(context)
    except BaseException:
        # A cleanup fault is unconfirmed custody, never ordinary completion.
        return False


def _cleanup_process_checked(context):
    if context.spawn_ambiguous:
        return False
    process = context.process
    if process is None:
        return True
    _close_streams(process)
    if not context.reaped:
        if context.tree is not None and context.tree.identified:
            record = process_record(process.pid)
            if record is not None:
                if record[2] != context.tree.root_birth or record[0] != os.getpid():
                    return False
                if not record[3]:
                    # Only this actual unreaped Popen child, after a fresh birth
                    # check. No external ps call/group signal/foreign PID input.
                    process.kill()
        try:
            process.wait(timeout=context.cleanup_ns / 1_000_000_000)
        except (OSError, subprocess.TimeoutExpired):
            return False
        context.reaped = True
    # Reaping a root without the actual no-fork helper ready/custody boundary
    # does not establish that every possible descendant is gone.
    if not context.guarded or context.tree is None or not context.tree.identified:
        return False
    context.roots_reaped += 1
    context.process = None
    context.tree = None
    context.guarded = False
    context.reaped = False
    return True


def _exchange(context, mode, data=None):
    context.sample()
    arguments = ['prepare', str(context.limits.max_rows), str(context.limits.max_field_bytes)] if mode == b'P' else ['verify']
    command = ['/usr/bin/sandbox-exec', '-p', _profile(context.scratch),
               str(Path(sys.executable).resolve()), '-I', '-B', '-S', '-c', _BOOTSTRAP, str(_HELPER_ROOT), *arguments]
    context.spawn_ambiguous = True
    # Retain the actual handle before any later operation; no arbitrary PID or
    # executable/descriptor supplied by a client is accepted here.
    context.process = subprocess.Popen(
        command, cwd=context.scratch,
        env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'TMPDIR': str(context.scratch)},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        close_fds=True, start_new_session=True, bufsize=0,
    )
    context.spawn_ambiguous = False
    process = context.process
    record = process_record(process.pid)
    if record is None or record[0] != os.getpid():
        raise _Stop('signature_execution_birth_unavailable')
    context.tree = ProcessTree(process.pid)
    if not context.tree.identify({process.pid: os.getpid()}, birth=record[2]):
        raise _Stop('signature_execution_birth_unavailable')
    cap = SIGNATURE_PROCESS_READY_BYTES + (signature_payload_response_limit(context.limits) if mode == b'P' else 0)
    output = bytearray()
    offset = 0
    eof = False
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stdin.fileno(), False)
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        if mode == b'V':
            process.stdin.close()
        while True:
            now = context.sample()
            if eof and context.tree.root_exited():
                break
            wait = min(context.poll_ns, context.deadline_ns - now) / 1_000_000_000
            for key, _ in selector.select(wait):
                context.sample()
                if key.fileobj is process.stdout:
                    try:
                        part = os.read(process.stdout.fileno(), min(65_536, cap + 1 - len(output)))
                    except BlockingIOError:
                        continue
                    if not part:
                        eof = True
                        selector.unregister(process.stdout)
                    else:
                        output.extend(part)
                        if len(output) > cap:
                            raise _Stop('signature_execution_output_limit')
                    if not context.guarded and len(output) >= SIGNATURE_PROCESS_READY_BYTES:
                        marker = bytes(output[:SIGNATURE_PROCESS_READY_BYTES])
                        if marker != SIGNATURE_PROCESS_READY + mode + struct.pack('>Q', process.pid):
                            raise _Stop('signature_execution_invalid_output')
                        context.guarded = True
                        context.sample()
                        if mode == b'P':
                            selector.register(process.stdin, selectors.EVENT_WRITE)
                    if eof and not context.guarded:
                        raise _Stop('signature_execution_invalid_output')
                else:
                    context.sample()
                    try:
                        count = os.write(process.stdin.fileno(), memoryview(data)[offset:offset + 65_536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        raise _Stop('signature_execution_invalid_output') from None
                    offset += count
                    if count == 0:
                        raise _Stop('signature_execution_invalid_output')
                    if offset == len(data):
                        selector.unregister(process.stdin)
                        process.stdin.close()
    context.sample()
    code = process.wait(timeout=0)
    context.reaped = True
    context.sample()
    response = bytes(output[SIGNATURE_PROCESS_READY_BYTES:])
    if not _cleanup_process(context):
        raise SignatureExecutionUnconfirmed(context)
    return code, response


def _public_file(scratch, name, data):
    descriptor = os.open(scratch / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        while view:
            count = os.write(descriptor, view)
            if count == 0:
                raise OSError('fixed public file write failed')
            view = view[count:]
    finally:
        os.close(descriptor)


def _execute_signature_job(inputs, limits, deadline_ns, cancelled, poll_ns, cleanup_ns):
    """Blocking internal worker primitive; an unconfirmed error retains custody.

    This primitive is not a normal service/API operation. The fixed
    pool must reserve all copies before offering, keep the context on unknown,
    and never release intake from an exception or mere thread completion.
    """

    if (type(inputs) is not SignatureInputSnapshot or type(limits) is not SignatureWireLimits
            or type(deadline_ns) is not int or not 1 <= deadline_ns <= (1 << 63) - 1
            or type(cancelled) is not threading.Event
            or type(poll_ns) is not int or not 1 <= poll_ns <= 100_000_000
            or type(cleanup_ns) is not int or not 1 <= cleanup_ns <= 5_000_000_000
            or sys.platform != 'darwin'):
        raise ValueError('signature_execution_invalid_input')
    context = _Context(inputs, SignatureWireLimits(limits.max_rows, limits.max_field_bytes),
                       deadline_ns, cancelled, poll_ns, cleanup_ns)
    reason = 'signature_execution_completed'
    indices = []
    try:
        context.sample()
        context.scratch = Path(tempfile.mkdtemp(prefix='signature-execution-'))
        context.wire = encode_signature_inputs(inputs, os.urandom(16), context.limits)
        code, response = _exchange(context, b'P', context.wire)
        if code != 0:
            raise _Stop('signature_execution_worker_failed')
        context.batch = decode_signature_payloads(response, context.wire, context.limits)
        # The actual job keeps its original snapshot; decoded input data is not
        # an owner-origin, accepted verified result or fresh admission decision.
        original = decode_signature_inputs(context.wire, context.limits)
        if len(original.rows) != len(inputs.rows) or original.rows != inputs.rows:
            raise _Stop('signature_execution_invalid_output')
        for index, payload in context.batch.payloads:
            context.sample()
            for name, data in (('payload', payload), ('allowed-signers', original.signer),
                               ('signature', original.rows[index][5])):
                _public_file(context.scratch, name, data)
            code, response = _exchange(context, b'V')
            if response or code < 0:
                raise _Stop('signature_execution_worker_failed')
            if code == 0:
                indices.append(index)
            for name in ('payload', 'allowed-signers', 'signature'):
                (context.scratch / name).unlink()
        context.sample()
    except _Stop as stopped:
        reason = stopped.reason
        indices.clear()
    except BaseException:
        if not _cleanup_process(context):
            raise SignatureExecutionUnconfirmed(context) from None
        if context.scratch is not None:
            try:
                shutil.rmtree(context.scratch)
            except BaseException:
                raise SignatureExecutionUnconfirmed(context) from None
            context.scratch = None
        raise
    if not _cleanup_process(context):
        raise SignatureExecutionUnconfirmed(context)
    if context.scratch is not None:
        try:
            shutil.rmtree(context.scratch)
        except BaseException:
            raise SignatureExecutionUnconfirmed(context) from None
        context.scratch = None
    try:
        finished = context.sample()
    except _Stop as stopped:
        reason = stopped.reason
        indices.clear()
        finished = context.last_ns
    return SignatureExecutionResult(reason, tuple(indices), finished, context.roots_reaped, context.roots_reaped == 0)
