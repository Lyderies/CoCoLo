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
That pure request module alone does not implement deduplication, a replay cache, transport
framing, slow-sender deadlines, caller enrollment, responses, cancellation or persistence.
The later closed control increment below adds local observations and requested stops;
accepting cancellation remains distinct from confirmed child cleanup.
Account admission/claims, owner signatures and result-body transfer need separate bounded
business-operation contracts; they cannot be passed through these control operations.

Before enabling privileged IPC, independent review must establish authenticated enrollment,
session/project/runtime binding, generation revocation and rejection of forged or stale callers.
Then the service implementation must prove bounded fair queues, reserved control capacity,
independent timers and split-store recovery without duplicate launches or a direct-SQL fallback.
The message limit is an initial control-contract bound, not a measured throughput or latency
claim. Real admission, installed Claude probing and the complete release checks remain required.

## Completed dispatch report delivery

The compatibility CLI now preserves a report already returned by the bridge when
the subsequent collector resume raises `CollectorRestartError`. It emits one JSON
object with the exact `result` and `bridge_report`, `ready: false`,
`dispatch_completed: true`, and `post_operation_error` containing fixed
`reason_code: collector_restart_failed` and `recovery_required: true`. The failure
exit code remains 2 and exception details are not added. Dispatch completion here
means the host operation returned its bound report; child execution/verdict,
admission and actual cleanup retain their own facts.

The owning primary saves that returned data, restores learning through supported
`learning-status`, and records the unchanged result with only the actual nonnull
forced-terminal fact. Validation, ownership or dispatch errors before a report
exists keep their error path. The wrapper never repairs output, drops a fence or
relaunches a child. Normal successful responses retain their existing shape.

This protects available in-process delivery. It does not provide durable retry
after a missing response or process crash. The existing dispatch binding stores a
keyed digest; validated worker result files are created only at the later host
commit and permit up to 1 MiB. Complete result retention before response, exact
request reconciliation and bounded bulk retrieval remain service/outbox work;
the metadata frame ceiling must not simply be raised. No new endpoint or normal
client database migration is enabled by this compatibility correction.

## Durable validated-result storage

The internal project persistence owner now has a result-reservation and artifact
path. It remains disabled unless its trusted launcher explicitly supplies the
existing private digest key and a finite `ResultCapacity`: retained entry count,
reserved body bytes, pre-launch reservation window and committed-result retention.
The store does not create a key, mint authority, release an account claim or enable
a listener. Normal CLI/hook/bridge clients still need the authenticated API migration.

Each reservation captures the complete `AttemptBinding`, keyed canonical assignment
digest and actual original hard deadline. It charges the full supported 1 MiB body
size before launch, including interrupted writes. The deadline must fit strictly
inside the explicit reservation window. Exact retries retain the original times;
changed assignment, deadline, claim or other binding fields conflict. Reserving
storage must precede consuming launch authority; that ordering and native caller
proof remain mandatory integration gates. A storage reservation is not admission.
Assignment attempts require exact integers; a boolean cannot compare equal to an
integer attempt and enter the reservation or shared assignment-validation path.

`commit_result` checks canonical, bounded result bytes against the original assignment
and exact dispatch-result digest/fence metadata. Rejected bytes are never written.
It first binds the immutable prepared request, writes a generated private artifact
with exclusive/nofollow creation and file/directory synchronization, then registers
its reference, the terminal record and delivery outbox in one SQLite transaction.
Result and assignment text stay outside SQLite. A metadata-only terminal commit
checks all reserved unique identities (claim ID, run-handle/attempt and claim digest)
inside its transaction. An alternate claim or a target matching several reservations
cannot bypass an outstanding body reservation or consume its sequence. Exact committed
metadata replay remains available after body retirement. Valid legacy, negative-review and
host-fenced result bodies retain their exact bytes and separate verdict/terminal facts.
The service must independently prove the actual bridge facts and original assignment;
validation does not supply that authority.

A crash before file publication leaves a charged reservation and bounded partial
file; exact retry can finish the same prepared request. A published file before the
database commit remains inaccessible through result reads until its exact reference
and outbox commit. A crash after commit returns the existing object on retry. The
two fixed names of an interrupted hard-link publication may be reconciled only when
they identify the same private inode with exactly those two links; unrelated aliases
are refused. No retry launches model work or replaces a committed result.

