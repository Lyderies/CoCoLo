# RS-F S5: journal-less admitted probe status (narrowed)

Primary decision **188** keeps the decided **O0c P6** rule: an admitted probe
of an earlier collector, with no current in-memory original, holds capacity
through `deadline_at_ms + PROBE_ORPHAN_GRACE_MS`. The existing sweep releases
it after that cutoff (strictly greater, as P6 tests require). A current
collector's original still requires its existing child-ended evidence. No P6
test or existing assertion may be changed.

**RSF-S5-1 — Named held status.** While a journal-less admitted probe is held,
`account_holds_status` and the learning-status display report the named state
`probe_admitted_no_launch_evidence`, the fixed plain reason “No launch evidence
is available; this billing check holds capacity until its deadline plus grace.”,
and `release_after_ms = deadline_at_ms + PROBE_ORPHAN_GRACE_MS`. Details contain
only this state, reason and time, never request keys, account fingerprints,
paths, credentials or other private values. Each held probe gets its own cutoff.
The aggregate next eligibility is cutoff + 1 ms, matching P6's strict boundary;
a past-due hold awaits the sweep. Current originals remain evidence-held;
reserved and committed probes are not classified as this state.

**RSF-S5-2 — Durable release reason.** Keep P6's atomic, sealed committed denial
with fixed reason `probe_admitted_expired`, alongside the register release.
The stored reply explains the release after reopen and on retry, and a second
sweep cannot release the probe again. Reuse the existing record; no redundant
settlement mechanism or new release policy is required.

**RSF-S5-3 — Read-only status.** Status reads never sweep, settle, append records,
change generations or write the ledger, including beyond the release cutoff.
Hold counts, next eligibility and evidence-only counts reflect the same P6
classification as the release path. Existing reports without these optional
details remain accepted by the display.

**RSF-S5-4 — Red first and bounded validation.** Add store-level tests in a new
named module and run them red before production edits; then make the smallest
change and run green. Preserve every existing assertion, especially P6.
Run only `python3 -B -m unittest <module>` from `packages/codex-agents`, each
within 300 seconds. No git, sockets, other worktrees, owner settlement command,
boot-evidence mechanism, signatures API or schema migration is in scope.
Record actual runs and unrun socket/CLI follow-ups in
[validation](rsf-s5/validation.md).

The earlier assertion-change proposal is
[Not applied: decision 188 keeps P6](rsf-s5/assertions.md).
