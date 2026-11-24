# Changed existing test lines

Baseline: HEAD `b6ca996f` (line numbers below are baseline lines; inserted lines use the insertion point).
Every changed or inserted existing-test source line is recorded, including helper imports and decorators.
New RT-RX modules and new golden provenance sidecars are additions, not changed existing-test lines.
No golden JSON changed. No test line outside approved classes 124.2/124c, 124b, 124d and 124e was edited.

## 1. packages/codex-agents/tests/test_admission_metadata.py:89

Old line: `"        horizon = admission_metadata.EVIDENCE_HORIZON_MS"`
New line (89): `"        horizon = admission_metadata.FIVE_HOURS_MS"`
Requirement/decision: RT-RX-4 / 124.2, 124c (resetless HEADROOM lifetime only).

## 2. packages/codex-agents/tests/test_provider_restrictions.py:13

Old line: `null`
New line (13): `"from test_golden import legacy_merged_record"`
Requirement/decision: RT-RX-3/4/5/20 / 124d (legacy provenance projection).

## 3. packages/codex-agents/tests/test_provider_restrictions.py:69

Old line: `"        horizon = restrictions.EVIDENCE_HORIZON_MS"`
New line (70): `"        horizon = restrictions.FIVE_HOURS_MS"`
Requirement/decision: RT-RX-4 / 124.2, 124c (resetless HEADROOM lifetime only).

## 4. packages/codex-agents/tests/test_provider_restrictions.py:70

Old line: `"        self.assertEqual(86_400_000, horizon)"`
New line (71): `"        self.assertEqual(18_000_000, horizon)"`
Requirement/decision: RT-RX-4 / 124.2, 124c (resetless HEADROOM lifetime only).

## 5. packages/codex-agents/tests/test_provider_restrictions.py:81

Old line: `"                self.assertEqual(record(status, expected, 500), result)"`
New line (82): `"                self.assertEqual(record(status, expected, 500), legacy_merged_record(result))"`
Requirement/decision: RT-RX-3/4/5/20 / 124d (legacy provenance projection).

## 6. packages/codex-agents/tests/test_provider_restrictions.py:105

Old line: `"            \" assert result['windows'][0]['resets_at'] == 2000 + 86400000 + 1\\n\""`
New line (106): `"            \" assert result['windows'][0]['resets_at'] == 2000 + 18000000 + 1\\n\""`
Requirement/decision: RT-RX-4 / 124.2, 124c (resetless HEADROOM lifetime only).

## 7. packages/codex-agents/tests/test_providers.py:2631

Old line: `"        self.assertEqual(rejected_at + 24 * 3_600_000 + 1, merged[\"windows\"][0][\"resets_at\"])"`
New line (2631): `"        self.assertEqual(rejected_at + 5 * 3_600_000 + 1, merged[\"windows\"][0][\"resets_at\"])"`
Requirement/decision: RT-RX-4 / 124.2, 124c (resetless HEADROOM lifetime only).

## 8. packages/codex-agents/tests/test_providers.py:2637

Old line: `"        horizon = providers.EVIDENCE_HORIZON_MS"`
New line (2637): `"        horizon = providers.FIVE_HOURS_MS"`
Requirement/decision: RT-RX-4 / 124.2, 124c (resetless HEADROOM lifetime only).

## 9. packages/codex-agents/tests/test_providers.py:2671

Old line: `"        horizon = providers.EVIDENCE_HORIZON_MS"`
New line (2671): `"        horizon = providers.FIVE_HOURS_MS"`
Requirement/decision: RT-RX-4 / 124.2, 124c (resetless HEADROOM lifetime only).

## 10. packages/codex-agents/tests/test_providers.py:2726

Old line: `"        for at in (reset * 1000 + 1, rejected_at + providers.EVIDENCE_HORIZON_MS):"`
New line (2726): `"        for at in (reset * 1000 + 1, rejected_at + providers.FIVE_HOURS_MS):"`
Requirement/decision: RT-RX-4 / 124.2, 124c (resetless HEADROOM lifetime only).

## 11. packages/codex-agents/tests/test_providers.py:2728

Old line: `"        self.assertIsNone(providers._headroom_state(\"claude\", merged, rejected_at + providers.EVIDENCE_HORIZON_MS + 1))"`
New line (2728): `"        self.assertIsNone(providers._headroom_state(\"claude\", merged, rejected_at + providers.FIVE_HOURS_MS + 1))"`
Requirement/decision: RT-RX-4 / 124.2, 124c (resetless HEADROOM lifetime only).

## 12. packages/codex-agents/tests/test_routing_judgment.py:6

Old line: `null`
New line (6): `"import os"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 13. packages/codex-agents/tests/test_routing_judgment.py:6

Old line: `null`
New line (7): `"from test_framework import requirements"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 14. packages/codex-agents/tests/test_routing_judgment.py:6

