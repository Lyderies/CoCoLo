#!/usr/bin/env python3
"""Service host for one registered project: the owner process behind its sockets.

Started detached by the checked CLI entry, never by a request. It refuses
unless this installed runtime is the one the owner authorized, serves until
SIGTERM or SIGINT, and prints one closed status line when it stops.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ORCHESTRATION_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ORCHESTRATION_ROOT))

from codex_agents import event_shapes  # noqa: E402
from codex_agents.collector_contracts import CollectorError  # noqa: E402
from codex_agents.collector_paths import CollectorPaths, StateError  # noqa: E402
from codex_agents.collector_runtime import ensure_collector, require_authorized_collector_runtime  # noqa: E402
from codex_agents.project_service import serve_project  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path)
    parser.add_argument("--project-root", required=True, type=Path)
    args = parser.parse_args(argv)
    os.chdir("/")  # Hold no working directory open for as long as the host lives.
    try:
        state = CollectorPaths(args.state_root)
        require_authorized_collector_runtime(state)
        # The registry lookup needs the collector; start it the usual way if it is down.
        ensure_collector(state, ORCHESTRATION_ROOT / "cli.py", project_root=args.project_root)
        # Frame shapes are recorded only while the owner's flag file is in this folder.
        event_shapes.configure(state.root / "diagnostics")
        report = serve_project(args.state_root, args.project_root)
    except (CollectorError, StateError):
        report = {"ready": False, "reason_code": "runtime_unauthorized"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("ready") else 2


if __name__ == "__main__":
    code = 2
    try:
        code = main()
    finally:
        # The host has closed what it owns; no worker thread may keep the process alive.
        sys.stdout.flush()
        os._exit(code)
