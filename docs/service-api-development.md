# Local service API implementation status

Release 0.3.0 requires normal clients to use project/account services, with SQLite owned only
by their persistence components. This boundary is being implemented. Existing CLI workflows
still use the installed 0.8.12 implementation; the source codec does not enable a service.

The [native caller enrollment proposal](native-caller-enrollment-design.md) maps the
retained entry checks and the bootstrap, actual-primary and descriptor-custody proofs
needed before privileged integration. It does not enable the authorization adapters.

The first increment, `codex_agents.service_protocol`, is a pure contract for `project.status`
and `project.cancel`. It accepts one UTF-8 JSON message of at most 65,536 bytes. The envelope
and target have closed field sets. Duplicate keys, unknown operations, arbitrary method/SQL
fields, file paths and PID targets are rejected with fixed codes that contain no rejected text.
Status may target an orchestration or an exact run/attempt. Cancellation requires the latter.
Direct construction validates the same fields, including exact string/integer types.
Canonicalization validates them again. Decoder errors discard parser/decoder exception chaining;
diagnostic consumers use the fixed code and never serialize tracebacks or their local variables.

Every request includes a protocol version, request ID, project ID, primary-session ID,
supervisor instance/generation and runtime digest. These are **untrusted claims**, including
the session and instance IDs. The future service must match them against independently
authenticated caller context and authoritative ownership state. Parsing or hashing them never
proves identity, admission, launch permission or cancellation authority. A peer UID, socket
path or caller-supplied role cannot supply that proof.

Canonical request bytes include all these fields and the complete target. Their digest is
intended for parameter-conflict detection under a separately authorized request-ID scope.
This increment does not implement request deduplication, a replay cache, transport framing,
slow-sender deadlines, caller enrollment, responses, cancellation or persistence. In particular,
accepting a future cancellation request must remain distinct from confirmed child cleanup.
Account admission/claims, owner signatures and result-body transfer need separate bounded
business-operation contracts; they cannot be passed through these control operations.

Before enabling privileged IPC, independent review must establish authenticated enrollment,
session/project/runtime binding, generation revocation and rejection of forged or stale callers.
Then the service implementation must prove bounded fair queues, reserved control capacity,
independent timers and split-store recovery without duplicate launches or a direct-SQL fallback.
The message limit is an initial control-contract bound, not a measured throughput or latency
claim. Real admission, installed Claude probing and the complete release checks remain required.

## Metadata transport increment

`service_transport.exchange_frame` now supplies the existing collector client with one monotonic
deadline across connect, send and all receives. Incoming fragments cannot renew that budget. The
client still defaults to 0.25 seconds, now for the whole exchange. The helper accepts explicit
positive budgets up to 60 seconds, without clamping; business operations must select their own
bounded budgets. This ceiling is not a measured service latency claim.

Metadata requests have a maximum frame size of 81,920 bytes and replies default to 16,384 bytes;
the delimiter counts toward each limit. Only one request is sent, and only the first response
frame is returned. The legacy collector also permits EOF after a complete JSON response. Its
decoder still validates the response and checks runtime compatibility. A partial response is
not success. The helper closes its socket and reports fixed transport codes without OS exception
chaining. Legacy collector error-code mapping and admission/binding operations are retained.

The transport never retries an ambiguous send, opens SQLite, starts another collector or falls
back to a direct writer. Losing a reply does not prove that the server failed to commit; future
project/account operations must reconcile scoped request IDs and immutable launch claims.
Bulk worker-result transfer needs its own bounded contract rather than increasing these metadata
limits or accepting an arbitrary caller-supplied path.

This helper does not authenticate peers or enroll a caller. No new privileged endpoint is enabled.
`CollectorClient` now lives in `collector_client`, with shared wire metadata and errors in
`collector_contracts`. These modules do not import the persistence owner or SQLite, including
when an exchange fails. The old persistence-module imports remain compatibility aliases.
The CLI and hook use the new client import, but still use persistence/state helpers for other
work: normal CLI ledger ownership has not yet migrated. Independent supervisors, bounded
server intake/queues, account coordination and cross-store migration/recovery remain required.

## Queue scheduling increment

`service_queue.ProjectFairQueue` is an internal in-memory metadata queue, with explicit
per-project quotas and a round-robin turn for each active project. One project's backlog
cannot consume another admitted project's quota. The active-project ceiling, quota product
and item size bound retained entries and payload bytes. Overload has fixed codes. A consumer
takes one item without waiting for work; the queue does no I/O and executes no callbacks.

