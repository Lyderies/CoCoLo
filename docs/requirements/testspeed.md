# TESTSPEED: three scenario modules at their 60 s budget

Base: 0.8.63 (`38a96538`), branch `fix/testspeed-cli-runtime`. Owner rule: least test running.

`test_cli_billing_runtime`, `test_cli_open_client_runtime` and `test_run_network` sat at the 60 s per-module
budget of the scenario layer and overran it in three consecutive full runs (per-module totals across fragments:
61.0 / 62.8 / 61.6 s, 56.1 / 59.4 / 60.4 s and 57.2 / 62.5 / 62.5 s). Quiet runs passed.

## Requirements

- **TESTSPEED-1:** each of the three modules totals at most 45 s with full-run parallelism (`--jobs 16` beside
  heavy modules), measured as the sum of its fragments' seconds, which is what the budget check counts.
- **TESTSPEED-2:** no assertion is removed or weakened. Every changed assertion, renamed test or changed fixture
  behaviour is paired old -> new in [`testspeed/assertions.md`](testspeed/assertions.md).

Design requirements that follow from the findings below (each is checked by the modules still passing with every
original assertion in place, and by the before/after seconds in [`testspeed/STATUS.md`](testspeed/STATUS.md)):

- **TESTSPEED-3:** a fresh-CLI test that has no collector and no project service must not pay for starting them.
  The CLI's best-effort starts of the collector and of the project service end at once in such a fixture; the
  fixture ends them at once instead of starting an isolated interpreter that is bound to end. Only tests that
  opt in get this; real starts keep their own tests.
- **TESTSPEED-4:** the fresh CLI processes of one test class share one bytecode cache outside the source tree.
- **TESTSPEED-5:** a held fake provider child waits on a gate file the test opens, not on a fixed sleep.
- **TESTSPEED-6:** independent real-podman scenarios that share nothing run at the same moment.

## Findings (profile first)

Per-test seconds were taken with a one-off timing runner (not committed) plus `cProfile` of the fresh CLI
processes; `scripts/parallel_tests.py --save-timings` only records per-fragment averages.

1. **Doomed service starts (billing and open-client).** Every fresh `provider-observe` CLI in
   `test_cli_billing_runtime` first calls `ensure_collector`. The fixture has no collector, so the CLI spawns
   `learning-serve` under `-I -B -S -X pycache_prefix=/dev/null`; the child recompiles the whole runtime tree
   (no bytecode by design), finds nothing it can serve and ends with status 2 after about 0.9 s. The CLI waits
   for it and reports the collector unavailable, which is the outcome every one of those tests already expects.
   That is about 0.9 s of a 1.5 s CLI call, on every one of the ~20 calls of the module. In
   `test_cli_open_client_runtime` the first (no runtime hint) open also starts the collector and the project
   service twice each (~5 s per test, 4 tests = ~20 s of the module's ~57 s).
2. **Fixed sleep (billing).** `test_second_project_fresh_cli_observes_while_first_probe_process_is_held` held
   the first probe with a fake-client `{'sleep': 8}` frame: 8 s every run. The fake client already supports a
   `wait_for` gate file.
3. **No bytecode cache for fresh CLIs.** Every test CLI is `python -B -m tests.dispatch_cli_fixture`, so each of
   the ~16 CLIs per open-client test recompiles the runtime (0.32 s -> 0.09 s with a cache, more under load).
4. **Podman call count (run_network).** The three real-podman tests (10-18 s each) spend about 90 % of their
   time in 60-100 guarded podman calls of about 0.12 s each (a guard interpreter plus the podman client). The
   product's own sequence is: ensure the proxy, create the network, join, re-verify, push policy twice. The only
   single wait that is not a call is the 3 s ARP timeout of a child talking to a stopped proxy, which is the
   scenario itself. Probes inside the child are already fast (0.00-0.08 s) except that one.
   The three tests share nothing: each has its own project name, proxy, networks and folder.

## Root cause and fixes

| Module | Cause | Fix |
| --- | --- | --- |
| billing | doomed collector start per CLI; 8 s sleep | TESTSPEED-3, -4, -5 |
| open-client | doomed collector and service starts; no bytecode cache | TESTSPEED-3, -4 |
| run_network | sum of three independent podman-bound scenarios | TESTSPEED-6 |

No product code changed, so no runtime digest re-record is needed. A product-side saving exists for
run_network (the proxy policy is pushed twice per `prepare_run`, and `container inspect` of the proxy is
called twice in a row inside `_join`/`_push_policy`); it is worth about 5 s of the module and was left alone
because it touches the egress proxy's safety checks.
