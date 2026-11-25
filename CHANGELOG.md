# Changelog

What changed in each release of COCOLO (Claude Orchestrator, Codex Orchestrator, Local Orchestrator).

## 0.4.0 (in progress)

- Package 0.8.65: database reads stay fast as history grows (U3; malformed duplicate-guard records now refuse instead of being skipped); probe capacity past its limits proven (U4, tests); a standalone ledger owner queue (E1b, not yet wired); faster, steadier tests (TESTSPEED-2).

- Package 0.8.64: RS-B memory holds: driver entries and preparation slots are recorded holds, released only after retirement and kept on an unproven stop; a run opened during service start-up now registers. TESTSPEED: three slow test modules run 3x faster (tests only).

- Package 0.8.63: RS-F S6: project classification of old records (orchestration, open duty, settlement, result custody; late publications of old runs refused). 3.8 E1a: contention evidence tests. AP5: candidate evidence looked up by index (first open of a 286 MiB ledger 0.2 s).

- Package 0.8.62: RS-F S3: an interrupted hold-store upgrade resumes class by class from its saved cursor (bounded batches, published only after validation).

- Package 0.8.61: release batch.
  - RECOV: the status line shows recovery state correctly.
  - RS-B B7b: recovery reasons and measured budgets.
  - P9, P9b: attestation capture.
  - M11: dispatch admission.
  - RS-F S2: the store-owner fence, on store schemas 17 and 22.
  - UPDFIX: a refused update changes nothing, and a second setup is stable.
  - T7 S4, S5, S6: lifecycle end-to-end checks.
  - Test budgets raised per owner decision 238.

- Package 0.8.60: release batch.
  - RS-E E2a: a setup-bound launch record, copy pinning, and authenticated wake publishing and reconcile.
  - RS-E E2c: the collector starts a stopped project service that has due holds, with budgets and backoff,
    and parks after a 1-hour failure streak (owner decision 143); an owner-only status line shows it.

- Package 0.8.59: release batch.
  - RS-B B6: an orchestration left idle closes unattended, on store schema 21.
  - RS-F S7: settlement replies replay across the store migration and attempt pruning, with bounded evidence.
  - RS-B B5a: a crashed run is settled once its process is proven gone.
  - Product fixes: backup replies carry no file paths, and learning-expire keeps the event summaries.
  - T7 S3: learning, backup and guard checks in the lifecycle e2e.
- Package 0.8.58: release batch.
  - RS-F S4: M4 and R3 run-slot rules, with the owner confirm command shown for a held slot.
  - RS-F S5: a named probe state in account status.
  - T7 S2: recovery, status and deletion checks in the lifecycle e2e.
  - Product fixes: an orchestration closes after a lost run, status counts agree, and delete stops the
    service and removes history, leaving a pending-cleanup marker.
- Package 0.8.57: release batch.
  - B4s lost-run settlement: a run whose process is gone is settled from its recorded launch, with a
    write-ahead intent written before the settlement so a crash between the two steps is finished on restart.
  - T7 S1: run-control lifecycle checks (L6, L7, L9) run the installed CLI in the lifecycle e2e.
  - L6.1: a concurrent stop never overwrites a denial, and a clean frame reports zero denials.
  - L6.2: a refusal names the resolved route.
- Package 0.8.56: release batch.
  - RS-D S2 changed-runtime closure: an orchestration whose close was not recorded across an update is
    counted once across restart; one without a verified release stays open, with custody, to its deadline.
  - RS-E S6 login entry: status reports the login entry as on, off or stale; an update keeps the entry
    current (a failed systemd refresh puts a stopped or not-enabled unit back, and never recreates a
    vanished entry); the entry command passes the runtime check.
  - T7 S0: lifecycle e2e base (checks L1-L5, G1-G4, G8, G9) runs the installed CLI, split into groups with
    `--jobs`, plus a non-required CI lifecycle job.
  - RS-B B7a: holds report (`history.holds_report`) for open holds, with a capped page walk.
  - RS-F S1 (RSF-S1-1/2, owner decision 129.3): every mutating collector/account request
    must name the serving runtime's digest. Old-format repair requests and missing or
    different runtime identities are refused before any write with `runtime_upgrade_required`
    and a plain instruction to run `./setup.sh --update`. Before 1.0, older clients and
    runtimes have no compatibility path; update each project to the serving runtime.
  - B4m (LR-1): compatibility refusals name the version holding the state and print an update
    command from the local install record. Already-installed 0.3.x clients may still report
    `status_collector_unavailable` or `runtime_newer`: in a plain terminal, substitute your paths
    in `cd /path/to/CoCoLo && ./setup.sh /path/to/project`, choose Configure, then
    "Update the install and walk through every step". Existing account/history reply fields are
    frozen against the previous-release catalogue; new reply data requires a new operation.
- Package 0.8.55: an installed CLI reads the live provider state from its default state root (0.8.54 installed
  still routed "not_checked", with no reason: the checked default `--state-root` became "no state root"); a
  not_checked route always says why.
- Package 0.8.54: routing reads the live provider state on a real install (0.8.53 always routed "not_checked":
  the runtime hint is written before the account socket exists); a not_checked route says why.
- Package 0.8.53: routes around a used-up provider (RT-ROUTE-EXHAUSTED, decision 124). Routing and admission share
  one availability classifier; a provider at 90% or exhausted is skipped for workers and verifiers, in policy order;
  a "rejected" reading with no reset time lasts five hours and a newer good reading clears it; a verifier on the
  same provider as its worker is never independent; the verifier is chosen again when it opens; one re-route at
  open; when no provider has allowance the route says so with the next reset time. Test framework reset (decisions
  125/126): no fingerprint/approval machinery, affected tests by coverage, full run about 8 minutes.
- Package 0.8.52: D4 recovers a stored result after a crash: a publication interrupted between
  its prepare and commit is replayed from the kept packet or settled lost, and only a held reservation may
  publish.
- Package 0.8.51: B3 withdraws unclaimed opens (schema 19); D2 applies deadlines and retention;
  B2 lets CoCoLo record a finished helper's result (schema 20), with D3's retirement sweep clearing
  retired run state. E1 offers start at login during Set up. TF-26 keeps product fixes independent
  of test-owned legacy proof fingerprints. RS-A step 3 handles late owner preparation after a sweep,
  restores crashed slots, verifies the intended late-completion behavior, and indexes release-batch queries.
- Package 0.8.47 (container_stop, owner decision 49): the container stop tool owns all cleanup and the detached
  watchdog is a thin dead man's switch. The stop tool (`container_runtime`, used by the supervisor's stop action,
  C4's recovery, the HM-1 health check and the watchdog) now guarantees: every podman command runs in a process
  group of its own under a small guard that kills the group when the stop tool's process dies (SIGKILL
  included), and the stop tool kills and reaps the group when the command ends, so a podman child that outlives
  its leader no longer survives; one absolute deadline covers every command, wait and reap of a stop and a stop
  that runs out of time answers `deadline` (a gone container is still only the OS's word); the grace is capped by
  the time left (less a 4 s margin for the kill, the removal and asking the OS); a group that cannot be killed or
  reaped (`podman_cleanup_failed`) or a removal podman refuses is reported, never swallowed. The watchdog
  (`process_tree.py --container`), only when its service has died, makes ONE call of the stop tool (the run's
  labels checked first) inside one fixed total of 17 s (2 s of it kept for ending the tree), writes its result
  (`<token>.watchdog` beside the run's journal entry: owner-only, flushed, removed with the entry) and exits 0
  (gone, tree ended), 2 (not gone), 3 (tree end not confirmed: a failed `ps` is retried, then reported, never
  "empty") or 4 (the total ran out). The round-3 machinery is gone (per-try processes, retries, back-offs, the
  last try, `--stop-container`). Backstop: the run's deadline is also podman's own `create --timeout`, set only
  when `podman version` shows a client and server of at least 4.0 (an unverified minimum); otherwise the launch
  goes without it and its checks say why (`StartedContainer.timeout_backstop`). Containers stay off as shipped.
  Hung-stop worst case measured with the real watchdog over a podman-shaped script: exit 4 after 14.3 s.
- Package 0.8.47 (screens and re-sign): Owner decisions 36 and 37 (screens and billing re-runs). Screens show the owner what is needed to proceed or
  troubleshoot (the owner key shown while signing, the rule file path and the remove command), but nothing saved,
  logged, reported or read by an AI helper carries a private value: `learning-status` and the owner-key
  replacement report no longer include the owner key fingerprint (they say whether a key is configured and how
  often it was replaced). An answers-file re-run (`./setup.sh --answers`) now skips signing the billing
  statements when this project's attestation is already signed and current, as an update does; the new answer
  `"billing_sign_again": true` signs again, and the step-by-step menu offers "Keep the current signature" (the
  default, so a bare Enter never signs) or "Sign again" when the signature is current, and an update without an answers file says how to sign again.
  Also in 0.8.47 (screens and re-sign): Keep is the default when a signature is current; a straight Set up keeps a current
  signature; the replace-key report has exact fields. Known LOWs (screens r3): the multiline private-key scan in
  test_learning_cli (lines 583-596) can miss a leak; already_attested fails open to signing on transient errors
  (flow.py 1293-1305); billing_sign_again on fresh full runs is unverified; the register.log test is vacuous if the log is absent.
- Package 0.8.46 (billing admission safety fix): a billing probe that is answered from a recent probe record
  ("probe: reused") is now judged again before it is answered. Before, any recent probe record skipped the
  judgment, so a probe that had seen paid usage (overage in use) was answered "reused" instead of being refused
  with paid_usage_usable, and an unreviewed client version or a missing billing attestation was not noticed
  either. Now a reuse skips only the provider call: paid usage, the client version and the attestation are
  checked on every reuse and refused with their own reason. A provider API key in the environment was already
  stopped before admission; it is now refused with its own reason (provider_api_key_present) and the denial is
  recorded. A stored "reused" answer is replayed only inside its own request window (the time it was
  judged in); after that a retry of the same request is refused with probe_conflict, as a replay cannot judge
  again; stored denials still replay. A clean reuse still answers
  "reused" and starts no new probe.
  Owner decision 40 (a): when a probe is refused while the five-minute cooldown also applies, the refusal now
  names the real problem first (API key, paid usage, client version, attestation); a plain cooldown still says
  probe_cooldown.
  Review round 2: a stored "reused" answer is replayed only inside its request window and only while no newer
  billing or headroom evidence exists for the account; newer evidence (paid usage above all) refuses the replay
  with probe_conflict so the caller asks again and is judged afresh (stored denials still replay). The ledger's
  own probe path (LearningLedger.observe_claude_billing) now judges before it answers "reused", so a recent
  paid-usage record is refused with paid_usage_usable, not reused. A repeat admit() on an admitted request
  re-checks the API key. Accurate scope of the API-key fix: a key present when admit() judges is refused with
  provider_api_key_present and recorded; a key already present at reserve or prepare is refused as
  probe_not_authorized and is not recorded.
  Decision 47 (billing round 4, tests first): a "yes" that is ready but not yet taken is judged again if the
  inputs it judged have changed (a new reservation, identity observation and signature work; never a new provider
  call), and after two such new judgments it is refused as probe_replay_stale; a completed run's answer is judged
  again before it is first delivered. No authorization callback runs after the last input comparison: in the
  owner's judgment the post-commit callback is gone, and the publication check captures the inputs just before and
  just after its last callback and refuses a yes if they differ; the waivers are read after that last callback.
  Publication is bound to the exact committed row and the reply digest of the judgment generation. Every
  generation, a repeat's too, takes its own identity observation (the local client's login read, no model call,
  nothing spent), and a repeat measures that reading's age against the judgment's time. The owner-key turn is held
  until the transaction has committed. Known and accepted: `observed_at_ms` of every reply, a repeat's fresh yes
  included, is the time of the request's origin (its row's created_at_ms), not the time of the judgment; and each
  refused repeat still writes one probe_admission deny record (kept like any record, within the five-hour
  admission input scope), so a client that repeats a refused request in a loop fills that scope until the records
  age out; bounding it is an open owner choice.
- Known gaps (0.8.46): Billing: a just-finished billing check's own reading is not re-checked for paid usage before its answer is used (owner decision 55: the signed attestation is the user's responsibility); moved to the release that adds paid usage (own keys / credits); two test clean-ups pending (BR-19 launch test masked; ClaudeHostReviewTests shared state).

- Package 0.8.45 (Phase 1.2, core API round 6): the owner-facing core API (TM0) joins the 0.8.44 base after six
  review rounds (owner decision 34, no seventh). Fixed with tests that failed first: a single-digit price ("$5")
  is removed by the money filter; id allocation and refusal-log rotation are locked across processes; a failed
  connection audit, unencodable text and any unexpected handler failure are audited refusals; refusals keep a
  bounded audit budget past the deadline; a cut (incomplete) read answers `unavailable`, never `not_found`, also
  for every change it would feed; routing.json is flushed with its directory; writes that make no progress fail;
  a blank token-store entry no longer crashes a revoke; the history answer says when lines were skipped; the
  empty token store message names tokens.json and the delete step. Five tests that could pass on broken code now
  prove their requirement. A revoked token is now removed from the token store (it stays refused as an unknown
  token) and idle or revoked tokens leave the in-memory push windows, so neither fills up over time.

