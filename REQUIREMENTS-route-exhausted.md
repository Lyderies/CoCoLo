# RT-ROUTE-EXHAUSTED — requirements only

Branch/worktree: `feat/rt-route-exhausted`, this repository. Step 1 specifies behavior; it adds no implementation or tests and authorizes no commit. Acceptance statements below describe future assertions, not checks already run.

## Authority and source locations

Authority is, in order: owner decision 124 supplied for 2026-11-24; the standing owner rule below; analysis including verifier corrections; existing code. Conflicts are resolved in that order.

Owner decision 124, quoted exactly:

```text
124.1 B "Policy order": keep the routing policy's provider order and its other-provider verifier preference; only
  skip a provider that is exhausted (Codex/Claude window >= 90%, or Claude status rejected).
124.2 A "Newer reading clears": a restrictive reading with no reset time (empty windows, or resets_at null) is
  superseded by any later non-restrictive reading for the same provider and account; with no newer reading it
  expires after the 5-hour window, not 24 h. A reading with an explicit unreset resets_at still restricts until then;
  future-stamped restrictive readings stay restrictive after a clock rollback. Routing and open share one classifier.
124.3 A: skipping a used-up provider wins over the retry/escalation ladder.
124.4 A: a verifier on the same provider as its worker is always marked independent=false (every risk and family).
124.5 A: with both providers exhausted the route returns a structured unsatisfied result naming each provider's
  state and next known reset, instead of raising ContractError.
```

Standing owner rule: at 90% one provider, children go to the other and work continues; an unknown Codex window keeps Codex running; admission at open stays the final authority and is never weakened except by 124.2.

The requested `measurements/rtrx/ANALYSIS-0.4.0-rt-route-exhausted.md` does not exist in this worktree. The supplied analysis is present at `measurements/ANALYSIS-0.4.0-rt-route-exhausted.md:1` (20 lines, including verifier corrections at lines 8–19); that is the analysis used here. No separate owner-decision file was found; the quotation above is the controlling source.

For compact file:line references below:

- `routing.py`, `admission_metadata.py`, and `account_open_admission.py` mean files under `packages/codex-agents/native-package/.codex/orchestration/codex_agents/`.
- `cli.py` means `packages/codex-agents/native-package/.codex/orchestration/cli.py`.
- Test paths in the regression inventory are relative to `packages/codex-agents/tests/`.
- All line references describe the existing worktree before implementation.

## Numbered requirements

### RT-RX-1 — One provider-state classifier

Routing snapshots, open admission, and the existing before-launch check shall derive usage state from the same classifier, with the same provider, account evidence and evaluation time. Its externally distinguishable states shall be `ample`, `constrained`, `exhausted`, and `unknown`. These are usage states, not admission verdicts or provider-configuration eligibility.

**Acceptance:** Identical evidence and time produce identical state in snapshot construction, open and launch; no second routing-only threshold, stale-reading rule, or optimistic state translation exists. An exhausted state produces `provider_exhausted` when earlier admission gates pass.

**Keeps/changes:** Keeps open/launch's shared `headroom_state` use at `admission_metadata.py:284` and `admission_metadata.py:481`; changes the three-state/default-ample classifier at `admission_metadata.py:379` and the state-free route interface at `routing.py:1836`. Authority: 124.2; analysis lines 14–19.

### RT-RX-2 — State boundaries and unknown evidence

A live numeric window at or above 90% exhausts either provider; 75% through below 90% is constrained; usable live numeric readings below 75% are ample in the absence of a stronger applicable restriction. Claude `rejected` exhausts and `allowed_warning` constrains even for an empty windows collection, subject to RT-RX-3–5. The most restrictive applicable evidence wins except for the expressly authorized supersession. Missing Codex windows, all-null utilization, or only expired usage evidence shall be reported as unknown when no other applicable evidence establishes a state. Unknown shall not be invented as ample, shall not cause a usage skip, and shall pass the headroom gate at open; other admission gates still apply. Keep the current Codex `ordinary_usage_allowed=false` exhaustion rule separately identifiable as a billing-derived usage restriction.

**Acceptance:** Both providers classify numeric 74.99/75/89.99/90 as ample/constrained/constrained/exhausted; either a second live 90% window or Claude rejection dominates a low numeric reading. A Codex account with no usable window is `known: false` ("unknown") in the route snapshot, remains a routing candidate, is still rated ample by admission's headroom_state, and is admitted with otherwise passing evidence. Codex ordinary-usage denial remains exhausted, including the existing future-evidence case. Partial numeric coverage and Claude allowed-without-utilization are OPEN-2.

**Keeps/changes:** Keeps thresholds/status precedence at `admission_metadata.py:29`, `admission_metadata.py:64`, and billing exhaustion at `admission_metadata.py:407`. KEEPS admission's three-value rating vocabulary (exhausted/constrained/ample) and its ample default for absent or expired evidence (`admission_metadata.py:88`, `:395`) unchanged (primary 2026-11-24: no new stored or returned rating value; shrinking/expanding validated vocabularies breaks real-ledger upgrades). "Unknown" exists only in the route's provider-state snapshot, as `known: false` for a provider with no applicable evidence, and in the route audit. Authority: 124.1 and standing rule.

