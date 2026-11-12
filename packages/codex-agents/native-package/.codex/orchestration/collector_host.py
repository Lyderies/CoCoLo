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

from codex_agents.account_host import AccountServiceHost  # noqa: E402
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
    def terminate(*_):
        # Raise, so a wait in startup or in an owner step cannot outlive the signal.
        raise SystemExit(0)

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, terminate)
    ledger = LearningLedger(state)
    server = CollectorServer(ledger)
    account = None
    try:
        server.start()
        try:
            account = AccountServiceHost(ledger, CollectorPaths(state.root))
        except HistoryError:
            account = None  # Hooks keep working; account clients report unavailable.
        checked = time.monotonic()
        while not server.wait(0.002 if account is not None else 0.5):
            if account is not None:
                account.serve_once()
            if time.monotonic() - checked >= 1:
                checked = time.monotonic()
                # A removed state root or endpoint means nobody can reach or own this collector.
                if not state.root.is_dir() or not state.socket.exists():
                    break
    except SystemExit:
        pass
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
