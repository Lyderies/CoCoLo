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
- Not done: learning-status showing recheck_due_at (USAGE-RECHECK-4c) - the snapshot field is there, the
  owner-status view does not print it yet.
- Tests: PYTHONPATH=<repo>/scripts, TMPDIR=/tmp/claude-501/ur, from packages/codex-agents:
  `$PY -B -m unittest tests.test_usage_recheck tests.test_usage_recheck_live ...`
- No existing assertion changed (assertions.md not needed). Option A narrowed to far resets (> 1 h) so test_providers.py:1171-1173 holds.
- Green modules: usage_recheck(+live), admission_metadata, route_exhausted(+live,+open), route_live_root, provider_restrictions, providers, refusal_steps, billing_gate, billing_rejudge, account_billing_admission, billing_probe_worker, cli_billing_runtime, owner_terminal_flow, test_bridge.ClaudeHostReviewTests.