### RT-RX-3 — A later non-restrictive reading clears a resetless restriction

For the same provider and account, a restrictive headroom reading with no reset time, including an empty-window Claude rejection/warning or a restrictive window with `resets_at=null`, shall cease contributing its restriction when superseded by a later trusted non-restrictive reading. Do not require that reading to originate in the same project, run, or probe. This exception shall not clear an independent explicit unreset window or billing/paid-usage restriction. Mixed readings must retain any explicit unreset restrictive component.

**Acceptance:** A resetless 95% reading at t1 followed by a trusted 10% reading at t2, with t1 < t2 <= evaluation time, no longer exhausts; an empty-window Claude rejection followed by a later allowed 10% reading similarly clears. A reading for another account/provider or an older favorable reading does not clear. A separate explicit live 95% window still exhausts. Assert identical outcomes at route, open, and launch. Chronology ties and the complete meaning of non-restrictive are OPEN-1/2.

**Keeps/changes:** Changes unconditional restrictive-wins at `admission_metadata.py:170` and `admission_metadata.py:397`; keeps provider/account filtering at `admission_metadata.py:400`. Authority: 124.2; analysis lines 16–19.

### RT-RX-4 — Resetless headroom lasts five hours, not a day

Without a superseding reading, a resetless restrictive headroom reading shall expire after five hours from its observation time, including an empty-window status-only reading. Keep the existing inclusive age-bound convention: it still restricts at exactly five hours and expires at five hours plus one millisecond. The 24-hour horizon for paid usage and spend control shall remain unchanged. Do not manufacture a provider reset timestamp from this fallback expiry.

**Acceptance:** At ages five hours minus one millisecond, five hours, and five hours plus one millisecond, a resetless rejection/95% reading is restrictive, restrictive, then no longer restrictive. With no usable remaining reading the usage state is unknown, not ample. Paid-usage and spend-control evidence six hours old still denies under their existing rules. Audit distinguishes fallback expiry from a measured reset.

**Keeps/changes:** Changes resetless lifetime at `admission_metadata.py:55` and `admission_metadata.py:75`; keeps five-hour constant at `admission_metadata.py:38`, age comparison convention at `admission_metadata.py:61`, and billing horizons at `admission_metadata.py:328` and `admission_metadata.py:349`. Authority: 124.2.

### RT-RX-5 — Explicit resets and rollback protection

An explicit restrictive window with `resets_at > evaluation_time` shall remain restrictive despite a later favorable reading, until that reset. At the reset it no longer contributes. An expired explicit window shall not acquire a fresh resetless lifetime. A future-stamped restrictive observation shall not disappear merely because the evaluation clock moved backward; an older favorable reading cannot clear it.

**Acceptance:** At reset minus one millisecond an explicit 95% window still exhausts despite a later 10% reading; at reset it does not. The same assertions apply to an explicit Claude rejected window. A future resetless rejection/95% reading survives rollback even with an older ample record present; an explicitly elapsed window stays elapsed even if its observation is future-stamped. Future favorable supersession is OPEN-1.

**Keeps/changes:** Keeps `admission_metadata.py:57`, `admission_metadata.py:59`, and `admission_metadata.py:398`; narrows restrictive-wins only as RT-RX-3 permits. Authority: 124.2.

### RT-RX-6 — Account selection and account-wide evidence

The trusted snapshot builder shall supply the route's account scope separately for Codex and Claude. It shall learn each account fingerprint from the trusted provider identity selected by the same freshness and latest-reading rules as open, not from task JSON, the primary-provider name, or a caller-selected account. Usage/billing evidence shall be filtered by that provider and fingerprint across projects, as at open. Missing identity shall remain distinguishable from a known account with unknown usage and shall not become admission permission.

**Acceptance:** Exhaustion for account A does not exhaust account B; exhaustion for A in another project does affect A. Switching the trusted current identity from A to B changes the snapshot's scope and records its identity evidence. An arbitrary task fingerprint cannot select B or clear A. Missing/stale identity still fails open's identity gate; a valid identity with no usage passes only the usage gate. Account changes between snapshot and open require current admission, never reuse of A's state as B's.

**Keeps/changes:** Keeps identity selection at `admission_metadata.py:213`, account filtering at `admission_metadata.py:400`/`admission_metadata.py:410`, and account-wide inputs at `account_open_admission.py:19`/`account_open_admission.py:24`; changes the account-free route interface at `routing.py:1583`. Authority: standing rule; analysis line 11.

### RT-RX-7 — Trusted route input with time and evidence