The service must authenticate and validate requests before selecting a lane or using a project
label. Queues grant no caller authority, business idempotency, launch permission or durable
receipt. Separate instances are needed for reserved control/terminal/restriction capacity and
routine work. This helper does not start a service, choose installed capacity defaults, bound
in-flight workers, coalesce updates or make a blocked writer safe for local timers. Those
integration, measurement and recovery checks remain required; queued memory is lost on a crash.

## Local timer foundation

`liveness_clock.LivenessClock` makes monotonic decisions for one trusted supervisor
event loop without database, RPC or filesystem work. The owner supplies explicit
installed startup/quiet/stale/silence durations and the original absolute deadline;
there are no enabled provisional defaults. Assignment narrowing cannot widen any
duration or change that deadline. Narrowing and clock construction each capture a
validated copy, so later changes to a caller's policy object cannot extend a run.
Native event validation and run attribution must
finish before the owner invokes its startup or qualifying-activity callback.

The first startup starts the work-silence clock; repeats and polling never refresh
it. Activity strictly before cutoff can recover quiet/stale state. Each callback
first evaluates the earliest elapsed timer, with the hard deadline winning a tie.
Before a timer elapses, the first serialized cancellation or authoritative terminal
wins. No late event can replace the decision. Cancellation reports a stop request,
without claiming a cancelled result or confirmed process cleanup.

Snapshots contain fixed categories, bounded counts and millisecond ages/remainders.
They freeze at the stop/terminal observation; publication freshness and later cleanup
need separate reporting. No provider timestamp or event content enters this core.
Clock failure/regression latches unavailability so later reads cannot report healthy
activity. Ordinary clock exceptions yield a fixed code; nonlocal interrupts propagate
with the failure latch set. The owner must report monitoring unavailable and retain the detached hard
watchdog. Process/thread ownership prevents foreign callbacks from sampling or
mutating the clock; it is not external caller authentication.

This foundation does not activate a monitor, choose a Codex event allowlist, select
calibrated thresholds, enforce OS cleanup or implement Claude liveness. Real-client
cadence, independent status/cancel during dispatch, persistence-stall/flood tests,
native authority and both Codex-child paths remain required before the Claude phase.

## Project attempt monitor

`project_monitor.ProjectMonitor` now connects the local timer to a bounded internal
registry of exact `AttemptBinding` values. One project/primary/runtime/supervisor
generation owns each registry. Duplicate registration cannot rearm startup, work
silence or the original deadline; a changed claim, binding, duration or deadline
conflicts. Explicit capacity includes retained terminal entries, with no eviction,
automatic retry, account release or reuse of a run/attempt. Bindings and policy
values are captured rather than retaining caller-owned objects.

Trusted startup/activity/status/cancel calls use the complete immutable binding.
Wrong scope/process/thread refuses before clock sampling. Tick and pending-stop
reads perform no persistence, RPC, authorization or publication callback. A shared
sampling wrapper checks strict integer/range/monotonic values across all attempts.
Failure is sticky: undecided attempts yield unavailable metadata and request
`monitor_unavailable`, and new registrations refuse. Other entries still report
bounded metadata; existing frozen stop/terminal decisions retain their original
reason. Cached decisions use scalar tuples, so outward snapshot mutation cannot
alter them. The detached watchdog remains required.
Stop requests, actual authoritative terminal observations and confirmed tracked-tree
cleanup are separate facts; late terminal/cleanup observations retain the original
stop decision. Trusted terminal/cleanup facts are saved before the fallible clock
read, so nonlocal interrupts propagate without losing them. A nonlocal interrupt
can abort that tick; shared failure stays latched for subsequent observations.

Known cleanup has final `cleaned` health and a null liveness snapshot, without
asserting a completed result. Startup/activity/status/cancel and repeated cleanup
cannot sample or reactivate it. The first cleanup attempts one final timer poll;
the first actual terminal observation still classifies the original bounds after
cleanup when no prior decision exists. Cleanup and a null stop reason never prove
that a result passed its deadline or validators. `pending_stops` retains requested
stops until cleanup; `pending_cleanup` also includes normal authoritative terminal
observations without inventing a cancellation. Neither view releases account capacity.

This registry starts no event loop or native process and exposes no public endpoint
or caller credential. Trusted callers must establish native identity/authorization,
admission, event qualification and actual terminal/cleanup evidence before invoking
it. It cannot confirm OS cleanup itself. A disposable write-lock fixture exercises
local control/timers while a separate process holds a real SQLite write lock;
it is not the authenticated service integration, full C16, real-client cadence,
cross-store recovery or two-project release proof. Internal snapshot tuples are
bounded by explicit capacity; they are not external paginated API responses.