- Package 0.8.44 (Phase 1 merges): three reviewed changes join the 0.8.43 base.
  Review QA: operational QA judges a review, not the code (accepted when the verifier confirms the findings;
  the expected 'tests not run' statement is ignored; a refuted finding still disagrees; other tasks stay strict)
  (owner decision 32). O0c caps: lifetime caps on open requests and expired bindings, with their
  tests and limits file. DR-3: extra dispatch-wait and result-lifetime tests (the RC-3 test stays skipped).
  Identifier scan parity (Phase 1.3), below.

- Package 0.8.44, identifier scan parity: the identifier guard scanned a different file set in QA than in CI, and QA could miss a real identifier. QA
  and CI now scan one list that git produces (tracked files, even when an ignore rule names them, plus untracked
  files no `.gitignore` ignores); the QA runner writes it before the sandbox starts, with a copy of the owner's
  private denylist, and the shipped `scripts/qa-security.template` scans exactly that list. It refuses, rather
  than scan fewer files, when git, the list or the denylist is unavailable. Tracked symbolic links are scanned
  by name and target text, and a file name that is not valid UTF-8 is a finding. The placeholder exemption
  (`/Users/x`, `/home/user`) is exact, lower-case and whole-component only: `User`, `User Smith` and a name that
  merely starts with a placeholder now fail, and so does a placeholder followed by a character that can sit
  inside an account name (`user'smith`), or by more punctuation and then more name (`user''smith`, `user')smith`);
  file names get no prose exemption. `file:///Users/<name>` and `/System/Volumes/Data/Users/<name>` (and the
  `/home` forms) are found. A file name that holds a value, is not valid UTF-8 or holds a control
  character prints as `[redacted]`, and no error message names a file. QA diagnostics keep a path only if git
  lists it and it holds no value; the line is always 0, since a wrapper could choose the number. The scan and
  the runner now fail closed: the runner does not start a security check without the git file list and the
  denylist, and the scan refuses a file it cannot read, a path below a symbolic link, a pipe, a work tree moved
  by repository config, an oversize list, a run from a subdirectory, a denylist that was not provisioned, and a
  scanner that differs from the digest the approved wrapper carries (the wrapper now has a second placeholder,
  `@SCANNER_SHA256@`). Git lists files with ignore rules matched case-sensitively, whatever `core.ignorecase` says.

- Package 0.8.43 (RT-21 Phase 0): low/medium coding and research judgment/exploration,
  and planning synthesis, try GPT-6.1 Sol before Sonnet 5.5. Confirmed quality failures
  advance one admitted ladder rung, then use the strongest admitted route on the second
  failure; unadjudicated claims stay held. Low/medium coding, research and planning prefer an
  independent verifier; when the other provider is not admitted the same-provider fallback is
  kept and the route records `independent: false` (also on the verification route). Routes record
  review phase, parent lineage, expanded scope and unknown observations/usage separately.
  Policy v6 preserves v5 selectors for unopted cells, bounded coding choices and every
  high/critical route; legacy policies still load. No learning promotion is enabled.
  Verifier `invalid_result` fix (0.4.0 item 0.5): the bridge keeps each child's raw final answer
  (credential-shaped text removed; owner-only file in the project's artifact folder named by its SHA-256;
  digest, size and truncation in the report; expires after 7 days) before its private directory is
  removed. `inconsistent_axes` is split into `axes_status_verdict_mismatch`, `axes_verdict_without_completion`,
  `axes_admission_without_completion`, `axes_retry_lineage`, `child_set_admission` and `child_set_timeout`
  (older ledgers still read `inconsistent_axes`). Owner decision 26: the host sets a verifier's admission
  (`unverified`) and its status from its execution state and verdict, and records what it replaced in
  `host_set_fields`; a missing verdict or a child's timeout claim still fails, and worker results are
  unchanged. A verifier's output schema offers only `unverified` admission and no `timed_out` state.
  A Codex child's turns, commands and peak per-turn input tokens are recorded, and a run above its nominal
  `max_tool_calls` is stopped as `tool_call_limit_exceeded`.

- Package 0.8.42 (O0d): low/medium-risk verifiers use Sonnet 5.5 (medium) for Codex work and
  GPT-6.1 Sol (medium) for Claude work, including confirming reviews. Bounded coding at those
  risks tries GPT-6 Sol (medium), then Sonnet 5.5 (medium). High/critical routes keep their
  existing choices, including Opus for the first review of high-risk security code.

- A collector started before its code changed kept running the old code (Python never reloads its modules), so a
  fix merged afterwards did not take effect and a live check ran the old behaviour. The collector's health now
  says whether its runtime files differ from the digest it loaded; `learning-status` shows
  `collector_code_stale` with a plain note, and `provider-observe --billing-probe` refuses with
  `collector_code_stale` until the collector is restarted. A command started after the files changed sees the
  new digest, so a collector that loaded older code (or an older package version) is refused by the version
  check before it can report staleness: status and the probe now treat that refusal itself as the stale
  collector. A newer collector is not called stale; a collector that sent a malformed version, schema or digest is
  named `collector_identity_invalid`, and another build of the same version whose own files are unchanged is named
  `collector_build_conflict` (a restart would not fix it), and neither is refused as stale. Every remaining bare `probe_unavailable` now names its
  cause in the billing lane's last failure, including a deadline (`probe_deadline_expired`), a caller's cancel
  (`probe_cancelled`) and a close (`probe_closing`); a recognised observation reason is no longer labelled
  unrecognised, and an earlier named cause is kept. A result for a security reviewer or verification worker
  that completed (including a `blocked` one) with no `semantic_verdict` (missing or null) is refused
  (`missing_required_fields`), never filled in. `learning-review` refuses an empty verdict before recording the
  result, running `worker` again removes every file of the earlier run, and `verify`/`finish` refuse a stage
  file the runner did not write: wrong mode or shape, a digest its own recorded reply does not hold, or a
  provider, verdict, summary, route, assignment or result that does not match the keyed digest (HMAC) binding it.
  The key is derived from the host's private digest key (its state folder, owner-only), never held in the work
  folder, so a writer limited to the work folder cannot make a binding. The state folder is chosen once and given
  to every host command, the key is read before any child starts, and a state folder that overlaps the work folder, the
  packet or the project, either way round, is refused (`review_state_unavailable`); a rerun that stops there has
  already removed its earlier markers. A process running as the same user can
  read that key and is outside this check; the project host still checks both outcome digests and every
  assignment and result body against its own records before checks or QA are committed.
  Package 0.8.41.
- A project with the 0.8.40 Claude package could not be upgraded: the installer refused it as `untrusted_manifest`,
  because the Claude agent definitions changed after 0.8.40 (role model defaults). The package now records the
  previous release's bundle (`previous-releases.json`) and accepts an installed copy that matches it exactly;
  edited or unknown installs are still refused. The accepted release digests are pinned in the installer's code,
  so the record cannot vouch for itself, and a record may name only the files the package installs: a forged or
  altered record is refused (`previous_release_not_pinned`, `previous_release_path_refused`) and nothing changes.
  The setup update stops with the installer's own reason, in plain words that say what to do next. When an
  install, update or remove fails, setup undoes only what the packages themselves report they wrote in that run,
  after checking the report against the saved paths and disk, each file only while it still holds the bytes
  and mode the package wrote (checked on the open file, then moved or removed
  only if it is still that same file), and each folder a package created only while it is that same, empty
  folder. Anything else (a file you changed while setup ran, a folder you replaced or filled) is left as it is
  and named. A package step that gives no usable account of its writes stops setup at once, and nothing of that
  step is undone; one whose writes cannot be checked safely (the project folder changed, an unsafe path) also
  stops setup at once, and its undo then acts only on the files it can still check one by one (none, when the
  project folder changed). No later step runs, and setup says so. Nothing is undone for a package that reports a pending rollback, and its
  markers stay. Completed installs also use their checked journal, keeping preexisting folders and retaining
  Codex project configuration; a completed remove still uses the opposite operation. The setup
  wizard says "Nothing was changed" only when every undo completed with nothing left over. The Done screen no
  longer prints a remove command with an empty `--target`.
  Remaining LOW limits: filesystem checks and rename/unlink/rmdir are separate operations, so a concurrent
  writer can still change an entry between check and mutation (including folder creation and identity capture).
  The project root is reopened and its identity compared rather than held open throughout the operation.
  Parents recreated to restore a deleted file can remain unnamed if restoration fails. A partial Claude
  control-file write can report the intended digest; it is preserved on mismatch. Reports do not authenticate
  authorship against a deliberately dishonest package, which already has filesystem write authority; the
  snapshot check detects omissions only among preview and installed-manifest paths. Codex update/remove report
  completeness has no dedicated producer test. These limits are outside the install-first fixes above.
- Two commands that came together on a project whose service was not running could leave one of them reporting
  `orchestration_client_unavailable`: the start that lost to the other's service ended before that service
  held its lock and did not wait for it. It now waits up to 3 s while no host lock is held, within the start timeout, for the winning service.
- A billing probe asked within five minutes after a Claude child finished was refused as "billing probe
  unavailable": it reused the child's own rate-limit records (tagged with a run), which the reply contract rejects.
  Reuse now takes only the probe's own records, and a newer run record that is more restrictive (overage in use,
  paid usage usable, headroom not ample) stops reuse and re-evaluates
  admission, so a probe launches only if admitted (otherwise a named denial). A reply that fails its own
  contract is now `probe_reply_invalid` and is recorded in the billing lane's last failure.
- Claude Code 2.1.323 lists one built-in plugin, `cc-plugin-privacy`, in its init frame even when no plugin is
  installed (captured under COCOLO's own child settings), so the check that no plugin loads stopped every Claude
  child and the billing probe. The check now accepts that one plugin, only with exactly the keys `name`,
  `path` (`builtin`) and `source` (`cc-plugin-privacy@builtin`); its only effect is a hook that can restrict a
  tool call. A built-in COCOLO's settings disable (`cc-plugin-agents-md`, `cc-plugin-telemetry`,
  `cc-plugin-plugin-authoring`, `cc-plugin-easel`) that is listed means the settings did not apply and stops the
  child (`init_plugin_not_disabled`); any other plugin stops it too (`init_plugin_not_allowed`, renamed from
  `init_plugin_loaded`). Tools, MCP servers, the key source and the model are held as before, and a client that
  reports a `permissionMode` other than the one COCOLO sets, or any `additional_directories`, is stopped
  (`init_permission_mode_mismatch`, `init_additional_directories_present`). A billing probe stopped by the init
  check now reports that reason (in `provider-observe --billing-probe`) instead of `probe_incomplete`, and a
  probe records the client version its init frame reported (`client_version` in its reply). Package 0.8.40.
- The router can now read a task's risk and its failed attempts, and the verifier can follow the worker's risk,
  but only where the routing policy says so. Three optional policy entries do it: `risk_candidates` (a candidate
  list per risk band; high and critical may never go below the cell's first choice), `escalation` (a retry after
  a counted failure moves once to the next candidate up for work with an executable check, stays put for
  judgment work, and goes to the strongest candidate from the third attempt) and per-risk verifier settings
  (`by_risk`). A route that used one says so in `routing_basis`. The shipped policy has none of them, so every
  route is unchanged.
- A new read-only command, `learning-usage-report --project-root <root> --since <ISO> --until <ISO> [--limit N]`,
  reports what a window of children used: per provider and model, the runs, the runs with complete usage and
  the input, output, cache-creation and cache-read tokens, and per run an opaque run id, time, model, effort,
  role, outcome and tokens. It reads the project's store and the old ledger read-only: it starts no service and
  writes nothing, and names no task text, path or store identifier. It lists at most 500 runs (`--limit`, at
  most 2,000) and says when a window had more.
- A run's usage counted as unknown unless all eight usage fields were known, and a bridged child never reports
  model calls, tool calls, latency or cost, so `learning-status` counted every run as `host_usage_unknown` and
  `learning-orchestration-report` never said `child_attribution: complete`. A run is now complete when its
  input, cached-input and output token counts are known. Runs already recorded keep the flag they had; the new
  report decides each run from its own usage record.
- A result can be recorded after its service stopped. What a record, a verifier and QA need of a run (its assignment,
  task, route, client capability and host class) is now kept in the project's records with the run, from before
  its claim, instead of only in the service's memory, so a restarted service records a result left on disk, opens
  its verifier and runs QA on it. The record no longer has to come within an hour of the run's release, a service
  no longer forgets workers after 64, and a service holding recorded workers stops after the ordinary 15 idle
  minutes instead of 12 hours. A result kept from before this version is still kept to its deadline, but cannot be
  recorded, verified or run through QA (`history_worker_context_unavailable`), as after a restart before.