Live routing shall receive a trusted provider-state snapshot built with RT-RX-1, separate from task data and from the existing task-shape `trusted_routing_evidence_refs`. The snapshot shall identify its evaluation time, project/context binding, each provider and selected account fingerprint (or explicitly unavailable identity), identity evidence, usage state, deciding evidence references, reasons, known reset information and fallback expiry where applicable. Equal inputs and evaluation time shall produce deterministic results. A supplied state label alone is not trusted evidence. Snapshot transport, validation limits and freshness policy are OPEN-3.

**Acceptance:** A live route's recorded states and references match the builder's classification at the recorded time. Injecting an exhausted/ample label, fingerprint or evidence reference through task JSON cannot alter availability. A snapshot from another context cannot be treated as the current trusted snapshot. The snapshot grants no launch/admission authority. Offline behavior must explicitly distinguish unavailable live state; it must not claim ample (OPEN-3).

**Keeps/changes:** Keeps metadata's authority limitation at `admission_metadata.py:3` and trusted original-open boundary at `account_open_admission.py:3`; extends `routing.py:1587`/`routing.py:1836` and state-free callers such as `cli.py:1283`/`cli.py:2085`. Authority: analysis lines 10–15 and 124.2.

### RT-RX-8 — Worker selection preserves policy order

Within the existing eligible provider/model/agent candidates, select the first policy-ordered candidate whose provider is not exhausted. Do not rank ample ahead of constrained or unknown, insert an unconfigured provider, change risk cells, or change model/effort merely because utilization differs below 90%. Usage skips are additional to existing configuration, allowlist, mapping and containment exclusions. The standing rule to continue on the other provider applies when that provider has an eligible candidate.

**Acceptance:** For each policy order and both primary providers, the first eligible candidate wins with ample/constrained/unknown states in any combination. At 90% the exhausted first provider is skipped and the next eligible provider wins with its existing model/effort/agent/dispatch. At 89.99% it is not skipped. Disabled, disallowed or unavailable-agent alternatives are never synthesized to continue work.

**Keeps/changes:** Keeps first-eligible order at `routing.py:1603`, candidate construction at `routing.py:1639`, and configuration/agent exclusions at `routing.py:1660`; adds exhaustion filtering inside eligibility at `routing.py:1652`. Authority: 124.1 and standing rule; corrects the analysis's rejected reordering proposal at analysis line 9.

### RT-RX-9 — Exhaustion outranks every escalation or fallback choice

Every retry/escalation candidate shall pass the same exhaustion filter as the first worker pick. No ladder step, risk floor, legacy model fallback, or retry may restore an exhausted provider. A usage-driven provider switch shall not itself create a failed attempt, a semantic failure, evidence of independent verification, or entitlement to an additional attempt.

**Acceptance:** With the preferred/strongest ladder provider exhausted, attempts 1–3 never select it; an eligible surviving provider can continue even where the unavailable preferred setting was stronger. A usage switch leaves task failure count and failure evidence unchanged. If every provider is exhausted, return RT-RX-16 rather than forcing a ladder choice. Existing legacy-fallback authorization remains necessary and cannot bypass usage exclusion.

**Keeps/changes:** Keeps escalation's `first_eligible` use at `routing.py:1573` and fallback authorization contract at `routing.py:1853`; extends eligibility at `routing.py:1652` and fallback configuration check at `routing.py:1711`. Authority: 124.3.

### RT-RX-10 — Escalation remains meaningful after a usage switch

When the surviving selected setting is on the configured family ladder, keep existing failure qualification and escalation semantics: attempt 2 uses the next eligible higher rung when qualified, attempt 3 uses the strongest eligible higher rung, and no higher eligible rung retains the current surviving pick. Filter exhausted providers throughout. Do not silently disable an otherwise applicable escalation solely because the provider was switched for usage. A surviving setting absent from the ladder needs the explicit resolution in OPEN-4; no invented strength order is specified here.

**Acceptance:** A usage switch to an on-ladder setting still yields `next`, `strongest`, or `none_higher` according to the surviving ladder, with correct from/to settings. Unconfirmed judgment failures remain held and non-ladder families still do not escalate. A usage switch alone must not produce a misleading successful quality escalation or an unexplained `off_ladder`; the off-ladder fixture remains an explicit unresolved case until OPEN-4 is settled.

**Keeps/changes:** Keeps failure qualification at `routing.py:1532`–`routing.py:1562` and rung semantics at `routing.py:1567`–`routing.py:1580`; addresses `off_ladder` at `routing.py:1563`. Authority: 124.3; analysis line 13.

### RT-RX-11 — Verifier selection keeps the other-provider preference

For a worker's actual selected provider, prefer the other eligible, non-exhausted provider, then the worker's own eligible, non-exhausted provider. Use the existing family/risk verifier model, effort and agent rules. Constrained/unknown state shall not demote a preferred verifier. Verification tasks for known work shall use this same rule with the actual worker provider and bound outcome, rather than reuse an obsolete advertised verifier blindly.

