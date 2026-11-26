# S10 assertion ledger

One assertion changed (decision 355 wiring): `test_health_containers.CollectorWiringTests.test_HM_1_9_...`:
- check names `['self_check', 'containers']` -> `['self_check', 'containers', 'resources']`
- states `{'self_check': 'ok', 'containers': 'ok'}` -> `{..., 'resources': 'ok'}`
Reason: S10 wires a new `resources` check into the collector (requirement S10-HM6-8). The same test on the batch
also gains `workspaces` (S11); merge the two lists. No assertion was removed. One typed key was added to
`user_settings.UserSettings` (`memory_available_percent`); existing settings tests passed unchanged
(see STATUS.md / report for modules run).

## Review r1 fix round (reviews/040-s10-r1, NOT-READY)
No existing S10 assertion was changed or removed (one driver-test assertion: see the correction below). New tests: test_health_resources (2 added:
long/unprintable paths, invalid settings finding) and the new module test_health_resources_open (4, real CLI ->
account admission -> run driver path). One reply-field change, no test pinned it: the successful open report no longer
has `resource_refusal` (a refused open now raises and the CLI prints `ready:false, state:refused, reason_code:resource_low,
error:<plain reason>`, exit 2); a successful open gains `settings_warning` only while the settings file is invalid.

### Correction after the first module run (same round)
One existing assertion changed:
- `test_project_driver.StartupRecoverOpenTests.test_a_failed_start_up_recovery_refuses_the_waiting_register_as_an_unavailable_service`:
  driver replies seen by the client `[('refused', 'history_service_unavailable')]` ->
  `[('ready', None), ('refused', 'history_service_unavailable')]`.
  Reason: the review r1 HIGH fix adds one exchange before account admission (`run.check`, answered ready on a healthy
  disk). The register's answer, the requirement under test (RS-B-4), is unchanged and still checked; the open is still
  left unregistered and dispatch still answers dispatch_unavailable.
Also registered: `health_resources.py::finding_text` in tests/lifetime_caps.json (a per-sentence text bound, not a count).
