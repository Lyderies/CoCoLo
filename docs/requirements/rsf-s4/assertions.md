# RSF S4: required existing assertion changes — stopped

The user instructed: “Never edit an existing test assertion (if one must change,
stop and list old -> new in docs/requirements/rsf-s4/assertions.md).”

Source review found assertions that directly require the 24-hour policy M4 R3
replaces. Work stopped before adding tests or changing production code. No
existing assertion or test file was edited. Entries below are proposed changes,
not changes applied or permission inferred. Test paths are relative to
`packages/codex-agents/tests/`.

## Evidence-poor schema-six upgrade fixture

`LegacyRunsFixture.upgrade_with_runs` in `test_legacy_capacity_upgrade.py:32-53`
inserts historical bindings with no retained dispatch/unsent/process proof.
Pending/consumed alternation and `client_version` provide no such proof.

| Existing location | Old → new required by RSF-S4-1 / RSF-S4-4 |
| --- | --- |
| `test_legacy_capacity_upgrade.py:71` | `assertEqual(3, rows)` crashed releases → `assertEqual(0, rows)`; age alone releases none. |
| `test_legacy_capacity_upgrade.py:74` | `assertEqual(0, self.counted(ledger))` → `assertEqual(3, self.counted(ledger))`. |
| `test_legacy_capacity_upgrade.py:75` | `assertTrue(ledger.drained())` → `assertFalse(ledger.drained())`. |
| `test_legacy_capacity_upgrade.py:79` | `assertEqual(1, self.counted(ledger))` → `assertEqual(2, self.counted(ledger))`; the old claim also remains held. |
| `test_legacy_capacity_upgrade.py:83` | `assertEqual(0, self.counted(ledger))` after another day → `assertEqual(2, self.counted(ledger))`. |
| `test_legacy_capacity_upgrade.py:84` | `assertTrue(ledger.drained())` after another day → `assertFalse(ledger.drained())`. |
| `test_legacy_capacity_upgrade.py:92` | `assertEqual(claimed + LEGACY_RUN_MAX_MS + 1, status["next_eligible"])` → `assertIsNone(status["next_eligible"])`. |
| `test_legacy_capacity_upgrade.py:109` | `assertEqual(0, self.counted(ledger))` after 24 hours, for both client labels → `assertEqual(1, self.counted(ledger))`; neither fixture retains a deadline or release proof. |
| `test_legacy_capacity_upgrade.py:142-143` | `assertEqual({"holding": 2, "finished_by_ms": claimed[-1] + LEGACY_RUN_MAX_MS}, ledger.earlier_version_runs())` → `assertEqual({"holding": 3, "finished_by_ms": None}, ledger.earlier_version_runs())` if retaining that two-field status contract. |
| `test_legacy_capacity_upgrade.py:147` | `assertEqual({"holding": 0, "finished_by_ms": None}, ledger.earlier_version_runs())` → `assertEqual({"holding": 1, "finished_by_ms": None}, ledger.earlier_version_runs())`. |

The constant-value assertion at `test_legacy_capacity_upgrade.py:97` does not
itself need changing: a constant can remain for unrelated conservative checks,
but cannot authorize automatic release of evidence-poor legacy claims.

## Evidence-only status validation

`test_legacy_capacity_upgrade.py:160-165` currently includes
`{"holding": 1, "finished_by_ms": None}` among malformed responses and expects
`CollectorError` for every item. Old → new: that item must be accepted for an
evidence-only held run, with an equality assertion preserving the two values;
the other malformed cases remain refused. This requires changing the existing
expectation for one subtest even though the common `assertRaises` is unchanged.
It cannot be hidden by returning a fabricated finite finish time.

## Schema-eight capacity upgrade

`test_active_run_capacity.py:297-306` upgrades an existing bridged claim and
expects it to age out using claim + 24 hours:

- `:306`: `assertEqual(0, ...['runs_held_without_deadline'])` after that age →
  replace claim-age acceptance with assertions against the retained dispatch's
  deadline + one-hour grace. If no deadline proof survives the fixture, expect
  `1` even after 24 hours.
- `:303`: `assertEqual(1, ...['runs_held_without_deadline'])` → distinguish a
  retained bridged deadline (`runs_held_without_deadline == 0`, slot still held
  before the deadline/grace) from missing proof (`== 1`, held for evidence).
  The exact fixture classification must be verified when work resumes; the
  schema-six conflicts above already require stopping independently.

No red/green result is claimed for these proposed changes. See
[validation.md](validation.md) for pending named-module commands.

Approved: primary decision 164

Implementation resolution: the schema-eight fixture retains one validated
`dispatch_intent` for its run. Its `:303` expectation is therefore `0` without a
deadline. The `:304-306` claim-age check is replaced by recorded deadline + grace
−1, equality and +1 checks (holding 1, 1, 0; without-deadline count 0 throughout),
as approved above. Only the listed assertion changes were applied to existing
tests; existing test names and unrelated expectations remain unchanged.

## Not listed by the builder; found and mapped by the primary (decision 166)
tests/test_active_run_capacity.py (deadline/legacy test): old: `runs_held_without_deadline == 1`, then 0 after
claimed_at + LEGACY_RUN_MAX_MS (24 h). new: the bridged fixture carries a dispatch_intent deadline, so
`runs_held_without_deadline == 0` throughout, and `earlier_version_runs()['holding']` is 1 at deadline+grace-1 and at
the boundary, 0 at +1 ms. Same M4 R3 bridged rule as decision 164 (deadline + crash grace replaces the 24 h day).

## Security review round (Claude review, ready-with-corrections)
No assertion changed in value. One test renamed (nothing in scripts/test-requirements.json or other catalogues referenced
its old name): tests/test_legacy_capacity_upgrade.py
`test_RS_A_F8_a_recent_earlier_run_is_eligible_a_day_after_its_claim` -> `test_RS_A_F8_a_recent_earlier_run_has_no_finite_eligible_time`
(it asserts `next_eligible is None`, which is what it already did).
New tests: tests/test_rsf_s4_slots.py `LegacySlotCorrectionTests` (red first, then green).
Behaviour (owner screen, decision 36): the shown command carries project root, state root, run id and handle digest, as
two steps in one line (unsigned step prints the signing payload, then the same command with `--signature owner-signature.bin`).
Every held slot without a recorded deadline has one (reason: waiting for process evidence or owner confirmation for a native
slot, dispatch evidence is missing otherwise, including slots with no binding row). A confirmation names (run id, handle
digest); a record naming only a run id is honoured only if it matches exactly one slot. Owner confirmation can release a
slot whose child may still run; that is the owner's call. Process proof (journal, lock, process scan) is read only by the
sweep/adoption (which writes a durable release); counts, status and admission use the deadline rule only, so a slot whose
process ends later is released at the next adoption/sweep, not by a count. Not done: test_reply_shape_live DRIVERS covers
only account/history service replies; confirm_legacy_slot is a collector op outside reply_shapes, so no driver was added.
