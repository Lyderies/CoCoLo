#!/usr/bin/env python3
"""The learning collector and the account services, as one owner process.

Started detached by the checked CLI entry (`learning-serve` hands over to this
file) with a fixed environment. The thread that opens the ledger owns the
account services and runs their loop; the collector thread serves hooks. Both
keep the ledger's existing lock discipline. Stops on SIGTERM, SIGINT or the
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
from codex_agents.learning_store import CollectorServer, LearningLedger, LearningState  # noqa: E402


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

    def terminate(*_):
        # Raise, so a wait in startup or in an owner step cannot outlive the signal.
        stopping["signal"] = True
        raise SystemExit(0)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, terminate)
    # S1-U9a: how long each start-up phase takes, reported by learning-status (milliseconds only).
    phase_start = time.monotonic()
    ledger = LearningLedger(state)
    phases = {"ledger_open": int((time.monotonic() - phase_start) * 1000)}
    phase_start = time.monotonic()
    try:
        server = CollectorServer(ledger)
    except BaseException as error:
        # Owning the ledger already: say why it stops even here, close it, and raise as before (C10d).
        try:
            write_exit(state.root, started_at_ms=None, cause="signal" if stopping["signal"] else "error",
                       error=None if stopping["signal"] else error)
        except BaseException:
            pass
        finally:
            ledger.close()
        raise
    account = None
    server.account_services = "starting"  # O10 ST1: until the account services listen
    try:
        server.previous_exit = read_exit(state.root)
        server.start()
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
        while not server.wait(0.002 if account is not None else 0.5):
            if account is not None:
                account.serve_once()
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
                now_ms = time.time_ns() // 1_000_000
                decision = account_restart.check(account.health_notes, ledger.machine_idle,
                                                 account_restart.last_restart(state.root), now_ms=now_ms)
                if decision["action"] == "restart" and not account_restart.record_restart(
                        state.root, lane=decision["lane"], cause=decision["cause"], now_ms=now_ms):
                    decision = {**decision, "action": "suppressed"}  # C4: not kept, so not restarted
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
        if account is not None:
            try:
                account.close()
                end = time.monotonic() + 10
                while not account.drained() and time.monotonic() < end:
                    time.sleep(0.01)
            except Exception:
                pass
        try:
            server.stop()
        except Exception:
            pass
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
                write_exit(state.root, started_at_ms=server._started_at_ms, cause=cause, error=error)
        except BaseException:
            pass
        finally:
            ledger.close()
    print(json.dumps({"ready": True, "stopped": True, "account_services": account is not None}, sort_keys=True))
    return 0


if __name__ == "__main__":
    code = 2
    try:
        code = main()
    finally:
        sys.stdout.flush()
        os._exit(code)
