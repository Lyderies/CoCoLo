# RS-E E2c: the collector's starter, its budgets and its status (CoCoLo 0.4.0)

Source: `~/Code/temp/orchestrator-child-liveness/DESIGN-0.4.0-rse-e2.md` draft 2, its "Draft 3 corrections"
(primary decision 150, binding where they differ) and owner decision 143 (crash loop: "stop after 1 hour and wait for
the user"). E2a (on this branch: `docs/requirements/e2a.md`) built the launch record, copy pinning, the wake publish
and reconcile. E2c builds the starter, its limits and budgets, the backoff/park rule and the status lines. E2-6/T7 are
E2b. Paths are relative to `packages/codex-agents/native-package/.codex/orchestration/codex_agents/`.

Coverage of the design's tests: T1 E2c-1; T2 E2c-2, E2c-4; T3 E2c-2; T4 E2c-6; T5 E2c-7; T6 E2c-5; T8 and T9 are
E2a-5 and E2a-6 (no E2c gap: the starter adds no wake write); T10 E2c-3; T11 E2c-10 (E2a-7 proves the killed service
leaves `pending`; E2c adds "started within one interval" and "a collector restart keeps the backoff"); T12 E2c-8;
T13 E2c-11 (E2a-8 proves a collector that never answers; E2c adds the collector whose intake is closed);
T14 E2c-10.

Named constants (`project_start.py`): `STARTER_CHECK_SECONDS = 5` (due rows are looked for this often, well inside
the 60 s sweep interval), `START_SLOTS = 2`, `START_CLAIM_BATCH = 64`, `START_QUEUE_LIMIT = 1024`,
`START_BACKOFF_FIRST_SECONDS = 30`, `START_BACKOFF_MAX_SECONDS = 900`, `START_PROBATION_SECONDS = 60`,
`START_PARK_AFTER_SECONDS = 3600`, `START_RECHECK_SECONDS = 60`; a start's wait is
`project_host_client.START_TIMEOUT_SECONDS` (8 s), its kill point twice that.

### E2c-1 - A stopped project with due holds starts by itself (E2-1, T1)
A registered project with a launch record, no live host and a due wake row (`project_wake.due_at_ms <= now`:
published by its last service, or marked due by E2a-9's reconcile because its state is unknown) is started by the
collector within one sweep interval (`STARTER_CHECK_SECONDS` apart), with no owner or client action. A project that
is not due is not started. **Acceptance (real collector and project host processes):** a project that never ran
(unknown: due at collector start) and one whose service published `clean_stop` with a past next due are both
serving within the interval, without any client command.

### E2c-2 - Limits and lock discipline (E2-2, T2, T3)
Only the collector's starter thread starts projects. Its locked step only claims: one indexed read of due rows
(not parked, not backing off, no start outstanding; at most `START_CLAIM_BATCH`), each claimed row marked
outstanding with an attempt id and its claim time in the same transaction (`project_starts`), queued in memory
(at most `START_QUEUE_LIMIT`, a project at most once). Probing, verifying, hashing, spawning, waiting and reaping
run on the starter's own workers, off the ledger lock and never on the collector's request thread. At most one
start is outstanding per project and `START_SLOTS` overall; slots are refilled from the queue in claim order (due
order), so every due project is tried before any project is tried again (round robin). **Acceptance:** three due
projects, two of them ending at once: never more than 2 starts at once, never 2 for one project, the first three
starts name three different projects and retries alternate; while a start is blocked in its spawn, a collector
request that takes the ledger lock and a direct ledger read both complete, and the start then finishes.

### E2c-3 - Budgets (E2-2, T10; decision 150)
With 1,000 due projects the starter drains them (every one claimed and its attempt ended with an outcome) within
60 s; no claim step holds the ledger lock 750 ms or more and a 64-row claim batch takes under 50 ms; 1,000 due rows
add under 200 ms to collector start-up (nothing of the starter runs before ready); and the claim step's work does
not grow with 100,000 settled wake rows (rows with no due time). **Acceptance:** measured on a real ledger and a
real collector.

### E2c-4 - Backoff and the crash-loop park (E2-2, owner decision 143, T2)
A failed start (the host ended without serving, did not serve by twice `START_TIMEOUT_SECONDS`, or served but
ended within `START_PROBATION_SECONDS`) counts one failure of the project's streak; the first failure starts the
streak. The next attempt waits `START_BACKOFF_FIRST_SECONDS * 2^(failures-1)`, at most
`START_BACKOFF_MAX_SECONDS`. Failures and the streak are kept in the ledger (`project_starts`), so a collector
restart keeps them, and a publish never resets them. Once a start fails while the streak's first failure is
`START_PARK_AFTER_SECONDS` old, the project parks: `recovery failed: <reason_code>` and no further attempt. Only a
user action unparks: a new launch digest (setup ran again: a new install or update) or the owner's
`learning-start-project-service --project-root <root>` (owner-signed collector operation `start_unpark`, which also
ends the streak). A project reporting again (a publish) does not unpark it. A start that serves for
`START_PROBATION_SECONDS` ends the streak. **Acceptance:** a broken project backs off with growing waits; once
its streak is old enough it parks after its next failure; a republish leaves it parked; the owner command unparks;
a project that is fixed and serves ends its streak; a host killed within probation counts as failed.

### E2c-5 - What is launched (E2-3, T6)
The child is the launch record's copy only: entry `<launch_copy>/.codex/orchestration/project_host.py`, run with
`PYTHON_ISOLATION_FLAGS`, environment exactly `{PATH: os.defpath, xcrun_nocache: "1"}`, its own session
(process group), stdin/stderr to /dev/null and stdout to a private report file. The copy is pinned
(`pin_runtime_copy`) from its verification until the child serves or ends. **Acceptance:** the running child's
arguments name the copy's entry and the isolation flags, and a variable planted in the collector's environment is
absent from the child's.

### E2c-6 - Install changed (E2-4, T4)
Before each start, off the lock, the copy's name must equal `launch_digest` and `launch_target_problem(copy, root,
{})` (the account's own home) must be None; otherwise nothing starts and the status is
`recovery pending: install changed`, checked again every `START_RECHECK_SECONDS` (no failure counted).
A project with no launch record is not started and keeps E2a-11's line. **Acceptance:** a changed copy file, a
changed project runtime file and a missing copy each give `recovery pending: install changed` and no spawn.

### E2c-7 - Another version's live service (E2-5, T5)
When a live host of another runtime serves the project, nothing starts, no maintenance is taken and the collector
does not stop; the status is `recovery: needs update handover: run learning-start-project-service --project-root
<root>`, checked again every `START_RECHECK_SECONDS`. **Acceptance:** with another runtime's live host, the line
and command shown, no spawn, the same collector process still serving and no maintenance marker.

### E2c-8 - Unattended pre-start steps (E2-8, T12)
Before spawning, the starter does the CLI's pre-start steps that need no collector maintenance: a project whose
learning is not moved and has no history is marked moved (the ledger's `mark_learning_moved_if_empty`); one with
history to migrate, or a pending activation, is not started (`recovery pending: learning migration: run
learning-start-project-service --project-root <root>`, checked again every `START_RECHECK_SECONDS`, no failure);
the collector never migrates, never takes maintenance and never stops itself for a start. A host that ends at once
keeps its fixed `SERVICE_START_REASONS` code from its private report file as the failure reason. A live compatible
host is never started again (E2b handles an unresponsive one). **Acceptance:** an empty project is marked moved and
then serves; a project with a pending activation is not started and shows the migration line; a host that ends at
once shows its own reason code in the status.

### E2c-9 - Status, scoped to its subject (E2-7 status part)
The owner-signed, read-only collector operation `start_status` (checked before any lookup, as E2a-11's
`launch_status`) answers `{"ok": true, "recovery": <line or null>}` for exactly the project its proof names, never
hold or result content: null when nothing is due or outstanding; `recovery: starting`; `recovery: started <UTC
time>`; `recovery: backing off (<n> failed: <reason_code>, next <UTC time>)`; `recovery failed: <reason_code>; stopped trying
after an hour of failed starts: run learning-start-project-service --project-root <root>`; `recovery pending:
install changed`; `recovery: needs update handover: <command>`; `recovery pending: learning migration:
<command>`; `recovery pending: service unresponsive` (a live compatible host whose due time is
`START_RECHECK_SECONDS` past). `learning-status` shows it as `recovery` when a launch record exists (E2a-11's line
otherwise). A request without the owner's proof gets the same refusal whether the root is registered or not; a
project session (no owner proof) sees null. attest has no status surface (unchanged). **Acceptance:** each line
over the real collector; unsigned requests for a registered and an unknown root get byte-identical refusals.

### E2c-10 - Stranded claims, timeouts and restarts (T11, T14; decision 150)
A claim carries its attempt id, claim time and, once spawned, the child's pid. At collector start (on the
starter's thread, before its first claim) and once per sweep interval, every outstanding claim that is not one of
this starter's own is reconciled: no child, or a child gone, releases it (a child gone without serving counts
failed); a surviving child keeps it until it serves or ends, and past twice the start timeout it is released as
failed without being signalled (its identity cannot be proved after a restart). A claim never stays outstanding
after a collector crash. The starter's own child still alive at twice `START_TIMEOUT_SECONDS` gets SIGKILL to its
process group and counts failed. A service killed between its pending acknowledgement and its commit, or between
its commit and `exact`, is started within one interval; a collector restart keeps a project's failures and
backoff and reconciles unknown projects as due. **Acceptance:** a claim left by a dead collector with a dead child
is released and the project started; one with a live child is kept until that child ends; a host that never serves
is killed at twice the timeout; E2a's killed service is started; the backoff survives a collector restart.

### E2c-11 - Clean stop with the collector's intake closed (T13)
A project service stopping while the collector's account intake is closed (its socket gone or refusing) finishes
its best-effort `clean_stop` within the 2 s deadline and its stop is not delayed past it. **Acceptance:** a real
project host stopped after the collector is gone exits within the deadline plus its own stop.