## Project history persistence owner

`project_history.ProjectHistory` now owns one project's SQLite connection and writer lock in
an existing protected directory. A descriptor walk rejects replaceable ancestry and aliases;
root/owner sticky temporary directories are allowed only with root/owner entries. These are
POSIX mode/ownership checks; the trusted launcher must establish effective protection including
ACLs and keep state outside admitted model write roots. Owner permissions do not
isolate arbitrary unsandboxed programs running as that same owner.
Its connection belongs to one process and thread. Different
projects use independent files and locks; a transaction in A does not take B's writer. The store
creates no account state, signing material or digest key and imports neither `LearningState`
nor `LearningLedger`. It cannot admit, launch or release account work.

Both the handler and store bind their original process and Python thread object, rather than a
reusable numeric thread ID. An inherited handler refuses before authorization or opening a store,
and foreign close calls refuse before the idempotent-close path or any descriptor cleanup. These
local resource-ownership checks do not authenticate an external client or prove native enrollment.

The internal methods accept already authorized work from a future project service. They are
not external request handlers: no listener, native enrollment or normal CLI/hook/bridge path
is enabled by this increment. A role, project label, `AttemptBinding` or receipt digest supplied
by a client cannot be trusted as authorization. The service must establish caller ownership,
the immutable account claim and any completion receipt before calling this persistence owner.

The initial supported records are run events, intents, outcomes and verification bindings.
New writes require an integer schema version; the legacy reader's bytes and equality semantics
remain unchanged. Unsupported record types are refused before their generic validators run.
Canonical bytes and content digests are retained unchanged, with project sequence, ingest time
and late-arrival metadata outside the record. A dispatch result can only be committed together
with its terminal outbox entry. The outbox retains the exact project/session/runtime/supervisor
generation/orchestration/run/attempt/claim binding and result digest. One run-handle/attempt or
claim digest cannot be rebound through a different claim ID. Stored binding/record corruption
returns a fixed incompatibility code. Duplicate commits return
the same delivery; changed content or binding conflicts. Pending metadata pages are bounded to
16 KiB of canonical JSON including newline. An acknowledged delivery retains its immutable
row and rejects a changed receipt. Acknowledging it cannot change central account capacity.
A valid unfenced completion retains its null forced-terminal state; the trusted service must
establish actual worker completion and cleanup separately. Parsing a record or its fence never
proves cleanup or authorizes account capacity release.
Stored BLOBs are checked without coercion, including replay paths: an SQLite integer is never
passed to `bytes()`. Binding bodies are at most 4 KiB, records retain their 64 KiB limit, and
the owner's SQLite connection caps complete row/value lengths at 128 KiB. Oversized corrupt
rows fail with a fixed unavailable code before delivery; invalid stored types fail closed.

Tests exercise the actual persistence methods in separate processes, abrupt process exit before
and after commit, reopen/retry, terminal atomicity and independent B writes during an A write
transaction. The process harness supplies fixture input directly; it does not establish native
caller authentication, a production service API, power-loss behavior or full C1-C19 acceptance.
This schema has no legacy importer yet. Exact source sequences, expiry, links, signed bytes,
owner/account facts and cross-store authority ordering still need the drained migration and
account-service implementation. Local control/timers must run independently of this writer.

## Existing account transaction units

The existing shared ledger now uses explicit account write transactions for host-run open,
dispatch intent and dispatch result. A late error rolls back the complete unit, including
binding/provenance, admission, records and lineage. These units refuse an outer transaction.
Initial admission-denial recording also owns an explicit transaction and raises its denial
only after that transaction commits. A failed denial append leaves no partial denial record.
The lineage assertion uses a real outcome-to-host-event link inside the internal account unit;
ordinary open/intent/result fixtures do not generate new lineage themselves.
Open refuses an outer transaction before starting admission preparation. A non-claiming
intent preflight performs no write reservation. The existing shared-account admission and
single-dispatch checks still own the decisions; this adds no launch or caller authority.
Fault/reopen assertions and real SIGKILL cuts exercise the open commit boundary. They do not
establish the distributed central/local claim protocol or an atomic SQL-to-OS launch.

The fixed database/WAL mode/path checks run before account-unit commit. A failure there rolls
back open, intent or result writes; it does not lose an already committed handle. A crash or
lost reply after the real commit can still leave complete durable state unknown to the caller.
The separate distributed claim/idempotency protocol must reconcile that ambiguity.