Old line: `null`
New line (8): `"from test_golden import compare_json"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 15. packages/codex-agents/tests/test_routing_judgment.py:69

Old line: `null`
New line (72): `"    @requirements(\"RT-RX-21\", why_not_scenario=\"Pure legacy route golden comparison.\")"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 16. packages/codex-agents/tests/test_routing_judgment.py:70

Old line: `"        old = json.loads(LEGACY_GOLDEN.read_text())"`
New line (None): `null`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 17. packages/codex-agents/tests/test_routing_judgment.py:72

Old line: `"        high = {key: value for key, value in old.items()"`
New line (75): `"        high = {key: value for key, value in actual.items()"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 18. packages/codex-agents/tests/test_routing_judgment.py:75

Old line: `"        self.assertEqual(high, {key: actual[key] for key in high})"`
New line (78): `"        for path in (LEGACY_GOLDEN, GOLDEN_PATH):"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 19. packages/codex-agents/tests/test_routing_judgment.py:76

Old line: `"        shipped_golden = json.loads(GOLDEN_PATH.read_text())"`
New line (79): `"            compare_json(path=path, actual=high, keys=high, requirement=\"RT-RX-21\","`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 20. packages/codex-agents/tests/test_routing_judgment.py:77

Old line: `"        self.assertEqual(high, {key: shipped_golden[key] for key in high})"`
New line (80): `"                         declared={\"RT-RX-21\"}, known={\"RT-RX-21\"}, environ=os.environ)"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 21. packages/codex-agents/tests/test_routing_judgment.py:172

Old line: `null`
New line (175): `"    @requirements(\"RT-RX-21\", why_not_scenario=\"Pure legacy route golden comparison.\")"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 22. packages/codex-agents/tests/test_routing_judgment.py:176

Old line: `"        self.assertEqual(json.loads(LEGACY_GOLDEN.read_text()), golden_buckets(legacy))"`
New line (180): `"        compare_json(path=LEGACY_GOLDEN, actual=golden_buckets(legacy), requirement=\"RT-RX-21\","`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 23. packages/codex-agents/tests/test_routing_judgment.py:176

Old line: `null`
New line (181): `"                     declared={\"RT-RX-21\"}, known={\"RT-RX-21\"}, environ=os.environ)"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 24. packages/codex-agents/tests/test_routing_risk.py:13

Old line: `null`
New line (13): `"import os"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 25. packages/codex-agents/tests/test_routing_risk.py:13

Old line: `null`
New line (14): `"from test_framework import requirements"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 26. packages/codex-agents/tests/test_routing_risk.py:13

Old line: `null`
New line (15): `"from test_golden import compare_json, legacy_route_availability"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 27. packages/codex-agents/tests/test_routing_risk.py:83

Old line: `"        return route_task(task, policy, primary_provider=primary)"`
New line (86): `"        return legacy_route_availability(route_task(task, policy, primary_provider=primary), task, policy)"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 28. packages/codex-agents/tests/test_routing_risk.py:299

Old line: `null`
New line (302): `"    @requirements(\"RT-RX-21\", why_not_scenario=\"Pure policy cross product with the legacy availability projection.\")"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 29. packages/codex-agents/tests/test_routing_risk.py:300

Old line: `"        expected = json.loads(GOLDEN_PATH.read_text())"`
New line (304): `"        compare_json(path=GOLDEN_PATH, actual=golden_buckets(self.shipped), requirement=\"RT-RX-21\","`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 30. packages/codex-agents/tests/test_routing_risk.py:301

Old line: `"        actual = golden_buckets(self.shipped)"`
New line (305): `"                     declared={\"RT-RX-21\"}, known={\"RT-RX-21\"}, environ=os.environ)"`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 31. packages/codex-agents/tests/test_routing_risk.py:302

Old line: `"        self.assertEqual(sorted(expected), sorted(actual))"`
New line (None): `null`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 32. packages/codex-agents/tests/test_routing_risk.py:303

Old line: `"        changed = sorted(name for name in expected if expected[name] != actual[name])"`
New line (None): `null`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 33. packages/codex-agents/tests/test_routing_risk.py:304

Old line: `"        self.assertEqual([], changed, \"routes differ from the approved shipped policy\")"`
New line (None): `null`
Requirement/decision: RT-RX-21 / 124b (golden comparison, projection and requirement decoration).

## 34. packages/codex-agents/tests/test_admission_metadata.py:102

Old line: `"                    arguments['headroom'].append([headroom, '9' * 64])"`
New line (102): `"                    arguments['headroom'] = [[headroom, '9' * 64]]"`
Requirement/decision: RT-RX-3/4/5 / 124e (boundary setup uses only the appended restrictive record; boundary assertions and expected-value lines unchanged).

(Entry 35 withdrawn by the primary: the billing line is back to its original append so the fixture keeps its fresh billing record; only the headroom list is isolated under 124e.)