**Acceptance:** For both worker providers, every risk and supported family, a non-exhausted eligible other provider wins even if constrained/unknown. If it exhausts, the worker's provider is the fallback when eligible and non-exhausted. If neither is available, no verifier is invented. With unchanged state, advertised and actual verification routes agree on provider/model/effort; meaningful work still requires verification and verification does not recursively require verification.

**Keeps/changes:** Keeps preference and risk settings at `routing.py:1473`/`routing.py:1489`, known-work selection at `routing.py:1608`, and verification requirement at `routing.py:1734`; extends `_select_verifier` at `routing.py:1458` with usage filtering. Authority: 124.1.

### RT-RX-12 — Same-provider verification is always non-independent

Whenever the verifier's actual provider equals its worker's actual provider, the advertised verifier and actual verification route shall explicitly record `independent=false`, regardless of family, risk, policy version or escalation scope. The marker shall survive a verifier re-pick. A different provider shall not acquire this false marker merely from a previous advertised fallback, and provider diversity alone shall not attest a completed independent verification verdict.

**Acceptance:** Cross-product assertions over both providers, all risks and all supported worker families find explicit false on every same-provider pair, including high/critical, default installations and families outside judgment scope. A re-pick from other-provider to same-provider becomes false. A re-pick back to a different provider does not retain false from the old route; execution/verdict audit remains unobserved until real evidence arrives.

**Keeps/changes:** Broadens conditional marking at `routing.py:1506`, scope checks at `routing.py:1618`/`routing.py:1744`, and actual-route projection at `routing.py:1822`; keeps route-versus-observation distinction at `routing.py:1810`. Authority: 124.4.

### RT-RX-13 — Re-pick the verifier at its own open

The worker route's verifier is a recommendation at that snapshot time. At the verifier's own open, obtain current trusted state and select the verifier again using the worker's actual provider, original verification family/risk, and bound worker outcome. If the advertised provider has become exhausted since the worker route, select the remaining eligible provider, apply RT-RX-12, and admit that actual verifier normally. Do not rerun, relabel or replace the completed worker to avoid this race.

**Acceptance:** A worker routed at t1 advertises the other provider; that provider exhausts before verification at t2. The verifier open chooses the eligible survivor with the correct verifier model/effort, records both recommendations and the reason for change, marks same-provider false, and keeps the same worker/outcome binding. If both exhaust at t2, verification is unsatisfied; a worker result alone is not recorded as verified.

**Keeps/changes:** Keeps known-work route correspondence at `routing.py:1608`, risk selection at `routing.py:1617`, and final usage check at `admission_metadata.py:287`; changes reliance on the earlier advertisement at `routing.py:1737`. Authority: analysis line 12 and 124.1/124.4.

### RT-RX-14 — At most one exhaustion-triggered re-route at open

For each logical child open (worker or verifier), a fresh open denial whose reason is exactly `provider_exhausted` may cause one automatic refresh of the trusted snapshot, re-route, and alternative open. This is at most two open attempts total for that logical child, including across replay/recovery; it is not an unbounded provider ping-pong. The replacement still uses normal admission, account-wide run caps, task attempt limits and execution bounds. A second exhaustion denial terminates recovery with an explicit unsuccessful outcome; both exhausted uses RT-RX-16. Non-exhaustion denials do not trigger this mechanism.

**Acceptance:** A provider becoming exhausted between route and open causes zero launches there and at most one alternative open. Inject a second exhaustion denial: no third open occurs, including after a replay. A survivor at its cap returns `window_run_cap_reached` and is not launched or retried via another provider under this mechanism. Passing admissions are counted normally and never charged twice; denied opens do not create admitted runs. Keep logical task/parent/outcome lineage and link both attempts in audit. Durable retry ownership is OPEN-5.

**Keeps/changes:** Adds bounded recovery around `admission_metadata.py:287`; keeps account cap at `admission_metadata.py:289`, future-admission counting at `admission_metadata.py:296`, route bounds at `routing.py:1751`, and authoritative original-open evaluation at `account_open_admission.py:44`. Authority: analysis lines 14–15 and standing rule.

### RT-RX-15 — Admission and before-launch checks retain final authority

A non-exhausted route snapshot is advisory. Open shall still perform the full current admission sequence, and launch shall still perform its existing recheck. Preserve API-key refusal, identity/client/subscription checks, owner attestation, paid-usage/spend-control restrictions, billing freshness/bootstrap limits, run caps, signature/current-permission checks and atomic original-open behavior. Only the headroom classifier correction in 124.2 changes admission permission; reporting unknown separately does not deny an otherwise admitted unknown window.

**Acceptance:** A route selecting any provider cannot bypass each existing denial reason when independently injected. Exhaustion arriving after open still prevents launch. An unknown Codex window with valid remaining evidence still admits. API-key, paid usage, missing identity/attestation, billing and cap denials keep their existing priority and cannot be treated as exhaustion-triggered failover. Launch does not recount the admission cap.

