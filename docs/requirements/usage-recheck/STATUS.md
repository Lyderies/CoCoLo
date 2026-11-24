# usage-recheck status

State: RECOMMENDATION BUILT (option A + bounded re-check), owner decision 134 pending. No release bookkeeping.

- Analysis: ANALYSIS.md. Requirements: requirements.md (USAGE-RECHECK-1..5, UX-HCU-1).
- Commit 1 (ca9a0537): docs, red tests, UX-HCU-1 fix. Commit 2: the implementation (tests green).
- Code:
  - admission_metadata.py: USAGE_RECHECK_INTERVAL_MS / USAGE_RECHECK_FAR_RESET_MS (1 h each);
    classify_provider_state: early-reset supersession of explicit windows (`reset_early`, option A) and
    `recheck_due_at_ms`; `usage_recheck_due`; evaluate_admission(probe=True) skips the exhausted gate only when due;
    ProviderStateSnapshot.recheck (CLI-only audit, not on the wire).
  - cli.py `_usage_recheck` (called by `_service_snapshot`, so learning-route, route, and the open re-pick):
    Codex observe_provider_client / Claude billing_probe_client for each due provider, then re-read snapshot;
    availability.recheck {provider: {outcome cleared|still_exhausted|failed:<reason>, observed_at_ms}}.
  - refusal_steps.py: host_client_unknown own step (UX-HCU-1).
- Review round 1 (Codex, NOT-READY) fix round (commit 3):
  - HIGH 1: early-reset proof needs a known window identity (scope or duration); two unnamed windows never clear
    each other (`reset_early`).
  - HIGH 2: one attempt per provider/account per interval, reserved before any I/O and counted whatever it yields:
    the route reserves it on disk under the state root (`cli._reserve_usage_recheck`, flock + atomic replace);
    a Claude probe's committed `probe_admission` (pass) also starts the interval in the classifier
    (`recheck_attempts_ms`, snapshot + both probe admission paths), so an explicit probe cannot spend twice either.
  - HIGH 4: proof compares the resets the readings themselves reported (a merged component's inherited reset never
    counts); MED: the far-reset condition is judged at the clearing reading's time, so a proof does not lapse.
  - MED: the ledger-backed learning-route takes the same re-check through the services before it opens the ledger;
    the audit's `observed_at_ms` is the account's new reading (`last_reading_at_ms`) or null, and 'cleared' needs one.
  - LOW NOT DONE (needs a primary decision): learning-status showing recheck_due_at (USAGE-RECHECK-4c) needs one
    more account read (`account.provider_state_snapshot`) from status, which breaks existing assertions
    `tests/test_cli_status_client_boundary.py:131,294` (`['account.owner_key'] == requests`). Built, red->green, then
    reverted: `_provider_usage` (snapshot due merged with the route's reservation) + a live test. Options: change
    those two assertions to allow the read, or have the status reply carry the usage from an existing request.
- Tests: PYTHONPATH=<repo>/scripts, TMPDIR=/tmp/claude-501/ur, from packages/codex-agents:
  `$PY -B -m unittest tests.test_usage_recheck tests.test_usage_recheck_live ...`
- No existing assertion changed (assertions.md not needed). Option A narrowed to far resets (> 1 h) so test_providers.py:1171-1173 holds.
- Green modules: usage_recheck(+live), admission_metadata, route_exhausted(+live,+open), route_live_root, provider_restrictions, providers, refusal_steps, billing_gate, billing_rejudge, account_billing_admission, billing_probe_worker, cli_billing_runtime, owner_terminal_flow, test_bridge.ClaudeHostReviewTests.
- Confirming-check HIGH (reservation files): `cli._reserve_usage_recheck` now uses a 0700 checked directory fd,
  O_NOFOLLOW|O_NONBLOCK opens, fstat regular/uid/mode/nlink/size(4 KiB) checks, LOCK_NB with 1 s deadline,
  random O_EXCL tmp, fsync file + directory. Any refusal or held lock = no re-check this route. Tests:
  tests.test_usage_recheck.ReservationFileSafetyTests (8). No existing assertion changed.
