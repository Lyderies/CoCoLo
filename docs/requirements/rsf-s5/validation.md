# RS-F S5 validation — narrowed by decision 188

Implemented the named, private-value-free status for journal-less admitted
probes and its plain learning-status display. Status reports each probe's
`deadline_at_ms + PROBE_ORPHAN_GRACE_MS` cutoff, accounts for P6 in due/next
eligibility counts, and performs no writes. The existing sealed denial with
`probe_admitted_expired` already records the release reason durably; its writer
and the P6 release policy are unchanged. No schema or account operation was added.

All test invocations below used exactly `python3 -B -m unittest <module>` from
`packages/codex-agents`. Every completed run finished within 300 seconds.
No git or socket operations ran. No existing test file or assertion was edited;
[the previous assertion proposals are not applied](assertions.md).

| Phase | Command | Result |
| --- | --- | --- |
| Initial fixture attempt, before production edits | `python3 -B -m unittest tests.test_rsf_s5_probe` | 7 setup failures, 141.191 s: the reused signature-worker fixture never returned (`original signature return expired`). This is not the S5 red proof. |
| Repeated initial fixture attempt | `python3 -B -m unittest tests.test_rsf_s5_probe` | Interrupted during fixture cleanup after the setup timeout; no completed result. An editing command had used the wrong working directory, so this attempt still used the old fixture. No production edits had occurred. |
| Store-only fixture correction, before production edits | `python3 -B -m unittest tests.test_rsf_s5_probe` | 7 setup errors, 0.323 s: the synthetic admission needed a nonempty evidence-digest list. Corrected fixture data only; assertions unchanged. |
| **RED**, before any production edit | `python3 -B -m unittest tests.test_rsf_s5_probe` | **7 tests, 5 failures, 0.353 s**. Missing named state and per-probe cutoffs, missing aggregate next eligibility and due count, and missing display details. Current-original and malformed-detail checks passed. |
| **GREEN** | `python3 -B -m unittest tests.test_rsf_s5_probe` | **7 passed, 0.378 s**. |
| Register/status regression | `python3 -B -m unittest tests.test_run_holds` | **71 passed, 29.240 s**, including unchanged legacy status shapes and display strings. |
| Reply-shape regression | `python3 -B -m unittest tests.test_reply_shape_compatibility` | **3 passed, 0.003 s**; existing account/history reply fields remain unchanged. |

The seven new tests use real SQLite, real request/origin seals and admission
records, and an unstarted worker pool. They launch no signature worker or model
and create no sockets. They cover reopening while held, individual cutoffs for
multiple holds, project isolation, fixed reason/state and private-value exclusion,
current-original evidence custody, reserved probes, the strict P6 boundary,
atomic durable denial/register release, replay after reopen, idempotent sweeping,
plain display and rejection of injected detail values. A SQLite authorizer denies
INSERT/UPDATE/DELETE/transaction/savepoint operations during repeated status and
capacity reads; the database dump and total change counter remain identical,
even after the release is due. Shared existing fixtures emit ResourceWarnings
about their unclosed launcher-file reads; those fixtures were not edited.

## Unrun worker, socket and CLI follow-ups

These are listed for a future authorized environment; they were **not run** in
this no-socket session. Run named modules separately from `packages/codex-agents`
with a 300-second runner limit. No new settlement command or settlement socket
test is needed under narrowed S5.

| Kind | Command | Follow-up / limitation |
| --- | --- | --- |
| Original worker-backed P6 coverage | `python3 -B -m unittest tests.test_account_billing_admission` | Both original P6 tests remain untouched. The full module was not run because its reused signature-worker setup timed out above; the new store tests independently verify the unchanged strict grace boundary, durable denial, replay and release-once behavior. |
| Status CLI and real collector/account sockets | `python3 -B -m unittest tests.test_cli_status_client_boundary` | Not run: opens Unix sockets and invokes the CLI. Future integration coverage should seed a held journal-less probe, inspect named state/reason/cutoff through learning-status, compare ledger state across repeated status calls, then sweep and check the persisted expiry reply. |
| Runtime ingress socket regression | `python3 -B -m unittest tests.test_rsf_s1_ingress_socket` | Not run: sockets prohibited. No ingress or operation set changed. |
| Real reply-shape integration | `python3 -B -m unittest tests.test_reply_shape_live` | Not run: live service/socket integration. Pure reply-shape checks passed above. |

Final executed green total: **81 tests passed** across three named modules.
The original worker-backed P6 module and socket/CLI integration are not claimed
as passing validation.
