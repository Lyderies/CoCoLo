# S13e status

Branch `feat/s13e-tests`, base `ce81bd96` (0.8.67). **Closed by decision 332: the product change is not merged.**
Nothing under `packages/codex-agents/native-package` changes; VERSION and the runtime digest catalogue are unchanged.

## Result

The proposed change (lazy bridge and learning imports) measured as noise. Real start, begin event to serving,
both due projects pooled, median seconds (per-sample data in [evidence-fix-round.json](evidence-fix-round.json)):

| Condition | Base | With change |
| --- | --- | --- |
| Quiet | 0.547 | 0.531 |
| Loaded (`--jobs 16`) | 0.586 | 0.595 |

The base runtime passes E2c-1 5/5 at `--jobs 16`. E2c-1 remains a known load flake, attributed to plan step S13f
(the cause search for slow project start under load). Import-only figures are in [evidence.json](evidence.json).

## Kept

| Item | Where |
| --- | --- |
| S13E-2 bytecode refusal (timestamp, stale, checked-hash, unchecked-hash, source-less; positive controls; isolation flags; CLI inventory refusal) | `tests.test_project_host_start_cost.ProjectHostStartCostTests.test_host_and_deferred_imports_ignore_planted_and_stale_bytecode` |
| Start and import measurement | `scripts/measure_project_host_start.py`, `scripts/measure_project_host_imports.py` |
| Evidence | `evidence.json`, `evidence-fix-round.json` |

Dropped with the product change: the import-boundary test (S13E-1) and the deferred-module verification tests (S13E-3).
See [assertions.md](assertions.md) for the later-note on a `project_host.py` inventory gate.

## Rerun

`$PY` is the test interpreter; use a short temporary root.

```sh
TMPDIR=/tmp/s13e "$PY" -B scripts/run_measurement.py scripts/measure_project_host_start.py -- --output /tmp/s13e-quiet.json
TMPDIR=/tmp/s13e "$PY" -B scripts/run_measurement.py scripts/measure_project_host_start.py -- --load --output /tmp/s13e-loaded.json
TMPDIR=/tmp/s13e "$PY" -B scripts/parallel_tests.py --jobs 4 --module codex:tests.test_project_host_start_cost --module codex:tests.test_runtime_release
```
