# Test framework requirements for CoCoLo 0.4.0

Status: step-1 review r1 applied; implementation authorized. Next review stop is after step 3 (owner, 2026-11-22).
Owner also authorized a temporary test-only dependency environment; no product installation is authorized.
Baseline: `a1d25560` (0.8.47), branch `feat/test-framework`.

Authority: owner decisions 67, 69, 71–73, 75 and 76–81, and the owner's test-framework work order.
Decision 75 replaces the five-minute target, including the older TR-9 and the verifier's three-under-300s
gate. Total elapsed time is reported for watching trends, not a pass criterion. The research and verified
analysis are inputs, not proof of tool compatibility or measured savings. Python 3.11+, macOS and Linux
are required; Windows is out of scope. Requirements precede failing tests, which precede implementation.

## Terms and review choices

A **test** is a discovered unittest test ID, qualified by suite (`root`, `codex`, `claude`), or a registered
lifecycle check. Inherited tests count under their discovered subclass ID; generated tests must have stable
IDs. Subtests and Hypothesis examples inherit the enclosing test's requirements. A **module** is the full
`suite:module`, even when the runner splits it into several jobs. A **check** is a test/check that actually
asserts a requirement; a tag alone is not evidence. **Job-seconds** are the sum of worker elapsed times,
including their imports and fixtures, not CPU seconds and not the sum of overlapping timing-map entries.

The budgets and stage mapping below are the implementation contract under step-1 review r1.
Step 2 first reports, from `measurements/test-framework/baseline-timings.json`, how many baseline modules
exceed each proposed budget, per layer. Use complete module totals, never add fragments or per-test
estimates to an already summed module value. The owner selected TF-8 option A: a generated, committed,
shrink-only budget-debt file permits only named baseline debt at its recorded ceiling. This is separate
from missing requirement metadata on unchanged existing tests (TF-7).

| Layer | Purpose | Existing scheduling stage | Budget per complete module (job-seconds) | Real process / git / socket |
|---|---|---|---:|---|
| unit | Pure logic, in-memory models and property checks | unit | 5 | refused / refused / refused |
| component | In-process CLI; real temporary files and SQLite | integration | 30 | refused / refused / refused |
| crash | Recovery, bounded faults, disk model and real kill contracts | integration | 60 | allowed / allowed / allowed |
| scenario | Product flows and explicit process/socket/git boundary contracts | integration or e2e (see below) | 60 | allowed / allowed / allowed |
| soak | Explicit long campaigns and complete fault sweep | integration or e2e, explicit soak opt-in only | 900 | allowed / allowed / allowed |

Keep `scripts/test-stages.json` values `unit`, `integration`, `e2e` compatible with existing callers and
tests. Add a companion `scripts/test-layers.json`, keyed identically, for the five layers. Existing integration
boundary tests remain in integration; installer/terminal flows already in e2e remain e2e. A layer change
must not silently reduce CI frequency (integration -> e2e). The layer controls resource permissions and
budgets; the stage controls selection. `integration/crash` means stage integration, layer crash. This avoids
turning existing real-process integration tests into release-only checks. New scenario entries use integration
unless they run the installer or interactive terminal as programs, in which case they use e2e.
Soak additionally requires explicit opt-in.

## Normative requirements and required failing witnesses

Each witness below must first fail against the absent/incorrect rule, then pass after the implementation.
Names below are planned evidence labels, not claims that tests already exist. Framework self-tests use tiny
fixture test suites so their deliberately invalid metadata cannot contaminate the real catalogue.

**TF-1 — Retain the supported runner and isolation.** Keep unittest and `scripts/parallel_tests.py`, the three
suites, module/stage/shard selection, separate worker processes and per-worker temporary directories, secret
stripping, and bounded worker teardown. A full normal run uses the runner's DEFAULT job count (no fixed 6).
Sharding/module splitting must not omit or duplicate tests. Preserve existing `load_tests` discovery hooks.
Witness: discovery and execution of inherited, ordinary, `load_tests`-filtered and generated unittest cases;
stable shard partition; failing worker and import error both make the
runner nonzero; child environments omit fixture secrets; concurrent workers have distinct temporary roots
and cannot see each other's fixture files; an intentionally stuck worker and its owned descendants are
reaped within the configured teardown bound with diagnostics; default jobs remains `max(1, (os.cpu_count() or 2) - 2)`.