Body retention starts with the durable prepared request before the first possible
artifact write, separately from the pre-launch reservation window, and never
exceeds seven days. A failed metadata commit, later retry or policy change cannot
refresh it. The trusted lifecycle owner must also retire bodies when their actual
orchestration closes. Retirement is persisted before deletion; failed deletion keeps
the registration and capacity charged. Only confirmed deletion releases body space.
Replay tombstones remain and count against the explicit retained-entry cap; reaching
that cap refuses new storage work. No automatic tombstone eviction or identifier
reuse is implemented. Future drained generation retirement remains a migration gate.
The future persistence-owner event loop must schedule bounded expiry sweeps and
restart cleanup; this internal store does not start a background collector or timer.
An owner-local expiry cursor gives each due reservation a turn, including across
one-row batches. Failed or corrupt rows stay charged and report a fixed failure,
while other selected rows can finish cleanup. Candidate selection reads only
bounded SQLite row positions before fetching each reservation, so one oversized
row does not abort the entire batch. The cursor wraps and retries failures; it is
scheduling state, grants no authority and is not a durable cleanup receipt.

Reads recheck the exact binding, retention, private file ownership/type/mode/link
count, byte count and keyed digest. Trusted time is checked again after artifact I/O;
the last observed time is durable so a backward clock cannot resurrect expired data.
Missing, corrupt, expired or mismatched data stay unavailable. No previous lost
report is reconstructed, and a full legacy `bridge_report` is not synthesized from
the stored result and closed terminal metadata.

Project-history schema 2 adds only fixed result metadata tables. Upgrade requires
the exact schema-1 DDL, columns, application ID and project identity before any
migration write. Normal opens refuse schema 1 without changing it, even when a
result key/policy is configured. Only a trusted persistence-owner upgrade path may
set the exact-boolean `allow_schema_upgrade` option after its separately proven
authorization and drain; no wire request selects this option. The flag itself is
not proof of a drain or native authority. The table additions and version advance
are one transaction.
Unknown schemas/triggers are refused; prior record bytes, counters and outbox
identities remain unchanged. This is not the legacy shared-ledger importer or a
production migration approval.

Disposable tests include real writer-process exit before/after publication and
terminal commit, death during the schema upgrade, exact 1 MiB bodies, retry and
deletion failures. These are local persistence proofs, not power-loss/native-auth
proof, normal-client migration or full C7/C8/C19 acceptance. Bounded bulk transport,
authenticated original-assignment lookup, account/claim reconciliation and independent
review remain required before the new storage path is enabled.

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
If a later sample first fails during a tick, its returned batch is rebuilt through
the failure-aware path without further clock reads. Earlier undecided rows cannot
remain healthy or disappear from that call's pending-stop/cleanup views. Frozen
decisions and known cleanup retain their original facts. Each tick makes at most
two bounded passes; this is not a measured latency guarantee.
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

`account_api` contains a pure codec and client for four closed read operations. `account_service`
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
No private key bytes, signatures, roots, paths, SQL or arbitrary methods are returned or selected by JSON.

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

The additive `account.owner_public_key` read requires trusted adapter source `owner`
and the specific allowed operation; host, hook and app-server contexts are refused
before backend access. It returns the public SSH key with the existing fingerprint,
sequence and replacement metadata from one locked owner observation. A missing or
mismatched signer returns no public key. An `ok` response requires the full canonical
public encoding to match its fingerprint. The complete current 4 KiB signer-line
limit fits inside the existing 16 KiB response bound.

Pure `owner_key_contracts` supplies the shared encoding helpers without persistence
or signing dependencies. Legacy names remain reexported from `learning_store`, with
their existing identity fallback and RSA normalization. Existing status responses
and historical bytes retain their behavior. The new strict read returns a fixed
incompatibility error for an older noncanonical encoding instead of rewriting it.
This operation prepares the canonical key read needed by human attestation; the
normal attester still needs migration to an authenticated owner endpoint. Public
key data and its generation grant no native owner or signing authority, and no
signing purpose is added or changed.

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

## Bounded retained-result reads

