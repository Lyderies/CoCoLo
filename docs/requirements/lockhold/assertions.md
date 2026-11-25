# Assertion changes

`test_event_retention.EventRetentionTests.test_one_collector_sweep_pass_leaves_no_expired_events`
is replaced by `test_collector_sweep_cycles_drain_expired_events_in_bounded_batches`.

- Old: after one `_sweep_cycle()`, all 1,501 raw events are gone and summaries total 1,501.
- New: successive cycles remove exactly 500, 500, 500 and 1 events, each leaves no
  transaction open, and the final raw-event set is empty and summaries total 1,501.
- Reason: LOCKHOLD-1 / decision 304 explicitly replaces the old one-cycle drain
  contract. The complete-drain and summary-total assertions are retained; per-cycle
  bounds and transactional completion are added. Automatic continuation is proved
  separately in `test_lockhold`.

No other existing assertion is changed or removed.