**TF-2 — Every module declares one valid layer.** Before running bodies, reject missing, duplicate, unknown,
or stale module declarations and incompatible stage/layer pairs. Stale means no longer discovered. Check all discovered suites, including
modules selected out of the current run, and import failures; never silently default an undeclared module to
integration. Witness: remove one module's layer, add a bogus layer, add a stale entry, duplicate a JSON key,
or declare layer component with stage unit; each is rejected with the full module ID. Valid mappings above pass.

**TF-3 — Every new or migrated test names its requirements.** Use one stdlib test-only decorator
`@requirements("TF-3", ...)` (method or class, resolved to a concrete set for each discovered test) and the
existing lifecycle `@check(...)`. Empty, malformed, unknown or stale requirement IDs fail preflight. A subclass
override cannot accidentally inherit a method's old proof: it must redeclare its IDs or inherit an explicit
class declaration. Every generated state-machine TestCase names its ML/TF IDs. Witness: new untagged test,
unknown ID, empty tag, untagged override and untagged generated case fail with their stable IDs; tagged
inherited and generated cases pass. Existing exceptions are solely TF-7.

**TF-4 — Requirements have text and a real check.** Maintain a checked-in catalogue of active requirements
(unique ID, exact statement, source document/section, check IDs, check kind and evidence records). Check kinds are executable checks or registered review
checks. The semantic review clauses of TF-5/6/19/21/22 require review-record checks with a source revision,
review outcome and an immutable evidence record; executable checks alone do not discharge those clauses.
Future-step checks (TF-19 step 3, TF-20 step 4) stay `not_run` until executed. Neither planned status nor
missing review evidence makes the full gate pass. Import all TF requirements and all 80
`tests/e2e/requirements.json` requirements without paraphrasing; import ML-1..ML-30 when the merge-library
branch is integrated; other existing requirements are added with their source as legacy tests migrate.
Reject an active requirement with no discoverable check and a check naming no known requirement, in both
directions. A requirement cannot be removed or marked inactive just to clear this gate. Witness: absent
check, missing reverse mapping, duplicate requirement ID, unknown check, empty statement, a catalogue
statement differing from its cited source, and a missing or mismatched review evidence record each fail.
The 80 missing lifecycle checks remain failures until step 4 supplies them; they receive no coverage waiver.

**TF-5 — Coverage metadata is not a passing outcome.** A full gate reports a requirement passing only when
every registered required check has run and passed. Skipped, missing, errored, assertion-free lifecycle checks,
and unexpected successes cannot establish a passing requirement. Partial runs report unselected requirements
as not run, never green; global structural validation still runs, and the selected checks determine runtime
results. Expected failures also cannot count as a passing proof. Preserve `Phase.expect` assertion counting and requirement-level AND across all scenario parts,
including G1–G9 checks selected for every affected flow. Witness: two checks for one ID, one failing or
missing from a shard result, cannot aggregate to pass; skipped-only and expected-failure-only proofs are
not coverage. An assertion-free lifecycle proof and an unexpected success both fail the coverage gate. Assertion counting
is not proof of semantic correctness: review against the statement and fault sensitivity are also required.

**TF-6 — Make duplicate slow proof reviewable.** Every non-unit new or migrated test declares a nonempty
`why_not_scenario` rationale (class-level rationale may cover inherited cases) naming the boundary, fault,
concurrency or edge condition a named lifecycle check cannot prove, or records that it IS the canonical
scenario check. Enforce presence; review truth against the requirement. Witness: a tagged non-unit test (including a fast one) without
the rationale fails and names its ID. Existing tests in TF-7 are temporarily exempt from this metadata only.
Rationale alone never authorizes deleting another proof.