`result_read_api` and `result_read_service` add the single closed operation
`result.read_chunk`. Each request names the complete `AttemptBinding`, the expected
owner-keyed result digest and an aligned offset. Its read identity includes the
digest and offset within that binding scope. Each reply binds the entire request
and includes the immutable body size, terminal record ID and retention deadline.
There are no producer, role, filename, SQL, publication or acknowledgement fields.
This is a read operation, with no durable transfer acceptance or snapshot receipt.

Chunks contain at most 8192 raw bytes, represented as canonical base64. Both wire
frames, including newline, are capped at 16 KiB; maximal valid identities and
integer values are covered by an envelope-size test. The existing 1 MiB body limit
therefore needs at most 128 exchanges. Whole reads share one explicit caller time
budget, reject changed metadata or malformed chunks and return no partial body on
failure. The client never automatically resends a request, starts an owner, reads
the digest key or falls back to persistence. An explicit new read can retry the
same retained body; it cannot repeat model work or refresh its retention.

The handler defaults to denial before opening a store. A trusted server adapter
must authorize the complete read and current owning primary, and must check again
after file I/O. The handler also rechecks the reservation after reply construction
and final authorization; observed retirement or expiry prevents release of cached
bytes. Every chunk reads through the existing owner integrity/retention checks;
there is no cross-request content cache. This verifies the complete bounded body
for each chunk, so a full-size read may check up to 128 MiB of file content. No
throughput or production scheduling guarantee is claimed by this implementation.

The client verifies framing, closed fields, complete response binding and stable
metadata. It cannot independently verify the private keyed digest without its
withheld key. It requires an already trusted endpoint; neither JSON labels, a
socket path nor the authorization dataclass authenticate an endpoint or caller.
No production listener or native enrollment adapter is enabled here. Publication,
original execution-output custody, control/terminal queue isolation, native caller
authority and migration of the legacy normal clients remain separate required work.

Disposable fixtures cover full-size delivery, unchanged negative/fenced bodies,
actual Unix-socket lost replies and owner restart, scope forgery, post-I/O
revocation, expiry/retirement between chunks, corrupt artifacts, strict response
types and process/thread affinity. An import guard and the source ownership audit
cover this client without authorizing persistence fallback or completing C19.

The shared `result_contracts.MAX_RESULT_BODY_BYTES` is also the unchanged 1 MiB
limit for bridge parsing and recordable worker results. The legacy persistence
module retains its `MAX_WORKER_RESULT_BYTES` name as an alias. Bridge result
validation now reads the pure contract directly; it no longer imports a
persistence owner merely to check the result size. A fresh-interpreter import
guard exercises valid, invalid and oversized results with SQLite and owner imports
blocked. Other bridge ledger/error imports and direct publication calls remain
in the ownership audit; this preparatory correction does not complete migration.

## Owner-side terminal construction

`ProjectHistory.publish_result` is an internal business operation for the future
trusted project service. It takes the already reserved complete binding, original
assignment, canonical result bytes and a `ResultObservation` captured by the
trusted executor. It computes the private keyed result digest and constructs the
existing v3 dispatch-result record inside the persistence owner. Callers supply
neither a digest key, result digest nor record ID. The record ID retains the
existing rule of hashing terminal metadata containing the owner-keyed body digest.

The observation contains the actual bridge run ID, original completion-observation
time, fence/review/terminal facts and closed safe diagnostics. Its types and the
existing shared dispatch-result validator are checked before publication; nested
values and the assignment are copied before file I/O. Observation time must fall
between the original reservation and the current trusted owner clock. It is never
regenerated on retry. These facts, original assignment and validated body feed the
existing immutable prepared commit before any private result file is written.
Changed facts/body conflict; exact retry after restart returns the same terminal
identity and preserves first-preparation retention. A new observation time after
a lost response cannot silently replace the original result.

Tests cover valid negative and host-fenced results, full 1 MiB input, key-free
caller parameters, strict closed observations, exact assignment/binding, nested
input mutation, no implicit reservation/key creation and failed metadata commit.
Owned subprocesses die before the file write, after file publication and after
the terminal transaction; exact retries reconcile all three without another
sequence or retention extension.

This adds no wire operation, listener, producer credential or caller authorization.
`ResultObservation` validates values; it does not prove that a caller observed the
execution. The eventual service still must prove registered executor/output
custody, original assignment and captured host facts. The protected completion
outbox/intake design, native authority, control/terminal queue isolation and legacy
normal-client migration remain required. Earlier failed/exhausted review gates
remain unchanged until a genuine combined integration review can cover the work.