**Keeps/changes:** Keeps fixed-order gates at `admission_metadata.py:207`–`admission_metadata.py:307`, launch rules at `admission_metadata.py:431`–`admission_metadata.py:483`, and raw recapture/commit authority at `account_open_admission.py:3`; changes only RT-RX-1–5's headroom semantics. Authority: standing rule.

### RT-RX-16 — Structured result when both providers are exhausted

When both provider states are exhausted, return a structured, non-executable unsatisfied routing result instead of `ContractError`, for worker selection and verifier selection, including refresh/re-route paths. Its observable contract shall contain an unsatisfied discriminator, cause `provider_exhausted`, task identity, selection phase (worker/verifier), snapshot evaluation time/reference, and entries keyed by both `codex` and `claude`, each with state `exhausted`, account scope, deciding evidence/reason and next known reset or explicit null. Do not fill successful execution fields with a fake selected worker/verifier. Exact wire names/version are OPEN-6; these semantic fields are required.

**Acceptance:** Both exhausted yields an inspectable result naming both providers and their states, never either no-candidate exception. Known future reset times are surfaced; absent reset stays null, with fallback expiry separate. With multiple blocking explicit windows, the next known reset is the earliest future reset among the applicable blocking windows, not a guarantee that all restrictions end then; later blockers remain inspectable. Consumers stop without launch or a success/verified claim. A successful worker whose verifier is unsatisfied retains its worker result and an unsatisfied verification phase.

**Keeps/changes:** Changes usage-caused no-candidate behavior at `routing.py:1683` and `routing.py:1510`; extends successful-only output at `routing.py:1773`. Authority: 124.5; resolves the missing shape noted at analysis line 9 at the semantic level.

### RT-RX-17 — Bridge/configuration failure is a separate cause

Usage routing shall not turn on `codex_primary_may_bridge_to_claude` or bypass disabled providers, model allowlists, unavailable agents or bridged-write containment. With Codex exhausted and Claude not exhausted but blocked by the Codex-primary bridge flag, report the distinct bridge/configuration no-candidate cause, not “both exhausted.” Preserve existing policy-only `ContractError` behavior for invalid inputs and no eligible configured candidates; 124.5 does not authorize converting every error to exhaustion. If both states really are exhausted, RT-RX-16 applies and any additional policy exclusions remain separately visible.

**Acceptance:** With both providers enabled, bridge=false, Codex-primary and Codex exhausted/Claude ample, no Claude launch occurs and the diagnostic identifies the bridge prohibition separately from Codex exhaustion. Turning the flag on makes an otherwise eligible Claude candidate selectable. Disabling both providers with no exhausted evidence retains existing no-candidate refusal. Invalid primary/task/configuration still fails closed. Exact mixed-cause diagnostic representation is OPEN-6.

**Keeps/changes:** Keeps bridge flag at `routing.py:1287`, provider/model allowlists at `routing.py:1271`, writable mapping exclusion at `routing.py:1666`, and policy-only no-candidate errors at `routing.py:1684`; adds distinct usage-versus-policy diagnostics. Authority: 124.1; analysis lines 12–13.

### RT-RX-18 — Availability audit coexists with v5 selection identity

Record a separately identifiable provider-availability audit alongside the existing selection audit: classifier/snapshot format identity, evaluation time and snapshot reference, per-provider account/state/evidence/reset/fallback expiry, candidates considered in policy order and their skip causes, actual worker/verifier choice, usage-driven switch, and re-route count/trigger with original and replacement open references. Preserve policy/configuration/mapping digests and existing risk/escalation/failure lineage. For unopted v6 cells, keep `selection.policy_version=5` and the existing selection identity; do not relabel those cells v6 or conflate availability with policy/model selection.

**Acceptance:** Audit reconstructs why a worker/verifier skipped each provider and distinguishes `unknown` from `ample`, usage from bridge exclusion, quality escalation from usage switch, and initial route from open recovery. For an unaffected unopted cell, the legacy selection subtree is identical and its policy version remains 5; added availability data lives outside that subtree. Requested settings remain distinct from observed execution and verdicts. Same-provider marking required by 124.4 is still present even when it changes the full route serialization.

**Keeps/changes:** Keeps explicit v5 identity at `routing.py:1754`–`routing.py:1760`, provider config digest at `routing.py:1792`, mapping digest at `routing.py:1824`, and observed/verdict placeholders at `routing.py:1809`–`routing.py:1815`; adds availability audit outside `_selection_metadata` use at `routing.py:1753`. Authority: analysis line 10, 124.2/124.4/124.5.

### RT-RX-19 — Evidence remains attributable and bounded

The snapshot/audit shall reference trusted identity and deciding headroom/billing evidence, plus the later clearing reading when it supersedes resetless evidence. Evidence from another project may decide account state, but shall retain its owning-project provenance; do not falsely present foreign references as locally held admission evidence. Preserve bounded admission evidence and existing project-local citation filtering. A Codex billing-only exhaustion must have an attributable billing reason/reference even if no headroom digest exists. Do not expose raw account identifiers or authentication secrets.

