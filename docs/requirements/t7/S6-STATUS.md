# T7 S6 status: core API and terminal (API-2, 4..15, 17..25, TERM-13 checked; API-1, 3, 16, 26, 27, TERM-5, 6, 7, 17 unchecked)

Branch feat/t7-s6, base cbc757d5 (0.8.60). Commit 1 bb7ae3cd; fix round (Codex review t7s6-r1 NOT-READY, primary decisions 258 and 259) on top.
Two modules: `scripts/e2e_checks_core_api.py` (API-2, 4, 5, 6, 24, 25) and `scripts/e2e_checks_core_records.py` (API-7..15, 17..23,
TERM-13). Guard tests `tests/test_e2e_core_api.py` (registered as `root:test_e2e_core_api`; `parallel_tests.py --module ...` shows
`"metadata_errors": []`). Nothing pushed or installed, no product code changed, no VERSION or digest change. Every assertion changed in the
fix round is listed old -> new in docs/requirements/t7/assertions.md ("Slice S6 fix round").

## Decision 259: green only through the real product path
Ten IDs have NO check and are marked `"deferred": true` with a `"reason"` in tests/e2e/requirements.json (tests/test_e2e_requirements.py allows
those two extra keys only together with an empty `checks` and a reason, so an unchecked ID is explicit, never silent). A run of `--flows API,TERM`
reports them `no_check` and `passed: false`, as before:
| ID | Why unchecked | Needs |
|---|---|---|
| API-1 | runner survival and the `orch` fall-back to the files: no runner and no `orch` command exist in 0.8.60 (the core is a view over files "the runner" writes; nothing writes them) | runner (A5/A6), `orch` |
| API-16 | delivery at real safe points: nothing in 0.8.60 calls `CoreService.deliver_at_safe_point` (grep: only its definition) | runner (A5/A6) |
| API-17 | quoted delivery of a push: its only green path was a manual `deliver_at_safe_point` call, and 0.8.60 has no production safe-point delivery (decision 259). Reason in requirements.json: "needs runner (A5/A6): no production safe-point delivery". The label, size and rate parts of the statement are covered by `packages/codex-agents/tests/test_core_api_talk_control.py`: `test_api17_push_is_labelled_by_source_and_quoted_as_untrusted`, `test_api17_push_is_never_an_owner_decision`, `test_api17_size_cap`, `test_api17_rate_cap_resets_with_time`, and `test_core_api_round5.py`: `test_refused_pushes_write_no_intent_rows`, `test_push_and_query_ids_never_repeat_when_the_inbox_is_cut`, `test_a_64_character_non_ascii_label_cannot_block_later_rows` | runner (A5/A6) |
| TERM-6 | run counts: the API's counts come from the runner's unit records; nothing writes them. Decision 258's wording fix is made (now "match learning-orchestration-report (its `runs`)"); the check itself waits for the runner (a check that hand-writes units equal to the report is the stand-in 259 forbids) | runner (A5/A6) |
| TERM-7 | usage: nothing writes run usage into the core's records. Wording fixed (decision 258: `learning-usage-report --since --until`) | runner (A5/A6) |
| TERM-5, TERM-17 | no terminal client exists in 0.8.60: the owner console (DESIGN-0.4.0-orchestrator-console.md) is unbuilt; the package has `terminal_delivery.py` and `review_runner.py` only, and `CoreClient` is a library, not a terminal. Nothing to drive in a pty | terminal |
| API-3 | a connection from another uid: needs a second Unix user (owner-gated) | second Unix user |
| API-26, API-27 | later adapters (dashboard milestone; tested last), no product code | the adapters |
The stand-in versions of API-1, API-16, API-17, TERM-5, TERM-7, TERM-17 are in commit bb7ae3cd; reuse their ideas when the runner and the terminal exist.
What those checks also asserted about the CORE alone (a headless core with no tty, a separate process holding the socket, safe-point block of at
most 300 bytes, query.reply -> query.answered, usage "unknown" when unreadable) is no longer a registered check (a check must name a requirement);
unit tests in packages/codex-agents/tests/test_core_api_*.py cover it.

