# RT-ROUTE-EXHAUSTED analysis a1 (2026-11-24)
Packet reviews/040-rtrx-a1. Worker: CoCoLo, Codex gpt-6.1-sol high (reviews/040-rtrx-a1-work/result.json), not-ready.
Verifier: native Claude Sonnet (CoCoLo refused Claude: stale windowless 'rejected' reading, 24 h), agrees not-ready.

Confirmed: no usage state reaches the route (routing.py route signatures 1583/1836; first_eligible, _select_verifier
take no headroom); headroom is judged only at open (admission_metadata 287/481). Cross-provider verifier is preferred,
not required (decision 17 fallback); independent=false only for low/medium judgment cells.
Verifier corrections: RT-RX-2 as proposed KEEPS the stale-block bug; RT-RX-3 conflicts with "provider candidates only
reorder, first eligible wins" (routing 1603); RT-RX-5 changes ContractError behaviour (1684) with no defined shape;
RT-RX-6 conflicts with v5 audit identity for unopted cells (1754-1760); route has no account input though admission is
keyed by account fingerprint (02:400/410); "unknown" vs admission's default ample (02:395); the verifier is opened
after the worker, so race recovery must re-pick the verifier at its own open; Codex-primary bridge flag (1289) is a
second no-candidate path; off_ladder (1563) after a usage-driven switch may disable escalation.
Recommended design: option 2 (trusted provider-state snapshot into the route, same classifier as admission) plus a
bounded option 3 (re-route once at open on provider_exhausted, within run caps 02:289).
Correct rule for the stale case (verifier): a restrictive reading with no reset time is superseded by any later
non-restrictive reading for the same provider and account; without one it expires at the provider window length,
not 24 h; readings with an explicit unreset resets_at still restrict; future-stamped restrictive readings survive
clock rollback. Routing and open share the one corrected classifier.
Owner decisions: 124.1-124.5 in OWNER-DECISIONS-0.4.0.md.
