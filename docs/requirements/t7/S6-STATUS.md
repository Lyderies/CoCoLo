# T7 S6 status: core API and terminal (API-1..25, TERM-5, 6, 7, 13, 17)

Branch feat/t7-s6, base cbc757d5 (0.8.60). Two modules: `scripts/e2e_checks_core_api.py` (API-1, 2, 4, 5, 6, 24, 25, TERM-5) and
`scripts/e2e_checks_core_records.py` (API-7..23, TERM-7, 13, 17). Guard tests `tests/test_e2e_core_api.py` (13, registered as
`root:test_e2e_core_api`, scenario/integration; `parallel_tests.py --module root:test_e2e_core_api` shows `"metadata_errors": []`).
Nothing pushed or installed, no product code changed, no VERSION or digest change, no existing assertion changed or removed
(requirements.json: only the `checks` arrays of the IDs above; e2e_lifecycle.py: two imports and the fault tuple; e2e_core.py: two
GROUP_SECONDS guesses).

## How the checks work
- A REAL headless core process per check group: `python -m codex_agents.core_api --records DIR --socket S` (runpy of the module, shipped
  defaults) from the INSTALLED project's own copy of the package, through the World's stand-in Python, no stdin, own session (no
  terminal), output in a file. Spoken to only over the unix socket: `Wire` in the module speaks newline-delimited JSON-RPC itself, so the
  contract is not judged by the product's own client. The product's `CoreClient` and `in_process` run in ONE helper process each
  (API-6 parity, TERM-5 separate client), as the analysis allows.
- Tokens are minted the real way: the core's own `--mint` path at a pseudo-terminal (`World.terminal`), screen kept out of the saved outputs.
- Each value is compared with something read another way: the records and audit files on disk, the token store, `ps`, `lsof`, the stat of
  the socket, the helper process of the product's library, a real `learning-usage-report`.
- The producer (runner A5/A6) is not built: the records are hand-written in the shapes of tests/test_core_api_terminal.py. Every check
  says so in its reason code `records_hand_written_runner_not_built`; API-1 also `runner_not_built_services_stand_in`.
- Limits cores: for caps and clocks the shipped defaults cannot reach in a minute (60 s confirm expiry, 10 per minute push window, one
  hour TTL, event buffer, audit rotation at 4 MiB, a 10 s method limit), the same installed CoreService is built in its own process with
  the constructor's documented arguments (`event_buffer`, `write_seconds`, `audit_max_bytes`, `method_seconds`, `clock`, `monotonic`,
  `slow_for_test`). The shipped-defaults core is used for everything it can reach (1 MiB line, 64 connections, 16/8 subscriptions, 2 KiB text).