- Claims, opens and probes no longer stop machine-wide after two account executions end unconfirmed. An
  unconfirmed signature execution, a billing job left unknown or a probe worker held unknown now faults its
  account lane, so the account restarts itself at its next check; unknown work held in one lane no longer stops
  another lane's restart. The restart first proves every child the account started is gone (each signature
  helper reaped, each probe's process tree confirmed empty or never started) and waits, naming the lane, while
  one may still run. An unknown probe whose child is proven gone has its admitted row settled at once, so it no
  longer holds the machine's probe capacity. A claim, open or probe that could only wait for a stuck signature
  lane is refused at once (`claim_unavailable`, `open_unavailable`, `probe_unavailable`).
- The learning cycle and case review read the last 90 days, not all time. Before, every case and candidate a
  project ever mined counted for good against 30,000 records (the project store) or 20,000 cases (the old
  ledger), and a project that reached them got `learning_capacity` on every cycle; only the owner's
  `learning-expire` removed any. Now unpromoted cases and candidates past 90 days are removed in small batches by
  the project service's cleanup pass (the collector's sweep for a project still in the old ledger). Kept
  however old: cases in a controlled corpus, candidates past `proposed`, and anything a later record names.
  The 90 days is one setting (`LEARNING_RETENTION_MS`), the same window outcomes and operational QA already use.
  another lane's restart. The restart first proves every process the account started is gone: each signature
  helper reaped (inside the macOS sandbox that refuses every new process, or with its group and session empty
  on Linux), each probe's process tree confirmed empty or never started, and each provider CLI the observation
  lane started reaped with its process group empty (its group is killed first). A start that failed with no
  process left is not in doubt. While one cannot be proven, `learning-status` names the process, the command
  that checks it and the next step (restart the collector once nothing is listed). An unknown probe whose model
  is proven gone is settled at once (retried while the records are busy): one that ran keeps its billing
  records, so the next probe reuses them; one that never ran is settled as a denial. A claim, open or probe that
  could only wait for a stuck signature lane is refused at once (`claim_unavailable`, `open_unavailable`,
  `probe_unavailable`).
- A project's run opens no longer stop for good after 256: the account counted every open request ever made
  (and refused the next with `open_busy`), in every release since the open journal was added. Only opens in
  progress count now (four at once), and a full limit says so (`open_capacity_full`, with its next step).
  Finished opens are removed, with their run binding and receipt, a week after the run's expiry; replaying one
  answers `open_expired`. `learning-status` shows `account_capacity.open_requests`.
- The project's records keep a register of every result lease and reservation, written together with it: what is
  held, what released it (withdrawn before its claim, denied, fenced, bound to its claim, completed) and when. A
  project opened with a lease or reservation missing from the register is refused. A new service releases at once
  the unarmed leases a stopped service left, instead of waiting for their deadline. A reservation the account never
  released is no longer removed with the finished rows.
- The project's records gain format 18: four empty tables for the run supervisor's project-side holds, kept
  results' contexts, prepared publications and account settlements, with checks that run at every open and
  inside the upgrade. The service upgrades a project when it starts, keeping a copy as it was
  (`history.sqlite3.before-18.<time>`); a failed check leaves format 17 untouched. An earlier 0.4.0 build
  refuses a format-18 project.
- `learning-status` shows launches the service cannot settle (`unresolved_launches`: how many, the oldest's age, and
  up to 16 by run and state), instead of keeping them out of sight. The launch journal now reads every entry however
  many there are, and removes lock files a stopped service left behind.
- `dispatch` refuses, plainly, a route the project service does not run (`dispatch refused`, exit 2); the old direct
  path, which no route could reach, is removed.
- A result whose publication fails while the service is running is retried until it is recorded or the run's
  deadline passes, instead of waiting until the service stops.
- Leases a stopped service left are released by the service's own regular pass; a restarted service recognises what
  its predecessor left; leases that were never admitted no longer use up cleanup slots.
- Claim and capacity lookups stay fast as a project's history grows.
- A run the account settled as lost, or released as crashed, no longer keeps a cleanup receipt counted against
  the project's limit of 4,096 (`learning-status` -> `account_capacity.cleanup_receipts`); before, each such run
  counted for good, and at the limit every new run of the project was refused `claim_cleanup_unavailable`.
  Runs lost or crashed before the update stop counting too. A late release for such a run is refused plainly
  (`claim_cleanup_conflict`).
- A collector's stop signal is never lost, whatever it was doing when the signal arrived.
- The setup wizard is now Python. `./setup.sh` is a short launcher: it checks `python3` before it
  runs, as before, then starts the wizard, which shows the same screens, asks the same questions and
  keeps every safeguard. Path prompts take typing, Backspace, Ctrl-U, Ctrl-W, Ctrl-V and Tab to
  complete a path; moving the cursor inside a typed path is no longer offered.
- `./setup.sh --update <project>` (and the menu's first entry, "Update this project") updates an
  installed project with no menu: providers, profile and commands rule stay as they were, the install
  is committed, the collector registered, and attest.py asks for the passphrase. It stops plainly,
  changing nothing, on a failed check, a client not signed in, uncommitted managed files, or earlier
  records that need your choice.
- After the project is chosen, setup offers the recommended way first. A new project: "Set up
  (recommended)" runs every step and asks only your own choices (providers, committing, earlier
  records, the Codex profile and the commands rule; attest.py still asks for the passphrase), with no
  do-it-now menus or pauses; "Configure step by step" asks every question as before. An installed
  project: "Update this project (recommended)", or "Configure" for the full menu as before.
- An update skips the billing attestation, and so the passphrase, when every enabled provider is
  observed again and already has an attestation for its current account. Anything else (a provider
  that cannot be observed, a missing attestation, a status that cannot be read) runs it as before;
  an answers file and "Re-run the billing attestation only" always run it.
- `./setup.sh --answers FILE` installs a project with no menu from a JSON answers file
  (docs/setup-wizard.md); a missing, unknown or invalid answer is refused by name before anything
  changes.
- Setup starts the project's own service once the collector is ready, so it closes what an earlier
  service left at once, not when the next child runs. `learning-start-project-service` does the same
  from the command line.
- A service that will not take a connection now says why before closing it: too late, an enrolment
  that did not verify, too busy, or unavailable (`service_refused_*`), instead of closing silently.
- An orchestration opened under an earlier service or version is refused
  `history_orchestration_other_scope`, no longer reported as `host_client_mismatch`.
- A refused run open now says why instead of `open_unavailable`: the account is still starting
  (`open_account_starting`) or closing (`open_account_closing`), its database stayed busy
  (`open_account_busy`), the project's service is not running (`open_service_not_running`), is an
  earlier version's (`open_service_other_version`) or cannot be reached (`open_service_unreachable`), its
  hint is not this account's private file (`open_service_hint_refused`), the
  project is not active (`open_project_inactive`), stored open records disagree
  (`open_state_inconsistent`), or something else failed (`open_failed`). Codex package 0.8.33.
- `learning-status` shows how long each collector start-up phase took (`collector_startup_ms`).
- When the account services are stuck (a part has failed, or the billing probe worker has been held for
  over ten minutes) and nothing is running anywhere, the collector restarts them by itself, at most
  once an hour, and its exit record says why (`account_restart`). While anything runs it never
  restarts; `learning-status` says so and what is held.
- `learning-review worker|verify|finish` runs one review (the worker, its verifier, then operational QA)
  as a COCOLO command, saving each step's reply; it stops at a refused step, or when a child gave no
  verdict.
- Every refusal the command line prints now carries `next_step`: what to do next, in plain words, for its
  reason code (`reason_code` and `error` are unchanged).
- `learning-status` shows the account's own health (`account_health`): `ok`, `degraded` (work kept
  after an error, a held probe worker, or a slot past its deadline), `faulted` (a lane that failed,
  with its fixed cause and error type), or `unknown` with the reason, and an `attention` list in
  plain words. It is read from notes each account lane publishes, so a stuck lane or a busy account
  cannot stop it; `ready` keeps its meaning. While the account services start, status says
  `status_account_starting`.
- An orchestration report counts a run that failed before its open: `runs.admitted`,
  `runs.failed_before_open` and `failed_before_open_reasons` (fixed reasons only), and such a run is
  `abandoned`, no longer missing. `runs.opened` still counts real opens. The project's records gain
  schema 17 (one small reporting table); setup upgrades them as before.
- Tests are sorted into stages: `unit`, `integration` and `e2e` (`scripts/test-stages.json`).
  `scripts/parallel_tests.py --stage unit` runs only the unit tests; a unit test that starts a
  process, opens a socket or sleeps fails. `scripts/measure_test_behaviour.py` measures each module.
- A setup step that runs a while shows how many seconds it has run, and after ten seconds says it can
  take a few minutes, so an upgrade backing up large records no longer looks stuck.
- That step also shows what it is doing, such as `backing up project 2 of 5` and then `upgrading the
  records`. Setup asks the commands it runs for these lines (`COCOLO_PHASES=1`); they are fixed text
  with counts only, never paths or names. A phase line that cannot be written is dropped, and the
  upgrade carries on as before; the progress line fits the terminal's width. Codex package 0.8.23.
- CI runs by stage: pull requests run unit tests on Linux and macOS and integration tests on Linux;
  `main`, tags and a nightly run add the rest on both systems. Each job uses every core.
- A wait for the account's records is bounded: at most 750 ms each, 800 ms for one whole request. A
  request that cannot get them in time is answered `account_busy` (or `open_account_busy` for a run open)
  with a next step, never a hang or another failure; `learning-status` counts these refusals, even while
  the records are busy (`ledger_waits`). Codex package 0.8.34 to 0.8.37.
- Recording a hook event no longer reads all of the project's records (on a 418 MB ledger it took 889 ms;
  now under a millisecond), and the collector's start-up check reads only the record types it needs. Both
  come from indexes the ledger gains when it opens; nothing else in it changes.
- The project's service upgrades its own records when it starts (format 16 to 17), under its own lock,
  keeping a copy of the store as it was first (`history.sqlite3.before-17.<time>`, owner-only). A learning
  move that fails while the service starts is kept, shown by `learning-status`, and cleared by the next
  start that succeeds; a service that stops at once reports its own reason to setup.
- A service started by setup from a clone prepares children from the project's own files, so Claude
  children can be prepared again.
- A run refused because running children hold the space for results is answered
  `result_capacity_busy` (wait for them), no longer "the store is full"; `learning-status` shows the
  results in flight (`results_in_flight`).
- `dispatch` survives a slow service: a status check or result read the service did not answer in time is
  tried again until the run's own wait ends; a service that is gone still ends the wait at once.
- A Claude child's result is held to the result schema, as a Codex child's is (`--json-schema`), so an
  extra field no longer loses the whole result; a run that gives no schema-valid result is refused
  `structured_output_missing`. Claude Code 2.1.311 or later is needed.
- The skills say what to do when a dispatch loses its reply: wait for the run to end, then close.
- Expired run bookings and finished billing probes no longer count against their limits (4,096 each per
  project), so neither can fill for good; `learning-status` shows both against their limits
  (`account_capacity.pending_bindings`, `account_capacity.billing_probes`). Codex package 0.8.38.
- A collector asked to stop always ends within 18 s: after 15 s its exit record names the step it was
  stuck in (`cause: stop_forced`, `stalled_in`), and a kernel alarm ends it whatever it is doing.

## 0.3.4: no limit fills for good (Codex package 0.8.32)

- A project could record only 64 results in its whole life. After that every child ended
  `run_failed` with no named cause, and its orchestration could not be closed. The result limit
  counted every result ever stored, and nothing in the product retired results. Now:
  - only live results count;
  - the project's service retires results by itself, at start and every 10 seconds, once their
    orchestration is closed or their retention has passed;
  - a reservation that finds the store full first retires what it may;
  - a finished result is removed with everything that refers to it once nothing can present it again:
    its orchestration is closed, and its hard deadline plus one hour has passed.
  A store that is already full recovers by itself after the update. A store that is really full now
  says so: `dispatch` answers `result_capacity_full`, with what frees space, instead of `run_failed`.
  Found in live use.
- Eight more limits counted everything that had ever happened rather than what is live. Each is now
  counted live, and its finished rows are removed after a retention:
  - orchestrations count only open or paused ones, and closed ones go after 90 days;
  - QA records, check receipts and their requests go after 90 days, and their ceiling is 16,384 per
    90 days;
  - provider observations go a day after their deadline;
  - cleanup reservations go with their result;
  - released cleanup receipts no longer count;
  - two checks that refused once a store held more than 64 result rows of any kind, one on every new
    run and one on every orchestration close, now bound only their verification work (4,096 rows).
    Before, with more than 64 results, no orchestration could close.