## How the checks work
- A REAL headless core process per check group: `python -m codex_agents.core_api --records DIR --socket S` (runpy of the module, shipped
  defaults) from the INSTALLED project's own copy of the package, through the World's stand-in Python, no stdin, own session (no
  terminal), output in a file. Spoken to only over the unix socket: `Wire` speaks newline-delimited JSON-RPC itself, so the contract is not
  judged by the product's own client. The product's `CoreClient` and `in_process` run in helper processes (API-6 parity of reads and of changes).
- Tokens are minted the real way: the core's own `--mint` path at a pseudo-terminal (`World.terminal`), screen kept out of the saved outputs.
- Each value is compared with something read another way: the records and audit files on disk, the token store, `lsof`, the stat of the socket,
  the helper process of the product's library.
- The producer of the records (runner A5/A6) is not built: the records are hand-written in the shapes of tests/test_core_api_terminal.py. Every
  check says so in its reason code `records_hand_written_runner_not_built`. The IDs left checked are about what the CORE does with records it is
  given, which is why they do not need the producer.
- Limits cores: for caps and clocks the shipped defaults cannot reach in a minute, the same installed CoreService is built in its own process with the
  constructor's documented arguments (`event_buffer`, `write_seconds`, `audit_max_bytes`, `method_seconds`, `clock`, `monotonic`, `slow_for_test`).
- The records module no longer does a real run (that was only for TERM-7): its scene is a real install plus the core, which saves about 38 s.

