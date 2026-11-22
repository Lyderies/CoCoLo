# Merge assertion replacement ledger (step 3, in progress)

Source assertions are from eae32657's merge_library_support.py. Replacements are derived from
ANALYSIS-0.4.0-merge-library-states.md §§1–3/5, not current library outcomes. No test function is deleted.
All paths below refer to `packages/codex-agents/tests/merge_library_support.py`.

| Old file line / assertion | Replacement file line | Requirement/table basis |
|---|---:|---|
| `321` `test.assertEqual(kind, listed[name].classification, name)` | `517` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `492` `test.assertTrue(matching)` | `538` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `493` `test.assertEqual(report.state, matching[0].state)` | `538` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `495` `test.assertTrue(report.recorded)` | `546` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `496` `test.assertTrue(any((r.state == 'landed' and r.kind == ('undo' if kind == 'undoing' else 'forward') for r in matching)))` | `538` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `499` `test.assertTrue(any((r.kind == 'forward' and r.state == 'undone' for r in rows)))` | `550` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `501` `test.assertEqual(expected, tree(f.project))` | `612` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `503` `test.assertTrue(report.recorded, 'reverted requires a durable terminal outcome')` | `546` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `504` `test.assertTrue(any((r.state == 'reverted' for r in matching)))` | `538` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `505` `test.assertEqual(f.expected(batch) if kind == 'undoing' else f.before, tree(f.project))` | `550` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `507` `test.assertEqual(f.operation_before, tree(f.project))` | `612` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `509` `test.assertEqual('needs_owner', report.state)` | `554` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `510` `test.assertEqual('io', report.reason)` | `573` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `511` `test.assertTrue(rows, 'unresolved work needs pinned history')` | `549` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `512` `test.assertTrue(all((r.contents_kept for r in rows)))` | `575` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `513` `test.assertTrue(any((r.kind == 'forward' and r.batch == batch for r in rows)), 'pinned history must retain the exact sealed old/new contents and modes')` | `550` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `517` `test.assertIn(actual.get(path), (pre.get(path), post.get(path)), path)` | `624` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `524` `test.assertEqual('landed', report.state)` | `554` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `525` `test.assertTrue(report.recorded)` | `546` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `527` `test.assertEqual(1, len(matching))` | `538` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `529` `test.assertEqual('landed', row.state)` | `517` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `530` `test.assertEqual('undo' if kind == 'undoing' else 'forward', row.kind)` | `550` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `531` `test.assertEqual(batch, row.batch)` | `517` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `533` `test.assertTrue(any((r.kind == 'forward' and r.state == 'undone' for r in rows)))` | `550` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `538` `test.assertIn(name, expected)` | `612` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `539` `test.assertEqual(stat.S_IFDIR, expected[name][0], name)` | `612` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `541` `test.assertEqual(expected[name], actual[name], name)` | `612` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `542` `test.assertFalse(any((p.startswith(name + '/') for p in actual)), name)` | `517` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `544` `test.assertEqual(tuple(identity), (info.st_dev, info.st_ino), name)` | `517` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `547` `test.assertEqual(expected, actual, 'cleanup changed paths outside listed empty folders')` | `612` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `551` `test.assertFalse(row.cleanup_complete, 'failed cleanup must remain retryable')` | `583` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `558` `test.assertTrue(fault == 'short' or call.operation == 'sqlite_commit')` | `517` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `560` `test.assertIn(report.state, ('reverted', 'needs_owner'))` | `554` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `715` `test.assertEqual(expected, tree(f.project))` | `612` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `717` `test.assertTrue(any((name == p.path for r in reports for p in r.paths)))` | `517` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |
| `773` `test.assertEqual(durable_endpoint(f, batch, rows, operation.__name__), tree(f.project))` | `549` | TF-19; single durable-direction checker, I-PIN, S/T, CU/EL/OW rules; §§1–3/5 |

Compatibility names outcome_oracle and fault_outcome_oracle now delegate to check_end_state;
production-path rows arguments no longer provide direction. Synthetic harness controls supply an explicit
independent read_history callback. Their assertions are unchanged.

The kill helper no longer changes Fixture.before to admit an unrecorded directory. Its independent
candidate set and empty/named checks supply that authority. Both recovery passes and the final fault-sweep
check route through the same checker, using status when recovery has no report.

Owner explicitly approved C-1/C-2 in CONTRADICTIONS-merge-checker.md. The per-path classification assertion
is retained for every other path; only the independently injected validation read path must be unknown.
Waiting-owner preservation is strengthened: owner bytes must remain at the target, not merely in staging.
Normal sample selection changes iteration inventory only; original full enumeration remains available under soak.
Full 41-cell acceptance/rejection, memory disk/state machine, mutation proofs and final timings remain work in progress.