- A run whose result was never recorded no longer blocks its orchestration's close for good. This
  happens when its dispatch reply was lost or refused and the project's service then restarted. Close
  now accepts it once the account's release of the run is verified, and counts the run abandoned. While
  the service still runs, dispatching the same handle once more returns the same fenced result to
  record; the skills now say so.
- An update no longer leaves orchestrations nobody can close. An orchestration is tied to the runtime
  that opened it, so after an update its project's new service could not even read one left open,
  and it counted against the 256-open limit for good. The project service now ends those, as
  `interrupted`, through the ordinary close; a run not yet over still keeps one open. Found live.
- `learning-status` shows how near each project is to each limit: `project_capacity` (results, open
  orchestrations, cleanup reservations, QA records, check receipts) and `account_capacity`
  (observations, unreleased cleanup receipts), each with its live count and limit.
- New tests run more lifecycles than every limit, and check that every way a run ends returns every
  live count to where it started.

## 0.3.3: dispatch on a busy account (Codex package 0.8.29)

- On a busy account, a child's open was admitted but its dispatch was refused with
  `admission_capture_limit`. Before admitting an open, and again at its dispatch, the account service
  reads the provider readings and decisions that can still change the decision, in one check of at most
  512 records and 256 KiB. One ordinary busy day of readings (about 180 headroom and 70 billing readings,
  40 recent decisions) filled about that much. The dispatch reads a little more than its open, since the
  open adds its own decision and the dispatch observes the provider again first. The check now holds
  4,096 records and 2 MiB, about ten times that day; past it, an open or dispatch still refuses plainly
  and changes nothing. What is read and how admission decides are unchanged. Found in live testing.
- A dispatch refused at its claim could leave its run stuck for good. Before closing such a run, the
  account checks that no earlier dispatch record names it, and on a project with long history (about
  200,000 records) that check ran out of its small read budget every time, so the run was never closed:
  `learning-classify-host-run-abandoned` answered `history_conflict`, and the run held one of the
  project's four places for unfinished results. The check now has the account's ordinary read budget.
  Runs already left stuck this way are closed by the project's next service by itself, after the
  update: it asks the account to close each claim request an earlier service left open, and the
  account checks the whole original request before doing so. Found in live testing.
  This works for a run an earlier version left, whose claim request names that version.
  While it is still closing such a run, `learning-classify-host-run-abandoned` says so instead of
  `host run orchestration is unavailable`, and an unreachable account does not stop it trying. Known
  limit: that service stops trying for one run after ten other refusals (a collector that closes the
  connection mid-request counts as one), or when one read of that run's history fails while it lists
  the runs to close. The project's next service tries again (one starts after the current one stops,
  for example once it has been idle for a while, or at the next update).

- Up to six children now run at once on the machine, shared by every project, and six per provider
  account (it was two); one project runs at most three. Testing found that with four or more children
  in one project a cancellation was not always carried out, so the per-project limit is three until
  that is fixed. To match, the account queues provider observations for at most eight projects at a
  time, three each (it was sixteen projects, two each).
- One failed Claude billing probe could stop every later one on the machine. There is one probe worker,
  and a probe admitted but stopped before its model started (for example when two account calls
  overlapped) kept that worker until the collector restarted. Every later probe from any project then
  waited a minute and answered `probe_unavailable`, so no Claude child could start. A probe that has not
  started now frees its worker and answers `probe_step_failed`. A probe whose model may have started is
  still kept, so it never runs twice. When no worker can free without a restart, a probe answers
  `probe_worker_unavailable` at once. Found in a live soak test.
- When the collector stopped while a call was in flight, the caller got an empty reply and reported
  `account_invalid_response`, and nothing recorded why the collector had stopped. Such a reply is now
  `account_closed_without_reply`. The collector now records how it stopped: a signal, a shutdown
  request, its state removed, its thread ending, or an error (only the error's type and code
  locations, never its message). `learning-status` shows that as `collector_previous_exit`. Found in a
  live soak test.
- The collector stopped by itself now and then, cutting off every call in flight
  (`account_closed_without_reply`). The first recorded stop showed why. The account service and the
  collector share one database connection, kept apart by a lock, and some account reads ran without
  that lock. A session bind from a hook arriving during one of them was refused, and the refusal ended
  the collector. Every such read now holds the lock, so the bind waits. A read waits for the lock no
  longer than its own time budget, then is refused as busy. If the database is busy while the
  collector serves a request, only that request is refused and the collector keeps running. Any other
  database error still stops the collector, and `collector_previous_exit` says why. Found in a
  live soak test.
- The orchestration report counts a run that closed without a result as abandoned, not pending.
- A 0.3.2 project service that holds a stuck run never stops when an update asks it to; the update then
  reports `project_service_other_version`. docs/troubleshooting.md says how to stop it by hand.
- Setup no longer fails its collector step while the collector is still starting: after registering
  restarts it, setup waits (up to 90 seconds, showing the time) while its status says it is starting,
  instead of checking once after five seconds.
- For contributors: run the tests with `python3 scripts/parallel_tests.py` (about 3.5 minutes on 16
  cores), or only the modules you changed with `--module`. Test-only fixes removed idle waits from the
  setup wizard and attest tests, and the collector-exit tests no longer fail under load. The suite still
  does about 30 CPU-minutes of work; 0.4.0 cuts that.

## 0.3.2: admission on a project with history (Codex package 0.8.22)

- From a busy project after upgrading to 0.3.1: no child could be opened and no Claude billing probe
  run. Before admitting anything, the account service reads the provider readings and earlier
  decisions it decides on. It read every one ever recorded and refused once more than 512 matched,
  and the project had over 2,000. The open then reported `account_unavailable` and the probe
  `probe_unavailable`, which pointed at the service rather than at the cause. Admission now reads only
  the history that can still change a decision:
  - identities from the last 30 minutes, decisions from the last 5 hours, and billing and headroom
    from the last day;
  - every billing attestation;
  - headroom whose window has not reset yet, at any age;
  - the first admitted Claude run of each account, and Claude's latest older billing and headroom
    per account.

  Every restriction that still applies still denies, and decisions and their cited evidence are
  unchanged. Those last rules select records by a field inside them (the account, a reset time, a
  Claude admission), which the account service never parses. So a new table, `admission_evidence`,
  holds those fields: the ledger fills it as each record is written (schema 14), and the upgrade
  fills it once for existing records. If even the bounded history cannot be read in one check, the
  open or probe now says so with `admission_capture_limit` (or `admission_capture_work_limit`), and
  nothing is admitted. A launch claimed hours after its open reads history from its open on, so it
  still finds the decision and identity it was admitted on. A damaged old record that no rule keeps no
  longer blocks admission; every record admission reads is still checked in full.
- A Claude billing probe refused because every run slot of its account was in use reported
  `probe_unavailable`; it now reports `probe_busy`. A probe stopped before it launched now names the
  step: its identity observation's own reason (such as `observation_unavailable`),
  `probe_preparation_failed`, `probe_preparation_unknown`, `probe_worker_failed` or `probe_step_failed`.
  A refused identity observation is tried once more first: straight after setup restarts the services,
  the first one could be refused, and observing spends nothing. Found in an owner's live tests.
- Under the owner's protected command rule, no bridged child could start, Codex or Claude. The project
  service runs from the protected runtime copy there, and prepared children from the copy, which holds
  only runtime code and none of the agent files a child's role comes from. It now prepares them from
  the project the copy is bound to, exactly as the CLI does; a copy serves no other project. Only the
  agent files, which the run's route binds by digest, come from the project: the result schema and
  client capabilities still come from the runtime the service runs from, as for the CLI. Found in an
  owner's live test of 0.3.2, which ended `preparation_failed_at_role_prompt`.
- A run that could not start said only `run_unavailable`. It now says which stage stopped it:
  `preparation_managed_configuration` (managed client settings are present), `preparation_client_missing`,
  `preparation_client_unsupported`, `preparation_client_changed`, `preparation_export_failed` (the
  committed files the child reads could not be exported), `preparation_backend_refused`,
  `preparation_invalid_input`, `preparation_not_authorized`, `preparation_full` or `preparation_busy`
  (the run was refused before preparing), `preparation_failed_at_<step>` (an unexpected error at
  that step of preparing), `preparation_worker_busy` or `preparation_worker_failed` (the prepared run
  could not be handed to its worker), `run_client_changed` (the client observed just before launch is not the one prepared) or
  `run_account_call_failed`. Fixed names only; no paths or messages. Found
  in an owner's live test of 0.3.2.
- A run admitted but refused before it claimed a slot (its preparation failed, say) stayed pending at
  the account for ever and counted as possibly running: after an update every open was refused with
  `project_service_other_version`, and the collector could not be replaced. A child launches only after
  its claim, so replacing the collector or a project service now waits only for children that may run;
  the learning move counts such a run until its binding expires. Found in an owner's live test.
- Runs that 0.2.x recorded still count as running for a day after they started (a cautious rule: 0.2.x
  enforced no deadline on a native Claude child), but they no longer hold back updating COCOLO or
  replacing a project service: an update said "a child run is active" while none was. They never ran
  under the collector or a project service. This applies once 0.3.2's collector is running; updating to
  0.3.2 itself, the earlier collector still waits for them. Found in an owner's live test of 0.3.2.
- After an upgrade from 0.2.x, `learning-status` and `learning-migrate-project --status` say how many
  runs the earlier version left still hold a slot, and when the last stops counting
  (`earlier_version_runs`); a learning move refused with `migration_not_drained` says the same.

## 0.3.1: upgrading busy projects (Codex package 0.8.14)

- From an owner's upgrade of a busy project to 0.3.0: its records (about 192,000 learning records,
  312 MB) could not be upgraded at all. Each project's backup, taken before the upgrade and again before
  its learning history moves to the project's own store, was built as one document in memory and refused
  over 128 MiB, and the move read that whole document back in memory, with its own 128 MiB and
  100,000-record limits. Backups are now streamed: each is a private SQLite file, written, checked and
  compared with the records a batch at a time, so a step that held about twelve times a project's
  records in memory now holds a few batches whatever its size. The move archives that file as its
  source image and streams the records from it, and takes up to 10 million records. Backups made by
  earlier releases can still be read.
- After an upgrade from 0.2.x, every run the earlier version had made was kept as a reservation that
  could never be counted as finished: two of them blocked every new child, and any of them blocked the
  move of the project's learning history (`migration_not_drained`). A 0.2.x child ran inside its own
  command under a deadline of at most six hours, so one started more than a day ago now counts as
  finished; its record is kept as it was. Found by rehearsing the 0.3.1 upgrade on a copy of an owner's
  records.
- Every project's backup is sized, and the disk checked, before any is written: when they cannot fit,
  the upgrade stops with `ledger_upgrade_capacity`, says how much space is needed or which project is
  too large (by position, never by name), and nothing is changed. Setup also says what stopped a failed
  upgrade (`ledger_upgrade_failed`), when that is a plain fixed message, instead of nothing.
- `provider-observe` and the billing probe report `account_unavailable` when COCOLO's own services
  cannot be reached, so attestation says to run setup instead of telling you to sign in to the client.
- Raw hook events no longer pile up: they were kept 90 days and removed only when the owner ran
  `learning-expire` (one project had 191,000 of them). Each event code now has a weight in the installed
  `config/retention-policy.json`: high-volume events such as tool and turn starts are kept 7 days, session
  and turn milestones 30, retries, repairs, escalations and stalls 90, and the host's own lifecycle events
  as long as their run's outcome. The collector removes expired raw events by itself, a small batch at a
  time, after writing one `event_summary` per run and day (counts per event code, first and last seen,
  span), kept 90 days. Linked events and backups are left alone, and records an earlier version kept keep
  the expiry they were given.

## 0.3.0: COCOLO, watched and stoppable children (Codex package 0.8.13)

- From the first CI run on Linux: the account service started only on macOS, because its owner-signature
  helper ran only under macOS's `sandbox-exec`, so no child could run on Linux. On Linux the helper now
  confines itself before reading any input, with a seccomp filter (no new processes or sockets) and,
  on Linux 5.13 or later, Landlock (no writes outside its private folder). Also: a command that has
  just started the collector waits up to 20 seconds for it (it was 8, too short on a busy machine),
  and an account request made while the account services are still starting waits for them, bounded,
  instead of failing. Tests that assumed macOS (its `/private/tmp`, how it closes a refused
  connection, its process table) now also run on Linux, and the provider-observation CLI test runs
  the account services in its own process, so it observes the fake clients and never the developer's
  own installed one.

