# S11 status - HM-2a

Branch `feat/s11-hm2a`, base `ce81bd96` (0.8.67). Requirements and 23 red tests are committed first; the module,
caps and registrations follow. Not merged, not installed, VERSION and digest catalogue unchanged.

- Code: `codex_agents/health_workspaces.py` (new; reads launch journals read-only; pending reader injected).
- Tests: `tests/test_health_workspaces.py` (25), mapped in [s11-hm2a.md](../s11-hm2a.md); registered in
  `scripts/test-layers.json`, `test-stages.json`, `test-timings.json`, `scripts/test-requirements.json`
  (source `tests/requirements/s11.json`); count limits in `tests/lifetime_caps.json`.
- For the primary after S5: wire `health_workspaces.register(monitor, state_root, pending=<reader>)` in
  `learning_store.py` next to the container check, where `<reader>` returns the `workspace` paths of preparation hold
  rows whose evidence says `removal: pending`, read through the owner thread (E1b-1, decision 311), or None when it
  cannot be read. Until then the check is not part of the collector.
- Run: `scripts/parallel_tests.py --jobs 4 --module codex:tests.test_health_workspaces --module
  codex:tests.test_health_monitor --module codex:tests.test_codex_preparation --module
  codex:tests.test_lifetime_cap_guard` with a short TMPDIR; expect `metadata_errors: []`.

- `runtime_files.py` lists the new module (a test keeps the list equal to the package). The runtime digest in `runtime-releases.json` is now stale by design: the primary bumps VERSION and records it at merge (`tests.test_runtime_release` fails until then).
