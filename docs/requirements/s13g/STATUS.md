# S13g status (feat/s13g-budgets)

Done: run_retention_scale fixture (two sizes + growth check), project_start shared bytecode cache and shorter test intervals.
See assertions.md for every change old -> new.

Numbers (summed fragment seconds from scripts/parallel_tests.py --jobs 16):
- test_run_retention_scale: 37.4 alone (12 units) -> 9.9; 56.8 under the 8-module load run -> 17.6 (budget 60).
- test_project_start: 168.1 alone -> 144.3; 193.8 under load -> 191.8 (debt ceiling 183.4). NOT yet inside the ceiling under load:
  one fragment varies 22-40 s between runs of identical code; the module is wait-bound (probation, backoff, product-interval drain of
  ~7.7 s at production intervals, 15 collector + host launches) so more cuts need clock-controlled rewrites (S13d `clock` option) of
  the probation tests or fewer real launches. Next candidates: ProbationTests (12.8 s serial), BackoffTests (17 s serial), ProductionIntervalTests (12 s serial).
- Load run: modules test_run_retention_scale, test_project_start, test_billing_gate, test_container_stop, test_host_commands,
  test_b2_service_outcome_paths, test_write_merge, test_lost_run_settlement, --jobs 16. metadata_errors [] (no module or fragment added).
