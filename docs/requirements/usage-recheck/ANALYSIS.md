# usage-recheck: analysis (base 8d4a7ce3 = installed 0.8.55)

Paths are under `packages/codex-agents/native-package/.codex/orchestration/` unless noted.

## Where the "exhausted" state comes from
- The route asks the account service for an advisory snapshot: `cli.py:1286-1297` ->
  `codex_agents/client_runtime.py:1050-1068` (`provider_state_snapshot_client`, a plain read, 10 s timeout) ->
  `codex_agents/account_service.py:1288-1290` -> `codex_agents/learning_store.py:6663-6690`
  (`provider_state_snapshot`: reads the stored `provider_headroom` / `provider_billing` records and classifies them).
  Nothing in this path takes a new reading; it only re-reads what is stored.
- Classifier: `codex_agents/admission_metadata.py:410-496` (`classify_provider_state`), per record
  `_headroom_state` `:72-115` and `_window_live` `:63-69` (an explicit window counts until `resets_at`).
  A Claude record with `status: "rejected"` and any live window is exhausted (`:107-110`).
- Routing then skips the exhausted provider for workers and verifiers: `codex_agents/routing.py:1478`, `:1524`,
  `:1697`, `:1731`.

## Where readings come from
- Codex: `providers.observe_codex` `codex_agents/providers.py:667-700` sends `account/rateLimits/read` to the
  Codex app-server (`:693`). This reads usage WITHOUT spending allowance (CLI help: "non-spending", `cli.py:1669-1671`).
- Claude: `providers.observe_claude` `codex_agents/providers.py:427-457` runs only `claude auth status --json`
  (identity; no usage numbers). Claude usage arrives only from stream `rate_limit_event`s
  (`providers.claude_rate_limit_records` `:711-792`): during a real child run (bridge) or in the billing probe.
- Billing probe: `codex_agents/billing_probe_worker.py:182-212` runs
  `claude -p --model claude-haiku-5-5 --effort low --tools '' --max-turns 1 -- "Reply with the single word OK."`
  (`codex_agents/bridge.py:2012-2014`). Cost: one Haiku turn, a few tokens in and out (no tools, no project
  files) - small but NOT zero. It is the only Claude usage reading. Cooldown 5 min after a pass, reuse 5 min
  (`codex_agents/account_billing_admission.py:38`, `:42`).
- There is no free Claude usage reading in this code. (Claude Code's interactive `/usage` exists, but no
  scriptable, documented non-spending read is used or known here.)

## What clears a reading
- Decision 124.2 / RT-RX-3: a later good reading clears only a RESETLESS restriction
  (`admission_metadata.py:459-462`, the `fallback` branch). Otherwise a resetless one expires after 5 h.
- RT-RX-5 (`REQUIREMENTS-route-exhausted.md:69-75`, authority 124.2): an EXPLICIT window with `resets_at` in the
  future stays restrictive "despite a later favorable reading, until that reset"
  (test: `packages/codex-agents/tests/test_route_exhausted.py:83-85`).

## Does anything re-check while exhausted with a far reset? No - three blockers in a row
1. B1 Routing skips the exhausted provider, so no Claude child runs and no child reading arrives (routing.py above).
2. B2 The billing probe's own admission refuses it: `evaluate_admission(probe=True)` still applies the
   exhausted gate (`admission_metadata.py:295-299`; probe callers `account_billing_admission.py:549`, `:599`).
   Asserted by `tests/test_bridge.py:3739` `test_exhausted_window_blocks_probes_at_ninety_percent` (reset 1 h away).
3. B3 Even a fresh "allowed" reading would not clear the live block: the live reading has a measured reset
   (next_reset 1795780800000 = Sat 2026-11-28 01:00 NZDT, so explicit), and RT-RX-5 keeps an explicit window
   restrictive until that reset. An early reset by the provider is not a case RT-RX-5 foresaw.
Nothing runs periodically: the only automatic observation is before an already-routed launch
(`codex_agents/project_driver.py:1253`) and before a review child (`codex_agents/review_runner.py:209-210`).

## Cheapest fresh reading
- Codex: free (`account/rateLimits/read`). Re-check is cheap; still blocked by B3 when its window has a reset.
- Claude: one billing probe (one Haiku low-effort turn, a few tokens). If the account is still rejected the
  provider most likely refuses the turn (little or nothing spent - not verified here); if the reset happened
  early it spends one tiny turn and records the proof. At one probe per 60 min while exhausted that is at most
  ~24 tiny turns a day, only while a block with a far reset is in force.

## Conclusion
Fixing B1+B2 (a bounded re-check) is not enough on its own: B3 needs a change to RT-RX-5, which is owner authority
(124.2). Recommended narrow change (USAGE-RECHECK-3): a later reading of the SAME provider, account and window
(same scope and window length) that rates that window non-restrictive AND reports a different reset time for it
than the restrictive reading proves the window reset early, so it supersedes that window's restriction. A later
reading with no reset, or for another window, still does not clear it (RT-RX-5 otherwise unchanged, all its
current tests keep passing). Assumption to check live: after an early reset Claude reports the weekly window with a
NEW resetsAt. If it keeps the old reset time, the wider option B is needed: any later measured reading of the same
window that rates it non-restrictive supersedes it (this changes `test_route_exhausted.py:84`).