**Acceptance:** Cross-project exhaustion and clearing are reproducible using resolvable, scoped snapshot evidence, while stored admission citations remain a subset of that project's held records. A large history does not produce an unbounded list of admission digests. Billing-only exhaustion is explainable without a fabricated headroom reference; unknown does not cite imaginary favorable evidence. Raw provider credentials/identity values do not appear in routing audit. Snapshot size limits are OPEN-3.

**Keeps/changes:** Keeps evidence intersection at `admission_metadata.py:200`, bounded deciding-level intent at `admission_metadata.py:388`, and local citation construction at `account_open_admission.py:29`; addresses billing-only state without a headroom digest at `admission_metadata.py:413`–`admission_metadata.py:418`. Authority: standing admission rule and analysis lines 11/14.

### RT-RX-20 — Collection, compaction and replays preserve the classifier's answer

Evidence retention/merge/capture must preserve enough trusted later favorable evidence to implement 124.2; a collector that drops every favorable reading after a rejection cannot satisfy it. Compaction, cross-project capture, overflow, restart or replay shall neither resurrect a superseded resetless restriction nor drop an explicit live/future-stamped restriction. Preserve billing restrictions and cap accounting independently. No requirement here authorizes a new arbitrary observation append or weakens evidence authenticity.

**Acceptance:** The same sequence of resetless restrictive then later non-restrictive readings yields the same state before and after overflow/merge/capture/restart; explicit unreset and rollback cases stay restrictive through the same operations. Replay does not duplicate run admissions or restart the RT-RX-14 retry allowance. Existing bounded histories still work. Retention changes must be limited to evidence needed for the new classifier; the exact storage design remains implementation work.

**Keeps/changes:** Extends the account-wide evidence contract at `admission_metadata.py:178` and `account_open_admission.py:24`; changes the old unconditional restrictive-wins assumption at `admission_metadata.py:170`. Existing retention behavior is exposed by `test_providers.py:1398` and `test_providers.py:2608`; those behaviors cannot silently defeat 124.2. Authority: 124.2 and standing rule.

### RT-RX-21 — Preserve named regression behavior, with explicit authorized exceptions

The regression inventory below shall remain the preservation baseline. Tests whose assertions conflict directly with 124.2/124.4 or additive availability audit must be identified for narrow future revision, not cited as evidence that the owner decision can be ignored. This step does not edit tests, fixtures or golden files.

**Acceptance:** A later implementation's validation report names these existing tests, preserves unaffected assertions, and accounts explicitly for each changed assertion under RT-RX-1–20. No blanket golden regeneration hides unrelated provider/model/effort/risk/bounds/lineage changes. Existing no-candidate configuration errors remain covered separately from new both-exhausted results.

**Keeps/changes:** Keeps the behaviors exercised at the file:line locations below; permits only the exceptions stated below. Especially preserve `routing.py:1603` and `routing.py:1759`, while applying 124.2/124.4 to their affected outputs.

## Existing tests found by grep (not executed)

Inventory method: `rg -n 'def test_' packages/codex-agents/tests/test_routing*.py packages/codex-agents/tests/test_admission_metadata.py packages/codex-agents/tests/test_providers.py`, plus targeted name searches across `packages/codex-agents/tests` for exhaustion, caps, headroom, launch and account scope.

The following existing named assertions must keep passing in their current scenarios (unexhausted/unchanged evidence unless the test itself injects exhaustion):