**TF-7 — Grandfather only the unchanged baseline, and only shrink it.** Generate and commit the initial
legacy list from discovery on `a1d25560`, recording full suite/test IDs, baseline commit and a SHA-256 source
fingerprint of the test implementation (including an inherited implementation's defining source and class
setup/teardown sources). Include the effective setUp/tearDown/setUpClass/tearDownClass definitions across
the MRO, module setUpModule/tearDownModule, and repository-local functions/classes transitively referenced
by those fixtures or the test body (including imported shared fixture helpers). External stdlib/test-tool
implementations are excluded; their versions are independently pinned. Resolve dependencies by module
and qualified name with cycle detection; unresolved dynamic helper dependencies need an explicit declared
source dependency, otherwise fail closed for grandfather eligibility. Keep deterministic sorted output and the generator command. The list exempts only
missing requirement/rationale metadata. Compare later lists against an immutable initial ceiling AND the
review base version: reject additions, restored removed entries, changed fingerprints or a replaced baseline.
Renamed, overridden or changed tests must be tagged and removed from the list; missing/stale IDs must be
removed. Duplicate IDs are rejected. Witness: add a new test, rename a legacy test, edit its assertion or a shared fixture/helper, grow
the list, re-add a migrated entry, or replace its fingerprint; each fails. A tagged migration removes the entry
and passes. The checker must fail closed if the review-base comparison cannot be made; use an explicit base
commit argument locally and a trusted CI merge-base; never trust a base supplied by the candidate manifest itself
or silently substitute candidate HEAD. Missing or unavailable trusted base fails closed.

**TF-8 — Enforce module budgets, not worker-fragment budgets.** For each module sum actual elapsed seconds
of every executed fragment (including import/setup/body/teardown), compare with the layer budget above, and
name EVERY over-budget module with stage, layer, measured seconds, limit and fragment count. Apply
owner-selected option A: generate and commit `scripts/test-budget-debt.json` from a1d25560's recorded
module job-seconds, recording each module and its baseline ceiling. Every run names EVERY remaining debt
entry as a warning (including unselected modules, explicitly marked not measured). A debt module fails if
its complete measured total exceeds its unchanged recorded ceiling. A complete measurement at or below
its layer budget requires removal from the debt file; afterward any excess over that budget fails. The
file may only shrink: no added/restored modules, increased ceilings, or baseline replacement. Validate
against the immutable generated baseline and the explicit trusted review base. Partial shards cannot
retire debt or certify a complete module. All new modules and modules absent from the debt file fail
above their layer budget. Do not bail out after the first offender.
Separate shards produce mergeable records; the combined gate enforces the aggregate module budget and
debt ceiling. A shard labels its measurement partial. Witness: two 20s component fragments fail its 30s
budget unless authorized baseline debt covers 40s; equality passes and a value just above fails; two
offenders are both printed. A baseline debt warning appears even when unselected; ceiling regression,
restored debt, edited ceiling and debt retained after a complete under-budget result are rejected. Use an
injected clock for these tests, not real sleeps. Overall wall time has no five-minute failure threshold.

**TF-9 — Preserve honest timing and timeout evidence.** Save per-worker elapsed values and run identity,
selected tests, actual jobs, platform/interpreter, completion/timeout status and module totals. Sum only
current-run workers for job-seconds (never stale seed timings, module totals plus per-test estimates, or CPU).
Keep wall time separate; report discovery and preflight seconds separately from worker job-seconds,
including on preflight failure. Always write available measurements on test failure and timeout. Name timed-out
workers and keep their diagnostic log. The scheduler's historical estimates are not measurements. Witness:
seed timings with obsolete entries, fail one worker and time out another; current totals and diagnostics remain
correct. Hard worker timeout is a safety bound separate from the module's budget. Overall elapsed time is
reported without a five-minute failure threshold.

**TF-10 — Refuse real processes and git in unit/component.** Extend the existing audit guard, installed before
test-module imports, to both restricted layers. Deny subprocess starts, spawn, exec, fork/forkpty and system
shell calls; a real git invocation is denied regardless of executable spelling or wrapper. Fakes/in-process
functions remain allowed. Guard violations name layer, module, operation and test ID if known, and cannot be
turned into green by catching the exception (worker records the violation and fails at completion). Witness:
try each available process API, git under an absolute path, an import-time spawn and a caught denial; verify
no child-side marker is created. Run platform-specific API witnesses on their supported OS; the matrix must
contain evidence for both macOS and Linux.

