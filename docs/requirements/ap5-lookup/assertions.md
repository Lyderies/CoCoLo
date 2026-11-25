# Assertion accounting

No pre-existing test method or assertion was changed, removed or renamed.
The existing `test_AP5_2_the_grant_check_reads_only_the_five_types_it_uses`
remains intact as grant coverage; the new candidate evidence test closes AP5-2's
separate gap. All slice assertions are additions in
`tests.test_candidate_evidence_lookup`.

The new tests ran before the production change: AP5-2's growth assertion and the
two AP3 index assertions failed; both AP5-3 behavior assertions passed as baseline
characterization. No failing assertion was weakened to obtain green.