- From the final security review: a child's process that detached from its tree while its parent
  exited between two checks could keep running after its place was given back. Every child now
  carries a fresh, private run marker in its environment, which everything it starts inherits, and its
  place is held while any of your processes still carries it or is still in the child's session. A
  process that clears its environment, or on macOS runs one of Apple's own tools such as `sleep` (macOS
  hides their environment), and also starts a session of its own is still not found; it stays inside
  the child's read-only sandbox. A process that could have carried the marker and ends mid-check makes
  the check list your processes again, so it cannot hide what it started (a busy build does not slow
  this down); a new Terminal tab (whose `login`
  cannot be read) never holds a place. Also: a run whose
  claim fence the account refused now asks again, waiting longer each time, instead of holding one of
  the project's four preparations until the service restarted; a preparation whose cleanup failed is
  cleaned up again later instead of being lost; and a finished run's monitor place that could not be
  freed is asked for less and less often instead of on every step. The documentation now says that the
  account settles a lost run on the project service's own evidence, within your own user's trust.
- From the real install's containment check on Codex 0.169: a Codex child was stopped with
  `codex_tool_not_allowed:mcp_tool_call`. It had called Codex's own built-in `list_mcp_resources`
  tool (reported as an MCP call from server "codex"). Codex 0.164 offers the same tool, and both
  builds also offered every child the sub-agent tools (`spawn_agent` and five more), because the
  model catalog turns them on whatever the feature switches say. Children are now launched with a
  setting that removes the sub-agent tools (checked on 0.164.0 and 0.169.0 without a model call), and
  are told not to call the MCP resource tools, which no setting removes; a call to one is still stopped.
- The product is now called COCOLO: Claude Orchestrator, Codex Orchestrator, Local Orchestrator. The
  README, the user documentation and the setup wizard's header and welcome screen use the name. Folder,
  package, skill, command, file and field names are unchanged, so nothing you type or see in command
  output changes. The README is rewritten as a short, friendly introduction; the technical detail it
  held (architecture, records, admitted client versions, known limits) is in the documentation pages,
  which were reviewed against this release: how-it-works describes the project services, run health,
  capacity limits and runs that close by themselves; the Codex commands rule is documented with its 27
  everyday commands (it said 23); statements about native Claude children are removed or corrected;
  troubleshooting explains `claim_busy`; and maintainers' design notes are listed separately, for
  developers.
- From the second real install: a finished Codex child never gave its slot back (Claude children did),
  so after two Codex runs no child could start anywhere on the machine (`claim_busy`). Real Codex runs
  short shell commands that it reaps itself; the process tracker could not tell such a vanished
  process from one that escaped, so it never confirmed the child's tree clean. A process whose id the
  kernel positively no longer knows now counts as stopped; an unreadable one, or one still running
  outside the tree, still does not. Checked on the real install: the slot is released two seconds
  after the child ends.
- From the first real install's checks (the tests had used short time limits and one child per
  project): a child with the route's standard time limit (6 hours) was refused at claim
  (`claim_storage_expired`), because its result space was reserved for 30 minutes; the reservation
  now outlasts the longest route time limit. A project could prepare one child at a time and a refused run
  never gave that back, so the next child failed (`run_unavailable`) until its service restarted;
  a project now prepares up to four (as many as it may run) and a refused run frees its share.
  `dispatch` now says why a run could not start, with the run's own reason code. The verifier's
  `validate-assignment` and `result-preflight` no longer pause the shared collector (they were
  refused while any child ran).
- A run that can never launch now closes by itself, with no command: a claim the account denied
  (stale billing, say) is fenced by the account, its result space and preparation are freed, and
  `dispatch` gives the denial's reason; a run that failed before its claim was sent frees them the
  same way. The orchestration can then close, and the next child launches. Called anyway,
  `learning-classify-host-run-abandoned` answers `already_closed`. A claim refused without a
  receipt (`claim_storage_expired`, say), or whose reply was lost, is now settled by the account's
  new claim fence: if the account admitted the claim the run keeps everything; otherwise the fence
  makes sure it can never be admitted and the run closes the same way, and the account no longer
  counts it as pending. A denied Claude child closes like a Codex one.
- From the first real install: setup, `learning-status` and the billing attestation run in your own
  terminal, with no Codex or Claude Code markers, and the account service refused them, so setup said
  "Collector status: not ready" and attestation stopped with "provider observation unavailable". Your
  terminal may now read status and observe providers (nothing that starts work or spends usage). A test
  walks those steps with every client marker removed.
- Setup is easier to follow: a failed step says why, with the orchestrator's reason code; `?` shows what
  ran in the step; projects set up before are offered in a list; the attestation names your own key file
  in its prompts, finds the key by its `.pub` file whatever its name, and asks again after a mistyped
  key location instead of stopping.
- Updating from 0.2.x: the wizard's Collector step asks before it upgrades your orchestrator records,
  keeps a checked copy of them first and says where it is. Every project on the machine shares those
  records and needs this release. Until this change the step failed on existing installs, and on a
  new machine it refused to create the records at all.
- Commands wait up to eight seconds for the shared collector to start (it used to be one, which a busy
  machine exceeds), and wait their turn when another command has the collector paused.
- Result-contract failures retain a bounded validator reason in the bridge report and durable
  dispatch metadata. The failed fence and exact result binding remain in force; rejected content
  is discarded. Host-result reporting obtains the reason from the trusted dispatch record.
  Version-1 records remain readable without invented historical reasons. This diagnostic slice
  uses Codex package 0.8.13; the development project's installed stable toolchain remains 0.8.12.
- Malformed explicit result axes now produce `invalid_status` instead of escaping the validator
  as an unhashable-value exception. Dispatch regressions cover failed fencing and durable reasons
  for list/object values on all three axes; legacy pending results still bind correctly after restart.
- Unrepresentable result usage numbers produce `invalid_usage`. Reroute annotation validates the
  original answer before changing its verdict or claim list. Invalid string encoding at the parser
  boundary and parser recursion keep the existing parse-failure outcomes; Codex last-message bytes
  retain their existing UTF-8 replacement decoding. New dispatch-record versions require integers. These fixes address
  findings from independent review/verifier pairs; the corrected slice awaits fresh review.
- A deeply nested Codex result that parses successfully but exceeds optional-null normalization's
  recursion limit now follows the existing `unparseable_result` fence and remains recordable.
- Bridge preflight rejects child-authored admission decisions and timeout claims before immutable
  result binding, matching the host's existing admission rules. These become failed replacements
  with `inconsistent_axes`; actual bridge-observed timeouts retain their authoritative outcome.
- Results that exceed the existing host-outcome evidence capacity are fenced as `result_too_large`
  before binding. The durable limit is retained, including room for assignment/result digests.
- Markdown result fences use linear extraction, preserving accepted syntax while avoiding regex
  backtracking on long whitespace in an incomplete fence. A bounded subprocess regression covers it.
- Legacy version-1 record validation retains its original numeric representation semantics and bytes;
  strict version typing is confined to the new dispatch metadata, preserving existing ledger replay.
- Reroute annotation updates the legacy status projection with its unknown verdict, so a valid
  blocked answer stays unreviewed without a host-created `inconsistent_axes` rejection.
- Malformed Claude init-frame tool entries retain the existing `init_tool_not_allowed` fence,
  child cleanup and recordable failed result instead of escaping through unhashable-value errors.
- Bounded Claude client observations retain process exit status, final-frame presence/subtype and
  allowlisted assistant error codes separately from result-validator reasons. Unknown values use
  fixed fallbacks. Version-3 dispatch metadata preserves these observations through cleanup and
  host commitment; version-1/2 bytes remain readable without invented historical observations.
  Existing outcomes, failed replacements, result/verifier digests and admission rules are unchanged.
- Unknown Codex event/item names now use a fixed `unknown` denial label before constructing
  returned or stored fences. Only known denied item names are retained. Regression tests pin
  independent legacy fence bytes and cover CLI rejection reasons, invalid Claude reroutes,
  and observed positive/signal exit statuses without retaining rejected child text.
- Offline service foundation adds a pure, closed request codec for project status and cancellation.
  It bounds messages, rejects duplicate/authority/SQL fields and binds canonical request digests to
  project, primary session, supervisor instance/generation, runtime and exact attempt target.
  Valid requests confer no authority. Authenticated transport, responses, account operations,
  independent supervisors and database migration remain unimplemented; no endpoint is enabled.
- Service-contract constructors enforce the same closed types as decoding; canonicalization
  revalidates their snapshot. Parser/decode exceptions are not chained onto fixed public errors.
  Adversarial checks distinguish specific identifier formats, enforce size checks before parsing,
  and independently bind run handles in request digests.
- The collector client uses a database-free metadata transport with one deadline across connect,
  send and response fragments. Slow replies cannot renew that deadline; ambiguous exchanges are
  never retried automatically. Existing framing, error mapping and runtime checks are retained.
  Project/account authorization, server queues and database ownership migration remain incomplete.
- Malformed peer runtime versions use the existing compatibility error path, including Unicode
  digit strings and integer-conversion rejection. Valid ASCII three-part versions retain their
  numeric comparison behavior. Adapter regressions retain negative binding/error propagation.
- Bridge prompts include the validator-owned enums and consistency rules from result preflight,
  with explicit legacy status projections for completed verdicts. The sample positive template
  does not request a positive conclusion. Result validation, host admission and fencing remain
  in force; the additional guidance is not an output field or new authority.
- The bridge and its detached watchdog now share one absolute monotonic hard deadline,
  including the existing watchdog grace. Fixture clock disagreement cannot kill a child
  early or extend cleanup after bridge death. No host clock setting is changed by the tests.
- Collector client code and its shared metadata/errors are separate from the SQLite owner.
  Existing imports remain compatibility aliases; protocol behavior and runtime checks are
  preserved. This is preparation for the service boundary, not completed API migration.
- An internal metadata queue has finite per-project quotas, round-robin scheduling and fixed
  overload codes. Separate lane instances can reserve control/terminal capacity. It grants no
  caller authority or durable receipt; service integration and measured defaults remain open.
- `dispatch-preflight` previews a committed bridge manifest and effective configured tools and
  bound units in pages of at most 16 KiB, without admission or launch. Export pins one commit,
  checks sizes before reading blobs and bounds Git output. Optional expected-commit checks
  prevent later preview pages or dispatch from silently switching snapshots. Exact tool-call
  enforcement remains unavailable and is reported explicitly; native preview remains open.
- Preflight resolves released learning candidates through the same trusted path as open and
  dispatch, without a base-route fallback or legacy fallback receipt. It reports nominal zero
  separately from the existing client turn-limit floor. Export rejects folded Git/context names
  and shares one absolute budget across inspection and copying; Git-command checks observe both
  process launch paths.
- Each project now has its own service, started on demand, that runs the project's children and
  owns the project's history. Children of several projects run at the same time; one project's
  child, registration, provider observation or owner attestation no longer waits for or stops
  another's. Normal commands talk to the project's service and the account service in the
  collector; they do not open the shared ledger or pause the collector.
- Every routed child is a separate read-only process, from either primary: `codex exec` or
  `claude -p`. A Claude primary's Claude children are no longer native subagents, so they can
  be watched and stopped from outside the session.
- `learning-run-status` reports a running child's health (`starting`, `active`, `quiet`,
  `stale`, `stopping`, `terminal`) and `learning-cancel-run` requests a stop. A child with no new
  work for ten minutes is stopped without a request. Stops are immediate for an invalid client
  start, a prohibited tool and a paid or rejected rate-limit event, with partial message
  streaming enabled for Claude children. Replayed, blank or other-model frames do not count as
  work.
- Verification and operational QA of a result go through the project's service. It keeps what a
  verifier needs only while it runs (twelve idle hours when it holds recorded results).
- A project's service can run any number of children over its lifetime: a finished,
  released and recorded child no longer keeps one of its four tracking slots (third real
  trial: the fifth run was refused with `run_failed`). Status of such a run answers
  `control_attempt_not_found`.
- If a project's service stops or crashes while a child is claimed, the project's next service
  settles that run by itself as soon as the operating system shows that the child, everything
  left in its process group and session, and its deadline watchdog are gone, and the watchdog
  confirmed before exiting that every process it tracked had stopped (or that it never
  started a child): the run is reported as `run_lost` (abandoned, outcome unknown, never an
  outcome) and its capacity slot is freed within seconds, exactly once (new account operation
  `account.settle_lost_run`, backed by a durable per-run launch journal in the project's state
  folder). A service stopped normally settles its own runs this way before it exits. Nothing is
  relaunched, and nothing is freed while any of the run may still be running. Only when that
  evidence is missing or damaged is the slot freed one hour after the run's hard deadline,
  measured on both the wall clock and the run's own monotonic clock (finding 18).
- Learning (case mining, analysis, inert proposals) runs on each project's own history after a
  result or its QA is recorded, and the command reports it (compact by default,
  `--full-report` for everything). A project's earlier learning history moves from the shared
  ledger to its store by itself on first use after an update, when no child is running;
  `learning-migrate-project` does it on request and can roll it back before new learning is
  recorded. Owner feedback, case review, analysis and the orchestration report work on the
  project's store. The candidate pipeline commands (evaluation, comparison, promotion, canary,
  rollback, drift, calibration) refuse for a moved project and return with 0.4.0.