| Existing file:line and test name | Behavior to preserve |
| --- | --- |
| `test_routing.py:911` `test_codex_primary_reaches_claude_only_through_the_bridge_flag` | Bridge opt-in is mandatory. |
| `test_routing.py:927` `test_project_model_allowlist_skips_candidates` | Model allowlist remains authoritative. |
| `test_routing.py:947` `test_provider_candidates_match_the_approved_routing_table` | Existing policy order/model choices. |
| `test_routing.py:997` `test_a_workspace_write_codex_mapping_explains_the_denial` | Containment exclusion stays explained. |
| `test_routing.py:1033` `test_no_eligible_candidate_denies_instead_of_falling_back` | Existing disabled-provider and bridge-only no-candidate scenarios still raise; these are not both-exhausted scenarios. |
| `test_routing.py:1050` `test_verifier_prefers_the_other_provider` | Prefer other eligible provider; same-provider fallback remains allowed. |
| `test_routing.py:1088` `test_legacy_fallback_stays_on_codex_and_obeys_configuration` | Legacy fallback keeps its authorization/configuration rules. |
| `test_routing.py:1108` `test_routing_is_deterministic` | Deterministic route for equal inputs/time. |
| `test_routing.py:1335` `test_verification_task_matches_the_advertised_verifier` | Same evidence produces matching advertised/actual verifier. |
| `test_routing.py:1395` `test_verification_task_matches_the_advertised_verifier_under_fallback` | Matching verifier under authorized legacy fallback. |
| `test_routing.py:277` `test_parallelism_is_bounded_and_explicit`; `test_routing.py:285` `test_verifier_does_not_recursively_require_verifier` | Bounds and verification topology. |
| `test_routing_risk.py:364` `test_high_and_critical_never_route_below_the_first_choice` | Preserve static policy validation/choice when providers are not exhausted; runtime exhaustion takes precedence under 124.3. |
| `test_routing_risk.py:443` `test_attempt_two_after_a_failed_executable_check_moves_one_up`; `test_routing_risk.py:463` `test_attempt_three_goes_to_the_strongest_candidate_and_is_reported` | Qualified ladder steps. |
| `test_routing_risk.py:469` `test_the_strongest_is_the_strongest_one_the_project_admits`; `test_routing_risk.py:508` `test_a_family_without_a_ladder_never_escalates` | Eligibility and ladder scope remain binding. |
| `test_routing_risk.py:568` `test_a_claude_verifier_follows_the_workers_risk`; `test_routing_risk.py:587` `test_a_codex_verifier_follows_the_workers_risk` | Verifier model/effort follows risk. |
| `test_routing_risk.py:608` `test_the_verifier_is_always_the_other_provider` | Its fixtures have both providers eligible; its name does not prohibit authorized same-provider fallback. |
| `test_routing_judgment.py:115` `test_confirmed_quality_failure_escalates_once_to_next_rung`; `test_routing_judgment.py:144` `test_unlisted_or_unadjudicated_failure_is_held`; `test_routing_judgment.py:157` `test_second_failure_goes_to_strongest_and_third_is_refused` | Failure evidence and attempt limits. |
| `test_routing_judgment.py:232` `test_retry_keeps_parent_lineage_and_failure_evidence` | Parent and evidence lineage. |
| `test_routing_judgment.py:265` `test_same_provider_fallback_is_marked_on_a_default_codex_only_install`; `test_routing_judgment.py:273` `test_verification_route_records_the_same_provider_fallback` | Existing same-provider false cases and absence of stale false for other-provider selection. Extend coverage to all families/risks later. |
| `test_admission_metadata.py:31` `test_mixed_future_reset_evidence_keeps_the_earliest_shared_cap_window` | Existing shared cap placement and rollback rules. |
| `test_admission_metadata.py:64` `test_future_resetless_restrictions_survive_clock_rollback_for_admission_and_launch` | Current future-stamped cases still restrict; already older-than-day expired cases still expire. Add five-hour boundary cases later. |
| `test_admission_metadata.py:131` `test_cold_import_excludes_provider_persistence_and_all_calculation_io` | Classifier calculations remain free of provider/persistence IO. |
| `test_providers.py:507` `test_claude_windows_use_the_codex_thresholds` | Numeric thresholds and explicit expired-window behavior. |
| `test_providers.py:1152` `test_codex_denials_follow_the_fixed_order`; `test_providers.py:1444` `test_open_host_run_always_admits_inside_the_ledger` | Admission ordering and authority. |
| `test_providers.py:829` `test_preparing_a_signature_does_not_cache_the_shared_account_run_cap`; `test_providers.py:847` `test_preparing_a_signature_does_not_cache_restrictive_account_evidence` | Current evidence/caps at final admission. |
| `test_providers.py:2247` `test_run_cap_counts_admissions_in_the_current_window`; `test_providers.py:2285` `test_run_cap_window_falls_back_to_the_last_five_hours` | Run cap independent of usage state. |
| `test_providers.py:2339` `test_many_headroom_records_keep_the_decision_bounded`; `test_providers.py:2375` `test_restrictive_evidence_stamped_after_now_still_counts` | Bounded admission evidence and rollback safety. |
| `test_providers.py:2529` `test_a_later_reset_reading_cannot_free_counted_admissions`; `test_providers.py:2557` `test_the_run_cap_counts_the_account_across_projects`; `test_providers.py:2576` `test_the_cap_window_is_placed_from_every_projects_readings` | Supersession cannot free counted cap places. |
| `test_providers.py:2769` `test_overage_in_one_project_denies_another`; `test_providers.py:2791` `test_the_bootstrap_is_once_per_account`; `test_providers.py:2803` `test_a_rejection_in_one_project_blocks_another_until_reset` | Account scope, bootstrap and explicit reset restriction. |
| `test_account_claim_jobs.py:295` `test_existing_rule_a_fresh_claim_with_no_headroom_reading_is_admitted` | Unknown usage remains admitted at the headroom gate. |
| `test_admission_history_growth.py:72` `test_an_old_headroom_record_whose_window_has_not_reset_still_denies`; `test_admission_history_growth.py:80` `test_paid_usage_from_the_last_day_still_denies_among_old_history` | Explicit resets and paid-usage horizon survive history capture. |
| `test_bridge.py:792` `test_launch_is_refused_when_billing_or_headroom_changes_after_open`; `test_launch_signature_preparation.py:177` `test_other_project_exhaustion_during_verification_refuses_launch` | Final launch recheck remains effective. |

Known assertions needing focused compatibility review in a later test step:

