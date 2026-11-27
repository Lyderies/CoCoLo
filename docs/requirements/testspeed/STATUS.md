# TESTSPEED status

Base `38a96538` (0.8.63), branch `fix/testspeed-cli-runtime`. DONE and committed; no product code changed (no
runtime digest re-record), no version bump, nothing pushed. Named modules only; no full run was made.

## Results (sum of each module's fragment seconds, which is what the 60 s budget counts)

| Module | before, 8 jobs | after, 8 jobs | before, 16 jobs + heavy modules | after, 16 jobs + heavy modules |
| --- | --- | --- | --- | --- |
| `test_cli_billing_runtime` | 51.6 | 16.9 | 70.7 (over) | 23.2 |
| `test_cli_open_client_runtime` | 57.2 | 26.1 | 66.7 (over) | 28.9 |
| `test_run_network` | 46.4 | 20.6 | 68.8 (over) | 28.9 |

TESTSPEED-1 (<= 45 s under load) holds for all three, with no budget failures and `metadata_errors: []`.
The loaded runs used `--jobs 16` with `test_project_driver`, `test_host_commands` and `test_learning_cli`
beside them. In both the before and the after loaded run one `test_learning_cli` test
(`test_status_recovers_collector_and_direct_command_restarts_it`, `status_account_starting`) failed under that
load; that module does not use any file changed here, so it is a separate load flake, not part of this unit.
`test_cli_observation_runtime`, `test_dispatch_client_runtime`, `test_lifecycle_cli_runtime` and
`test_service_host` (the other users of `dispatch_cli_fixture`) pass unchanged.

## Slow tests, one at a time (quiet machine), seconds before -> after

- billing: second_project_..._held 10.3 -> 1.4; C9 probe beside observation 4.8 -> 2.1; C9 observation between
  capture and admit 4.6 -> 1.4; invalid input and missing endpoint 4.1 -> 0.6; T2 reused probe 3.6 -> 1.1; both
  primary hosts 3.5 -> 1.2; explicit known request 3.4 -> 1.2; lost committed reply 3.4 -> 1.1; paid usage
  failure 3.2 -> 1.0. Module 44.3 -> 12.3.
- open-client: claude records terminal 18.0 -> 5.7; codex records terminal 17.1 -> 5.7; codex fresh open 15.7
  -> 4.8; claude fresh open 10.4 -> 4.4. Module 61.1 -> 20.5.
- run_network: real allowlist 18.8, real proxy down 18.4 (one run of 14.7), real release and sweep 10.1 (12.2)
  -> `test_real_scenarios_run_together` about 15 s for all three; `test_real_unreachable_podman_is_a_plain_refusal`
  2.3 unchanged. Module 50.4 -> 18.3.

## What changed

- `tests/dispatch_cli_fixture.py`: opt-in `COCOLO_TEST_ENDED_SERVICE_STARTS` (TESTSPEED-3).
- `tests/test_cli_billing_runtime.py`, `tests/test_cli_open_client_runtime.py`: opt in, shared per-class bytecode
  cache for the fresh CLIs (TESTSPEED-4), gate file instead of an 8 s sleep (TESTSPEED-5).
- `tests/test_run_network.py`: the three long real-podman scenarios run together (TESTSPEED-6).
- `scripts/test-timings.json`: existing keys of the three modules updated to the 8-job after run
  (module totals as recorded: billing 42.6 -> 16.9, open-client 23.7 -> 26.1, run_network 33.8 -> 20.6; the old
  open-client value predates its growth; per-test keys likewise).
- `docs/requirements/testspeed.md`, `testspeed/assertions.md` (every changed item old -> new), this file.

## Notes for the next session

- Further savings are possible only in the product: the egress proxy policy is pushed twice per `prepare_run`
  and the proxy is inspected twice in a row in `_join`/`_push_policy` (about 5 s of the run_network module);
  that code is a safety boundary and needs a runtime digest re-record, so it was not touched.
- `parallel_tests.py` workers run `python -B`, so every worker recompiles the runtime for each module; a shared
  `pycache_prefix` for workers would speed every module but is a framework change, not made here.
- Rerun: from the repository root with the test interpreter as `$PY` and a short `TMPDIR`,
  `$PY -B scripts/parallel_tests.py --jobs 8 --module codex:tests.test_cli_billing_runtime --module codex:tests.test_cli_open_client_runtime --module codex:tests.test_run_network --save-timings <file>`.