Decision 100 A (2026-11-23): discovery and test workers, locally and in CI, inherit only an explicit environment allowlist: PATH, HOME, temporary-directory variables, locale variables, COCOLO_TEST_* and named framework necessities. Every other variable is dropped, including GITHUB_TOKEN, AWS_SESSION_TOKEN and arbitrary *_PASSWORD variables. TF-10 witnesses must verify the actual worker boundary and permitted-variable controls.

**TF-11 — Refuse real sockets in unit/component.** Deny socket creation/connect/bind and applicable address
resolution/network APIs before OS effects; include AF_UNIX and AF_INET and socketpair. Patch/audit supported
stdlib entry points without blocking construction of test doubles. Explicitly refuse socket.socket,
socket.socketpair, socket.create_connection, socket.create_server, socket.fromfd and socket.fromshare
(where available), connect/connect_ex/bind/listen/accept/sendto/sendmsg, and getaddrinfo/gethostbyname/
gethostbyname_ex/gethostbyaddr/getnameinfo. Cover asyncio open_connection/start_server and Unix variants
through the same underlying guards; there is no asyncio exemption without an explicit evidence-backed
requirement change. Existing creation/connect/bind/socketpair protections remain mandatory. The denial is sticky as in TF-10. Witness:
import-time creation, bind/connect, socketpair and caught denial fail without creating socket files or contacting
a service; in-memory transport fakes pass. Allowed-layer probes use only local fixture sockets.

**TF-12 — Keep unit time deterministic.** Preserve the existing unit sleep prohibition; reject positive-duration
explicit waits/polls in unit tests (thread condition/event waits, select/poll and joins that would block).
Allow immediately ready/nonblocking operations and injected clocks. Component may wait on in-process
threads, subject to its budget. Witness: unit sleep still fails; a positive wait fails without waiting; zero-time
poll and a ready event succeed; a fake clock advances without real time. This is an accidental-I/O guard,
not a security sandbox against malicious tests using native libraries or disabling the harness.

**TF-13 — Allowed layers retain real boundary evidence.** Crash/scenario/soak may use real processes, git and
local sockets under isolated fixtures and their budgets. Do not globally stub these resources to obtain speed.
Maintain a checked-in inventory of shipped process entry points with the named real check for each;
missing inventory entries or missing real checks fail. Witness: the same tiny process/git/socket fixture refused in unit/component succeeds in each allowed layer,
and all owned resources are reaped. Keep at least one real invocation per product entry point during pruning;
process-boundary/concurrency/durability assertions require suitable real contract evidence, not only a fake.

**TF-14 — One shared golden-output comparison.** Provide one stdlib test-only helper (about 30 lines of core
comparison/update logic; security/metadata checks may be separate). New/migrated file-based expected output
goes through it. The framework rejects new/migrated golden comparisons that bypass this helper;
a direct golden read-and-assert bypass must fail preflight. Ordinary scalar assertions stay allowed.
Exact bytes or canonical UTF-8 text comparison produces a bounded useful diff; missing
goldens fail. Deterministic, explicitly reviewed normalization happens before the helper; never silently mask
states, reason codes, content, permissions or evidence required by a statement. Ordinary scalar/value assertions
are not forced into snapshots. Witness: equal, differing and missing golden, binary or documented text-only
behavior, and a changed required reason code. Existing assertions are retained until a named equal proof exists.

**TF-15 — Golden updates need explicit intent and requirement provenance.** A golden can be created or changed
only with exactly `UPDATE_GOLDEN=1` AND a valid requirement ID supplied to the helper and declared by the
calling test. Fail closed for missing/unknown/unrelated IDs and other environment values. Restrict writes to
the checked-in test golden root; reject traversal and symlink escapes. Persist/review the requirement ID with
the golden change in an adjacent `<golden-name>.requirement` UTF-8 sidecar inside the checked-in golden
root, included in review with the golden; report update mode visibly, and do not count an update run as passing evidence. Witness:
unset/0/true without exact 1 cannot write; invalid ID and escape leave files untouched; authorized update
records its ID, and a subsequent normal run is needed for green evidence. Never update a golden to fit code
that contradicts its requirement.

