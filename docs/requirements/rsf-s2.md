# RS-F S2: fence persistence owners before adoption

Sources: `ANALYSIS-0.4.0-rsf-migration.md` §§1–4/S2 and
`DESIGN-0.4.0-B4-takeover.md:128–139` in the planning worktree;
owner decision 129.3. Base `cbc757d5` (0.8.60). This slice provides the
store fence; signalling a live service and adopting its children belong to B4.

**RSF-S2-1 — Exclusive lifetime ownership.** Both store owners acquire their
existing, protected writer flock before opening/migrating a database. A live
predecessor, including a stopped/paused executable, prevents adoption and new
admissions. Do not infer exit from a failed health check. Keep the same lock
inode/descriptor until the connection closes; refuse lost, replaced, unlinked,
inherited or released ownership, without reacquiring it. Never unlink locks.
A released lifetime lock proves the predecessor has retired its connection;
real executable tests additionally wait for process exit before migration.

**RSF-S2-2 — Atomic epoch before adoption.** Each successful owner acquisition
advances one durable singleton `runtime_takeover` epoch, retaining the preceding
and current loaded runtime digest. Install it in the schema transaction before
any historical hold adoption; roll it back with a failed migration. A successor
opening the same build also advances the epoch. Publish no owner/admission
interface until initialization and validation succeed. Supported historical
records still upgrade through the existing authorization and backup gates.

**RSF-S2-3 — Every current write.** A central connection boundary checks the
captured epoch and lifetime ownership inside every write transaction and again
before commit. Cover account/project admissions, direct writes, autocommit,
cursor/batch/script writes, explicit transactions, sweeps, backup registration,
deletion and VACUUM/journal maintenance. Transactional writes by a paused stale
writer must refuse with zero committed changes after takeover, even if it
retains a connection. VACUUM/journal-mode maintenance checks immediately before
and after the operation while retaining the lifetime flock. SQLite cannot wrap
these operations in the epoch transaction: a revocation during maintenance can
be detected afterwards but cannot undo its already durable changes.
Rollback and connection close must remain possible after revocation.

**RSF-S2-4 — Exclude old executable reopen.** Advance ledger schema 16→17
and project schema 21→22 so genuine older executables cannot reopen either
migrated store, including an offline CLI. Test an unmodified git archive of
0.8.55 (`8d4a7ce3`) holding each store, racing migration, and retrying after
migration. Old stores are positive upgrade fixtures; old runtimes are negative
writer fixtures. Do not change VERSION or the runtime digest catalogue.

**RSF-S2-5 — Plain refusals.** Current fencing refusals identify ownership or
runtime replacement in plain language and include the exact trusted command
`./setup.sh --update`. Keep existing busy/incompatible reason codes so wire
reason inventories do not grow. Already installed old binaries cannot acquire
new message text; their existing schema refusal is the negative fence proof.

**RSF-S2-6 — Preservation and validation.** Preserve signed/historical records,
existing backup formats and project partitions. The machine-local epoch never
travels in a per-project learning backup or source image. Register new tests,
run them red before code then green, and prove `metadata_errors: []`. Run only
named modules, including every copier/backup/restore/move/enumeration module
affected by the schema step. Record evidence and unrun checks in
[STATUS.md](rsf-s2/STATUS.md). Existing assertions may change only with an
old→new entry in [assertions.md](rsf-s2/assertions.md).

## Fixed-scope full-run corrections (2026-11-25)

RSF-S2-1/2/4/6: the ledger 16→17 step adds only a machine-local epoch;
it changes no project records and needs no per-project backup or extra records
upgrade approval. Like the existing additive steps, registration may perform it
after the earlier collector has drained and released its lifetime lock. Older
record-changing steps keep their existing authorization and backup requirements.
Released 0.8.59 remains a schema-16 fixture; current opens produce schema 17 and
preserve its records, backup, restore and move behavior. Pin the unchanged
launch-receipts layout for schema 17 and document its epoch-only upgrade.

RSF-S2-3: named SAVEPOINT/RELEASE/ROLLBACK TO must retain SQLite nesting and
rollback semantics, both standalone and inside an explicit transaction. Opening
a savepoint must not commit it; releasing the outermost one rechecks the fence.
Cleanup issuance checks live custody authorization before examining historical
cleanup material; rejected callers retain `history_not_authorized`.

RSF-S2-3/6: read-only hold reports check process/thread ownership without opening
the lock or any other private file, and write nothing. Record the schema-version
comparison caught by the lifetime-cap guard with its end and named regression
test. No VERSION or runtime digest catalogue change belongs to this round.

## Fixed-scope review corrections (2026-11-25)

RSF-S2-6: classify `runtime_takeover` with exact-column global account metadata,
never project-keyed local tables. Backups, source images, restores and moves
must preserve project records without exporting or partitioning this singleton.

RSF-S2-3: a failed fence check at RELEASE/COMMIT rolls back; a SQLite error from
the operation itself retains SQLite's transaction state, including earlier
writes in an enclosing transaction. Maintenance must pass a fresh fence check
after the short epoch transaction ends and before its operation starts, and
check again afterwards subject to the durability limit above.

RSF-S2-3/6: pure-read `_operation` methods check only process/thread/closed-owner
affinity, with no lock probe or epoch query. Outcome-page SQL must use the
outcomes index for every statement. Reads that persist a clock floor or retire
records remain write operations.

RSF-S2-3: a write transaction performs full lifetime-lock probes at its opening
and before commit, with cheap epoch comparisons per intervening write statement.
A 500-statement transaction must use at most two full probes, including nested
savepoints and normal owner transaction helpers. Revocation at commit still
rolls back all pending writes. A lock-only revocation with an unchanged epoch
is detected at the commit boundary; intervening statements may remain pending
but must never become durable.

RSF-S2-3: `FencedConnection.executescript` and its cursor counterpart execute
statements in the current owner transaction without SQLite's usual implicit
COMMIT. An error leaves prior script statements pending in that transaction;
the caller can roll them all back. Explicit transaction control in a script
retains its ordinary fenced semantics.
