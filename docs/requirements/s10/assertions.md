# S10 assertion ledger

One assertion changed (decision 355 wiring): `test_health_containers.CollectorWiringTests.test_HM_1_9_...`:
- check names `['self_check', 'containers']` -> `['self_check', 'containers', 'resources']`
- states `{'self_check': 'ok', 'containers': 'ok'}` -> `{..., 'resources': 'ok'}`
Reason: S10 wires a new `resources` check into the collector (requirement S10-HM6-8). The same test on the batch
also gains `workspaces` (S11); merge the two lists. No assertion was removed. One typed key was added to
`user_settings.UserSettings` (`memory_available_percent`); existing settings tests passed unchanged
(see STATUS.md / report for modules run).
