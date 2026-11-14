# Changelog

What changed in each release of COCOLO (Claude Orchestrator, Codex Orchestrator, Local Orchestrator).

## Unreleased: COCOLO 0.3.0 (in progress)

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