## One owner for metadata and retained-result reads

`HistoryService` now composes its eight existing metadata operations with the
separately closed `result.read_chunk` handler. Both borrow the same lazy project
connection; one successful authorized request opens it once. Previously composing
two standalone handlers against the same project directory made the second one
fail with `history_writer_busy`. Both request orders reproduce that failure
before the correction and succeed through the composed owner afterward.

The metadata and result-read authorizers remain independent and default to denial.
Granting metadata operations does not grant result reading, and a read context
cannot append records or gain metadata authority from sharing a connection. The
bounded operation peek selects a codec only; the chosen handler validates and
authorizes its complete request before accessing the common backend. Result reads
retain their 16 KiB frame and post-I/O authorization/retention checks; the metadata
operation set and limits are unchanged. Unknown fields cannot select lifetime,
factory, connection or authorization policy.

The result reader's borrowed-connection lifetime is trusted constructor state.
Closing that facade leaves the owner open; closing the composed owner closes its
common connection once. Process/thread guards run before routing or backend work.
Disposable shared-socket tests mix metadata reads/appends and exact result reads,
lose a body reply, retry explicitly and restart the owner, asserting one factory
open per owner and unchanged original metadata. Separate tests exercise default
denial, context separation, factory faults and closing a misbound backend.

These fixtures use synthetic transport context, not native caller proof. There
is still no enabled native listener or publication wire operation. File I/O can
stall this persistence loop; independent live-control routing, reserved queues,
durable publication intake and normal-client migration remain required. Local
timers/cancellation must not be placed on this owner loop or await its work.

## Closed local control service

`control_api` and `control_service` add bounded status/cancel observations for the
separate `ProjectMonitor` owner. Both frames are at most 16 KiB including newline.
The existing complete scope and target are validated again. A deterministic
metadata-only request ID binds every canonical operation/scope/target field;
changed parameters cannot reuse that identity. Polling needs no growing receipt
cache. Exact cancellation retries preserve the monitor's first serialized stop
decision and original bounds; they cannot rearm or launch work.

The handler defaults to denial and requires a separately authenticated current
owning-primary context bound to the complete request digest, scope, operation and
target. The trusted adapter must use bounded already verified in-memory state;
native/signature verification belongs outside this owner loop. These injected
authorization values do not establish native proof. The monitor resolves the
target from its actual registered full binding. No arbitrary PID, path, activity,
limit, terminal or cleanup assertion is a supported client operation.

Attempt replies expose closed liveness metadata, stop reason, actual terminal and
cleanup observations, and a distinct cancellation state. Orchestration replies
contain bounded counts rather than an attempt list. The decoder rejects terminal
observations alongside live, unstopped liveness, and constrains summary stop/terminal
counts to compatible health categories. A later terminal preserves the first stop,
and cleanup may precede a terminal observation; both remain valid independently
observed facts. A requested stop never claims the tree is gone. Cleanup is confirmed
only after the trusted executor supplies
the actual tracked-tree observation. Local ages freeze at a stop/terminal decision;
publication carries its own monotonic timestamp. The client requires the same host
clock domain, rejects publication outside its request interval or total explicit
time budget, sends once and never starts an owner or falls back to persistence.

Replies explicitly say `durable=false`: they are live local observations, not
durable cancellation/outcome/account receipts. A lost/error cancel reply may follow
a latched stop; explicit retry queries that same target and decision. Handler
closure leaves the monitor and local safety work alive. Process restart needs the
separate authoritative supervisor-generation/recovery protocol; these values do
not reconstruct or reauthorize an old monitor. Publication-clock regression,
reentry or interruption latches unavailable replies without replacing prior stops.

Two disposable process fixtures stall a real result-publication callback and an
injected SQLite writer transaction together while A/B control runs in separate
owners. A's cancel is requested within the deterministic two-second bound; tracked
root/descendant cleanup is acknowledged afterward. B remains unaffected until its
own silence or hard-deadline decision. A trusted fixture-clock advance triggers
that decision and actual cleanup without a status request, while publication is
still stalled. Tests also cover stale/forged scopes, complete response binding,
unavailable clocks, target conflicts, lost replies, scope summaries, import and
process/thread boundaries. Fixtures use synthetic caller context and accelerated
clocks, not real provider admission or timing calibration.