- Commands that need the shared ledger to themselves refuse while any child is running instead
  of stopping the collector under it. A project service left running by an earlier version is
  replaced when no child is running.
- Not yet done in this entry: testing on a real install with real provider clients (event
  shapes and timings for both providers, two projects at once). Write-capable children belong
  to 0.4.0 and are excluded from this release.

## 0.2.2: readiness and hook diagnostics (Codex package 0.8.12)

- Repeated hook deliveries include their observation time in their identity, matching their timestamped
  contents. Previously, a repeated payload carrying a turn, agent or tool ID could conflict with the
  earlier record and be rejected. Conflicts remain denied and now return the fixed `record_conflict`
  code. Hook health retains the last fixed ingestion rejection code without event contents.
- `learning-status` distinguishes idle ingestion from waiting for the first hook event and points to
  `/hooks` on the main checkout. It shows each provider's attestation state using admission's existing
  owner-verified project/account lookup. `billing_attestation_missing` names the wizard's re-attestation
  option; admission and child permissions are unchanged.
- Validation explains that a writable Codex mapping is skipped: routing tries other eligible providers
  or denies the task. The usage guide documents proposed patches applied by the primary; wizard step 8
  names the clone and folders containing set-up projects, and the hook trust reminder applies to main checkouts.
- The manual QA recipe runs Python wrappers in-process and puts no flags in their first line. It warns
  about bytecode and resource limits. Operational QA returns separate fixed failure-stage codes and
  each check's exit code, while keeping stdout/stderr private and preserving the check report and receipt
  contracts.
- The Claude skill documents `result_attempts`. Observation admission errors name the failed contract
  rule without values. The attestation test helper closes its pty before waiting for the child.
- The Codex session guard now reports the existing sandbox-disabled refusal when Codex omits
  filesystem entries for `danger-full-access`, rather than masking it as an unknown record format.
  Sandboxed sessions still require the complete filesystem policy; admission is unchanged.
- Contributors must test a release on a real install from its `version/*` branch before opening its
  pull request to `main`. This feature branch does not satisfy that owner-run release gate.
- **Independent verification.** A routed Claude Opus/high source review found a false packaged-fallback
  warning, QA documentation overclaims and a misleading collector-down status message. These are
  corrected, with CLI regressions for missing-attestation guidance, slow signature lookup and unavailable
  status. The lookup uses a longer status timeout; signature checks still share the collector thread,
  so status can delay or drop hook events and produce ambiguous failure counters as attestations grow.
  Historical lifecycle gaps cannot be attributed from
  aggregate counters alone. The earlier Haiku research report was rejected by its routed Codex/high
  verifier for overclaims; only the reproduced hook collision informed the fix.
- **Release security review continuation.** A routed Codex Astra/high source review and its independent
  Claude Opus/high verifier found no reproducible authority, privacy or QA-diagnostic security defect.
  The verifier caught SECURITY.md still naming the previous release; its version text is corrected.
  Both operational checks passed, but managed QA rejected the original review's documentation criterion.
  That recorded rejection remains; these documentation corrections have local checks only.
  The repository suite passed 271 tests (two skipped), and the Claude suite passed 100. The Codex suite
  passed 710 of 711; the remaining bytecode-fixture test conflicted with the invocation's cache-prefix
  override and passed in a 17-test focused rerun without it. Ruff and the file and new-commit identifier
  scans passed. The full-history scan still flags two pre-existing merge authors against the local
  private denylist. Source review and these checks do not satisfy the real-install release gate.

## 0.2.1: Codex primary usability and owner key replacement (Codex package 0.8.11)

- **Codex skill input forms.** A Codex primary kept failing on its own inputs, so:
  - `references/contracts.md` opens with a worked task, assignment and parent grant, and the skill says to
    read it before writing the first one. A test runs the example through the real validators.
  - `host-command` also prints `command_prefix` and `next_command`, once the rule is installed: the command
    as text to run as printed, because the rule did not match a command with every word quoted. A word is
    quoted only if it cannot run otherwise, such as a path holding a space; whether the rule matches such a
    path has not been tested.
  - `input validation failed` now carries `field` for a task, assignment, parent grant or route, such as
    `task.family`. It is built only from the contract's own names and list indexes, so an input's values and
    unknown keys are never printed.
- **Attestation scope in the docs.** Troubleshooting and the security model now say attestations are kept per
  project, so a project not yet attested cannot run children without a new signature.
- **Replace a lost owner key.** A new wizard option (`attest.py --replace-owner-key`, CLI
  `learning-replace-owner-key`) creates a new passphrase-protected key, records it and signs for the project.
  It cancels billing statements and unused grants the old key signed, when a run opens and when a bridged
  child launches; QA, calibrations and promotions stay in force, and a replaced key cannot be made current
  again, in any encoding.
- **Owner key tampering closed at the ledger level.** An emptied or edited signer file used to accept any
  new key, even one with no passphrase, and billing statements kept counting after a swap. The ledger now
  records the enrolled key (`owner_key_history`, append-only), a signer file that differs is refused, and
  admission checks each statement's signature against the current key, using the checked bytes rather than
  re-reading the file. `learning-status` reports `owner_key`. Claude deny rules now cover the default records
  folder. The security model's new section lists what this cannot stop.
- **Independent security review of the owner key change.** Two rounds, each by Codex (`gpt-6-astra`) and
  Claude (Opus), reading the code by hand because the toolchain cannot run children until this ships. Both
  rounds returned NO-GO. Neither found a way to get a statement counted without the current key's signature,
  short of same-user tampering. Round one found a run still launching after a replacement, the signer file
  being re-read after its check, and overclaiming docs. Round two found a replaced RSA key returning in
  another encoding and malformed keys being recorded. All were fixed; the round two fixes (the strict key
  parser, damaged signer files, the docs) have tests and were not reviewed a third time.

## 0.2.0: cross-provider read-only routing (G2; dogfood toolchain v1, Codex package 0.8.10)

- **Codex package 0.8.10.** The final v1 fixes below get their own package version: a collector
  authorizes exactly one build per version, and the earlier 0.8.9 build (recorded again in
  `runtime-releases.json`) was already authorized on the pilot machine.
- **Documentation layout.** The README is now a short overview; the detail moved into `docs/`, indexed by
  `docs/README.md`: getting started, using it, configuration, how it works, the security model (which
  replaces the README's safety section and the design document's residual-risk lists as the place
  accepted limits are listed) and an FAQ. CONTRIBUTING.md, a pull request template and a bug report form
  were added. The delivery plan and the G2 design moved, with the research, to the separate `planning`
  branch, so release branches carry only user and contributor documentation.
- **Final documentation pass.** Troubleshooting moved to `docs/troubleshooting.md`, with new entries
  for `host_client_unknown` (a Codex session started from inside Claude Code), the unmoved macOS
  records and an unusable `XDG_STATE_HOME`. The setup wizard reference covers the install step's
  close-sessions line, when to trust the hooks, the approved records move and step 8's default;
  the README and SECURITY.md now list S6's residual for the old macOS records; the plan lists the
  owner-copy hooks as a v1.1 candidate.
- **G2 v1 final hooks (0.8.10).** Codex runs the learning hooks outside its sandbox, and the
  static `.codex/hooks.json` ran `python3` from `PATH` (on macOS the `/usr/bin` xcrun shim,
  which reads a cache a session can write) and found the hook through `git rev-parse`
  (which follows `.git` and `core.worktree`).
  - The installer now renders the file per checkout (`codex_agents/hook_config.py`): every
    hook runs `/usr/bin/env xcrun_nocache=1 '<interpreter>' -I -B -S -X pycache_prefix=/dev/null
    '<project>/.codex/orchestration/learning_hook.py' --checkout '<project>' --client-version
    codex-hooks-v1`, with
    the trusted interpreter the installer runs under (the same checks as the Codex rule) and
    no PATH lookup or git. Paths holding a quote, a backslash or a control character are
    refused.
  - The file holds this checkout's absolute paths, so it is never committed: the package's
    `.codex/.gitignore` keeps it and `.codex/orchestration/local-install.json` (the
    interpreter and the file's digest, which verify, update and remove use) out of Git. An
    update replaces 0.8.x's committed static file, and the wizard's commit takes it out of
    the index.
  - Codex 0.164 runs a linked worktree's events through its main checkout's hooks, so the
    command pins `--checkout '<project>'` and the hook first checks, with the standard library
    only, that the event's folder belongs to that checkout (it exists and resolves inside it,
    and the nearest `.git` is its own; a `.git` that cannot be read refuses); events from a
    worktree or a nested repository import nothing and start no collector. A subfolder's events record for the checkout.
  - Verify compares the file byte for byte with a fresh render and re-checks the recorded
    interpreter; the old form, an edited command or an untrusted interpreter is a project
    error that says to run `./setup.sh`.
  - Codex trusts hooks by hash: after an install or update, open `/hooks` in Codex and trust
    the orchestrator's learning hooks (the wizard says so; the installer never writes trust).
  - The hook's collector start gets `xcrun_nocache=1`; an unusable state location is a
    silent no-op like an unregistered project. The 0.8.10 runtime digest is recomputed.

