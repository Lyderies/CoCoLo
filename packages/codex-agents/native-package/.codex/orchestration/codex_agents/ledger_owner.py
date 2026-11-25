"""Trusted whole-operation handoff to the thread that opened the account ledger.

The host constructs and pumps this on its final owner thread. Offline callers
continue to use LearningLedger directly. The client facade exposes no SQL or
cursor to submitting threads; existing operation exceptions cross as plain
values, while fatal defects are raised by pump on the owner for C10.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
import sqlite3
import threading

from . import ledger_waits
from .ledger_owner_queue import LedgerOwnerQueue, LedgerQueueBusy
from .learning_store import LearningLedger, LedgerError, StateError, _passing_refusal
from .sql_read_budget import SQLReadError
from .contracts import ContractError
from .learning_contracts import canonical_json, validate_learning_record

SYSTEM_PROJECT = 'prj_' + '0' * 32


@dataclass(frozen=True)
class _Failure:
    kind: type
    message: str
    code: int | None = None
    name: str | None = None
    args: tuple = ()
    attributes: tuple = ()


_PLAIN = (str, int, float, bool, bytes, type(None))


def _plain(value):
    return type(value) in _PLAIN or (type(value) is tuple and all(type(item) in _PLAIN for item in value))


def _failure(error, *, sqlite=False):
    """Plain values only: no traceback, frame or owner object crosses the handoff (LOW-3: but every plain
    attribute does, so CollectorError.code, OSError.errno and reason codes keep their meaning)."""
    args = error.args if all(_plain(item) for item in error.args) else (str(error),)
    attributes = tuple((key, value) for key, value in vars(error).items()
                       if type(key) is str and not key.startswith('__') and _plain(value))
    code = getattr(error, 'sqlite_errorcode', None) if sqlite else None
    name = getattr(error, 'sqlite_errorname', None) if sqlite else None
    return _Failure(type(error), str(error), code, name, args, attributes)


def _rebuild(failure):
    try:
        # __new__ without __init__: constructors with their own arguments (ResultContractError) are not re-run.
        error = failure.kind.__new__(failure.kind, *failure.args)
        error.args = failure.args
        for key, value in failure.attributes:
            setattr(error, key, value)
    except Exception:
        error = failure.kind(failure.message)
    if failure.code is not None:
        error.sqlite_errorcode, error.sqlite_errorname = failure.code, failure.name
    return error


@dataclass(frozen=True)
class _PreparedOwnerRecord:
    origin: object
    project_id: str
    record: bytes
    signature: bytes
    owner: tuple | None
    signer: bytes


@dataclass(frozen=True)
class _BindPreparation:
    project_id: str
    inputs: tuple


class _LedgerClient:
    def __init__(self, owner):
        self.owner = owner
        self.state = owner.state

    @property
    def _collector_active(self):
        self.owner.guard()
        return self.owner.ledger._collector_active

    @_collector_active.setter
    def _collector_active(self, value):
        self.owner.guard()
        self.owner.ledger._collector_active = value

    def lock_refusals(self):
        counts = self.owner.ledger.lock_refusals()
        for caller, causes in self.owner.queue.snapshot()['refusals'].items():
            counts[caller] = counts.get(caller, 0) + sum(causes.values())
        return counts

    def bind_session(self, project_root, session_id, producer, client_version, **options):
        owner = self.owner
        captured = owner.call(lambda: owner.ledger.bind_session(
            project_root, session_id, producer, client_version, _capture_only=True, **options), caller="bind_prepare", contain_errors=True)
        if type(captured) is not _BindPreparation:
            return captured  # The existing hot binding path needs no signature work.
        prepared = owner.ledger._verify_attestation_inputs(captured.project_id, "codex", *captured.inputs)
        return owner.call(lambda: owner.ledger.bind_session(project_root, session_id, producer, client_version,
                          _prepared=prepared, **options), project_id=captured.project_id, caller="bind_session", contain_errors=True)

    def commit_owner_record(self, project_id, *, record, signature, **options):
        from .learning_engine import OwnerGrantAuthority
        try:
            checked = validate_learning_record(record)
        except ContractError:
            raise LedgerError("record admission rejected") from None
        if checked["record_type"] not in {"feedback", "grader_calibration", "billing_attestation", "run_slot_confirmation"}:
            raise LedgerError("authenticated owner record type is unsupported")
        owner = self.owner
        def capture():
            ledger = owner.ledger
            with ledger._held():
                if not ledger._connection.in_transaction and ledger._owner_generation_locked() is None:
                    ledger._record_first_owner_key()
                return ledger._owner_generation_locked()
        generation = owner.call(capture, project_id=project_id, caller="owner_record_prepare", contain_errors=True)
        authority = OwnerGrantAuthority.for_state(self.state)
        if not authority.verify_owner_record(project_id, checked, signature):
            raise LedgerError("owner record signature is invalid")
        prepared = _PreparedOwnerRecord(owner.ledger._attestation_origin, project_id, canonical_json(checked),
                                        signature, generation, authority.signer_line)
        return owner.call(lambda: owner.ledger.commit_owner_record(project_id, record=checked, signature=signature,
                          _prepared=prepared, **options), project_id=project_id, caller="commit_owner_record", contain_errors=True)

    def __getattr__(self, name):
        owner = self.owner
        if name.startswith('_'):
            owner.guard()
            return getattr(owner.ledger, name)
        method = getattr(owner.ledger, name)
        if not callable(method) or name in {'close', 'repository'}:
            raise AttributeError(name)
        def invoke(*args, **kwargs):
            project = kwargs.get('project_id', args[0] if args else SYSTEM_PROJECT)
            if not (type(project) is str and project.startswith('prj_') and len(project) == 36):
                project = SYSTEM_PROJECT
            return owner.call(lambda: getattr(owner.ledger, name)(*args, **kwargs),
                              project_id=project, caller=name, contain_errors=True)
        return invoke


class LedgerOwner:
    def __init__(self, state=None, *, ledger=None):
        self._pid, self._thread = os.getpid(), threading.current_thread()
        self.queue = LedgerOwnerQueue()
        self._fatal = None
        self._running = False
        self._closed = False
        self.after_request = None
        self.ledger = LearningLedger(state, sole_owner=True) if ledger is None else ledger
        connection = getattr(self.ledger, "_connection", None)
        if connection is not None:
            connection.require_owner()
        self.state = state if state is not None else self.ledger.state
        self.ledger._owner = self
        self.state._ledger_owner = self
        self.client = _LedgerClient(self)
        # Never call execute_next on a temporary starter thread.
        self.queue.execute_next()

    def owns_thread(self):
        return self._pid == os.getpid() and self._thread is threading.current_thread()

    def guard(self):
        if self._pid != os.getpid() or self._thread is not threading.current_thread():
            raise RuntimeError('ledger wrong owner')

    def offer(self, operation, *, project_id=SYSTEM_PROJECT, caller='ledger', deadline_ns=None, contain_errors=False,
              _outcome=None):
        if deadline_ns is None:
            deadline_ns = ledger_waits.deadline_ns()
        def execute():
            outcome = run()
            if _outcome is not None:
                _outcome.append(outcome)  # The owner-thread caller's own answer, kept even if it ran late.
            return outcome
        def run():
            self._running = True
            connection = getattr(self.ledger, "_connection", None)
            before = getattr(connection, "total_changes", None)
            try:
                with ledger_waits.request_until(deadline_ns):
                    value = operation()
                # A native cursor/transaction must never leave this thread.
                if connection is not None and connection.in_transaction:
                    connection.rollback()
                    raise RuntimeError('ledger operation left a transaction open')
                if isinstance(value, sqlite3.Cursor):
                    value.close()
                    raise RuntimeError('ledger operation returned a cursor')
                if self.after_request is not None:
                    try:
                        # No SQL: published health is only marked stale (review 040-e1bw-r1 MED-2).
                        self.after_request(project_id, before)
                    except Exception:
                        pass  # Display publication cannot replace a committed operation's answer.
                return value
            except (LedgerError, StateError, TypeError, ValueError) as error:
                return _failure(error)
            except BaseException as error:
                if isinstance(error, Exception) and (contain_errors or _passing_refusal(error)):
                    return _failure(error, sqlite=True)
                self._fatal = error  # Kept only on the owner, raised immediately by pump.
                return _Failure(RuntimeError, 'ledger owner failed')
            finally:
                self._running = False
        try:
            return self.queue.offer(project_id, execute, caller=caller, busy_reason='operation_rejected',
                                    deadline_ns=deadline_ns)
        except LedgerQueueBusy:
            raise SQLReadError('sql_read_busy') from None

    def result(self, handle):
        try:
            result = self.queue.wait(handle)
        except LedgerQueueBusy:
            raise SQLReadError('sql_read_busy') from None
        return self._answer(result)

    @staticmethod
    def _answer(result):
        if type(result) is _Failure:
            raise _rebuild(result)
        return result

    def call(self, operation, *, project_id=SYSTEM_PROJECT, caller='ledger', contain_errors=False):
        if self.owns_thread() and self._running:
            return operation()  # Nested work belongs to the same whole transaction.
        if not self.owns_thread():
            # E1b-4: another thread waits at most its deadline; a late request still ends whole.
            return self.result(self.offer(operation, project_id=project_id, caller=caller,
                                          contain_errors=contain_errors))
        # The owner thread runs its own request (review 040-e1bw-r1 HIGH-1). Its deadline refuses it only before
        # it starts; once it has run, its answer is this caller's, as a direct call's was before the wiring. A
        # deferred completion it consumed or a ticket it registered is therefore never answered busy and lost.
        outcome = []
        handle = self.offer(operation, project_id=project_id, caller=caller, contain_errors=contain_errors,
                            _outcome=outcome)
        while not handle.handled.is_set():
            self.pump()
        if outcome:
            return self._answer(outcome[0])
        return self.result(handle)  # Refused before it ran: the existing busy answer, nothing consumed.

    def pump(self):
        self.guard()
        handled = self.queue.execute_next()
        if self._fatal is not None:
            error, self._fatal = self._fatal, None
            raise error
        return handled

    def pump_ready(self, limit):
        """Handle up to `limit` ready requests now (review 040-e1bw-r1 MED-3); returns how many ran."""
        handled = 0
        while handled < limit and self.pump():
            handled += 1
        return handled

    def close(self, *, record_exit=None):
        self.guard()
        if self._closed:
            return
        self._closed = True
        self.queue.close()
        if not self.queue.drain(0):
            raise RuntimeError('ledger owner closed during a request')
        try:
            if record_exit is not None:
                record_exit()
        finally:
            try:
                self.ledger.close()
            finally:
                if getattr(self.state, "_ledger_owner", None) is self:
                    self.state._ledger_owner = None
