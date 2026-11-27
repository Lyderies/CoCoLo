# Batch 0.8.74 (branch merge/batch-0874)

Base be6f17a6 (0.8.73, installed in the control project). Contents: S14I/S14G-4 (1bae3a8b, decision 399: custody scan and
next_hold_due use the live hold index) and S14H (f0c96408, decision 400: account_holds_status profile, no product change).
`version/0.4.0` not moved, no push.

## Bump 0.8.74
VERSION 0.8.74; runtime digest f86147a3... recorded in runtime-releases.json; CHANGELOG; previous-release record for 0.8.73
(source be6f17a6; claude-agents package unchanged since, bundle digest eb34209d... unchanged; PINNED comment extended);
catalogue 33 -> 34. Floor 0.8.74 (0.8.68..0.8.73 refused). scripts/test-requirements.json regenerated with
`scripts/generate_test_metadata.py --catalogue` (second run byte-identical); the renamed floor test's old entries removed.
Assertions changed (old -> new), reason: floor and version move with the release:
- `test_b4t_takeover.FloorTests.test_a_store_written_by_this_build_refuses_..._0871_and_0872` renamed `..._0871_0872_and_0873`; stored floor '0.8.73' -> '0.8.74'; refused builds add '0.8.73'.
- `test_b4t_native_p69`: every 0.8.73 -> 0.8.74 (proves 0.8.69 -> 0.8.74 natively); tests/fixtures/b4t/README.md text.
- `test_previous_release_catalogue`: 33 -> 34 (twice).

## Full run (`--base be6f17a6`, real sockets; output docs/batch-0874-full-run.txt, redacted)
exit 0; units 574; elapsed 620.0 s; preflight 8.5 s; metadata_errors []; budget_failures []; failed_units [];
requirement_failures []. Budget warnings and debt_retirement_required (test_container_stop, test_session_guard) as listed in the output.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