- `test_admission_metadata.py:87` `test_empty_unified_windows_keep_rejection_or_warning_until_fallback_expiry` currently derives the fallback from the 24-hour `EVIDENCE_HORIZON_MS` and expects ample after expiry. Revise only headroom expiry/state expectations to five hours/unknown; retain status and explicit-reset protections (RT-RX-2/4/5).
- `test_admission_metadata.py:122` `test_legacy_calculations_and_import_entrypoints_are_preserved` compares 35 exact legacy vectors. Keep import identity and unaffected decisions; inspect vectors for authorized stale/unknown differences rather than demanding byte-identical legacy classification.
- `test_routing_risk.py:299` `test_shipped_policy_routes_identically_over_the_cross_product` and `test_routing_judgment.py:69` `test_high_critical_routes_are_unchanged` hash whole route outputs (`test_routing_risk.py:139`). New audit and 124.4 false markers can change those hashes even when selection is unchanged. Preserve v5 selection identity and unaffected execution choices; do not promise unchanged whole-result hashes.
- `test_providers.py:1381` `test_fabricated_favourable_event_cannot_clear_a_restriction` uses an explicit unreset restriction, so its denial must remain. Its assertion that a later favorable event is discarded (`test_providers.py:1400`) needs review if retention changes; do not carry that discard rule into resetless supersession.
- `test_providers.py:2608` `test_a_reset_less_rejection_survives_the_overflow_merge`, `test_providers.py:2636` `test_later_aggregate_and_resetless_restrictions_are_not_discarded`, `test_providers.py:2670` `test_billing_probe_merge_preserves_aggregate_and_resetless_expiry`, `test_providers.py:2711` `test_empty_aggregate_restriction_survives_the_overflow_merge`, `test_providers.py:2836` `test_future_resetless_exhaustion_cannot_be_erased_by_project_deletion`, and `test_provider_restrictions.py:68` `test_merge_preserves_resetless_horizon_across_rollback_and_validates`: retain bounded merge, authenticity and rollback protections; inspect old 24-hour lifetimes and restrictive-only retention for the narrow 124.2 exception. A superseding trusted reading must not be lost.
- `headroom_state` also feeds probe-reuse comparison with a three-state rank/default ample (`admission_metadata.py:561`). Preserve existing probe provenance and relative-restriction behavior, including `test_admission_metadata.py:214` `test_a_run_as_constrained_as_the_probe_s_own_reading_does_not_restrict` and `test_admission_metadata.py:225` `test_PC5_complementary_half_failed_probes_at_equal_stamps_restrict`. Adding unknown must not crash the rank comparison or silently weaken this separate gate; exact unknown comparison is OPEN-2.

## Resolved OPEN items (primary, 2026-11-24, under decision 124; owner rule: framework/plumbing choices are the primary's)

1. **Chronology.** "Later" = strictly greater `observed_at_ms` than the restrictive reading, and `<=` the evaluation time.
   Equal times: the restrictive reading wins. A favorable reading stamped after the evaluation time never clears.
   Ingestion order never matters; ledger order only breaks ties between equally rated readings, as today.
2. **What clears.** A later reading with live numeric windows replaces a resetless restrictive reading with its own
   rating (ample or constrained). A later Claude reading with status `allowed` and no windows clears to ample (the
   mirror of `rejected`-with-no-windows -> exhausted); `allowed_warning` with no windows -> constrained. A later
   reading with no usage numbers and no explicit status (all-null utilization) clears nothing. Probe-reuse rank
   comparison uses the same corrected rating; unknown ranks as ample there (unchanged default, 02:561).
3. **Snapshot boundary.** The snapshot is built only by the project/account service from its ledger with the RT-RX-1
   classifier, for the one account the service is bound to, at the route call's evaluation time (2 providers, bounded).
   It is never read from task or assignment JSON. Store-free `route` paths with no service get no snapshot: providers
   are `not_checked`, selection is exactly today's, and the audit says `availability: not_checked`.
4. **Off-ladder survivor.** Unchanged: `off_ladder` as today; the audit records the usage switch that caused it.
5. **One re-route.** Enforced inside one `learning-open-host-run` call (re-route once in-process on
   `provider_exhausted`, then refuse). Not persisted across restarts; the existing run caps (02:289) bound attempts.
6. **Wire shape.** Unsatisfied route: `{"status": "unsatisfied", "reason_code": "no_provider_allowance" | 
   "no_eligible_provider", "providers": {"claude"|"codex": {"state": ..., "cause": "usage"|"configuration",
   "next_reset_ms": int|null}}}`; the CLI prints it with `ready: false` and exit 2, like other refusals.
   `next_reset_ms` is an observed upcoming reset only (never the 5-hour fallback expiry).
7. **Sources.** measurements/ANALYSIS-0.4.0-rt-route-exhausted.md is the complete analysis summary; the packet and
   results live in the control project (reviews/040-rtrx-a1, reviews/040-rtrx-a1-work/result.json).