Host-run open and informational admission now prepare owner-signature checks outside the
account lock and write transaction. Their owner-local immutable preparation pins the exact
record bytes, digest, source, sequence, signature bytes, signer-file bytes and monotonic owner
history sequence. The owner rechecks these inputs before using the verified attestations;
changed inputs, another owner instance or a reopened owner invalidate the preparation.
An identical key value under a newer history sequence cannot reuse old verification. Current
account restrictions, billing/headroom, cap/bootstrap history and decision time are still read
fresh at the final admission check; preparation does not cache a passing admission.

The preparation is never a JSON/native caller credential. No production account endpoint or
normal-client migration is enabled. Other legacy probe/launch helpers still need the
same ownership audit and conversion. Signature-job intake/count/deadline limits and immutable
policy/runtime generation snapshots remain service work: this library refactor does not make
unbounded historical input a bounded API. A daemon still needs verified actual launch-caller
facts instead of its own environment. Independent local timers/control cannot wait here.

Collector session binding now prepares signatures before its writer and commits session,
root provenance and its first passing admission together. A denied root still records events
without gaining result admission. An identical computed root-run ID is decided once across
hook/app-server producers, including concurrent first binds; another producer cannot upgrade a
denied root or spend another account-window place. Existing provenance without a pass remains
unadmitted. Known complete producer/session bindings take no writer reservation or signature
work. Missing producer metadata is added atomically without revisiting admission, while
inconsistent immutable provenance on any producer is refused before returning or adding either
binding. A bounded root query also refuses unknown/extra producer rows. Project activity is
checked before writes.
These are persistence-owner rules, not new proof that a native client is authorized.

## Closed history API increment

`history_api` provides a pure codec and non-retrying client; `history_service` provides the
connection-owning handler. Eight closed operations record events, intents and outcomes, bind a
verifier, commit or acknowledge a terminal delivery, read one record and page pending deliveries.
No request may select SQL, a method, database path, PID, producer, role or authorization flag.
Project/runtime labels and parsed `AttemptBinding` values remain claims rather than authority.

The handler's default authorization adapter denies before opening a store. A configured trusted
adapter must independently verify native caller identity, current session/generation, permitted
operation and exact record/claim/receipt semantics. Its server-only output binds the complete
canonical request digest and operation set. The handler rechecks that digest after authorization,
pins project/runtime, and limits hook/app-server sources to event recording. This adapter seam
does not establish native enrollment: no production adapter, listener, protected launcher or
normal CLI/hook/bridge migration is enabled by this increment.
Every decoded-request reply also binds that complete request digest, including acknowledgement
receipt/content parameters; a reply for different parameters cannot confirm the new request.
Ordinary terminal commits and acknowledgements require the binding runtime to match the
pinned request runtime; an ACK checks the persisted binding inside its transaction, even on retry.
Pending history may retain older runtimes unchanged; cross-runtime terminal recovery requires
its separately reviewed recovery protocol. Malformed/denied replies disclose no configured
labels: they return null labels or only the validated labels supplied by that caller.

Mutation request IDs are immutable business identities: the record ID for record operations and
the claim ID for terminal/acknowledgement operations, scoped by authorized project/operation.
Existing durable canonical-content/source and claim/receipt conflicts supply replay protection;
there is no transient request cache to evict. Exact retries retain first-ingest metadata or the
same delivery. Changed data conflicts. This API deletes no rows or identities; retention and
generation revocation must be defined before deleting replay state. Read IDs only correlate fresh
views and do not promise a retained snapshot. Cancellation needs its separate durable protocol.

The whole request frame is at most 80 KiB including newline. Records travel as nested JSON objects
with the five supported history types and exact integer schema versions on requests and replies.
This new wire contract does not change the shared legacy reader or rewrite its historical bytes;
legacy import and its transfer capabilities remain a separate migration gate. Records retain
their exact canonical bytes, rather than expanding through double escaping/base64.
Replies are at most 16 KiB including the wire envelope; pending pages shrink to fit that actual
encoding. An explicit single-record read may use the 80 KiB metadata-transfer ceiling. These
limits do not enable up-to-1MiB worker-result transfer or LIVE4 full-report retrieval.
Terminal receipts and pending entries include the persisted closed dispatch-result metadata,
so its validator reason, fence, terminal state and client observation codes survive delivery.
Their digest is checked against the immutable delivery; no rejected model body is returned.

The client has one deadline per exchange and never starts a service, opens SQLite, stops another
writer or falls back to persistence. A lost or invalid reply may follow a commit: it is an
ambiguous delivery, and retry is explicit with the same immutable business identity. Recording a
receipt or result does not prove child completion/cleanup or release shared account capacity.