**TF-16 — Only pinned test-only dependencies.** Hypothesis is the only newly introduced third-party test tool.
Record its exact version and SHA-256 artifact hash(es) in a test-only requirements file; also pin/hash required
transitive dependencies so a hash-required offline-prepared environment is reproducible. No shipped module,
runtime manifest, installer or provider imports/installs them. Keep shipped code stdlib only and runnable with
Python 3.11. Only the subsequently owner-authorized temporary test-only environment may receive these dependencies. Witness: static distribution-boundary check and a
shipped-code smoke test with test packages unavailable; dependency file rejects unpinned/unhashed additions.
Select/verify the concrete Hypothesis release and artifact hashes from primary sources in step 2, not from the
unverified version claim in a review packet. If an existing prepared interpreter lacks it, record that limitation
and use the authorized temporary test environment; do not silently skip required property checks.

**TF-17 — Explicit reproducible Hypothesis profiles.** Provide test-only `dev` (20 examples), `ci` (100 examples,
`derandomize=True`), and `soak` (at least 1000 examples). Runs default to ci; dev and soak need explicit selection. Unknown profiles fail. I/O/stateful rules use `deadline=None`, and still obey module budgets and
hard timeouts. Keep unittest-compatible generated TestCases. Witness: inspect each loaded setting; reproduce
a deliberately failing state machine and verify its minimized trace and unittest failure are retained in the
runner log and yield nonzero. Parser compatibility must be demonstrated, not assumed from unittest ancestry.

**TF-18 — Normal sampling and soak separation.** Normal crash runs include an in-memory disk model
with separate volatile and durable file-content, mode and namespace states. Writes change volatile file
contents; create/unlink/rename/exchange/mkdir/rmdir change volatile namespace links. A file flush persists
that file's complete bytes/mode; a directory flush persists that directory's name-to-object links, never
implicitly an unflushed child's contents. SQLite commit state is tracked separately. Crash discards volatile
state and exposes only durable contents reachable through durable namespace links; recovery is then checked
against the single ML invariant and table. Permitted outcomes include complete S or T according to the
durable row, preserved owner objects and named unrecorded directories; torn published contents, lost
owner edits and a history direction inconsistent with recovered contents are rejected.
Witnesses distinguish flushed from unflushed writes, creates and renames, including flushing only one
rename parent; a missing production flush must change the model's post-crash outcome and fail the check.
A checked-in `tests/merge-persistence-samples.json` names the normal sample inventory by operation,
phase, before/after side and commit label where applicable, plus stable occurrence/reproducer. Validate it
against discovered persistence-significant calls; every operation kind and relevant boundary needs a normal
sample. Normal selection includes the model and ci-profile generated cases, excluding complete sweeps.
Complete crash/fault sweeps, including recovery faults, run only under explicit `--profile soak` in
local-routing; soak includes the complete inventory, not merely persistence samples. Reject explicit soak
module selection with dev/ci, a complete-sweep request without soak, conflicting profile selectors and
unknown profiles. Report the effective profile and sample seed or minimized reproducer. The Linux/podman
power-loss lane is later work (81 A); kills and the model do not prove it, and this task installs no podman.

**TF-19 — Preserve the single merge oracle and its review gate.** Step 3's `check_end_state` is derived from
sections 1, 2, 3 and 5 of `ANALYSIS-0.4.0-merge-library-states.md`, with one POLICY dictionary at Q1–Q6 recommended
defaults. Read durable history independently; never infer direction from a report or all-new tree. The state
machine selects land/retry/cancel/undo/crash/recover and fault points, not a second handwritten outcome oracle.
Trace every replaced assertion to a table cell and replacement file:line; primary checks the table line by line.
Witness: every permitted outcome in all 41 cells is ACCEPTED and deliberately wrong outcomes for each cell are rejected; the four required mutations (private-location
check, flush before A, third history commit, injected EIO falsely landed) each have a named catching test.
If a current test contradicts its requirement, stop on it and record test name, exact requirement and conflict;
the already-identified oracle corrections are requirement-driven work, not permission to fit expectations to code.

