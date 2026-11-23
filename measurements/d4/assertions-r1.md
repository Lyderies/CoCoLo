# D4 round-one assertion ledger

Compared with `633f548007b04e195773f9ee42c34295cdea466b`. Only D4's `test_publication_replay*.py` modules are changed/added. No pre-D4 test or fixture source is edited. All 30 original D4 test names remain registered. New assertions supplement the originals except for the three replacements below. An AST comparison of every original `assert*` call found exactly these three unmatched calls; all other original assertion calls remain, including their multiplicities within their methods.

| Original assertion (633f5480) | Replacement | Reason |
|---|---|---|
| `test_publication_replay.py:483`, R6 body/usage: `assertEqual([claim(2)], replay_publications())` | Same assertion for `claim(3)` at line 509 | Claim 2 now removes supplied usage without changing its trusted digest and must refuse `history_conflict`. Claim 3 is prepared with `strand(3, usage=False)` and proves genuine absence; `(1,1,0)` remains asserted. |
| `test_publication_replay_driver.py:265`, `check`: `assertEqual(('committed',), settle[1], ...)` for the first call | `assertEqual(('committed',), event[1], ...)` for **every** call at line 385, plus target identity, nonempty calls, equal call/return counts and nonempty completed target assertions | Prevent a later uncommitted settlement or unfinished target from hiding behind the first good call. The original final durable-state/count assertions remain. |
| `test_publication_replay_driver.py:280`, expiry-first: `assertEqual(('expiry',), events[0])` | `assertEqual('expiry_done', events[0][0])` at line 404; shared scenario additionally waits for a successful writer expiry request and completed sweep, and asserts their actual execution order | Observe finished work rather than request creation. |

Changes around retained assertion calls (none silently dropped):

- P1's six injected faults and original counts, digest/retention equality, read-back, release count and consistency assertions remain. The clock advances by 17 ms before each retry; final cleanup advances to the **last** reservation's retention so every original release assertion still applies.
- Changed-material P1 keeps `assertRaises(HistoryError)` and the unchanged-state assertion, adding the exact `history_conflict` reason.
- Completed R2, ended-context R5 and releasing X3 keep every refusal/state/count assertion and add legitimate account-release/retention cleanup and absence checks.
- R6's envelope mutation is now canonical JSON with the original digest. Existing binding/record/usage alterations remain; phase/provider/time are additional cases in a separate test so the fixture's existing capacity is not enlarged.
- R4 recovery-first retains its counts/state/consistency assertions, then retries live after settlement and verifies unchanged counts. The unrecoverable branch retains `assertRaises` and adds `history_result_expired`. A separate case exercises successful live-first commit, arbitration, settlement and equal retry.
- X2 retains both live and direct replay `assertRaises` calls, but restores the exact material/body **before** them. Each now also pins `history_result_expired`; final charge/count assertions remain.
- L1/L2 retain terminal-helper/attestation call counts, final-link count and full lifecycle assertions. Their helper now also observes actual driver `retry`, live `_publish`, identical selected-packet object/time and pre-call reservation state (`reserved` for L1's retry; `committed` for L2's retry).
- L3 retains execution/durable counts, unequal-refusal reason and fresh-install assertions. It adds effective installation count, exact installed-object/evidence/selected-object identity preservation, and store-backed writer arbitration coverage.
- Stopped-instance tests retain reserved-before-stop, no-settlement-before-successor and final counts/state assertions. The readiness predicate now waits on the actual injected terminal fault. Exit asserts a completed target settlement with no successor started. Both sweep orders record real sweep completion and successful writer expiry completion. Target settlement observation continues through return/account capacity release.
- Replacing the imported `stop_host` alias with a local implementation retains its joined-host assertion and enforces the shared module deadline. The unused `expect_calls` argument is removed; no assertion depended on it.

`test_publication_replay_proofs.py` adds five socket-free checks that the strengthened proofs reject concrete regressions. All five fail against the original D4 proof bodies (`red-proofs-baseline-r1.log`) and pass against the corrected bodies (`green-unit-r1.log`). The ordinary store/lane tests are still run without those injected regressions.