- **G2 v1 final runtime fixes (0.8.10).** From the final whole-release review.
  - **Learning state location.** On macOS the default learning state was
    `~/Library/Application Support/codex-agents`, which a Codex session opened on `~/Library`
    could write (ledger, digest key, collector authorization, `owner-allowed-signers`). The
    default is now `~/.local/state/codex-agents` on every platform, with the home folder
    from the account database, never HOME. `XDG_STATE_HOME` moves it only when it is
    canonical and strictly inside a hidden folder of HOME (such as `~/.local/state`); any
    other value refuses ("XDG_STATE_HOME must be inside a hidden folder in your home, such
    as ~/.local/state; unset it or move it"). The folders above the state must be the
    owner's, writable by no one else and not symbolic links; missing ones are created 0700.
  - **No second hard link.** State files, including the owner's allowed signer and the
    digest key, refuse a link count other than one. The allowed-signers file is validated
    whole: anything over 4096 bytes refuses, since `ssh-keygen` reads every line.
  - **One-time move.** The owner's `learning-register` (the wizard's collector step) moves
    the old macOS folder by copying, not renaming: with the collector stopped and the
    ledger locked, it refuses a hard-linked, symbolic or special file, copies every file
    into fresh inodes in an owner-only staging folder (the ledger through SQLite's backup
    API), checks the copy, renames it into place and renames the old folder to
    `codex-agents.moved-<UTC time>`, which `state_moved` names. On any failure the staging
    folder is removed and the old folder is untouched; both folders present, or a socket
    path that would be too long, refuse. Every other use of the default state refuses while
    only the old folder exists, telling the owner to run `./setup.sh` (collector step).
    `host_paths.owner_state_root()` gives the owner tools the folder in use until then. The
    old copy, with the digest key, stays readable until the owner deletes it.
  - **Session guard coverage.** The guard also refuses a session that can write the learning
    state (with its `scratch`), the bridge's or the owner tools' host-private scratch
    container, a folder that strictly contains the copy's bound project, or the project's
    `.codex`, `.agents` or `.git` as a folder of its own: Codex runs a nested trusted
    project's `.codex/config.toml` and hooks outside the sandbox. `dispatch` is guarded like
    every rule-matched command, so a session that could write the bridge's control files
    never starts a child. Open Codex on the project itself.
  - **Session scan.** `codex-session-scan` also warns about a recent session that could
    write the learning state (and the old macOS folder while it exists) or that was opened
    on a folder containing a set-up project (named by runtime-copy bindings or the ledger's
    registrations). Each warning carries `holds` (`home`, `clone`, `state`, `project`) and
    a plain `message`.
  - **Install verification with mapped writers.** The package smoke routes use the package
    policy alone, so a project that maps a workspace-write Codex agent (for coding, or as
    verifier) no longer fails install verification; validation warns instead ("<agent> is
    workspace-write; Codex children are bridged read-only in this release, so it is not
    used for Codex routes"), and a route denied for that reason says so.
  - The 0.8.10 runtime digest is recomputed.

- **Final review (0.8.10).**
  - The package installer refuses, before writing anything, when the project's
    `.codex/orchestration` holds files it does not install (a `socket.py` planted by a
    session that ran before the install, bytecode, a package), and verify reports it. The
    hook and the project's `cli.py` append their folder to `sys.path` instead of putting it
    first, so with `-I` a planted module never shadows the standard library. The wizard says
    to trust the hooks in Codex only after `check-runtime` passes, and the install step
    starts with "Close every Codex session in this project first".
  - The one-time move of the old macOS state needs the owner's approval
    (`learning-register --move-legacy-state <signer file SHA-256 or none>`): the wizard shows
    the old folder's creation time, the owner key it names and whether that is
    `~/.ssh/codex-agents-owner.pub`, asks, and the runtime checks the digest on its fresh copy.
  - Verify warns when `.claude/settings.json` defines hooks; the README, SECURITY.md and S6
    list the residuals (sessions running during an install; Claude settings and skills a
    Codex session can rewrite). The 0.8.10 runtime digest is recomputed.

- **Owner key links and client output (owner review).** `attest.py` refuses an owner private
  key whose file has another hard link ("This key file has another link elsewhere; make a fresh
  copy in ~/.ssh (cp, not mv) and remove the old one"), at every use and again on the file it
  opens itself: a key moved into `~/.ssh` from a folder an agent can write kept that link, through
  which the agent could rewrite it after the checks. Each operation that uses the key (the
  passphrase check with the derivation, signing, the passphrase change) now works on a private
  0600 copy read once into the owner's scratch container and removed afterwards, so a
  descriptor kept on the key's inode cannot change what was checked; the passphrase change
  saves the re-encrypted copy only after checking it, as a new file renamed over the key. `client-status` reads a client's output with
  the runtime's bounded reader (`providers.read_bounded`) and stops the client's process group the
  moment it passes 256 KiB, instead of buffering it all first.

- **Owner tools follow the state rules.** `scripts/attest.py` and `scripts/install.py` show the
  runtime's plain refusal when the learning state location is unusable (an `XDG_STATE_HOME`
  outside a hidden home folder or not canonical, or an unsafe `~/.local`), and the wizard maps
  it to a fixed message. The collector step's one sentence about the move now names the old
  copy (`state_moved`, control characters removed) and says it can be deleted once you're
  happy. Tests set a canonical `HOME` and `XDG_STATE_HOME`.

- **Owner-side final fixes (whole-release review).**
  - `setup.sh` no longer runs `codex` or `claude` from the inherited `PATH`, where an earlier
    folder a Codex session can write could hold a planted program. The sign-in checks go
    through attest.py's `client-status` owner tool, which finds each client with the
    runtime's own `providers.resolve_client` (only `/opt/homebrew/bin`, `/usr/local/bin`,
    `/usr/bin`, `/bin` or `~/.local/bin`, its real file location-checked before it runs) and
    runs only its fixed status command with the runtime's minimal client environment
    (`client_environment`: no `CODEX_HOME` or API keys) in a private working folder, with a
    deadline. A client that fails is reported in plain words and never run; one elsewhere on
    `PATH` is named as such.
  - The wizard starts with `/bin/bash` (not `/usr/bin/env bash`) and finds its basic tools
    only in `/usr/bin:/bin:/usr/sbin:/sbin`; the owner's `PATH` is used only to look up
    `python3`, which is checked before it runs.
  - `attest.py` takes an owner private key only from the account's own `~/.ssh` (the home
    folder from the system account database, not `HOME`): no symbolic link anywhere on its
    path, `~/.ssh` and below owned by the user and writable by no one else, checked again
    before every `ssh-keygen` use (enrolment, signing, passphrase change). A key anywhere
    else is refused, which closes the swap between the encryption check and the derivation.
  - Owner tools locate the learning state only through this repository's package
    (`host_paths.owner_state_root`), so the installer's ownership record follows the move to
    `~/.local/state/codex-agents` and is never created beside the old macOS folder before
    the collector step moves it; their scratch container is the runtime's
    `OWNER_SCRATCH_CONTAINER`, which the guard protects. The collector step says once when
    `learning-register` moved the records (`state_moved`), and the session warning shows the
    scan's own `message` for each finding (home, clone, state or project). Tests pin
    `XDG_STATE_HOME` inside their scratch home and fail if the state would resolve outside it.
  - README, the setup wizard reference and manual setup now say to open Codex on the project
    itself, never on a folder that contains a set-up project (Codex runs a nested trusted
    project's `.codex/config.toml` and hooks outside the sandbox), and that the clone is
    only for running `./setup.sh`.

- **G2 v1 runtime hardening (0.8.9).** Fixes from two whole-release security reviews.
  - The Codex command rule no longer runs the project's `.codex/orchestration/cli.py`,
    which a session opened on a parent folder could rewrite. `install-codex-rule` copies
    exactly this package's runtime files (a constant of the installing code, never the
    project's manifest), verified byte for byte and against this package's recorded
    runtime, to an owner-only `~/.codex/agent-orchestration/<project>/<runtime>/` copy
    bound to its project, and the rule names that copy. The runtime digest, also the
    collector's identity in `runtime-releases.json`, now covers every file the copy holds
    (configuration and schemas included). Setup refuses a project whose
    `.codex/orchestration` holds anything else (a planted `socket.py`, a package, bytecode,
    an extension module), and the copy refuses an extra entry of its own before its folder
    is importable. Only the account's real `~/.codex` is supported (the home folder comes
    from the system account database): a HOME or `CODEX_HOME` that disagrees with it or a
    symlinked `~/.codex` is refused, and `host-command` reports it as `unsupported`.
  - **Session workspace guard.** Before every primary-host command the protected copy runs,
    it finds the calling Codex session's record from `CODEX_THREAD_ID`
    (`~/.codex/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl` under the account's home, no
    symlinks, owner-checked), reads only its `session_meta` and its last `turn_context`
    (backwards, bounded), and refuses a session without its sandbox (`danger-full-access`),
    without a record (`--ephemeral`), with several or a shape it does not know, or able to
    write `/`, the home folder or a folder above it, `~/.codex`, the copy, the clone that
    installed it (now recorded in the copy's binding) or a trusted installation folder.
    Paths are compared by file-system identity and case-folded name, as recorded and as
    they resolve, so a symlink swap or a case variant cannot hide a protected folder; a
    read-only session's workspace roots are not counted as writable.
    `host-command` reports the verdict as `session_guard`, and the wizard's first screen warns
    about a recent session that could write the clone or the home folder.
  - The interpreter the rule names, provider clients and git must come from a trusted
    installation folder (`host_paths.TRUSTED_INSTALL_ROOTS`: system folders, Homebrew,
    python.org, Xcode and its Command Line Tools, Nix) or a hidden top-level folder of the
    home folder such as `~/.local` or `~/.pyenv`; a sibling install such as `~/Work/tools`
    is refused. The copy serves only its bound project, only
    the primary-host commands, and only while the project's installed files still match
    it. A project without an install manifest gets no rule. `host-command` reports
    `installed` only when rule, copy and project all match (earlier rules are `stale`),
    and its `argv_prefix` names the copy. New owner command `remove-codex-rule
    --project-root <root>` removes the rule and the copy and prints
    `{"ready": true, "removed": [...]}` (an empty list when there is nothing, for any
    project, including one in a temporary folder).
  - The supported threat model is stated plainly in the wizard, README, design and Codex
    skill: while the rule exists, no Codex session may use the home folder, its parent or
    `/` as its workspace, or add `~` or `~/.codex` with `--add-dir`; home-installed tools
    such as Claude Code stay supported under that rule.
  - Every Python launched outside the sandbox (the rule's prefix, the collector, the
    deadline watchdog, the learning hooks and the Claude skill's commands) adds
    `-X pycache_prefix=/dev/null`: `-B` only stops writing
    bytecode, and CPython still loaded a planted `.pyc` from `__pycache__`. Rules without
    it are `stale`, and a copy holding any file its manifest does not list, bytecode
    included, fails verification.
  - Git runs from a real binary (Command Line Tools, Xcode or Homebrew on macOS, never
    the `/usr/bin/git` xcrun shim whose cache sits in `$TMPDIR`) outside the inspected
    repository, with `xcrun_nocache=1` for git, provider clients and operational checks.
  - A provider client is refused unless its resolved file passes the same location checks
    as the interpreter, outside the project and every temporary directory, before it
    runs.
  - A worker result kept for its verifier is deleted when its orchestration closes, and
    seven days after its outcome at most: each ledger start clamps older expiries (90 days
    before) and sweeps, the collector sweeps every minute, and an expired result is never
    read. A failed deletion stays registered and is retried, and a collector request must
    arrive within five seconds, so a slow sender cannot hold off the sweep. Only digests
    stay.
  - Every Codex child is bridged (`codex exec --ignore-rules`, read-only), from a Codex
    primary too, so no child shares the primary's command rule. Codex routes that need
    write access are not eligible, and the native-only legacy fallback is refused for
    bridged routes.
  - `dispatch` fences a child result that does not validate and binds a keyed digest of
    the result it returns, with its terminal state, to the run; recording any other
    result, or dropping or inventing the fence, is refused.
  - Both bridged launches pass `--` before the prompt, so an objective can never be read
    as a client option.
  - Bridged children receive each required criterion's description, not only its id, and
    a bridged Codex child receives its routed role's developer instructions (as
    `-c developer_instructions=...`), refused unless they match the run's recorded
    execution configuration.
- **Owner-side round 2 (0.8.9).**
  - Setup and the installer no longer run `status`, `add` or `commit` on the project: state
    comes from `ls-files`/`ls-tree` and byte comparison, and commits from
    `hash-object -w --no-filters`, `update-index`, `write-tree`, `commit-tree --no-gpg-sign`
    and `update-ref` with the old value. Every owner Git process sets `GIT_ALLOW_PROTOCOL=""`
    and `GIT_NO_LAZY_FETCH=1`, and partial clones and promisor remotes are refused.
  - The interpreter rule is the runtime's (`host_paths.TRUSTED_INSTALL_ROOTS` and
    `install_location_problem`), replacing the home tool-folder list.
  - The deny-rule ownership record moved to the owner's learning state; an in-project record
    from an earlier version is used once, only without an owner record and only for generated
    rules, then deleted. Settings are staged in that owner folder and renamed into place after
    re-comparing the destination.