Tests exercise the actual client/handler/owner in disposable writer processes: B commits through
its API while A holds a write transaction, lost replies keep first-ingest metadata on explicit
retry, and terminal acknowledgements survive restart. Additional checks cover default denial,
closed schemas, full-request binding, source/operation/thread separation, paging and a client
import guard that blocks SQLite and persistence owners even when the endpoint is unavailable.
Fixture context/channels are explicitly synthetic; they do not prove native authentication,
production intake fairness, independent local timers, account coordination or complete C1-C19.

## Closed account read API increment

`account_api` contains a pure codec and client for three closed read operations. `account_service`
receives one already opened global `LearningLedger`; it creates no per-project key, ledger or
connection and does not start or migrate persistence on request. Its project view returns only
the authorized active project's ID, status and creation time. Its owner-key view returns the
public fingerprint, history sequence, replacement count/time and signer-file consistency state
through that same owner's connection. A mismatch stays visible without changing owner history.
With empty history, only a missing or safely checked empty signer file is `not_configured`;
malformed, unsafe or present-but-unrecorded signer material reports `mismatch`.
Signer reads check regular-file type before opening, open nonblocking, and recheck descriptor
type, owner, mode, link count and inode before a bounded read. A FIFO or replacement between
inspection and open cannot stall this view. First-owner enrollment uses the same checked read.
No key bytes, signatures, roots, paths, SQL or arbitrary methods are returned or selected by JSON.

Authorization defaults to denial before backend work. The server-only adapter output must bind
the full request digest, project, runtime and permitted operations; the handler rechecks it after
the callback. Hook/app-server sources are limited to health. Health reports transport response
only, explicitly `database_checked=false`, and avoids the persistence backend and its writer lock.
SQL views require the process/thread that opened the connection, stamped on the ledger rather
than inferred when a wrapper is constructed. Thread checks use object identity so a subclass's
equality method cannot make a foreign thread an owner. A newly constructed view cannot adopt another
thread's or an inherited ledger. Views check this origin before each owner lock; forked handlers
refuse before acquiring inherited locks. Closing a handler leaves the shared persistence owner
open. The origin stamp does not change legacy collector thread handoff behavior.

Both request and reply frames are at most 16 KiB including newline, with exact integer protocol
versions, closed fields and operation-specific result types. Responses bind the full request;
malformed requests return null labels and denials echo only validated caller labels. Error codes
are fixed and omit exception text. The client makes one exchange under one deadline, never
imports SQLite, starts a service or automatically retries. Read IDs correlate fresh views; they
do not preserve snapshots. EOF with empty or truncated JSON is an invalid response. Another
explicit read with the same ID is still a new view.

Disposable socket tests read two projects through one account owner, restart it without changing
the global key, and drop/truncate replies to check explicit retry. Separate tests check default
denial, scope, full-response binding, redaction, thread/fork guards and health while a backend is
blocked. These fixture adapters are synthetic. No production listener, native caller enrollment,
endpoint identity, mutation/launch-claim API or normal-client migration is enabled or proven by
this increment. Those gates and the complete concurrency/release criteria remain mandatory.

## Source ownership audit

The repository-only audit reads Python syntax without importing the inspected modules or
opening learning state:

```sh
python3 -B packages/codex-agents/scripts/audit_service_ownership.py inventory
python3 -B packages/codex-agents/scripts/audit_service_ownership.py check
```

`inventory` returns a source-tree digest and explicitly defined counts, plus conformance for the
listed client roots. `check` exits 1 when those roots have forbidden dependencies or
unresolved static evidence; input/read/size errors exit 2. Current CLI, bridge and hook
persistence dependencies remain findings that must be removed during migration. There is
no baseline exception that makes those paths pass.

The default JSON is compact. `--full` adds per-module hashes and bounded import/call-site metadata;
it includes no source text or call arguments and fails rather than truncating oversized
output. Call counts are AST sites, not regex line counts or execution counts. The report
labels module-wide alias attribution and unknown object/external-package flows explicitly.
Traversal opens the selected root and its children through directory descriptors without
following symlinks; replacing an ancestor path cannot redirect subsequent file reads.
Directory entries and depth, aggregate source bytes before parsing, each parsed AST and
import/call records are bounded incrementally. A single parser allocation is bounded by
the per-file byte limit; this is not an operating-system memory quota.

`--client-root` selects explicit source-relative Python paths, for example the pure
account/history/collector clients. A pass covers those listed roots only. The check
includes relative and transitive imports, package initializers, recognized dynamic loaders,
known SQLite/persistence modules and conservative SQL-like calls. It is static evidence
alongside the complete client census, runtime integration and authority review; it does
not establish native authentication, an OS boundary or complete C19 acceptance.
