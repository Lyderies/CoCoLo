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

## Step-2 r1 assertion replacements and fixture corrections

- Reviewed `tests/test_test_framework.py:266` exact `max_examples == 1000` for soak -> `tests/test_test_framework.py:265` `>= 1000`, exactly TF-17; dev/ci exact-value checks remain.
- The newly added default-profile witness compared distinct Hypothesis settings objects by identity/equality; it now checks default ci example count, derandomization and deadline fields at `tests/test_test_framework.py:272`. No production assertion was removed.
- The new fail-closed runner fixture now uses its own small root at `tests/test_test_framework_guards.py:454`; both nonzero-exit and trusted-base diagnostic assertions remain, with a stricter 20s outer bound. No validation method is replaced in this probe.
- Process denial markers are read before temporary-root teardown; caught-denial probes now catch Exception and assert reached-marker, nonzero exit and recorded violations. Sidecar-symlink rejection now also checks original golden and symlink. These strengthen existing assertions.
- No existing test function was deleted. All other reviewed assertion expressions remain; the two earlier host help-probe replacements above remain the named start-to-finish checks.
