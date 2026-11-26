# S44a status

State: built and tested on branch `feat/s44a-usage` (base `a737ae9d`); not merged, not installed, not proven on the owner's
install (that is the primary's step: run `learning-usage-report --project-root . --since .. --until ..` from the project after the
install).

Done: S44A-1 (the `.` root; named refusals), S44A-2 (per-run usage both providers in the report as `usage_coverage`; status `run_usage` only names the report, because the status client opens no database),
S44A-3 (`window_data`). Requirements and tests: `docs/requirements/s44a.md`. No existing assertion changed; two tests of this step were reshaped (see assertions.md, 'Correction').

Files: `project_usage_report.py`, `cli.py` (status `run_usage`), `refusal_steps.py` (usage refusal steps), tests in
`packages/codex-agents/tests/test_s44a_usage_install.py`, registry entries in `scripts/test-*.json` and `tests/requirements/s44a.json`,
`docs/service-api-development.md`.

Not done on purpose: `ingestion.coverage.root_usage` and `child_usage` stay false (hook capability flags that gate candidate
promotion and are checked by `validation.py`).

To resume: run `python -B scripts/parallel_tests.py --jobs 4 --affected a737ae9d` from this worktree.

Correction: status_run_usage() read databases in the status client and broke test_health_monitor / test_cli_status_client_boundary (non-JSON traceback). Fixed; both green with test_s44a_usage_install and test_usage_report.
