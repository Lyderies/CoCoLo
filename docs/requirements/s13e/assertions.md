# S13e assertions

No existing assertion is changed or removed. E2c-1, its deadline and its fixture are unchanged.
The kept checks live in `tests.test_project_host_start_cost` (S13E-2 bytecode refusal only).

During red-test development the new assertion that arbitrary source-less `.pyc` imports fail was replaced with
the actual protected-copy CLI inventory refusal before imports. `pycache_prefix` prevents source cache reads;
the exact-file gate is what rejects extra source-less modules. No product assertion or bound was weakened.

## Note for later (not planned work)

`project_host.py` has no pre-import exact-file inventory check; only `cli.py` does. Proposal: give
`project_host.py` the same inventory gate as `cli.py`, which would also cover startup modules and any deferred import.
