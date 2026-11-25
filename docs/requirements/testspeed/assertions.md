# TESTSPEED assertion accounting

No assertion was removed, weakened or edited. Every `assert*` call in the three modules is byte-for-byte the
same. The changes below are to fixtures, process arguments, one fixed sleep and test ids.

## Test ids

- `RealNetworkTests.test_real_allowlist_isolation_and_record`, `.test_real_proxy_down_means_no_network` and
  `.test_real_release_and_sweep` -> their bodies, assertions and docstrings are unchanged and now live in
  `scenario_allowlist_isolation_and_record`, `scenario_proxy_down_means_no_network` and
  `scenario_release_and_sweep` (not collected by discovery). The new test
  `RealNetworkTests.test_real_scenarios_run_together` starts the three as their own test cases (own `setUp`,
  cleanups and project name) in three threads at the same moment, joins them, and reports each as a
  sub-test with that case's own failure text, or its skip. Why: the three share nothing and are podman-bound
  (TESTSPEED-6). A sabotaged assertion in one scenario was shown to fail the sub-test with the original
  assertion message. `test_real_unreachable_podman_is_a_plain_refusal` is unchanged.

## Process arguments and environment (no assertion involved)

- `test_cli_billing_runtime`, `test_cli_open_client_runtime`: the fresh CLI is started with
  `-X pycache_prefix=<per-class temp folder>` instead of `-B` (TESTSPEED-4), and with
  `COCOLO_TEST_ENDED_SERVICE_STARTS=1` in its environment (TESTSPEED-3; billing `start`, open-client
  `invoke_open`). `invoke_record` and the other CLI helpers in other modules do not set it.
- `tests/dispatch_cli_fixture.py`: with that variable set, the CLI process replaces (a) the collector's
  `subprocess.Popen` for `learning-serve` by a process that has already ended with status 2 and (b) the
  project service's `os.posix_spawn` by an interpreter that exits at once (its report-file actions are kept).
  Everything around them (the wait, the health counter, the refusal reasons) still runs. Without the variable
  the fixture behaves as before. Real starts stay covered by `test_project_start`, `test_learning_cli` and
  `test_host_commands`.

## One sleep

- `test_second_project_fresh_cli_observes_while_first_probe_process_is_held`: the fake client's frame
  `{'sleep': 8}` -> `{'wait_for': <gate file>}`; the test writes the gate file after
  `assertIsNone(first.poll())` (the first probe is still held when the second project is answered) and before
  `self.take(first)`. All three assertions around it, including `assertLess(..., 6)`, are unchanged. The hold is
  now exactly as long as the test needs, however slow the machine (a held child can no longer finish early on a
  loaded machine); a cleanup opens the gate if the test fails first.
