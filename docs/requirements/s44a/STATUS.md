# S44a status

State: built and tested on branch `feat/s44a-usage` (base `a737ae9d`); not merged, not installed, not proven on the owner's
install (that is the primary's step: run `learning-usage-report --project-root . --since .. --until ..` from the project after the
install).

Done: S44A-1 (the `.` root; named refusals), S44A-2 (per-run usage both providers in the report as `usage_coverage`; status `run_usage` from the service via `history.run_usage`),
S44A-3 (`window_data`). Requirements and tests: `docs/requirements/s44a.md`. No existing assertion changed; two op-set/request-list pins changed (see assertions.md).

Files: `project_usage_report.py`, `cli.py` (status `run_usage`), `refusal_steps.py` (usage refusal steps), tests in
`packages/codex-agents/tests/test_s44a_usage_install.py`, registry entries in `scripts/test-*.json` and `tests/requirements/s44a.json`,
`docs/service-api-development.md`.

Not done on purpose: `ingestion.coverage.root_usage` and `child_usage` stay false (hook capability flags that gate candidate
promotion and are checked by `validation.py`).

To resume: run `python -B scripts/parallel_tests.py --jobs 4 --affected a737ae9d` from this worktree.

Correction (after review r1): S44A-2 restored as originally written. Status gets per-provider coverage from the running
service via the new read-only operation `history.run_usage` (null + plain reason when the service is not running); the
client opens no database. Partial measurements are `partial` (MED 4); outcome-only runs count in the recorded span (MED 2);
status behaviour is pinned through the real `learning-status` output (MED 3). Existing assertions changed: two op-set /
request-list pins, listed in assertions.md.

Fix (confirm r3c): the single 1 s deadline now covers all service work in `service_run_usage`: an injectable clock
(`_monotonic`) is checked in the Python row/decoding loops of `_rows` and `_service_reply` and once more before the reply;
expiry returns the existing partial reply (truncated, partial_reason, both providers unknown/null). The SQLite progress
handler and its finally-clear stay. Test: `test_s44a_2_the_one_deadline_covers_python_decoding_after_the_last_sql`.
lifetime_caps.json key moved `service_run_usage` -> `_service_reply` (the row-cap slice moved with it; no assertion changed).
