# E2a: existing assertion changes

Approved: decision 170 (applied exactly as listed below, plus the DRIVERS entry).

Adding `account.publish_wake` (task scope: a PROJECT_OPERATIONS op, in the B4m reply-shape table and RS-F S1's gate)
grows `ACCOUNT_OPERATIONS` by one. Four existing assertions pin the old set or its size. They were left unedited and
fail until the primary applies the change below (each is a pure vocabulary growth; no behaviour assertion weakens).

| Test | Old | New | Reason |
|---|---|---|---|
| tests.test_rsf_s1_ingress.AccountIngressTests.test_RSF_S1_2_every_account_mutation_refuses_missing_or_wrong_runtime_before_authorizer (line 162) | `self.assertEqual(15, len(mutations))` | `self.assertEqual(16, len(mutations))` | publish_wake is a mutation; the loop then also proves the gate refuses it (E2a-6 has its own refusal test) |
| tests.test_admission_inputs.AdmissionInputOwnerTests.test_exact_six_family_raw_capture_without_crypto_connections_or_writes (line 139) | exact 21-name set of ACCOUNT_OPERATIONS | same set plus `"account.publish_wake"` | new operation |
| tests.test_signature_job_intake.SignatureJobIntakeTests.test_capacity_is_reserved_before_capture_without_extra_database_secret_or_crypto (line 95) | exact 21-name set | same set plus `"account.publish_wake"` | new operation |
| tests.test_signature_inputs.SignatureInputOwnerTests.test_capture_matches_legacy_inputs_without_crypto_connections_or_writes (line 146) | exact 21-name set | same set plus `"account.publish_wake"` | new operation |

Removed assertions: none.

## Also needed (a driver list, not an assertion; NOT edited)

tests.test_reply_shape_live `DRIVERS`: add `'test_e2a_wake_wire'` to a group, so
`test_every_operation_in_both_tables_was_produced_live` sees a real `account.publish_wake` reply (its wire tests
produce them). Without it that check would list `account.publish_wake` as missing. Not run here: the module runs
over 5 minutes (290 s timeout hit), beyond this unit's run limit.

## Review round 1 (Codex security review NOT-READY at 5a024c5e; the one fix round)

Approved by the primary's fix brief ("the outage test that permits the unsafe write must become the safe behaviour"):

| Test | Old | New | Reason |
|---|---|---|---|
| tests.test_e2a_wake_wire.WakePublisherTests.test_E2a_7_unreachable_collector_lets_the_write_proceed_flagged | `publisher.before_due(100)  # never raises: the write proceeds` on a publisher with no state folder, then `assertTrue(unpublished)` ... | the same call on that publisher `assertRaises(project_wake.WakeRefused)` (no acknowledgement and no marker reconcile can see); then a publisher with a state folder: `before_due(100)` proceeds, `assertTrue(publisher.marker.exists())`, `assertTrue(unpublished)`, the unchanged retry steps, plus `assertFalse(publisher.marker.exists())` after the acknowledged retry | HIGH lost first wake: an unacknowledged first write after `clean_stop(null)` was invisible to reconcile |

NOT applied, needs a primary decision (not on the approved list; the test fails until decided):

| Test | Old | Proposed | Reason |
|---|---|---|---|
| tests.test_e2a_launch_record.CollectorRecordTests.test_E2a_3_record_launch_over_the_collector_wire_and_its_runtime_gate (lines 125-133) | `client.record_launch(root, copy, DIGEST)` succeeds and `assertEqual((copy, DIGEST), launch_record(a))`, twice | `assertRaises(CollectorError)` on that call and `assertIsNone(launch_record(a))`, twice (the runtime-gate and bad-digest assertions unchanged) | HIGH launch authority: `copy` is the fixture path `/var/empty/owner/.codex/agent-orchestration/<pd>/ccc...`, which no collector can verify at its trusted copy root, so the old assertion IS the unsafe behaviour. A real owner-signed recording over the wire is covered by E2a-10's test (green). |

Removed assertions: none. New tests: 17 (docs/requirements/e2a.md, "Review round 1" sentences).


## Decision 179 (the one final fix)

Existing assertions changed: the E2a-3 row above, applied 2026-11-25 by the primary after owner decision 176 ("yes"): both
success assertions -> refusal + no record; runtime-gate and bad-digest assertions unchanged; module 24/24 green. New tests: 11 (FinalFixTests 3, FinalWireTests 1,
WakeFinalFixTests 7).

## Rebase onto 0.8.59 (be3eb53f)

Conflict union only (both sides added one op to the same pins: base B4s `account.lost_run_closure`, E2a
`account.publish_wake`). Nothing dropped; no other assertion changed.

| Test | Old (base be3eb53f) | New (rebased) |
|---|---|---|
| tests.test_rsf_s1_ingress ...test_RSF_S1_2_every_account_mutation_refuses_missing_or_wrong_runtime_before_authorizer | `assertEqual(16, len(mutations))` | `assertEqual(17, len(mutations))` (+ publish_wake) |
| tests.test_admission_inputs ...test_exact_six_family_raw_capture_without_crypto_connections_or_writes | exact 22-name ACCOUNT_OPERATIONS set (incl. lost_run_closure) | same 22 + `account.publish_wake` = 23 |
| tests.test_signature_job_intake ...test_capacity_is_reserved_before_capture_without_extra_database_secret_or_crypto | exact 22-name set | same 22 + `account.publish_wake` = 23 |
| tests.test_signature_inputs ...test_capture_matches_legacy_inputs_without_crypto_connections_or_writes | exact 22-name set | same 22 + `account.publish_wake` = 23 |

Runtime (not assertions): account_service's project-source op set = base set (with lost_run_closure) + publish_wake, the
E2a wake_bound/source guard kept; account_api/account_host op lists keep both entries. Schema: no new step (E2a adds
no project-holds schema version; only wake hooks), B6 step 21 unchanged.