## ID -> check -> evidence (normal run: every check green)
| ID | Check | Evidence (read path other than the stimulus) |
|---|---|---|
| API-1 | api1_core_starts_headless_and_the_installed_commands_do_not_need_it | `ps` shows no tty; core answers; collector_host and the project service are running; core SIGKILLed; those same pids are alive after, `learning-status` still exits 0 with the same collector `started_at_ms`, records untouched. Caveat: the runner does not exist; the collector and project service stand in for "the runner" |
| API-2 | api2_socket_private_short_stale_replaced_no_tcp | lstat: socket mode 0600, its folder (made 0755) is 0700, path under 104 bytes; `lsof -i` empty and `lsof -U` shows the socket; killed core leaves the socket file, nothing listens, restart replaces it (new inode, 0600); stop removes it |
| API-3 | not checked | needs a second Unix user (owner-gated, analysis "cannot be proven") |
| API-4 | api4_framing_json_rpc_lines_parse_error_and_oversize | raw lines: -32700 with null id, connection lives; -32600, -32601; notification gets no reply; two lines in one write answered in order; string and integer ids echoed; 1 MiB+ line refused `too_large` and the connection lives; audit.jsonl holds parse_error, invalid_request, too_large rows |
| API-5 | api5_handshake_version_and_gate | protocol 1.0 and features; "2.0", "0.9", "9.1" refused `version_mismatch` with `supported`, gate still shut; "1.5" accepted; five methods before initialize `not_initialized`; no token `unauthorized`; unknown token refused; audit rows |
| API-6 | api6_schema_lists_the_contract_seq_at_everywhere_and_adapters_agree | schema.get methods (35, from the ten unit modules), event kinds, error codes, walled; `seq_at` equals the last seq of events.jsonl on 10 reads and on an error reply; helper process: `in_process` and `CoreClient` give identical results on 10 calls; this Wire reads the same |
| API-7 | api7_state_units_and_runs_equal_the_record_files | units.list/get/history, runs.list counts, state.get field for field against units.jsonl/state.json; neutral fields plus `extra` give the record back; a unit appended to the file shows at once |
| API-8 | api8_usage_history_ci_versions_equal_records_and_unknown_is_never_a_number | usage/ci/versions/usage.history/history.list equal the files; corrupt, partial, missing state, missing units (runs total "unknown"), missing usage history: "unknown"; a damaged line is counted |
| API-9 | api9_health_findings_have_a_code_and_a_plain_text | sound records: no finding; damaged state/history/usage: each a `{code, text}` (state_unreadable, history_damaged...), plain sentence, no traceback, no private path |
| API-10 | api10_files_stay_canonical_a_restart_answers_the_same | no cache file exists in the folder; 10 answers identical and every record byte for byte the same after a stop-restart and a SIGKILL-restart; the inbox the core wrote survives |
| API-11 | api11_events_are_ordered_gap_free_and_have_the_envelope | 1100 stored events: 30 replayed consecutive with {seq, ts, kind, data, v}; two live events (confirmed pause, resume) take the next two seq and are the last lines of events.jsonl |
| API-12 | api12_resume_replays_exactly_the_missed_events_an_old_cursor_is_expired | from_seq top-5 replays exactly 5; at the end replays none; from_seq 3 `cursor_expired` with snapshot equal to units.list/state.get; future cursor `invalid_params` |
| API-13 | api13_kind_filter_and_a_slow_subscriber_is_dropped_without_delaying_others | filter alert.guard delivers exactly the stored events of that kind; bad filters refused; limits core (buffer 50): a never-reading subscriber is dropped (notice or closed), a healthy reader gets all 300 events in order, no writer call took 2 s, slot freed |
| API-14 | api14_events_carry_ids_and_summaries_never_secrets_or_transcripts | stored event with transcript, token, passphrase, `password=...`, `Bearer ...`: none of the five planted strings in the bytes received; summary kept, other fields dropped; a forwarded question holding secrets is announced by `query_id` only |
| API-15 | api15_canned_questions_are_answered_from_records_and_nothing_is_queued | status, usage, CI, versions, history, why-stopped (reads the stop record, not the last row): `answered_by records`, `tokens_used 0`, `sources`, `as_of`; inbox.jsonl bytes unchanged; judgment question not forwarded for records_only or a read token |
| API-16 | api16_judgment_questions_queue_only_in_the_inbox_and_replies_come_back | 20 forwarded questions = 20 inbox rows (untrusted, ttl 3600, expires); no other record changed except one `query.queued` event (id only) each; helper process calls `deliver_at_safe_point` (the runner's call): block of at most 300 bytes, marked delivered in deliveries.jsonl, queries.get `delivered`; hour later `expired`, none delivered; query.reply by an orchestrator token gives the asker `query.answered` (id only), reply read back with tokens_used. Caveat: delivery is called in a helper process because the runner that makes that call is not built |
| API-17 | api17_pushes_are_labelled_capped_quoted_untrusted_and_never_a_decision | source label, `trust untrusted`, no decision created; delivered as one quoted JSON line, newline/quotes stay in the string; 2049 bytes `too_large`, 2048 ok; 11th push of a minute `rate_limited`; a minute later (core clock) allowed again |
| API-18 | api18_tokens_are_256_bit_hashed_revocable_and_minted_only_at_the_owners_terminal | 64 hex = 32 bytes, distinct; tokens.json (0600) holds only SHA-256 (computed independently); mint with no terminal exits non-zero, prints nothing, changes nothing; `token.mint` over the API `owner_terminal_only` for an owner token; default agent token reads and pushes, `forbidden` for control and reply; revoked token refused on an open connection and at the handshake and gone from the store. Revocation uses the library function (the core has no revoke command line) |
| API-19 | api19_each_scope_may_call_exactly_its_methods_and_the_rest_is_forbidden | five scopes x 33 methods: allowed sets from the requirement and unit tests, every other call `forbidden`; schema.get names the same scope per method |
| API-20 | api20_only_the_owner_role_answers_a_decision | read/agent/orchestrator/confirm `forbidden`, decisions.jsonl unchanged; owner answer is on record with `answered_by owner:...`; bad option `invalid_params`; second answer `conflict` |
| API-21 | api21_a_control_needs_two_steps_executes_once_and_the_id_is_bound | first call `confirm_required` with id and expires_in 60, no command and units unchanged; other token and other params `confirm_invalid`; second call executes: one command record, state requested; reuse `confirm_invalid`; after 61 s (core clock) `confirm_expired`; commands.list agrees |
| API-22 | api22_walled_actions_are_refused_even_with_an_owner_token | all eight walled actions `owner_terminal_only`, also with a confirm id; every record file byte for byte unchanged; each refusal an audit.jsonl row |
| API-23 | api23_every_change_is_a_command_line_every_refusal_an_audit_line_and_no_secret_in_either | intent and ok lines per push, confirm_required and ok for a pause, owner verdict; reads not audited; caller = scope + first 8 of the token's SHA-256 (computed independently); 80 refusals rotate audit.jsonl (cap 3000) while commands.jsonl never rotates and keeps every change line; no token and no planted secret in either file or the rotated one |
| API-24 | api24_request_string_method_time_connection_and_subscription_caps | 1 MiB line and 2049-byte text `too_large`; 200-char id and control character `invalid_params`; 65th connection `too_many_connections`, slot free after; 8 subscriptions per token, 9th and the 17th `too_many_subscriptions`; method time `timeout` on a limits core (50 ms limit, slow hook), audit row |
| API-25 | api25_no_provider_names_in_contract_provider_detail_only_under_extra | schema has no provider word; records carry claude_session_id, codex_thread, model: no provider key name outside `extra` in 6 reads and 5 events; the detail is under `extra` |
| API-26, API-27 | not checked | later adapters (dashboard milestone, tested last). `--flows API` reports them `no_check`; see decision 2 below |
| TERM-5 | term5_terminal_and_core_are_separate_processes_and_the_core_is_headless | core pid is not ours, its command line is the core module, no tty, `lsof -U` shows the core holds the socket and this process does not; a third process (CoreClient helper) reads the state and ends, the core keeps answering |
| TERM-6 | NOT checked (product does not meet the statement) | see finding 1 |
| TERM-7 | term7_usage_through_the_api_matches_the_usage_report_and_is_unknown_when_unreadable | a real run on the same World (fake Claude child reporting 1234 in, 56 out); `learning-usage-report` shows the run with those numbers; the state record built from the report's totals; usage.get and the canned usage answer equal them; corrupt state or no `usage` key: "unknown" |
| TERM-13 | term13_authority_is_decided_by_the_core_not_by_what_the_client_says | claimed scope in initialize params and envelope ignored (session scope = token's); read token claiming owner `forbidden` for control.stop; no token `unauthorized`; made-up token refused; confirm token `forbidden` on owner methods, `confirm_required` on its own; each of five tokens opens its own scope; audit rows |
| TERM-17 | term17_every_read_goes_through_the_api_which_reads_the_files_fresh_and_writes_nothing | every read of the screens is a `read` method of the contract; all reads called, no record file changed, audit file grew only by not_found refusals; a changed state record shows at the next call (no restart, no cache) |

## Injected faults (one per module, runner argument only: `--fault core_api_broken`, `--fault core_records_broken`)
Test-only exact-once source rewrites of the installed `core_api.py`, applied by the stand-in Python as the module is imported (the same
BOOT patch hook S1..S3 use; `tests/test_e2e_core_api.py` proves each rewrite applies exactly once and the fault is off by default).
`fault_isolated(normal, fault)` over every ID of the module (comparator unit-tested):
- core_api_broken (version check off, socket 0666, parse errors unanswered, seq_at dropped, connection cap off, provider keys at top level):
  RED API-2, 4, 5, 6, 24, 25; unchanged and green API-1, TERM-5; `isolated: true`.
- core_records_broken (read scope may control, confirm id unbound, wall open, push cap off, secrets kept in events, kind filter off,
  expired cursor accepted, missing ci answered 0, wrong run counts, audit keeps every parameter): RED API-7, 8, 12, 13, 14, 17, 18, 19,
  20, 21, 22, 23, TERM-13; unchanged and green API-9, 10, 11, 15, 16, TERM-7, TERM-17; `isolated: true`.

## Times (2026-11-25, this machine, other units running; the two modules in two processes at once, as `--jobs` runs them)
Named checks through the runner's own worker (`e2e_lifecycle.py --execute NAMES --results FILE [--fault NAME]`):
core_api 33-34 s wall (setup 22 s + checks 11 s); core_records 51-52 s wall (setup + a real run 38 s, checks 13 s).
Fault runs: 46 s and 41 s. `--flows API,TERM` also runs the every-flow G checks (minutes): use `--execute` or the named checks.

## Findings and decisions for the primary
1. TERM-6 cannot be met as written. Statement: "Run counts through the API match learning-orchestration-status." On 0.8.60
   `learning-orchestration-status` answers only the orchestration's lifecycle (keys: client_version, current, ended_at_ms,
   goal_time_used_seconds, goal_tokens_used, known, observed_at_ms, orchestration_handle, orchestration_id, ready, receipt, request_id,
   scope, sequence, started_at_ms, status, usage); it carries no run counts. The counts are in `learning-orchestration-report`
   (`runs`: abandoned, admitted, agent_bound, ...). Left unchecked, not weakened. Suggested rewording: "...match
   learning-orchestration-report", with the mapping of its `runs` counters to the unit states written by the (unbuilt) runner.
2. API-26/27 have empty `checks`: a run of `--flows API` ends `passed: false` with `no_check` for them. The analysis called for an
   explicit `"deferred": true` field, which changes the key set test_e2e_requirements asserts; not done here (owner/primary decision).
3. TERM-7 names `usage-report --json`; the command is `learning-usage-report --since --until` (JSON always, window required, at most
   MAX_WINDOW_MS). The check uses it and the intent is unchanged.
4. API-18 revocation exists only as the library function `revoke_token`; the core's command line has `--mint` and no revoke.
5. API-1 and API-16 are partly stand-ins until the runner (A5/A6) exists (see their rows); revisit then (risk R9: shapes copied from
   tests/test_core_api_terminal.py).
6. API-3 stays unchecked (second Unix user).
7. The checks use their own wire client; if the primary prefers `CoreClient` for every call, `Wire` is the only place to change.

## Reproduce
```
PY=<test venv python>; cd ~/Code/temp/cocolo-t7s6
$PY -I -B scripts/e2e_lifecycle.py --execute NAMES --results out.json            # NAMES: the declared check names, comma separated
$PY -I -B scripts/e2e_lifecycle.py --execute NAMES --results out.json --fault core_api_broken
$PY -B -m unittest tests.test_e2e_core_api tests.test_e2e_requirements tests.test_e2e_strictness
```
