**RSA3-1 — Outstanding owner preparation.** Decision 87 F6: a swept owner-API reservation whose prepare signature work is still running refuses its late return without faulting claims, forgets its handle and releases capacity once.

**RSA3-2 — Crashed-slot restore.** Decision 87 F5: back up and restore a project with a crashed or lost run_holds slot; consistency passes and slot accounting is preserved exactly.

**RSA3-3 — Late-completion comparison, test only.** ledger-local and account releases differ by design on a decided slot (RS-A-2, decision 106b).

**RSA3-4 — Release-batch query cost.** Every SQL statement run per release batch by account_holds_due and close_expired_reserved uses an index for its WHERE/ORDER; EXPLAIN QUERY PLAN on a real ledger shows no table scans.

**RSA3-5 — Release-batch rows unchanged.** The same seeded ledger selects the same due rows before and after the query-cost fix; account_holds_due agrees with _release_due_locked.
