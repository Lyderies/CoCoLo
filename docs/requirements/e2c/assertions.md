# E2c: existing assertion changes

None. No existing test was edited. New module: tests/test_project_start.py (20 tests).

Not an assertion change, for the primary: `tests.test_runtime_release.test_current_runtime_is_recorded_for_the_current_version`
fails on this branch by design. Runtime files changed (new `project_start.py`, learning_store, collector_client,
collector_contracts, client_runtime, cli, runtime_files), so the computed runtime digest is
`0eb7787ad120755ae5decf712f25a69c52878f3185989a261b85f9a1db34cdd0`. As briefed, `runtime-releases.json` keeps
`"0.8.59": "dcc18633d92e87eeabf2d52e3e8e0058a03e39621ccf8ff95b2d1e811846bfe9"` (E2a's in-branch refresh to 848d3e3a... was
put back) and no 0.8.60 key or VERSION bump was added: in this repo the version bump is the primary's release
bookkeeping commit (VERSION, runtime-releases.json, claude-agents previous-releases.json, catalogue test, CHANGELOG).