- **Owner-side review fixes (0.8.9).** Two further reviews of setup, attestation and the
  installer, under the narrowed threat model (sessions may be opened on a project's parent
  folder, never on the home folder, its parent or `/`; this repository must sit outside every
  folder opened in Codex, as the wizard's first screen and the README now say).
  - Owner commands (registration, status, attestation, `host-command`, `install-codex-rule`,
    `remove-codex-rule`) run this repository's own orchestrator CLI with `--project-root`,
    never the project's `.codex/orchestration` code. Steps that depend on the install first
    require it to be this release byte for byte, and the check now refuses any extra entry in
    `.codex/orchestration`: a symlink of any name (an extensionless `hashlib` link to a
    package passed before), a package, bytecode or another file. `install-codex-rule` from this
    repository copies bytes equal to this repository's runtime; uninstall runs this
    repository's `remove-codex-rule` whatever version the project has.
  - `attest.py` signs in memory: the approved bytes go to `ssh-keygen -Y sign` on stdin and the
    signature comes back on stdout; the enrolled key and the signature reach the CLI through
    pipes (`/dev/fd`), and `check-novalidate` verifies the in-memory bytes. No key, statement or
    signature file exists.
  - Exactly one owner signer: `attest.py` and the runtime's `owner_allowed_signers()` refuse
    an allowed-signers file that is not exactly one canonical `codex-agents-owner <type> <key>`
    line (0.8.9's recorded runtime digest updated).
  - Git runs from a trusted real binary (the runtime's `git_executable` rules: never macOS's
    `/usr/bin` shim or a PATH lookup) with hooks, the file-system monitor, commit and tag
    signing and automatic maintenance off. Before any status, staging or commit, setup and the
    installer read the repository's own settings with `git config --get-regexp` plumbing and
    refuse when they name a helper program (filters, diff and merge drivers, `core.sshCommand`,
    `gpg.program`, credential helpers, pagers, editors, trailer commands or any include).
  - The interpreter is trusted before it first runs: refused in a temporary folder, the project
    (the starting folder's project, then the chosen one), a virtual environment, macOS's
    `/usr/bin/python3` shim, or a folder under home other than an interpreter manager's. Every
    owner Python launch and every command shown to the owner adds `-X pycache_prefix=/dev/null`.
  - The chosen operational-checks file is snapshot into private scratch at once, and the preview
    and the install read that snapshot. A scratch base under the home folder is refused.
  - The permissions record counts an entry as the installer's only when the current or an
    earlier release generated it, so a planted record cannot delete a user's rule. Settings are
    replanned just before writing and refused if either file changed since it was read; the
    record is written before the settings on install and removed after them on uninstall, so an
    interruption leaves a state the next run repairs.
  - On Linux only the operational-check sandbox failure is reported as the known limitation;
    every other verify failure is shown. The Commands step explains `codex_rule: unsupported`
    with its reason and the fix (the real `~/.codex`, no `CODEX_HOME` elsewhere, no symlink).
- **Owner-side hardening (v1 security review).**
  - `setup.sh` and `scripts/attest.py` keep every working file in a fresh owner-only folder in
    the per-user cache folder (macOS) or `$XDG_RUNTIME_DIR` (Linux, refused when unset), created
    through no-follow, ownership-checked descriptors; nothing goes to `/tmp` or `$TMPDIR`, where
    a sandboxed agent could swap the enrolled key, the statement or the install plan.
  - `attest.py` passes the billing grant on stdin, rebuilds the signing payload from the approved
    statement (the runtime's `authenticated_owner_record` construction) and refuses any other
    bytes, checks with `ssh-keygen -Y check-novalidate` that the signature covers them, and after
    enrolment refuses unless the configured owner key is the one it showed.
  - Every Python the wizard starts, `attest.py` included, is the real file behind `python3` on
    `PATH` (refused in a temporary folder, the project or a virtual environment) and runs with
    `-I -B -S`; `attest.py` imports only the standard library. The project's CLI runs only after
    its runtime files match this repository's copy byte for byte (same version, no extra
    modules), with `-X pycache_prefix=/dev/null` so a planted `__pycache__` file is never loaded.
    The owner-side review fixes above later moved every owner command to this repository's own
    CLI.
  - Git on the project runs with `-c core.hooksPath=/dev/null -c core.fsmonitor=false`
    (`--no-optional-locks` for status); the wizard says its commit skips the project's hooks.
    Owner-side round 2 above replaced `status`, `add` and `commit` with plumbing.
  - `scripts/install.py remove` first removes the Codex commands rule and its protected runtime
    copy through the project's `remove-codex-rule`; an installed CLI without that command makes
    it stop and name the rule file to delete (the owner-side review fixes above later made it run
    this repository's own `remove-codex-rule` instead). The Commands step now says every Codex
    session loads the rule, which runs a protected copy, and never to use the home folder's parent
    or `/` as a Codex workspace.
  - The installer merges Claude Code permission deny rules into `.claude/settings.json` for every
    owner-only CLI subcommand (the parser's commands minus the 23 primary-host ones) and `Edit`
    rules for `.codex/**`, `.agents/**`, `.claude/settings*.json` and `.git/**`, keeping user
    entries; `verify` reports missing rules and `remove` takes out only what install added. These
    rules are a policy layer, not a sandbox.
- **Bridged child containment (0.8.8).**
  - Bridged Codex children run with `--ignore-rules`: `--ignore-user-config` skips only
    `config.toml`, so a child still loaded the owner's execpolicy rules, and the setup-written
    rule would have let it run the primary-host commands, `dispatch` among them, outside its
    sandbox. A client whose `codex exec --help` lacks the option is refused before launch.
    `approval_policy="never"` is pinned and `skill_mcp_dependency_install` is disabled.
  - Codex system or managed configuration (`/etc/codex`, `~/.codex/managed_config.toml`, macOS
    managed preferences) refuses a Codex dispatch (`codex_managed_config_present`).
  - Cleanup reaches descendants outside the child's process group, conservatively: a
    fixed-path `ps` lists candidates, and a process is adopted only when its own kernel record
    (parent, group and birth time read together) names a tracked parent whose record still
    matches; every SIGKILL is preceded by a fresh record read, and the child's own group is
    killed before the child is reaped. The
    deadline watchdog verifies the child's identity before the run continues and tracks the
    tree too. A process that detaches between snapshots is left running in the sandbox rather
    than risk killing an unrelated one.
  - Native Codex children shared the primary's session and its command rule; the design's
    residual risks and the Codex skill said so until 0.8.9 bridged every Codex child.
- **Setup wizard retry.** A wrong owner-key passphrase no longer stops `scripts/attest.py`: it says
  "That passphrase did not unlock the key." and offers Try again or Exit, for the key check, the
  signing and the passphrase change (other failures still stop). In `setup.sh`, no failed step
  ends the wizard: after a failure the choices are Try again, Back to the main menu (the
  project menu) or Quit, the final summary also offers Back to the main menu, and only Exit or
  Quit ends the script. Saving a summary returns to the final menu, and going back starts the
  next attempt from a clean state.
- **Route-first model guidance.** The Codex skill and its routing reference no longer summarize
  per-family models, which had drifted from policy v5's provider candidates; they defer to the
  route's provider, model and effort.
- **Codex agent paths bind (0.8.7).** Codex 0.164 names a spawned child by its agent path
  (for example `/root/<task_name>`); `learning-bind-host-agent` accepts slash-separated paths of
  the same bounded tokens, and the Codex skill said to bind that path. Since 0.8.9 a Codex
  primary spawns no native children, so the Codex skill no longer binds one.
- **Setup wizard polish.**
  - The commit step lists and commits only files that really changed; with none it says
    "Nothing to commit" and skips the question. An existing install is described as an update.
  - `scripts/attest.py` finds the owner key at its default location without asking, and an entry
    that is not a file location is refused without being echoed. `--change-passphrase` (also in
    the wizard's existing-install menu) changes the owner key passphrase through `ssh-keygen -p`.
  - Key-location prompts are read with echo off, `ssh-keygen` never receives `SSH_ASKPASS`, the
    standalone passphrase change refuses an unsafe or temporary state folder and checks the key
    before changing it, and the commit step reports files that `.gitignore` hides and keeps a
    rename's deleted source.
  - The Codex profile step compares effective settings, so a profile with comments and the same
    settings is reported as matching, and a differing setting is named.
  - The saved failure summary defaults to the home folder and asks before saving into a Git
    working tree.
- **Provider-qualified routes.**
  - Policy v5 lists Codex and Claude models and roles.
  - Each family and route key has an ordered list of provider candidates, matching
    `docs/routing-table.md`, and a table-driven test fails CI on drift.
  - Selection is the first candidate that passes the static eligibility checks; there is no
    silent fallback.
  - Routes carry the provider, the dispatch path and the provider configuration digest, and
    replay exactly.
- **Project provider configuration.** `.codex/orchestration/provider-routing.json` enables
  providers and models, and controls whether a Codex primary may bridge to Claude. It must be
  committed and clean.
- **Admission gate.** Fails closed, and its decision is bound to the run. It denies:
  - API-key and alternate-backend environments;
  - an unreviewed client version;
  - an identity older than 30 minutes or a non-subscription plan;
  - a missing owner billing attestation;
  - stale, unknown or paid-usage billing evidence;
  - exhausted headroom (90% of a window, or a rejection);
  - a per-account run-count cap.

  Billing, headroom and bootstrap evidence apply account-wide, across projects.
- **Dispatch bridge.** Starts a provider's client for a read-only child: the other provider's,
  and since 0.8.9 every Codex child, from either primary.
  - The workspace holds only the committed bytes of granted paths.
  - The Claude profile is locked.
  - The Codex profile is locked: account connectors, web search, image generation and goals are
    off, and an event allowlist detects anything else.
  - Every run has a durable intent, a hard deadline and no resend.
  - Billing is re-checked at launch, and a run stops on in-run overage or rejection.
  - Bridged runs bind and commit only their own dispatch.
- **Claude primary.** A `claude-host-v1` host client and a Claude orchestration skill, installed
  with the Codex package.
  - Host detection and per-orchestration host ownership.
  - Native Claude spawn parameters are computed by the CLI.
  - A gated billing probe supplies the evidence native Claude children cannot.
- **Observations.** Headroom, billing and usage records are bound to their run and phase. A failed
  reading is recorded instead of dropped.
- **Codex credits attestation.** An owner billing attestation for Codex may carry
  `codex_credits_spending_disabled: true`, which lets a reading of credits that are not unlimited
  be admitted, at admission and at the launch re-check. `unlimited` credits still deny.
  `spendControlReached` is now recorded and denies as `codex_spend_control_reached`.
- **Package version.** The Codex package moved to 0.8.0. It had kept the imported 0.7.2, so an existing
  collector authorized for 0.7.2 refused the changed build as a conflict instead of upgrading.
- **Release guard.** A collector authorizes exactly one build per package version, so every runtime
  change needs a version bump. `packages/codex-agents/runtime-releases.json` records the runtime digest
  for each version, and a test fails when the code changes without a new version and entry. This
  was 0.8.1.
- **Collector bytecode.** The detached learning collector runs in isolated mode, which ignores
  `PYTHONDONTWRITEBYTECODE`, so it wrote `__pycache__` into the project's managed `.codex/` paths.
  It now starts with `-B`. This was 0.8.2.
- **CI.** The checkout and Python setup actions move to releases that run on Node 24.
- **Verifier handoff and lifecycle fixes (0.8.3).** A verification task may set
  `verification_for_outcome` to the worker's `host_outcome_digest`; the router checks it is
  this orchestration's outcome from the named provider. A bridged verifier gets the worker's
  ledger-recorded result read-only at `.orchestration-input/worker-result.json`; a native
  verifier gets it from `learning-open-host-run`. `learning-record-host-result` prints the
  `host_outcome_digest` that operational QA takes, a bridged run's outcome carries the
  bridge's recorded usage, `provider-observe` shows `paid_usage_waived_by_attestation`, and
  both skills give the exact QA commands with every argument.
- **Claude verifiers pass operational QA (0.8.4).** QA accepted only the Codex verification
  role, so a native Claude verifier of Codex work could never pass. It now accepts the routed
  verification role of either provider, bound to the verifier run's admitted provider; a
  Claude verifier must be the other provider than the worker.
- **Review fixes (0.8.5).** A verifier's open now stores the worker outcome it was bound to and
  refuses an assignment whose context names any other; operational QA accepts only a verifier
  opened for its exact worker outcome. A verification-required result too large to keep as
  canonical JSON is refused before the run is consumed, instead of being recorded without
  the copy its verifier needs.
- **Codex host command boundary (0.8.6).** A Codex primary runs the CLI inside Codex's
  sandbox, which blocks the learning collector, so every collector-backed command failed.
  A new wizard step, Commands, offers a per-project Codex command rule in the user's rules
  layer. It lets only the primary-host subcommands, run with an exact interpreter, flags
  and project `cli.py`, run outside the sandbox; owner commands stay sandboxed.
  `host-command` reports that exact command prefix and whether the rule is installed, and
  the Codex skill runs it first and stops until the rule is in place. Because the rule
  cannot constrain later arguments, an installed CLI pins these commands to its own
  project, the default learning state and its installed policy, refusing any other
  `--project-root`, `--root`, `--state-root` or `--policy` before it runs git or reaches
  the collector. Nothing such a command runs or trusts comes from a sandbox-writable place:
  the rule names the resolved, trusted base interpreter with `-I -B -S` (never a virtual
  environment's, a project's or a temporary one), and the collector and watchdog it starts
  get the same flags; a project in a temporary directory never gets a rule; trust checks
  use git plumbing only, with every repository-configured helper and every transport off,
  and refuse a shared repository inside the working tree or in a temporary directory and
  a partial clone; bridge exports, control files and child, probe, operational-check and
  provider-client temporary directories move from the system temp directory to owner-only
  host directories that overlap no project or temporary directory. A new rule never overwrites one that
  another setup wrote meanwhile. A sandboxed collector failure now carries a hint. The
  result template and the bridged child's prompt state one evidence entry per criterion,
  at its most severe outcome, so neither a child nor a repair can soften a `fail`. The
  orchestration report's `runs.native_agent_bound` is now `agent_bound`, because it counts
  bridged runs too. 0.8.9 replaced the project `cli.py` in the rule with an owner-only protected
  copy and added `-X pycache_prefix=/dev/null` to every Python launched outside the sandbox.

Security: two whole-release reviews by two vendors, over two rounds, found 14 defects; all were
fixed. Two further whole-release reviews of the v1 candidate returned NO-GO; their findings are
fixed in the G2 v1 runtime hardening and the owner-side entries above. A separate review of the
Codex lockdown narrowed the reviewed Codex client range to the build where containment was shown
live.

## 0.1.0: import and one-command install (G1)

- **Packages imported.** The Codex and Claude agent packages are now developed here, in `packages/`.
  `PROVENANCE.json` records each file's source commit and hash, and lists the files changed after
  import, with reasons.
- **One-command installer.** `scripts/install.py` installs, verifies or removes both packages in a
  Git project.
  - Preview is the default, and nothing is applied unless both packages preview cleanly.
  - It refuses targets that are mid-merge, rebase, cherry-pick, revert, bisect or sequence, that
    have unmerged paths, or that are not the top of their working tree.
  - When applying, it also refuses uncommitted changes in managed paths.
  - It adopts an existing install without changing it, and rolls back if a later package fails.
- **Linked worktrees.** Both installers accept a linked worktree's `.git` marker. The Claude
  packager checks the marker without following it.
- **Public-repository guard.** An identifier scanner runs on every commit and in CI. It reports
  only the category and location of a finding, never the value.
- **CI.** Linux and macOS run the repository tests and both package suites. Failures are reported
  as annotations. The Codex package's operational-check sandbox is macOS-only, so those tests are
  skipped elsewhere.
- **Routing table.** `docs/routing-table.md` is the living, evidence-driven table the orchestrator
  routes by, with a changelog.

Verified by rehearsal on a clone of the pilot project:

- installing over its existing packages changed nothing;
- removing and reinstalling restored the identical Git tree;
- both packages verified ready.
