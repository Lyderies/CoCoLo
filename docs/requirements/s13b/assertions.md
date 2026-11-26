# S13b assertion changes

Only existing assertion changes are the planned RS-10-DUE-PROD flip in
`S13Budgets.test_rs10_finding_the_service_maintenance_cadence_misses_the_60_s_drain_of_1000_due_holds`:

- released == 1000 -> unchanged.
- pass_limit == 10 -> == 64 (RS-B section 12).
- passes == 100 -> <= 18 (bounded batches for 1,000 rows).
- released_within_60_s == 60 -> == 1000 (RS-10).
- len(production_violations(value)) == 1 -> production_violations(value) == [].
- seconds_until_drained > 900 -> < 60 (RS-10).

No other existing assertion changed or removed. The historical test name is retained to preserve registry links.
