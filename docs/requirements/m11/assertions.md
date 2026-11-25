# Assertion changes

No baseline assertion is removed or changed. The original synthetic M11 test is
preserved. New assertions cover busy exhaustion, registration replay, CLI refusal,
durable registration and claim counts, continued service, automatic service exit,
and successful dispatch of the same retried account run.

The new, socket-blocked scenario was strengthened during review: two completed
results with `driver.busy()` became one completed result plus an actual running
child. The second result-success check became launch, live-status, cancellation,
and cancelled-terminal checks. This prevents a retained result view alone from
standing in for active work. Its initial cancellation-state guess
`cancel_requested`/`already_terminal` was replaced before execution by the existing
public cancellation contract: `stop_reason=cancel_requested`, `stop_requested=true`,
and `cancellation_state` in `stop_requested`/`cleanup_confirmed`. Its cancelled
`outcome` check became the existing running-child contract's cancelled
`terminal_state` check. These match the established CLI cancellation test; no
product behavior was changed to accommodate them.

Continuation: the primary's failing lifecycle test is preserved exactly. No
existing assertion was changed or removed. Added checks require a refreshed
advisory availability audit to replay the retained account open with the same
identity, original clock/deadline and no duplicate writes or resources, while
changed original content still conflicts. A second socket-free check observes
the committed journal, pending account binding and binding hold left by a
synthetic project registration refusal, then retries through the real client
and account owner. Its execution needs signature workers outside this sandbox;
the pure account-preflight unit supplies the observed product red -> green.
