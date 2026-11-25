#!/usr/bin/env python3
"""The learning collector and the account services, as one owner process.

Started detached by the checked CLI entry (`learning-serve` hands over to this
file) with a fixed environment. The thread that opens the ledger owns the
account services and runs their loop; the collector thread serves hooks. Other
threads hand whole ledger operations to its bounded owner queue. Stops on SIGTERM, SIGINT or the
collector's own shutdown request.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

ORCHESTRATION_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from codex_agents import account_restart  # noqa: E402
from codex_agents.account_host import AccountServiceHost  # noqa: E402
from codex_agents.collector_exit import read_exit, write_exit  # noqa: E402
from codex_agents.collector_paths import CollectorPaths  # noqa: E402
from codex_agents.collector_runtime import require_authorized_collector_runtime  # noqa: E402
from codex_agents.history_contracts import HistoryError  # noqa: E402
from codex_agents.ledger_owner import LedgerOwner  # noqa: E402
from codex_agents.ledger_owner_queue import DEFAULT_LEDGER_QUEUE_LIMITS  # noqa: E402

# At most the queue's whole retained capacity per loop pass, so the account step still runs every pass.
OWNER_REQUESTS_PER_PASS = DEFAULT_LEDGER_QUEUE_LIMITS.requests
from codex_agents.learning_store import CollectorServer, LearningLedger, LearningState, sole_owner_open  # noqa: E402
from codex_agents.service_host import PassPacer  # noqa: E402
from codex_agents.shutdown_watchdog import ShutdownWatchdog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path)
    args = parser.parse_args(argv)
    os.chdir("/")  # Hold no working directory open for as long as the collector lives.
    state = LearningState(args.state_root, create=False)
    require_authorized_collector_runtime(state)
    # Why this collector stops, recorded for the next one to report (0.3.3, C10). Recorded from
    # the moment it owns the ledger; before that, the command that started it reports a failure.
    stopping = {"signal": False, "error": None, "removed": False, "requested": False, "restart": None}
    # O10 SD2: whatever a step does, the process ends within the bound, naming the step. O10 SD3-3:
    # made (its thread started) before the handlers exist, so the handler can start the bound.
    watchdog = ShutdownWatchdog(state.root, started_at_ms=None)
    watchdog.prepare()

    def terminate(*_):
        # O10 SD3 (ANALYSIS-0.4.0-SD3-shutdown.md): the raise below can be swallowed by code the
        # signal interrupts, which once lost the stop. So the stop is also recorded in a flag the main
        # loop checks on every pass (SD3-2), and the shutdown bound starts here (SD3-3; the step is
        # safe in a handler, see shutdown_watchdog). A repeated signal does not re-arm it.
        first = not stopping["signal"]
        stopping["signal"] = True
        if first:
            watchdog.step("signal_received")
        # Raise, so a wait in startup or in an owner step cannot outlive the signal.
        raise SystemExit(0)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, terminate)
    # S1-U9a: how long each start-up phase takes, reported by learning-status (milliseconds only).
    phase_start = time.monotonic()
    try:
        # E1b-1 (review 040-e1bw-r1 MED-1): check_same_thread, so SQLite itself refuses every other thread's
        # cursor, statement, close or backup on this connection, not only the Python-level guard.
        with sole_owner_open():
            ledger = LearningLedger(state)
        owner = LedgerOwner(state, ledger=ledger)
    except BaseException:
        watchdog.done()  # Nothing to shut down; nothing may fire later (SD3-3).
        raise
    phases = {"ledger_open": int((time.monotonic() - phase_start) * 1000)}
    phase_start = time.monotonic()
    try:
        server = CollectorServer(owner.client)
    except BaseException as error:
        # Owning the ledger already: say why it stops even here, close it, and raise as before (C10d).
        try:
            write_exit(state.root, started_at_ms=None, cause="signal" if stopping["signal"] else "error",
                       error=None if stopping["signal"] else error)
        except BaseException:
            pass
        finally:
            owner.close()
            watchdog.done()
        raise
    account = None
    server.account_services = "starting"  # O10 ST1: until the account services listen
    try:
        server.previous_exit = read_exit(state.root)
        last = account_restart.last_restart(state.root)  # E1: shown from start-up, account or not
        server.account_restart_last = last if type(last) is dict else None
        server.start()
        watchdog.started_at_ms = getattr(server, "_started_at_ms", None)
        phases["collector"] = int((time.monotonic() - phase_start) * 1000)
        phase_start = time.monotonic()
        try:
            account = AccountServiceHost(ledger, CollectorPaths(state.root))
            server.account_lanes = account.health_notes  # S1-H1a: read without the account owner
            server.account_services = "running"
        except HistoryError:
            account = None  # Hooks keep working; account clients report unavailable.
            server.account_services = "unavailable"
        phases["account"] = int((time.monotonic() - phase_start) * 1000)
        server.startup_phases = phases
        checked = restart_checked = time.monotonic()
        pacer, engaged = PassPacer(), False
        while True:
            # MED-3 (review 040-e1bw-r1): wake as soon as a request is offered, then handle every ready
            # request (bounded by the queue's own capacity) before the account step, not one per 2 ms tick.
            # S13d-1: a pass every 2 ms measured as a busy-poll; once no account client or request has been
            # held for a while the wait grows (PassPacer). Owner requests still wake at once (the work signal).
            owner.queue.wait_for_work(pacer.wait_seconds(engaged))
            if server.wait(0):
                break
            if stopping["signal"]:
                break  # O10 SD3-2: a stop signal whose exception was swallowed somewhere.
            owner.pump_ready(OWNER_REQUESTS_PER_PASS)
            # Owner requests do not count as engagement: the work signal wakes the wait for them at once.
            engaged = account is not None and account.serve_once() is True
            if time.monotonic() - checked >= 1:
                checked = time.monotonic()
                # A removed state root or endpoint means nobody can reach or own this collector.
                if not state.root.is_dir() or not state.socket.exists():
                    stopping["removed"] = True
                    break
            if account is not None and time.monotonic() - restart_checked >= account_restart.CHECK_EVERY_SECONDS:
                restart_checked = time.monotonic()
                # S1-H1c (owner: restart automatically): only when stuck and nothing is running.
                # The decision and the stop are made here, between two owner steps: only the owner admits
                # claims and probes, so none can be admitted in between (review 040-s1-h1c, C2).
                now_ms, mono_ns = time.time_ns() // 1_000_000, time.monotonic_ns()
                last = account_restart.last_restart(state.root)
                server.account_restart_last = last if type(last) is dict else None  # D5
                # O0c M8-M10: and only once every child the account's lanes started is proven gone.
                decision = account_restart.check(account.health_notes, owner.client.machine_idle, last,
                                                 now_ms=now_ms, mono_ns=mono_ns,
                                                 read_children=account.unended_children)
                if decision["action"] == "restart" and not account_restart.record_restart(
                        state.root, lane=decision["lane"], cause=decision["cause"], now_ms=now_ms, mono_ns=mono_ns):
                    decision = {**decision, "action": "suppressed", "reason": "unrecorded"}  # C4/D6
                server.account_restart = decision
                if decision["action"] == "restart":
                    stopping["restart"] = decision["cause"]
                    break
        # Read before teardown: server.stop() sets the same flag (C10d). A self-restart is a request:
        # the exit record keeps a cause older readers know (review 040-s1-h1c, C5).
        stopping["requested"] = server._stop.is_set() or stopping["restart"] is not None
    except SystemExit as stop:
        if not stopping["signal"]:
            stopping["error"] = stop  # Not from a signal: recorded as an error (C10b).
    except BaseException as error:
        stopping["error"] = error
        raise
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)  # Finish the shutdown that was asked for.
        # Each step is attempted even if an earlier one fails, so the ledger is
        # always closed; the entry below then ends the process, so a worker
        # thread that did not stop cannot keep it alive.
        # SD1: each step is timed, so a slow shutdown shows which step it was.
        stop_ms = {"account_close": 0, "account_drain": 0, "server_stop": 0}
        step_start = time.monotonic()
        if account is not None:
            try:
                watchdog.step("account_close")
                account.close()
                stop_ms["account_close"] = int((time.monotonic() - step_start) * 1000)
                step_start = time.monotonic()
                watchdog.step("account_drain")
                end = time.monotonic() + 10
                while not account.drained() and time.monotonic() < end:
                    time.sleep(0.01)
                stop_ms["account_drain"] = int((time.monotonic() - step_start) * 1000)
            except Exception:
                pass
        step_start = time.monotonic()
        watchdog.step("server_stop")
        try:
            server.stop()
        except Exception:
            pass
        stop_ms["server_stop"] = int((time.monotonic() - step_start) * 1000)
        owner.queue.close()
        if not owner.queue.drain(0):
            raise RuntimeError("ledger requests remain during exit recording")
        watchdog.step("exit_record")
        try:
            # Best effort, and never in the way of closing the ledger or the exit code. Decided
            # once the collector thread has stopped, so an error it raised late still counts;
            # written while this process still holds the ledger, so the next collector reads it.
            error = stopping["error"] if stopping["error"] is not None else server.thread_error
            if error is not None:
                cause = "error"
            elif stopping["signal"]:
                cause = "signal"
            elif stopping["removed"]:
                cause = "state_removed"
            else:
                cause = "requested" if stopping["requested"] else "thread_ended"
            if state.root.is_dir():
                write_exit(state.root, started_at_ms=server._started_at_ms, cause=cause, error=error, stop_ms=stop_ms)
        except BaseException:
            pass
        finally:
            watchdog.step("ledger_close")
            owner.close()
            watchdog.done()
    print(json.dumps({"ready": True, "stopped": True, "account_services": account is not None}, sort_keys=True))
    return 0


if __name__ == "__main__":
    code = 2
    try:
        code = main()
    finally:
        sys.stdout.flush()
        os._exit(code)