There is no enabled native listener or normal CLI/hook/bridge migration here.
Production fair intake, reserved control/terminal queues, authenticated native
actors, executor/event adapters, watchdog integration, durable publication and
restart recovery remain required. Slow admission/authentication and untrusted
event floods still need end-to-end checks before the complete concurrency gate.

## Bounded control socket loop

`control_loop.ControlLoop` polls the monitor around fair, bounded batches of
nonblocking local socket I/O. The trusted owner supplies already connected Unix
stream sockets and an opaque context for the existing default-deny authorizer.
The loop creates no listener, path, native identity, process or persistence owner.
It does not authenticate that context. A reviewed native frontend must perform
slow verification separately and supply bounded, already verified in-memory state.

Limits are explicit: at most 256 connections, a configured number of peer steps
per cycle, one 16 KiB request/reply per connection, a fixed connection lifetime
of at most 60 seconds and a maximum select wait of at most one second. These are
configuration ceilings, not installed defaults or real-client latency calibration.
Round-robin scheduling does not let a ready peer consume another peer's step.
Partial senders/readers cannot block the owner; partial traffic never refreshes a
deadline. Trailing bytes received with the first frame are refused. No second
request is executed. Deadline expiry after a cancel has latched can discard its
reply without undoing the stop; delivery failure does not prove no effect.

Thread ownership uses object identity, including the constructor's service check.
Every public operation rejects reentry, including clock callbacks during attach
and context finalizers during close. An already registered descriptor alias is
refused before changing its mode. Failed attachment rolls back partial selector
registration and restores the caller's exact timeout; it does not take ownership.
The frontend must still transfer each socket exclusively: a descriptor number or
its uniqueness in this selector does not prove native identity or exclusive access.

Clock sampling shares the monitor's failure latch. A monitor-clock failure or
interruption closes connections, including when first detected by the final timer
poll. It preserves unavailable/previous-stop decisions and any propagating nonlocal
interrupt. The service's publication-clock latch is separate: it can return fixed
unavailable replies while monitor polling continues. Process/thread checks precede
operations and close. Closing the loop
leaves the service and monitor intact. The trusted outer owner must keep polling
and run its separate actual cleanup executor and watchdog.

Socketpair tests exercise partial input, kernel reply backpressure, fair progress,
deadline expiry, valid request pressure, malformed frames and ownership/clock
failures. They do not establish a native frontend, connection-admission fairness,
reserved native-event/terminal lanes, actual provider observation, cleanup or
full C5/C16. Independent review remains required before installation or activation.

## Detached watchdog process ownership

The bridge retains its exact detached watchdog process handle before waiting for
startup acknowledgement. Interrupted acknowledgement therefore leaves the handle
available for finalization. The original isolated command, birth binding, ready
handshake and absolute deadline remain in force. A failed acknowledgement still
aborts the child. The child process already exists during this handshake; the
bridge processes no child output before the backstop acknowledges.

After child-tree cleanup, finalization waits once for at most five seconds to reap
the already owned watchdog. It never signals the watchdog at this stage, so an
unfinished helper retains its independent deadline. A failed wait or unsuccessful
helper exit fences an otherwise successful run with `watchdog_unavailable`.
An earlier specific abort remains unchanged, and an interrupt still propagates.
Active-child bookkeeping and stream closure run before the final helper wait.
No reaper thread, shared registry, persistence or additional process is introduced.

Two real simple-child fixtures reproduce the prior process-handle warning on
normal completion and timeout. Separate checks cover acknowledgement interruption,
bounded reap failures, error precedence and unchanged absolute-deadline arguments.
These concern the detached watchdog handle; they do not establish the cause of all
subprocess warnings or change the detached collector's ownership. This preparation
does not complete native control, provider liveness or release acceptance.

## Owner-local bounded signature input capture

`AccountOwnerViews.capture_signature_inputs` captures unverified billing-signature
inputs through its existing account owner, connection and lock. It checks the
process/thread owner and active project, and refuses an outer transaction.
Explicit trusted limits bound the row count and copied field bytes. Their finite
ceilings of 4,096 rows and 64 MiB are configuration ceilings, not installed defaults
or measured operating limits.

