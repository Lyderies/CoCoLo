# RT-RX stop for owner: merged-record provenance

Trusted starting commit: `e654b2613baeceddbe974294556f4316a963bb78`.
No production file or existing test has been edited in this attempt. The previously
approved edits (124.2, 124b, 124c) have therefore not been applied yet.

## Additional conflict, outside the approved lifetime-literal class

File: `packages/codex-agents/tests/test_provider_restrictions.py:81`

Old line (unchanged):

```python
                self.assertEqual(record(status, expected, 500), result)
```

Requirements: RT-RX-3/4/5/20 and the primary's third answer, which requires fallback
versus measured-reset provenance outside the existing window records.

This is a **whole-record** equality, not only a window equality. It fixes the
top-level fields and the content-derived record ID. Adding a top-level optional
`restriction_provenance` field therefore fails it even when every existing window
retains exactly its current shape and values (apart from the approved five-hour
lifetime change).

The counterexample is already in this test: a resetless rejection observed at
2000 is merged at 500 after rollback. The merged record currently contains an
explicit reset at `2000 + horizon + 1`. Without original observation/reset
provenance, a later classifier cannot distinguish that synthesized expiry from a
measured reset, cannot apply strict chronology, and cannot clear it on a later
favorable reading. Preserving the existing record byte shape loses information;
a process-local side table would lose it on serialization/restart. Moving the
provenance into a window would also break the requested window shape.

Proposed replacement line, subject to owner approval:

```python
                self.assertEqual(record(status, expected, 500), legacy_merged_record(result))
```

Proposed new helper `legacy_merged_record`: remove **only** the new top-level
`restriction_provenance` field, then rebuild the original record ID with the
existing `provider_restrictions._headroom_record` implementation (no digest-code
copy). Retain every original field in the comparison. The new RT-RX-20 test
separately pins successful later clearing after merge, inclusive fallback expiry,
and the absence of a manufactured measured reset in classification output.
Do not change line 82 (input immutability), line 83 (merge idempotence), or any
window equality to accommodate this conflict.

This proposal is not implemented. Per the requested stop rule, production work,
green runs and importer runs await the owner's resolution. `importers.json`
contains the conservative affected-module inventory and explicit not-run results.

## Restart under decisions 125/126 — additional conflict (current)

The earlier whole-record conflict above is **resolved by 124d**. Its approved projection is now implemented and passes. The old approved-edit/binding framework is no longer applicable.

Implementation stopped after the eight-module regression run exposed a different, unapproved scenario conflict. No assertion below was edited.

`packages/codex-agents/tests/test_admission_metadata.py:107`:

```python
                        self.assertEqual(rating, admission_metadata.headroom_state(
                            'claude', 'a' * 64, arguments['headroom'], arguments['billing'], now)[0])
```

`packages/codex-agents/tests/test_admission_metadata.py:113`:

```python
                        self.assertEqual(expected, reason)
```

The test appends a resetless restriction at `now - FIVE_HOURS_MS` to the existing
`claude-admitted` / `claude-launch` vectors. Both vectors already contain a trusted
`allowed`, 0% reading at **now**, with a live explicit reset. The old `active = delta >= -horizon`
(line 104) and expected rating/denial calculations (lines 105 and 112) still demand restriction
at the inclusive five-hour boundary. RT-RX-3 and resolved OPEN-1/2 require the strictly newer
reading to clear that resetless restriction. Changing only the approved lifetime constant
at line 89 therefore produces six failing subtests at lines 107/113.

Concrete narrow resolution for review (not applied): authorize this one test's setup to
replace its existing headroom list with the appended restrictive record, preserving its
boundary assertions; the prepared RT-RX chronology test independently verifies supersession.
Alternatively authorize its expected-value calculations to account for the newer reading.
Neither is among the supplied exact lifetime-constant, golden-projection or whole-record-projection classes.

Other observed failures are **implementation regressions, not requests to edit assertions**:

- `test_providers.py:2504`, `test_a_run_cannot_record_rate_limit_events_without_bound`:
  after overflow merging, the original explicit rejection expires at its first reset;
  the baseline conservatively denied until the merged later reset. This changes
  admission beyond resetless 124.2 and needs a production correction.
- `test_providers.py:523`, `test_claude_windows_use_the_codex_thresholds`:
  the private single-record helper returns `None` for null utilization where its legacy
  caller expects `ample`. The route must still report unknown and null readings must
  clear nothing; preserve the existing helper compatibility in production.

No further production work or test bodies were run after the additional conflict was identified.
Current run details: [restart-results.json](restart-results.json), [red run notes](red.md).

## Continuation under 124e — billing fixture conflict (current)

124e resolves the headroom supersession setup conflict above. Applied exactly:
lines 102/103 replace the headroom/billing lists with only the newly constructed
records. Every boundary assertion and expected-value line remains unchanged.
The two provider production regressions above now pass, as do all 10 new tests.

The replacement billing record shares the headroom observation time `now + delta`.
Removing the vectors' original current billing therefore exposes a separate,
unchanged launch gate: billing must be observed at or before now and no older
than 30 minutes (`admission_metadata.py:543` onward). The five-hour headroom
fallback cannot change that billing gate.

Executed failures: `test_admission_metadata.py:113`, for `evaluate_launch`, both
`rejected` and `allowed_warning`, at deltas `1`, `-18000000`, `-18000001`.
All six return `billing_unverified`; expected values remain `provider_exhausted`
for the two active rejected cases and `None` for the other four. All rating
checks and admission cases pass. No assertion change is proposed.

Additional setup change needed for review, outside literal 124e: make this
test's replacement billing record current independently of the headroom age.
Exact candidate line 103 (not applied):

```python
                    arguments['billing'] = [[{**billing, 'observed_at_ms': now}, '8' * 64]]
```

This retains only the newly constructed billing record, with identical billing
fields, while isolating headroom lifetime from billing freshness. It does change
that record's observation time, which was not expressly approved by 124e.
Production billing freshness/order was not weakened. Stopped under the user's
additional-conflict rule; no further test execution or production edits followed.
Evidence: [continuation-results.json](continuation-results.json), raw log
`/tmp/rtrx2-continue.log`, worker failure log `/private/tmp/ptfg_an879/9.log`.
