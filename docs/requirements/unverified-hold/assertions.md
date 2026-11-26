# 0.8.70 unverified hold: changed assertions

Decision 367 replaced the inferred marker (no `takeover_obligations` row) with a durable row written at adoption.

| Test | Old | New | Reason |
|---|---|---|---|
| `test_b4t_takeover.ChainedOutboxTests.test_UH_a_hold_the_takeover_left_unverified_is_its_own_plain_finding_not_overdue` | `assertIsNone(SELECT 1 FROM takeover_obligations WHERE kind='reservation' AND identity=<claim>)` | `assertEqual('unverified', SELECT original_request_digest FROM takeover_obligations WHERE kind='reservation' AND identity=<claim>)` | The marker source changed (decision 367): the refused hold now has its durable `unverified` row; the finding/overdue assertions are unchanged. |

No other existing assertion was changed or removed.
