# Batch 0.8.69: quiet A/B of codex:tests.test_reply_shape_live

Module: `codex:tests.test_reply_shape_live` (soak profile). Both runs were on a quiet machine
(load about 2.3 to 2.6, no `--base` full run in flight).

Command used for both runs (the spec command needed `--profile soak`; without it the harness
refused the module with `explicit soak module requires --profile soak` and ran nothing):

    $PY -B scripts/parallel_tests.py --jobs 2 --profile soak --module codex:tests.test_reply_shape_live

| Run | Tree | Exit | Tests | Elapsed | Failures |
|-----|------|------|-------|---------|----------|
| A | base `e46759a5` (0.8.68), detached worktree | 1 | 5 | 673.4 s | 1 failure |
| B | batch branch `68971385` (0.8.69 batch) | 1 | 5 | 633.1 s | 1 failure |

First failing test in both runs:
`test_every_optional_field_was_seen_on_a_real_reply` (operation `history.run_usage`, field `partial_reason`)
Error: `AssertionError: False is not true : Optional field never produced.`

Result: B fails the same way as A. Same on base. The B rerun rule did not apply because A also failed.

Other output in both runs: a `ConnectionAbortedError` traceback from a background thread in
`packages/codex-agents/tests/test_e2a_wake_wire.py` line 268. It is logged noise. It is not the reported failure.

Conclusion: the failure is not caused by load. It reproduces on a quiet machine on base, so it is a
pre-existing live-data gap: the live service did not produce a `partial_reason` field on `history.run_usage`
during the run. Load was not the trigger.

Logs: run A `/tmp/claude-501/rslA/out.txt`, run B `/tmp/claude-501/rslB/out.txt` (scratch, not kept).
