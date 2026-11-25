# Assertion changes

Decision 304 explicitly replaces the standalone queue's provisional defaults.
In `test_ledger_owner_queue.LedgerOwnerQueueTests.test_limits_and_handle_owner_validation`:

- `DEFAULT_LEDGER_QUEUE_LIMITS == LedgerQueueLimits(16, 8, 2, 800 * MS)` ->
  `DEFAULT_LEDGER_QUEUE_LIMITS == LedgerQueueLimits(12, 8, 6, 800 * MS)`.
- `LedgerOwnerQueue().snapshot()["maximum"] == 16` -> `... == 12`.

The new `test_measured_queue_defaults` independently went red on the old values.
`test_ledger_owner_queue_lifecycle.test_overflow_refusal_counters_are_bounded_without_close`
now constructs an explicit `LedgerQueueLimits(16, 8, 2, 800_000_000)` queue before
filling its two slots. Its original overflow, bucket and total assertions are
unchanged; it tests bounded counters independently of production sizing.

No other existing assertion is changed or removed.

The new slice-B exit test was strengthened during implementation: checking an
already-completed `['last']` request was replaced by an actual producer handoff
during `CollectorServer.stop`, asserting `['handled', 'exit']`, acknowledgement,
zero retained work and the C10 `requested` record. The new preparation test now
pauses the real protected-copy inspection boundary rather than a synthetic
preparation callback, retaining its authority-revocation assertion.
