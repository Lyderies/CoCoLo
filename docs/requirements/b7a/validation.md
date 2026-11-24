# B7a validation

Run from `packages/codex-agents`. Only these named unit modules are run here; no full suite, git or sockets.

| ID | Unit coverage | Primary socket/CLI coverage |
|---|---|---|
| RS-B-B7-1 | tests.test_holds_report.HoldsReportTests.test_B7_1_* | tests.test_holds_report_socket.HoldsSocketTests.test_B7_1_2_3_6_* |
| RS-B-B7-2 | tests.test_holds_report.HoldsReportTests.test_B7_2_*; HoldsUpdateTests.test_B7_2_* | same socket/terminal tests |
| RS-B-B7-3 | tests.test_holds_report.HoldsReportTests.test_B7_3_* (real D3 faults; 21 repairs past bound 20) | same socket/terminal tests |
| RS-B-B7-6 | HoldsProtocolTests.test_B7_6_*; HoldsClientStatusTests.test_B7_6_*; HoldsUpdateTests.test_B7_6_* | same socket/terminal tests |

Exact runnable unit command: `python3 -B -m unittest tests.test_holds_report`.
Initial red: 14 tests, 13 errors (missing report/operation/client), 0.101 s (0.457 s command wall).
Additional update/native-owner tests were written before implementation; red run recorded below.

Primary-only command (NOT run here; socket use forbidden):
`python3 -B -m unittest tests.test_holds_report_socket`.
This includes the isolated plain-terminal `learning-status` subprocess, real HistoryClient framing, HistoryService,
project store, exact output and foreign project/runtime denial. Native enrollment and unrelated collector/account
status are synthetic. The terminal runs `python -I -B -S -c ...`; it does not launch an installed runtime or provider.

B4m merge follow-up: this branch does not have `codex_agents/reply_shapes.py`. Add an entry for
`history.holds_report` to that reply-shape table when merged. Do not extend `history.capacity_report`.

No existing test assertions changed. B7b reasons/staleness/budgets are out of scope.

## Executed unit results

All rows below are the exact unit command above, run from packages/codex-agents.
Seconds are unittest's elapsed time; command wall time is also given for the first and final runs.

| Stage | Tests | Result | Seconds |
|---|---:|---|---:|
| Initial red | 14 | 13 missing implementation errors | 0.101 |
| Complete pre-implementation red (update + native owner added) | 16 | 15 missing implementation errors | 0.100 |
| Implementation check | 16 | 2 fixture setup errors (missing timeout; fixture deadline already expired); assertions unchanged | 0.126 |
| First green | 16 | pass | 0.135 |
| Verifier-start edge red | 19 | 1 failure: started verifier still counted as awaiting | 0.160 |
| Verifier-start green | 19 | pass | 0.159 |
| Legacy recorded result edge red | 19 | 1 failure: legacy admission receipt not excluded from update-unrecorded | 0.175 |
| Final green, including scoped limit-registry check | 20 | pass | 0.185 |

Final command wall time: 0.558 s. Sum of unittest elapsed time: 1.141 s across these runs.
Two earlier setup invocations failed at module loading (one before file creation, one from the workspace root);
no tests executed in those invocations, and they are not red-test evidence.
Only tests.test_holds_report was executed. No run approached five minutes.

Read-only syntax QA parsed the seven changed runtime Python files, the socket test module and both embedded terminal
scripts without executing socket code. The new limit sites are registered in tests/lifetime_caps.json; the new unit
module verifies those scoped registry entries and their named past-the-limit tests.

## Stop required by an existing assertion

Final read-only compatibility review found `AuthorityTests.test_operation_set_unchanged` at
`tests/test_b2_service_outcome.py:422`, which pins the full operation set before history.holds_report.
The user explicitly requires stopping if an existing assertion must change. Work stopped, leaving that file untouched.
See [assertions.md](assertions.md) for the exact old -> new expectation. The primary must resolve it and run the affected
regression module. Its exact primary command is `python3 -B -m unittest tests.test_b2_service_outcome`.
The socket command above remains unrun. B7a is not complete until this existing pin is resolved by the primary.

Changed files: docs/requirements/b7a.md, docs/requirements/b7a/validation.md, docs/requirements/b7a/assertions.md;
packages/codex-agents/tests/test_holds_report.py, tests/test_holds_report_socket.py, tests/lifetime_caps.json;
native-package/.codex/orchestration/cli.py and codex_agents/holds_report_contracts.py, project_holds_report.py,
history_api.py, history_service.py, service_host.py, client_runtime.py (package-relative paths).
