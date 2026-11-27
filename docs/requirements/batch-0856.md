# Batch 0.8.56 integration fixes

No existing test assertion was changed or removed.

- Runner: scripts/parallel_tests.py lifecycle_link_errors treats a tree without tests/e2e/requirements.json as having no lifecycle rows (as lifecycle_checks already does for a missing e2e_lifecycle.py); test_test_framework_r3 and _guards fixtures are unchanged.
- Stages/layers: registered codex:tests.test_holds_report, _holds_report_socket, _rsf_s1_ingress, _rsf_s1_ingress_socket.
- RSF-S1: refusal_steps EXACT gains runtime_upgrade_required (plain next step; no path), so H1b-2 holds.
- E1-9: its two InstallTests checks added to scripts/test-requirements.json and docs/requirements/e1.md.
