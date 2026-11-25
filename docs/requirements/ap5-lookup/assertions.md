# Assertion accounting

No pre-existing test method or assertion was changed, removed or renamed.
The existing `test_AP5_2_the_grant_check_reads_only_the_five_types_it_uses`
remains intact as grant coverage; the new candidate evidence test closes AP5-2's
separate gap. All slice assertions are additions in
`tests.test_candidate_evidence_lookup`.

The new tests ran before the production change: AP5-2's growth assertion and the
two AP3 index assertions failed; both AP5-3 behavior assertions passed as baseline
characterization. No failing assertion was weakened to obtain green.

## Fix round

- LOW/AP3, old → new: an unavailable `git archive` raised a raw
  `CalledProcessError` (or timeout/missing executable) → the compatibility test
  fails explicitly naming release 0.8.61 and source commit `782a7eed`, with fetch
  guidance and no skip. The real previous-runtime path/version/schema, retained
  records/order/late values, candidate validation, append, integrity and current
  reopen assertions are unchanged. The repository's previous-release file has
  Claude bundle contents and a source pointer, not a previous Codex runtime.
- MED-2/AP3/AP5-3: added populated drop/reopen checks for both lookups before and
  after owner rejection, compared with the frozen former validator; added exact
  `LedgerError` checks for each lookup on an already-open connection missing the
  index. The latter failed red with two raw SQLite `OperationalError`s.
- LOW/AP3: added missing-object, archive-timeout and missing-git checks. All three
  failed red with raw subprocess/OS exceptions before the diagnostic change.
- MED-1/AP3: added a separately named large synthetic measurement module. It
  uses real public appends and normal opens; it asserts scale, retained row count,
  index creation and positive file growth. Timings are observations, not a flaky
  one-second pass/fail threshold. No existing assertion was removed or weakened.
  The initial minimal-event fixture took 63.6 s (over the scenario's 60 s budget);
  the final fixture fills the three normal optional digest fields to reach the
  same byte scale with fewer public appends. All scale and correctness assertions
  are unchanged; no production behavior was adjusted for the measurement.
