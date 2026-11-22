# Assertion replacement ledger

## Step-2 review reconciliation: owner-approved entry-point probe corrections

TF-13 requires a real process boundary, not a zero exit for --help. Two new probe cases
incorrectly assumed argparse's SystemExit bypasses the hosts' mandatory exit fence. Work
stopped on those cases, the exact conflict was raised, and the owner explicitly selected
the named start-to-finish replacements. No shipped behavior or existing assertion changed.

| Removed assertion instance | Requirement | Replacement, unchanged |
|---|---|---|
| `tests/test_entrypoint_inventory.py:45` returncode == 0, collector_host case; `:46` usage output | TF-13/16 cold process boundary | `packages/codex-agents/tests/test_collector_exit.py:425` test_C10_a_shutdown_request_is_recorded: starts the actual collector, requests shutdown, observes exit and exact cause/steps |
| `tests/test_entrypoint_inventory.py:45` returncode == 0, project_host case; `:46` usage output | TF-13/16 cold process boundary | `packages/codex-agents/tests/test_project_service_startup.py:80` test_cli_starts_one_host_reuses_it_and_recovers_after_it_stops: starts actual host, checks active/paused/active states, reuse and restart |

No test function was deleted. The manifest moves these two cases to the named checks;
the cold-help assertions remain unchanged for entry points with that interface.
Evidence: `measurements/test-framework/review-replacement-run.json`, 46 tests, no failed workers.
The first attempt encountered the execution sandbox's Unix-socket bind denial; the authorized
rerun used actual local sockets. Neither the refusal nor its failed assertions was treated as passing.
