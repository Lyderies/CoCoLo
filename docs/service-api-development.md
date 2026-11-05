# Local service API implementation status

Release 0.3.0 requires normal clients to use project/account services, with SQLite owned only
by their persistence components. This boundary is being implemented. Existing CLI workflows
still use the installed 0.8.12 implementation; the source codec does not enable a service.

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
Ordinary terminal commits require the binding runtime to match the pinned request runtime.
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
