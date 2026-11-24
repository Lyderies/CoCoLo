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