**TF-20 — Scenario coverage uses the shared fixture and exact statements.** Step 4 extends the existing
`scripts/e2e_lifecycle.py` with a shared temporary project, prepared git repo, scripted stand-in Claude/Codex
binaries and service on a Unix socket. Build the shared project/providers first, then implement the
checks in six parts covering all 80 requirements. Cover all 80 requirements, not necessarily exactly 80 checks, in six
parts: API (27), G1–G9 (9), L12 (7), TERM (5), and two parts containing the other L requirements (32 total).
Parts do not multiply a module's budget; fragments aggregate under TF-8. Each part obeys the scenario budget, preserves all-flow checks and AND aggregation, and emits per-requirement
pass/fail with content-free reports. Every assertion matches its statement's exact reason codes/states. One
deliberate fault per group must make that group's checks red; shared G checks may also detect it and must
never be suppressed to obtain an 'exactly one group' presentation. `test_e2e_requirements` must pass unchanged
in meaning. Witness: all-80 execution, isolated and combined part execution, fault matrix, private-value checks.

**TF-21 — Prune only with equivalent named proof.** After step 4, address the ten heaviest modules by
baseline job-seconds (`MEASURE-test-framework.md` top-ten rows, originally lines 92–101) before wider
pruning. Before each step-5 module change, record its requirement
IDs and keep/move/remove disposition. A removed test requires a named start-to-finish check proving the same
statement and relevant edge cases; a moved function test keeps identical assertions/expected values and cites
the requirement. Every removed/changed assertion is listed with old and replacement file:line; no weakening.
Keep real boundary checks per TF-13 and remeasure the affected module after each change, quoting the timing
file's job-seconds. Witness: report links to executed replacement and retained assertions, with before/after
measurements; metadata-only replacement is insufficient. No pruning precedes the matching scenario proof.

**TF-22 — Review checkpoints and evidence.** Step reports are under 15 lines each and name commits,
requirements -> red/green or measurement evidence, every assertion replacement, and open issues. Owner subsequently set the next review stop after step 3 (step-1 review r1). Apply that stop before proceeding to steps 4–5. Use only own worktrees/branches; never push, install outside the authorized temporary test environment, or modify the
control project or 0.3.3. Step 2 is authorized; step 3 rebases the existing merge
branch only after step 2 is committed. Acceptance includes macOS and Linux / Python 3.11 evidence or an
explicit outstanding platform gate; this Mac run alone cannot prove portability.

## Implementation evidence plan (not completed)

| IDs | Step-2 failing fixture / later acceptance evidence |
|---|---|
| TF-1–7 | Runner metadata/coverage tests; generated legacy manifest; shrink/base comparison fixtures |
| TF-8–9 | Injected-clock aggregation, failed-worker and timeout fixtures; saved timing evidence |
| TF-10–13 | Restricted/allowed-layer process/git/socket/wait probes, both supported platforms |
| TF-14–15 | Golden equality/missing/update/provenance/path escape cases, normal rerun after update |
| TF-16–18 | Distribution checks, prepared Hypothesis profile/parser smoke, sample/soak selection fixtures |
| TF-19 | Step 3: 41-cell checker evidence, state machine, disk model and four mutation proofs |
| TF-20 | Step 4: all-80 mapping/runtime results and six-part injected-fault matrix |
| TF-21–22 | Step 5 assertion replacement ledger, remeasurements, commits and review handoffs |

Step-1 review r1 fixes 1–14 (and optional 15) are incorporated; option A is owner-selected for TF-8.
No legacy coverage waivers,
test expectation changes, new dependencies beyond Hypothesis's required dependency closure, or total-run
time target are proposed. Existing slow-module and lifecycle-coverage failures are visible debt, not reasons
to claim the framework implementation is green overall.


**TF-20 — Review witnesses need falsifiable evidence.** Every new or changed review-fix witness must name its requirement, positive control, an old-code red run at the reviewed base revision (round 3: `9c18c4a3`) with the intended failure diagnostic, and its green run after the fix. For a witness-only repair, the red must show that the old witness accepts a deliberately invalid result; the repaired witness must reject that same result and accept the valid control. A small committed evidence manifest declares each fix and its witnesses. A framework check rejects a declared fix without a requirement, positive control, reviewed base, red-run evidence/diagnostic, or green-run evidence. Referenced evidence files must exist and record the stated outcome. Human review verifies that the diagnostic and control prove the requirement; mechanical presence checks do not replace that judgment. No changed assertion may be omitted from the assertion ledger.
