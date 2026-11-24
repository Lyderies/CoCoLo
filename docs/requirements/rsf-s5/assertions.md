# Not applied: decision 188 keeps P6

Primary decision 188 rejects the historical proposals below. The narrowed S5
scope preserves the existing P6 release rule and every existing assertion.
The old → new entries are retained only as a record of the stopped proposal;
none is required or authorized for this build.

# Existing P6 assertion conflict: work stopped

The user's instruction is: “Never edit an existing test assertion (stop and
list old -> new in docs/requirements/rsf-s5/assertions.md).” Source inspection
found a direct policy conflict before new tests or production edits. No
existing test assertion, fixture, test catalogue or production file was edited.
The changes below are required proposals, not applied changes.

File: `packages/codex-agents/tests/test_account_billing_admission.py`.
Class: `AccountBillingAdmissionTests`.

## Earlier collector, beyond deadline plus grace

Test at line 813:
`test_O0c_P6_an_admitted_probe_of_an_earlier_collector_holds_its_slot_no_longer_than_deadline_plus_grace`.
After restart and `deadline + PROBE_ORPHAN_GRACE_MS + 1`, with no boot proof:

| Location | Old | Required new behavior |
| --- | --- | --- |
| 831 | `self.assertEqual(1, self.sweep_now())` | `self.assertEqual(0, self.sweep_now())` |
| 833 | `self.assertEqual('committed', row['state'])` | `self.assertEqual('admitted', row['state'])`; named status reason `probe_admitted_unknown` |
| 834–835 | `released_at_ms == self.clock` | `released_at_ms is None`; register remains held |
| 836–838 | capacity checks succeed for both providers with machine/account limit 1 | both checks still refuse `claim_busy` |
| 839–841 | same request replays `('denied', 'probe_admitted_expired', True)` | no timer-produced denial; retained admitted request cannot relaunch |
| 842 | billing probes `live == 0` | billing probes `live == 1` |
| 843–845 | another request reserves successfully and last row is `reserved` | no additional probe can be admitted while the original holds account/machine capacity; after authenticated owner settlement, capacity becomes available |

The test name and docstring also promise expiry; they would need to describe
holding until evidence or owner settlement. Keep its strict grace-boundary
checks as regression coverage for no release on either side of that boundary.

## Current original, then restart

Test at line 847:
`test_O0c_P6_an_admitted_probe_this_collector_still_holds_is_never_closed_by_time`.

Old at line 859:

```python
self.assertTrue(self.ledger.account_holds_due())  # an orphan is due
```

Required new, without recorded boot-change evidence:

```python
self.assertFalse(self.ledger.account_holds_due())
```

Reason: passing the obsolete timer must not make the supervisor repeatedly
schedule a release that requires evidence. The earlier assertions that a live
original is not closed by time remain unchanged.

## Supporting catalogue conflicts (not assertions; also not edited)

`packages/codex-agents/tests/lifetime_caps.json` entry
`account_billing_admission.py::close_orphaned_admitted` (lines 49–52) says
deadline plus five minutes closes the probe. It must instead describe release
on recorded boot change or authenticated owner settlement. Its
`learning_store.py::account_holds_due` entry (lines 986–989) ties due work
to that expiry predicate; it must describe evidence-backed work and avoid
timer-only polling.

No assertion change has been applied or authorized by this document. Work
stops here as instructed; the requested red → green sequence is outstanding.