Owner-history types, signer length and base field cost are checked before the
legacy owner helper fetches recorded signer bytes. Invalid stored types or an
oversized signer refuse without fetching that field. Integer projections use a
storage-type check, so malformed TEXT/BLOB values cannot cross as metadata.

The preliminary billing query returns only type, length and integer metadata for at most
the row limit plus one. It charges all of that project's billing attestations,
including other providers, before applying the legacy provider filter or copying
content/signature bodies. An extra row or excessive aggregate size refuses the
capture; nothing is silently truncated. Encoded metadata, content, signatures,
recorded/current signer bytes and fixed-width integers are counted. The original
provider, digest, type and sequence checks then construct the immutable inputs.
Fixtures inspect preliminary cursor values, maximal valid raw fields, independent
other-provider byte charges and post-copy underestimates as well as row limits.

The owner-local value has no verification result or admission status. Existing
signature preparation does not accept its type as proof. Reads remain fresh and
preserve raw historical signer bytes, including older encodings. Missing signer
files stay missing. No key/history mutation, crypto verification, second database
connection, new RPC operation, executor or normal-client migration is enabled.

These field limits bound returned/copied fields per capture. They do not bound
SQLite/Python heap use, wall time or retained job capacity. The legacy normal
preparation still needs migration. Indexed or otherwise bounded SQL work,
capacity reservation before capture, fair worker intake, fixed verifier custody,
fresh bounded commit rechecks and native authenticated endpoints remain required.
This increment does not complete C5, C18, C19 or release acceptance; earlier
failed/exhausted reviews and the failed planning run remain unchanged.

### Cooperative owner SQL work

Capture also requires explicit `SQLReadLimits`. The existing ledger connection is
constructed with `SQLReadConnection`; no second connection is created. Ordinary
legacy operations retain their existing serialized use. A bounded read call checks
the actual construction process/thread, refuses a transaction or nested scope, and
refuses an existing foreign progress callback without replacing it.

One fixed trusted owner function runs with query-only mode, no SQLite busy-handler
sleep, a narrowed existing value limit and a progress callback every 256 approximate
VM steps. A callback-count ceiling and monotonic deadline latch refusal. Clock
failure or regression refuses; no callback performs SQL or asynchronous interrupt.
Entry/exit checks also cover short reads that never invoke the progress callback.
The function and SQL are owner code, never caller-supplied RPC callbacks or SQL.

Budgeted statements return fetch-only views. Raw cursor creation and cursor
statement/batch/script execution are refused within the fixed read function.
Fetching and iteration check owner, active scope, clock and refusal latch before
touching SQLite; views cannot resume a completed scope. Ordinary legacy cursors
outside a read call retain their behavior. The fixed function must create fresh
reads through `connection.execute`; previously retained native cursors are not
adopted or claimed contained by this cooperative helper.

Scope-owned native cursors close before the callback is removed. Prior query-only mode,
built-in busy timeout and value limits are restored and checked. Restoration
failure makes the exact connection unusable and closes it. Backend failures are
converted outside exception handlers so fixed errors retain no raw cause/context.
An existing `SQLReadError` contributes only its validated reason to a fresh fixed
error. Restoration failure overrides an earlier fixed refusal with unavailable.
Capture also reports unavailable if a domain refusal coincides with a poisoned
connection. A read function that leaves a transaction open fails closed and its
connection is closed; a later operation cannot inherit that transaction.
The whole progress callback catches interruption faults and propagates a stored
`KeyboardInterrupt` after restoring the owner, rather than letting SQLite swallow
that callback exception.
Capture reports cooperative work, deadline, value-limit or lock refusal through
`signature_capture_work_limit`; no capture RPC is enabled.

This factory tracks the trusted Python owner's handler changes. It does not defend
against arbitrary native/base-class calls holding the same database capability or
restore an unknown native custom busy handler. Progress callbacks do not promise
an exact executed-instruction count, hard OS-I/O deadline, total heap bound or
installed service latency. Numeric ceilings are configuration limits, not measured
operating defaults. A billing-only index remains a private optimization candidate
requiring an explicit migration/drain contract; no index build is added to requests.
Reserved capacity, fair worker custody, independent health/control, bounded final
commit rechecks, native authentication, normal-client migration and release gates
remain required. This SQL preparation does not complete C3, C5, C18 or C19.

### Owner-local signature job intake

