# MED 2 (review rev040rsa3r1): same-revision regression evidence, run by the primary OUTSIDE the Codex sandbox

`--base b531844030fa619c1f62ddd6c092ebc2ea4fca38` is the framework's trusted grandfather baseline argument (TF-24), not
the code revision under test. Code revisions: base = 5ab0a9d3 (version/0.4.0, 0.8.50, clean tree);
candidate = 2357016b + the r1 fix. Same 13 regression modules (account_billing_admission, account_billing_jobs,
account_claim_storage, account_claim_jobs, account_lane_restart, account_ingress, account_observation_jobs,
account_original_open, cli_observation_runtime, ledger_busy_answers, open_request_lifetime,
project_execution_processes, required_host_open); candidate also test_rsa_step3. Default 16 jobs, hash-pinned venv.

| run | units | tests | failed units | budget failures | legacy metadata errors | RSA3 requirement failures |
|---|---|---|---|---|---|---|
| base 5ab0a9d3 | 18 | 295 | 0 | 0 | 0 | - |
| candidate | 19 | 300 | 0 | 0 | 1028 (framework gap 3, decision 105) | RSA3-3 (intended expectedFailure) |

The failures and the account_billing_admission timeout in report.txt / r1-report.txt came from runs inside the Codex
sandbox (no sockets) and appear on base and candidate alike; they are environment, not regressions.