## ID -> check -> evidence (normal run: every check green)
| ID | Check | Evidence (read path other than the stimulus) |
|---|---|---|
| API-2 | api2_socket_private_short_stale_replaced_no_tcp | lstat: socket mode 0600, its folder (made 0755) is 0700, path under 104 bytes; `lsof -i` empty and `lsof -U` shows the socket; killed core leaves the socket file, nothing listens, restart replaces it (new inode, 0600); stop removes it |
| API-4 | api4_framing_json_rpc_lines_parse_error_and_oversize | raw lines: -32700 with null id, connection lives; -32600, -32601; notification gets no reply; two lines in one write answered in order; string and integer ids echoed; 1 MiB+ line refused `too_large` and the connection lives; audit.jsonl holds parse_error, invalid_request, too_large rows |
| API-5 | api5_handshake_version_and_gate | protocol 1.0 and features; "2.0", "0.9", "9.1" refused `version_mismatch` with `supported`, gate still shut; "1.5" accepted; five methods before initialize `not_initialized`; no token `unauthorized`; unknown token refused; audit rows |
| API-6 | api6_schema_lists_the_contract_seq_at_everywhere_and_adapters_agree | schema.get methods, event kinds, error codes, walled; `seq_at` = last seq of events.jsonl on 10 reads and an error; helper process: `in_process` and `CoreClient` identical on 10 reads. CHANGES (fix round, then the confirming round): twelve changes (push, forwarded question, verdict, pause, resume, cancel, routing.set, message, query.reply, decision.answer, control.handoff, control.stop), each succeeds over the wire with `seq_at` = the events file's last seq (read from the file), the confirm step names the current seq_at, and the in-process adapter and the product's CoreClient, on two copies of the same records, give identical answers (random confirm/operation ids and `as_of` masked but required present) |
| API-7 | api7_state_units_and_runs_equal_the_record_files | every unit field for field (units.list, units.get, units.history, runs.list entries) equal to its record as the contract shows it (neutral fields on top, rest under `extra`); runs.list total and counts; the whole state.get object; a unit appended to the file shows at once |
| API-8 | api8_usage_history_ci_versions_equal_records_and_unknown_is_never_a_number | the whole usage object (windows by label and pct only), ci, versions, every usage.history sample, every history.list entry field for field; corrupt, partial, missing state, missing units (runs total "unknown"), missing usage history: "unknown"; a damaged line is counted and every sound entry is still there |
| API-9 | api9_health_findings_have_a_code_and_a_plain_text | sound records: no finding; damaged state/history/usage: each a `{code, text}` (state_unreadable, history_damaged...), plain sentence, no traceback, no private path |
| API-10 | api10_files_stay_canonical_a_restart_answers_the_same | no cache file exists in the folder; 10 answers identical and every record byte for byte the same after a stop-restart and a SIGKILL-restart; the inbox the core wrote survives |
| API-11 | api11_events_are_ordered_gap_free_and_have_the_envelope | 1100 stored events: 30 replayed consecutive with {seq, ts, kind, data, v}; two live events (confirmed pause, resume) take the next two seq and are the last lines of events.jsonl |
| API-12 | api12_resume_replays_exactly_the_missed_events_an_old_cursor_is_expired | from_seq top-5 replays exactly 5; at the end replays none; from_seq 3 `cursor_expired` with snapshot equal to units.list/state.get; future cursor `invalid_params` |
| API-13 | api13_kind_filter_and_a_slow_subscriber_is_dropped_without_delaying_others | filter alert.guard delivers exactly the stored events of that kind; bad filters refused; limits core (buffer 50): a never-reading subscriber is dropped (notice or closed), a healthy reader gets all 300 events in order, no writer call took 2 s, slot freed |
| API-14 | api14_events_carry_ids_and_summaries_never_secrets_or_transcripts | stored event with transcript, token, passphrase, `password=...`, `Bearer ...`: none of the five planted strings in the bytes received; summary kept, other fields dropped; a forwarded question holding secrets is announced by `query_id` only |
| API-15 | api15_canned_questions_are_answered_from_records_and_nothing_is_queued | status, usage, CI, versions, history, why-stopped (reads the stop record, not the last row): `answered_by records`, `tokens_used 0`, `sources`, `as_of`; inbox.jsonl bytes unchanged; judgment question not forwarded for records_only or a read token |
| API-18 | api18_tokens_are_256_bit_hashed_revocable_and_minted_only_at_the_owners_terminal | 64 hex = 32 bytes, distinct; tokens.json (0600) holds only SHA-256 (computed independently); mint with no terminal exits non-zero, prints nothing, changes nothing; `token.mint` over the API `owner_terminal_only` for an owner token; default agent token reads and pushes, `forbidden` for control and reply; revoked token refused on an open connection and at the handshake and gone from the store. Revocation uses the library function (the core has no revoke command line) |
| API-19 | api19_each_scope_may_call_exactly_its_methods_and_the_rest_is_forbidden | a core of its own with records that make every method answerable; five scopes x 35 methods (changes.diff and events.subscribe included): every allowed method is called with succeeding parameters (a confirm-step method in both steps) and must return a result (an `unavailable`, `unauthorized` or no reply is red); every other call is refused `forbidden` on the first call, before any confirm id; every change a scope made is an `ok` line of commands.jsonl under its own caller; the owner's decision answer and a read diff are read back; schema.get names the scope per method |
| API-20 | api20_only_the_owner_role_answers_a_decision | read/agent/orchestrator/confirm `forbidden`, decisions.jsonl unchanged; owner answer is on record with `answered_by owner:...`; bad option `invalid_params`; second answer `conflict` |
| API-21 | api21_a_control_needs_two_steps_executes_once_and_the_id_is_bound | first call `confirm_required` with id and expires_in 60, no command and units unchanged; other token and other params `confirm_invalid`; second call executes: one command record, state requested; reuse `confirm_invalid`; after 61 s (core clock) `confirm_expired`; commands.list agrees |
| API-22 | api22_walled_actions_are_refused_even_with_an_owner_token | all eight walled actions `owner_terminal_only`, also with a confirm id; every record file byte for byte unchanged; each refusal an audit.jsonl row |
| API-23 | api23_every_change_is_a_command_line_every_refusal_an_audit_line_and_no_secret_in_either | intent and ok lines per push, confirm_required and ok for a pause, owner verdict; reads not audited; caller = scope + first 8 of the token's SHA-256 (computed independently); 80 refusals rotate audit.jsonl (cap 3000) while commands.jsonl never rotates and keeps every change line; no token and no planted secret in either file or the rotated one |
| API-24 | api24_request_string_method_time_connection_and_subscription_caps | on a core of its own (the count of connections must not depend on what earlier checks left open): 1 MiB line and 2049-byte text `too_large`; 200-char id and control character `invalid_params`; 65th connection `too_many_connections`, slot free after; 8 subscriptions per token, 9th and the 17th `too_many_subscriptions`; method time `timeout` on a limits core (50 ms limit, slow hook), audit row |
| API-25 | api25_no_provider_names_in_contract_provider_detail_only_under_extra | schema has no provider word; records carry claude_session_id, codex_thread, model: no provider key name outside `extra` in 6 reads and 5 events; the detail is under `extra` |
| TERM-13 | term13_authority_is_decided_by_the_core_not_by_what_the_client_says | claimed scope in initialize params and envelope ignored (session scope = token's); read token claiming owner `forbidden` for control.stop; no token `unauthorized`; made-up token refused; confirm token `forbidden` on owner methods, `confirm_required` on its own; each of five tokens opens its own scope; audit rows |

## Injected faults (runner argument only: `--fault NAME`), red proof per ID
Test-only exact-once source rewrites of the installed `core_api.py`, applied by the stand-in Python as the module is imported (the S1..S3 BOOT
hook; `tests/test_e2e_core_api.py` proves each rewrite applies exactly once and no fault is on by default). The comparator
(`compare_fault`, unit-tested) now requires every ID of the module GREEN in the normal run, every target going green -> red, no other ID changing,
both reports naming every ID; two reports that are both red, or equal, are never `isolated`.
| Fault | Targets (red) | Result 2026-11-25 |
|---|---|---|
| core_api_broken | API-2, 4, 5, 6, 24, 25 | `isolated: true`, changed exactly those six |
| core_api_mutation_seq_broken (seq_at dropped from changes only) | API-6 | `isolated: true`; red on "a change's answer ... names seq_at: context.push", so the mutation assertions are what fail |
| core_api_newmutation_seq_broken (seq_at dropped from query.reply, decision.answer, control.handoff, control.stop only) | API-6 | `isolated: true`; red on the new methods' seq_at assertions only |
| core_records_broken | API-7, 8, 12, 13, 14, 18, 19, 20, 21, 22, 23, TERM-13 | `isolated: true` (API-18 here fails at the owner wall, see the next fault) |
| core_records_revocation_broken (a revoked token keeps working on an open connection) | API-18 | `isolated: true`; red on "an open connection is refused at its next call" |
| core_records_reads_broken (health finding gone, cache file beside the records, wrong live seq for a confirmed command, canned answer costing tokens) | API-9, 10, 11, 15 | `isolated: true` (a first version of the API-11 patch skewed the file's seq and turned API-12 red too; it now only skews what a live subscriber is told) |
Every checked ID of both modules now has a fault that turns it red on its own assertion; none is left without one. The red proofs of the removed
IDs (API-1, API-16, API-17, TERM-5, TERM-7, TERM-17) are gone with their checks.

## Times (2026-11-25, this machine, other units running; the modules run alone)
Named checks through the runner's own worker (`e2e_lifecycle.py --execute NAMES --results FILE [--fault NAME]`): core_api 36.6 s wall (setup 22-31 s +
checks ~6 s); core_records 47.7 s wall (setup 34 s + checks ~13 s). Fault runs: about the same each (35-50 s). Guard tests: `tests.test_e2e_core_api`,
`test_e2e_requirements`, `test_e2e_strictness` 45 tests in 1.6 s. `--flows API,TERM` also runs the every-flow G checks (minutes): use `--execute`.

## Findings and decisions for the primary
1. Decision 259 vs 258: 258 said to add the TERM-6 check; 259 says a check is green only on the real path and nothing in 0.8.60 writes run
   counts into the core's records, so TERM-6 (like TERM-7) is unchecked with 258's wording fix applied. Say if you want the stand-in registered anyway.
2. API-17 is unchecked (confirming review t7s6-r2c, decision 259): it was green only through a manual `deliver_at_safe_point` call. It returns with the runner's real delivery.
3. API-18 revocation exists only as the library function `revoke_token`; the core's command line has `--mint` and no revoke.
4. The checks use their own wire client; if the primary prefers `CoreClient` for every call, `Wire` is the only place to change.
5. A failing check keeps its client sockets alive in its traceback; API-24 therefore runs on a core of its own so the 64-connection cap does not depend
   on earlier checks.

## Reproduce
```
PY=<test venv python>; cd ~/Code/temp/cocolo-t7s6
$PY -I -B scripts/e2e_lifecycle.py --execute NAMES --results out.json            # NAMES: the declared check names, comma separated
$PY -I -B scripts/e2e_lifecycle.py --execute NAMES --results out.json --fault core_api_broken   # also core_api_mutation_seq_broken, core_api_newmutation_seq_broken,
                                                                                               # core_records_broken, core_records_revocation_broken, core_records_reads_broken
$PY -B -m unittest tests.test_e2e_core_api tests.test_e2e_requirements tests.test_e2e_strictness
```
