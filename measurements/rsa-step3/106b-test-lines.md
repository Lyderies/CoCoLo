# Decision 106b test lines

Only `packages/codex-agents/tests/test_rsa_step3.py` changed among test files. Every removed and added line is listed below; HEAD is efff396a. All existing setUp assertions and the account-side test are unchanged.

## Working tree added

```text
167:         decision_query = (
168:             "SELECT a.released_at_ms,a.release_digest,r.release_kind FROM active_run_capacity a "
169:             "JOIN run_holds r ON r.kind='run_slot' AND r.identity=a.handle_digest")
170:         self.decision_before = tuple(case.ledger._connection.execute(decision_query).fetchone())
```

## HEAD removed

```text
180: 
181:     @requirements('RSA3-3', why_not_scenario='Ledger-local transaction edge with synthetic cleaned registry custody: compare the durable late-release rule; no claim of process containment proof.')
182:     @unittest.expectedFailure  # F2: ledger-local release accepts a decided slot; owner decides the fix.
183:     def test_RSA3_3_ledger_local_late_completion_matches_owner_refusal(self):
184:         self.assertTrue(self.refused, 'ledger-local late completion accepted after crashed decision; owner release_capacity refuses claim_cleanup_conflict')
```

## Working tree added

```text
184:         self.decision_after = tuple(case.ledger._connection.execute(decision_query).fetchone())
185:         self.receipts = case.ledger._connection.execute(
186:             'SELECT receipt_digest FROM capacity_completions WHERE receipt_digest IS NOT NULL').fetchall()
187: 
188:     @requirements('RSA3-3', why_not_scenario='Ledger-local transaction edge with synthetic cleaned registry custody: late completion is kept by design per RS-A-2 / decision 106b; no claim of process containment proof.')
189:     def test_RSA3_3_ledger_local_late_completion_is_kept_by_design(self):
190:         self.assertFalse(self.refused)
191:         self.assertEqual(self.decision_before, self.decision_after)
192:         self.assertEqual(1, len(self.receipts))
```
