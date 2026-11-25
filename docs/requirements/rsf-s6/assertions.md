# RS-F S6 assertion audit

The original S6 implementation changed no pre-existing assertion. New tests exercise the owner
policy in decisions 129.2/129.3; fresh-store other-runtime refusals remain required.

New-test correction, before final green: the first pre-commit SIGKILL originally
asserted a pending table with a null lease cursor. Replacement: assert schema 18
and absence of the pending table. S3 explicitly rolls back the entire upgrade
before its first checkpoint. Later pre-commit cursors and every post-commit
cursor retain their original assertions; two-reopen full-store equality remains.
No pre-existing test assertion changed. The pending-open fixture now deliberately
omits stored inputs through the old reserve API, matching its named missing-input
case; no database row or test assertion was altered for that fixture correction.

## Fix round

- LOW-1 / RSF-S6-6: `late_refused` previously accepted any `HistoryError` from
  `publish_result`. Replacement: both `publish_result` and `commit_result`, with
  valid terminal input, must raise exactly `history_result_expired` (including
  `reason_code`), preserving the same full database, `total_changes` and artifact
  assertions at releasing, lost and reopened boundaries. This strengthens the
  refusal assertion; it already passes before the fix, so no new red is claimed.
- MED-1 / RSF-S6-4: renamed guidance test suffix `names_update_recovery` to
  `names_setup_update`. Old assertion: contains `update` and excludes `start that
  version`. Replacement: contains the real `Run: bash setup.sh --update`, excludes
  `recovery`, and retains the old-version exclusion. The command uses the same
  setup entry point as S2's fence message and preserves H1b's no-slash text rule.
  The new inventory assertion checks every S6 step literal against real CLI
  parser commands (or the existing setup update entry point).
- MED-2 / RSF-S6-2: new schema-21 denial/fence receipt cases assert the open duty
  is released `fenced` with the original receipt, the lease tombstone is unchanged,
  and successor withdrawal plus two reopens are idempotent. No old conservative
  missing-input assertion is removed: the unproven pending open stays held.
- LOW-3 / RSF-S6-5: new guidance assertion names unarmed expiry and rejects the
  old `Keep this hold` promise; the same test proves the existing expiry sweep
  releases the 70 unused leases as `withdrawn_unsent`.

Registered red before production edits: 10 tests, 3.8 s, `metadata_errors: []`;
11 assertion failures (seven step texts, setup guidance, both schema-21 receipt
kinds, unarmed guidance). No fixture/setup errors. Existing LOW-1 assertions pass.

The receipt-less MED-2 case additionally constructs a real schema-17 source with
`dd7e5074`, commits a real account denial and project closure, then upgrades it
with schema-21 runtime `e933d3a6`. B1 adopts `denied` with no receipt. Its new
assertion requires a `fenced` duty with no fabricated receipt, stable over two
reopens. It ran red (11 tests, 6.2 s, `metadata_errors: []`) before allowing that
existing sealed no-launch evidence. An initial supplemental fixture used SB's
schema 18, which does not run B1's 17-to-18 adoption; selecting the actual
schema-17 runtime corrected the fixture. No requirement or assertion was weakened,
and the fixture-precondition failure is not counted as a behavioral red.

Supplemental fixture precondition replacement (before its behavioral red):
`COUNT(project_holds) == 0` under SB schema 18 → actual schema 17 and absence of
the register under `dd7e5074`. This selects the true historical source on which
the existing 17-to-18 adoption runs; the required schema-21 and duty-release
assertions were retained unchanged.

The receipt-bearing test adds (never replaces) a release-time assertion under
a mid-batch wall-clock advance. Both receipt kinds ran red when the ordinary
denial helper sampled a fresh time. Migration now calls the same release
primitive with S3's frozen `now_ms`; no assertion was removed or weakened.