`SignatureJobIntake` reserves a full row, field-byte and job allocation before
calling the existing bounded capture. Explicit immutable limits bound active
projects, jobs per project, per-job fields and selected work. Products cannot
exceed 4,096 jobs, 8,192 reserved rows or 64 MiB of reserved fields. Each project
has its own configured share. These ceilings are not installed operating defaults
or exact process-heap accounting. No caller-supplied snapshot or callback is accepted.
One intake is bound to each existing ledger lifetime; duplicate wrappers over the
same ledger cannot create independent copies of that reservation budget. This
in-process binding is not native account identity or cross-restart recovery.

Expected encoding, storage and state failures retain fixed refusal codes without
backend exception context. Unexpected programming and resource faults propagate
after capture settings and unstarted intake reservations are cleaned up; they
are not reported as ordinary malformed inputs or unavailable storage.
Capture delegates unusable-connection and outer-transaction checks to the guarded
read boundary, including repeat calls after restoration has poisoned a connection.

Pending inputs use FIFO per project and round-robin project selection. Selection
skips a project already holding a selected slot without reordering its FIFO and
examines at most the active rotation. At most one job per project is selected.
Selection reserves a future execution slot; it does not launch or prove a worker.
No crypto, extra database, key/history write, client/RPC operation or listener is
enabled. Status contains bounded counts, and handles are in-process lookup keys
without native caller or project authority.

Cancellation before selection releases the owned inputs. A selected cancellation
retains all allocations until a separate trusted owner confirmation. A stopped
noncancelled job retains its inputs and row/byte reservation through final owner
consumption. The owner-only returned-input accessor supplies unverified data for
a future bounded final check; it keeps the reservation. Confirmation methods only
update metadata: the actual fixed adapter
must establish exact worker termination and drop its own input references first.
A handle, flag or dataclass is not termination, verification or admission proof.

This in-memory preparation is not durable global capacity authority. Creating a
fresh empty object does not recover orphan workers or old controller epochs.
Activation still requires durable claims/replay and conservative recovery, actual
fixed-worker custody, bounded fresh final checks, independent health/control,
native authentication and normal-client migration. Earlier failed/exhausted gates
and the failed planning result remain unchanged; C3, C5, C18, C19 and release
acceptance are not completed by these metadata transitions.

### Owner-record signing data

`owner_record_payload` calculates the existing canonical feedback, calibration and
billing signing bytes using metadata contracts alone. The existing
`OwnerGrantAuthority.owner_record_payload` entry point delegates to it, preserving
the purpose, exact project scope and validated record digest. The calculation can
be imported separately from the persistence and verification engine. Signature
verification, worker custody and fresh account admission remain separate requirements.

### Private signature-helper input

`signature_input_wire` provides a versioned binary input frame over captured,
unverified fields. Explicit immutable limits bound raw input, row count and each
field before copying. The frame preserves raw signer, content and signature bytes,
UTF-8 strings and null versus empty signatures. Its maximum wire size is the
retained-field budget plus 26 bytes, derived from the fixed header and field layout.
This bounds data and metadata counts rather than exact Python heap use.

The decoded value carries a correlation nonce and fields. Owner origins remain
in the parent; process/descriptor custody and fresh admission require separate
proof. This module performs no I/O and activates no worker or service operation.

### Private payload preparation response

`signature_payloads` consumes that input frame and calculates the existing owner
record signing bytes using metadata contracts alone. It preserves row positions
and the legacy omission of malformed, unsigned or nonowner rows. A missing or
incompatible current signer produces no payloads. No signature is verified.

The closed binary response binds its nonce, SHA256 of the exact input bytes and
row count. Entries have strictly increasing original row indices and bounded
canonical payloads scoped to the same project and owner-record purpose. Explicit
limits bound the entire response before decoding and each declared length before
copying. Its maximum is 64 bytes plus 262 bytes per configured input row, with a
256-byte payload ceiling. These are format limits; input/output copies, decoded
records and process heap still require separate reservation and accounting.

The returned batch is unverified data. A nonce, input digest, syntactically valid
payload or matching row position authenticates neither the worker nor its
calculation. Actual fixed process and descriptor custody, public signature
verification, durable claims/recovery and fresh bounded atomic account checks
remain required. No executor, API operation or admission path is activated.
