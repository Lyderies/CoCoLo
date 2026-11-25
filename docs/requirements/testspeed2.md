# TESTSPEED-2: setup_straight over budget under full-run load, and the lost-run flake

Base: 0.8.64 batch, branch `fix/testspeed2`. Owner rule: least test running.

`root:test_setup_straight` (4 fragments, budget-debt ceiling 293.12 s in `scripts/test-budget-debt.json`) took
285-299 s in the last five full runs but 195 s alone. Part B (the lost-run flake) is a separate cause in the same
unit.

## Requirements

- **TESTSPEED2-1:** `root:test_setup_straight` totals at most 230 s, measured as the sum of its fragments' seconds
  (what the budget check counts), with full-run parallelism (`--jobs 16` beside `codex:tests.test_project_driver`
  and `root:test_install`).
- **TESTSPEED2-2:** the lost-run test helpers `counted` and `claimed_bindings` read the account's tables under the
  ledger lock, never between the statements of an open account write (the load flake of the lost-run tests). Test:
  `LedgerReadHelperTests.test_the_ledger_read_helpers_wait_for_a_write_the_collector_has_open` (red on the old helpers).
- **TESTSPEED2-3:** no assertion is removed or weakened. Every changed assertion, renamed test or changed fixture
  behaviour is paired old -> new in [`testspeed2/assertions.md`](testspeed2/assertions.md).

## Findings (profile first, part A)

Per-test seconds (serial, one process, quiet): 19 tests, 165 s. Twelve tests start from `installed()`, a full
real wizard run (about 4.8 s), and the update and answers-file tests run the wizard 4-6 more times each
(`update_skips_billing...` 30 s, the answers-file tests 25 s and 17 s).

The wizard runs in-process (`tests/wizard_direct_driver.py`), so what costs is what it starts: one `cProfile` of
the wizard thread of the 5.7 s `test_S4_1_quit_on_an_installed_project...` shows 165 child processes, 16 of them
isolated interpreters (`python -I -B -S -X pycache_prefix=/dev/null`: the owner CLI, `install.py` and the Claude
`package.py` install/verify, the attest helpers) taking 3.4 s of the 5.7 s, and 79 `getconf` starts (0.24 s). The
interpreter flags forbid bytecode by design, so each of those starts recompiles the whole runtime tree (0.4 s for
`cli.py`, mostly compiling). Under full-run load compiling is CPU-bound and is what stretched 195 s to 290 s.

## Fix

`DirectDriverMixin` (test-only driver) gets an opt-in class attribute `shared_pycache` (set by
`SetupStraightTests`): the driver then runs every `subprocess.run` that carries `-X pycache_prefix=/dev/null` with
`-X pycache_prefix=<one directory shared by the test class>` and without `-B`, so the first start compiles and the
later ones read the cache; the directory is removed after the class. It changes only the start that is made: the
argument list the wizard builds and the logs record (`register.log`, `python-full.log`, ...) keep the original
flags, so every assertion that reads them is unchanged. It also answers the repeated `getconf DARWIN_USER_*_DIR`
start once per run. No product code changed (no runtime digest re-record); the real installer, wizard steps and
the real terminal runs (`KEPT_TERMINAL_RUNS`) are untouched. Other users of the driver
(`test_setup_wizard_direct`, `test_setup_update`) do not opt in; they would gain the same way.
