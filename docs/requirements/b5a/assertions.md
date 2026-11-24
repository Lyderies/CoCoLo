# B5a existing assertions

No existing assertion edited. No new account operation: RS-F S1's mutating
operation set, B4m's reply-field table and the three ACCOUNT_OPERATIONS pins
remain unchanged. `account.lost_run_closure` remains under the existing S1 gate.
B4s's live-child `crashed` refusal remains required and unchanged.

## Batch 4 integration (decision 219)
- `tests/test_crashed_settlement.py::test_stored_crash_proof_has_only_public_process_fields`: old: the hold's
  evidence equals exactly the four proof fields. New: the evidence also holds `closure` (S7's closure, asserted equal
  to the settlement row's); the remaining fields equal the same four. Reason: decision 219 (RSF-S7-1 for crashed).
