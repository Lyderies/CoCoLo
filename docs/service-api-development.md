# Local service API implementation status

Release 0.3.0 requires normal clients to use project/account services, with SQLite owned only
by their persistence components. This boundary is being implemented. Existing CLI workflows
still use the installed 0.8.12 implementation; the source codec does not enable a service.

## Project orchestration lifecycle (uninstalled source)

The closed project protocol has `open`, `pause`, `resume`, `checkpoint`, `close`
and `status` operations. Its request scope includes project, primary session,
runtime, supervisor instance and generation. A separate default-deny server
adapter must independently authorize that scope and the exact request digest.
The history service lends the same writer connection to the lifecycle handler;
it does not open a second project writer or run local timers on that thread.

Mutation replies distinguish the original committed `receipt` from `current`
state. Reusing the same scoped request identity with different parameters
conflicts. A known old active receipt is metadata and cannot reopen a terminal
orchestration. The client makes one bounded exchange and never automatically
retries a possibly committed request. Callers should retain an explicit request
ID for reconciliation after a lost reply.

Project schema5 adds only the lifecycle pointer, checkpoint and request journal
tables. Ordinary opens refuse older schemas; the explicit drained-owner upgrade
accepts exact recognized schemas1 through4. This path preserves old records,
outbox, reservations, retention windows and execution consumption. It does not
infer current ownership from legacy rows. Isolated old result fixtures explicitly
set `allow_legacy_orchestration_reference=True`; that setting is not a native
authorization adapter. Full migration, independent review and native activation
remain open. The installed0.8.12 runtime is unchanged.

Fresh reservations, arming, current storage evidence and execution consumption
check the actual active owned state. Historical receipts do not rearm work.
Close refuses reserved, uncertain or unadmitted results; a terminal lifecycle
blocks private result reads. Bounded `expire_results` verifies sealed closure
before retiring an unexpired body. Failed deletion remains charged and retryable;
the original retention window, terminal/outbox and known host receipt survive.
Closure and body deletion never release account execution capacity. Checkpoint
and request identities are sealed together, and a bounded chain check rejects
missing predecessors and a stale current pointer.

## Original project scope before account host open (uninstalled source)

`history.open_scope` resolves the original project-owned lifecycle and optional
worker dependency before the account service creates a run. The request contains
only the typed project/session/runtime/supervisor scope, orchestration handle and
digest of the prospective assignment, task, route and parent grant. The server
adapter supplies those full original inputs independently. No request field can
supply an assignment, result body, parent permission, admission or active flag.

The owner verifies its actual sealed active journal, the existing assignment
validator, original parent permissions, routed bounds and retry history. Only
read-only Codex bridge routes are supported at this stage; write-capable children
remain assigned to 0.4.0. A prospective verifier additionally needs the exact
retained worker result, original full inputs/client capability, consumed claim,
admission, host-open, terminal and outbox companions. Family/provider/role,
criteria and original outcome context must agree. Codex-of-Codex verification is
allowed by the existing policy. No central project-history lookup is used.

The bounded sealed reply includes safe scope/input digests, the original journal
checkpoint and optional worker metadata. It exposes no task, assignment or result
text and creates no run, lease, account record, clock or renewed retention window.
Current independent authorization is rechecked after body I/O and reply encoding;
pause, expiry, revocation, changed originals or missing companions refuse success.
The client exchanges once without SQLite or automatic retry. The account-side
byte verifier checks the MAC and independently expected scope/input digest; the
MAC does not establish native identity, freshness or permission to launch.

This is the project-to-account dependency handoff, not a completed account host
opener or activated endpoint. The actual account mutation, current native custody,
bounded admission preparation, normal-client migration and full recovery/live
validation remain required. Project8/account9 SQL layouts are unchanged. Tests use
synthetic enrollment and inert results, including a separate Unix-socket client
process whose imports reject SQLite.

## Original account run provenance (uninstalled source)

`account.run_provenance` reads the original registered claim, admission decision
and keyed host-start record. Its closed request identifies the registration,
registration digest and assignment ID. The project-only authorizer supplies the
independently expected bound storage scope and evidence. The producer creates no
registration, admission, execution or release and refreshes no original time or
retention window. Missing, conflicting or corrupt source rows refuse the read.

The account owner uses explicit trusted SQL read limits, a bounded two-candidate
uniqueness check and one read snapshot. There is no numeric production default;
an unconfigured operation is unavailable. Cooperative SQL limits do not establish
a whole-operation OS-I/O latency bound. The client exchanges once without retry
or SQLite fallback. Current authorization, full scope and service lifetime are
checked again before a reply; closure during an adapter cannot return evidence.

Managed project execution consumption now requires this receipt. The writer
checks its MAC and complete original grant, admitted provider, task family and
registered-claim identity. Protected `execution_provenance` metadata commits in
the same transaction as the original one-use consumption. A failed commit rolls
both back; a consumed grant cannot issue another ticket. Missing expected
companions refuse host-outcome admission rather than becoming unknown evidence.
Generic append cannot create this record. Only explicitly configured isolated
legacy-reference fixtures retain their earlier no-companion path.

The fixed persistence lane retains bounded immutable provenance bytes before
handoff and parses them on its writer. Its entry budget reserves the additional
bytes. The two-project inert fixture now exercises actual owned orchestration
state and the account receipt before one ticket/launch per project; cancellation,
terminal ACKs and retained account charges are unchanged. Enrollment and provider
execution remain synthetic. SQL layouts stay project5/account9; the new internal
record vocabulary, native peer authentication/current revocation, protected
publication and complete activation/legacy migration still require validation.
Neither the MAC nor a registered-assignment label proves a native PID or thread.

## Worker result persistence lane

### Client-observed usage travels with the actual return

The fixed Codex writer now preserves the selected packet's bounded client counts,
separately from the model's result-body `usage`. It takes the requested model from
the original prepared assignment and checks it against the selected client report.
No report/count parsing or database work runs on the control owner. Missing client
measurement creates the fixed `usage_unavailable` observation; it never becomes
zero or uses the model's own estimate.

`publish_result` accepts an internal `CodexUsageObservation`. This type confers no
caller, execution or billing authority. The same trusted-output custody and native
authentication gates still apply. The owner constructs an existing closed
`provider_usage` or `observation_unavailable` record, with a keyed full-attempt ID,
original observation time and provider source. Generic append cannot create it.
Its exact bytes join the prepared request digest before artifact I/O; the companion,
terminal record, outbox and result reference commit in one transaction. Changing,
adding or dropping usage on a retry conflicts. A lost response retries only the
original return; it cannot start another child or extend retention.

Project host admission now derives workflow totals from that durable companion,
using the existing Codex mapping. Inconsistent cached/reasoning subcounts stay in
the observed record but become unknown in workflow totals, as in the legacy
consumer. Actual fields retain `client_observed` provenance. Receipt schema2
binds the exact usage record/digest, and known receipts stay readable after body
retirement. A missing or altered expected companion fails the original prepared
digest check; it cannot silently downgrade to unknown. Safe companion reads use
the existing bounded history API, with no new measurement-write operation.

Existing requests without a client companion retain their original prepared
material and schema1 receipt bytes. They cannot gain measurements afterward.
SQL project4/account9 layouts are unchanged. Vocabulary/capability negotiation,
legacy migration and drained activation still need release validation. Actual
inert worker-to-writer tests and disposable account/project stores exercise
measurement versus model estimates, unavailability, atomic rollback, lost replies,
reopen, body retirement, corruption and retained capacity. These are synthetic
native enrollment/provider fixtures; no billing or containment proof is added.

### Project-owned host-result admission

`history.admit_host_result` now takes only an attempt binding and the exact durable
terminal ID/content/result digests. It accepts no result body, replacement
observation, repair receipt, configuration, acceptance flag or QA decision. The
server adapter must separately authenticate the current owning primary and supply
an independently expected full attempt binding, original assignment, task, route
and installed client capability. A different session/supervisor generation is
refused before opening storage. Labels,
dataclasses and shared-key MACs do not establish that authority; native enrollment
and current revocation remain activation gates.

The project persistence owner checks those inputs against its actual consumed
account execution grant, original reservation, outbox and closed terminal record.
It reads and verifies the original canonical result artifact, then derives the
existing diagnostic event/outcome with the existing builder. Completed work stays
unverified; fenced failures keep their rejection. The full fence,
review flag and bounded validator/client diagnostics remain in the terminal
reference. It rechecks authority after body I/O and checks retention before one
transaction stores the event, outcome and a protected `host_result_admission`
link record. This does not accept a result, issue a verifier or pass operational
QA. It releases no execution or shared capacity.

The bounded reply contains safe terminal metadata and exact event/outcome commit
IDs/digests. Raw task/result text stays out of records and replies. Known exact
receipts survive writer restart, body retirement and clock rollback without
reopening a private artifact or inventing a missing result. A first admission
after body expiry is unavailable. Changed inputs, scope, terminals, record links
or namespaces conflict; lost replies require an explicit exact retry. Generic
append cannot create the protected admission record. Project4/account9 SQL layouts
remain unchanged; only the typed internal record vocabulary grows.

Primary tests cover an actual Unix-socket client and writer-owned connection,
lost transport/commit replies, rollback, changed grant inputs, current-authority
revocation after I/O, expiry, corruption, diagnostic fencing and SQLite-free client
imports. Enrollment is synthetic. The normal CLI still consumes legacy shared
records: moving it and verifier/QA state to the project owner, retaining original
repair/evaluation semantics, remains required. No native endpoint is activated.

### Verified denial settlement

An armed lease stays charged until the original account outcome is known. A
timeout, cancellation, absent process or a caller's claim that it was never sent
cannot free it. `account.close_denied_attempt` now finalizes an exact durable
denial for an unlaunched Codex bridge attempt. It requires the original scoped
request/denial digest and lease identity, with separately authorized project
context and independently expected supervisor/actor scope. Native authentication,
current revocation and protected evidence publication remain activation gates.

Before a fresh closure, the account verifies the actual historical pending lease
and original denied receipt. One short transaction refuses any launch marker or
capacity row for that run, refuses native assignment/consumption, records a typed
permanent `claim_denial_settlement` and marks only that run abandoned with the fixed
reason `claim_denied`. The fence prevents later claim requests, including another
request identity, from dispatching it. A competing claim that wins first makes
closure unavailable and keeps the project charge. Role metadata alone is not an
execution identity; actual native assignment and consumed markers are checked.
Legacy intent/result scans use the existing cooperative SQL work/time/value
budget inside the transaction. Exhaustion returns busy and rolls back; row headers
bound settlement content before fetching it. Real OS-I/O/latency limits still need
the required installed validation.

The central settlement uses the existing lifecycle-record store, with keyed
binding/handle/request digests and the original safe denial receipt. It contains
no raw handles, private result or task text and has no retention expiry. An exact
known closure can be retrieved after account restart or clock rollback before
fresh project proof/signature work. Changed actor/lease/fence or a record-namespace
collision is a conflict; missing original receipts are not reconstructed.

Only after independently authenticating the original account reply may the project
writer verify its closed receipt/MAC and matching lease, confirm no local result
reservation, and transition that unbound pending lease to its existing `expired`
tombstone. This frees body charge while retaining logical identity quota, original
times and replay fencing. It neither invents cleanup nor releases an execution or
shared admission slot. An already bound/claimed or uncertain attempt stays charged.
Both SQL schemas remain unchanged; this is a new closed internal record type.

`attest_pending_result_lease` and the fixed lane's `pending_storage_evidence` return
actual historical pending metadata without asserting fresh launch timing. Fresh
admission still enforces the original storage window. The lane queues settlement
on its original writer, retains first reply bytes across explicit lost-response
retry, then lets its owner withdraw only the same never-bound prepared worker.
It charges an additional 16 KiB receipt buffer. No placeholder monitor claim,
execution ticket, new model request or automatic claim retry is created.

Tests exercise actual denied account requests, both claim/fence orderings,
account/local commit failure and lost replies/reopen, post-expiry uncertainty,
native/foreign misuse and fixed-writer settlement/withdrawal with no launch.
Orphan/missing-state and cross-generation recovery, tombstone lifecycle, native
publication/authentication, normal-client migration and all independent/installed
release gates remain required. These tests use synthetic enrollment and providers.

### Account acceptance of project terminal metadata

After local terminal/outbox commit, the project writer exports the actual consumed
attempt through `attest_terminal_delivery`. It rechecks the original account
registration, protected result reservation and stored outbox/record. The bounded
canonical sealed envelope contains only the original grant, exact delivery binding
and safe dispatch-result metadata. Export reads no private result artifact and
remains available after acknowledgement or body retirement.

`account.submit_terminal` accepts the original registration inputs and exact
registration/terminal/result digests. Its project-service authorization supplies
the independently expected original actor/supervisor scope, bound-storage proof
and protected terminal evidence. No request field supplies evidence, source,
cleanup flag, PID, executable or a model body. Default denial, native enrollment,
current permission/revocation and authenticated evidence publication still apply.

The account verifies bounded immutable evidence outside its transaction. It checks
the original scoped claim receipt and registered execution against authoritative
run, task/route/configuration, orchestration, lease and original clock/deadline
metadata. One short transaction inserts only the exact terminal ID/content digest
into the existing central completion table. Terminal records and private bodies
remain project-owned. An exact retry retrieves a stable keyed acceptance receipt;
a conflicting terminal cannot replace the first completion. Lost replies and an
account restart preserve this receipt without refreshing signatures or clocks.
Fresh writes refuse backwards or future observation time.

The receipt acknowledges terminal metadata. Central release fields remain null and
shared capacity stays held, including after confirmed local cleanup. A distinct
authenticated cleanup/release protocol remains required. Account acceptance does
not acknowledge a project outbox by itself: its owning writer verifies the closed
account reply, MAC and exact original grant/terminal binding before committing
`acknowledge_account_terminal`. Reopened stores reconcile the same receipt without
changing terminal bytes or retention. An invalid reply leaves the outbox pending.
The shared-key MAC provides binding/integrity; the service must independently
authenticate the account sender and its current epoch before that internal call.

`ResultPersistenceLane` captures terminal evidence after its actual publication
return. Its `acknowledge_terminal` queues the first immutable account reply for
writer validation; explicit retry without replacement retains those bytes after
an uncertain local response. Parsing, artifact reads and SQL remain off the control
owner. The lane charges two additional 16 KiB buffers per retained entry for
evidence and acknowledgement. Acknowledgement cannot retire an execution slot;
the original separate account-release requirement still governs retirement.

The two-process inert fixture now performs account acceptance and durable local
acknowledgement while A is OS-stopped, then reopens each project to check the
persisted outbox receipt. Both central claims remain held. Unit tests preserve
account/local commit failures, lost replies, exact restart/rollback reconciliation,
forged/changed proofs and receipts, caller/row binding, privacy and one-exchange
client behavior. These are synthetic enrollment and primary tests only.

Project schema4 and account schema9 remain unchanged. Native authentication and
evidence publication, separate cleanup release, orphan/denial recovery and normal
client migration remain mandatory before activation. In particular, normal-client
terminal reads must move to the owning project while preserving existing result,
fence, review and QA binding; the legacy central record consumer still needs that
migration. Both real provider phases and all independent/installed release gates
remain open.

### Project-local execution registration

The account owner can now durably register an original Codex launch claim for
execution by an independent project supervisor. `account.register_execution`
accepts only the original run handle, bridge claim ID/digest, claim request ID and
provider identity digest. Its independently authorized server context must be a
project service, bound to the original primary scope, with the exact expected
supervisor generation and protected bound-storage evidence. Other caller sources
are refused. Neither request JSON nor a sealed proof establishes native identity.

The account checks the actual claimed receipt, keyed task/route/assignment,
execution configuration, orchestration, lease and unchanged deadline against its
own rows. A short transaction records one registration without issuing an account
process ticket or opening project SQLite. The returned bounded registration
contains the original monotonic reference/deadline and wall-time window; it never
reconstructs a fresh timeout. An account restart can retrieve an existing exact
registration as metadata, but cannot mint a new one after losing the original
clock-owner association. Known metadata is not a new launch opportunity.

The project writer exports historical bound evidence through
`attest_bound_result_lease`; this asserts no current execution readiness. Claim
binding requires no placeholder monitor or invented monotonic deadline. Once the
actual registration supplies that deadline, the project monitor binds it and the
fixed writer checks the original immutable task/route/configuration before
`consume_execution_grant` commits one-use consumption. Exact retries after commit
return `history_execution_consumed`, including when the first reply was lost.
Missing or corrupt state cannot be repaired into fresh launch permission.

Only the actual writer's first successful return lets `ResultPersistenceLane`
issue one ticket from its own process-local registry. Issuing rechecks the original
prepared input and monitor deadline/cancellation; issuance uncertainty is latched
before entering the registry. Copied handles, decoded metadata and repeated calls
cannot issue custody. The default lane requires that same ticket at binding; the
explicit local reference option exists only for earlier inert-worker fixtures.
PID, owner-thread, Popen and ticket checks in the execution registry are unchanged.

Project-history schema 4 adds the fixed consumption table. Normal opens refuse
older schemas unchanged. The explicit drained-owner migration accepts only exact
recognized schemas 1, 2 or 3 and preserves their existing records, leases, counters,
artifacts and outbox. A table or MAC is not proof of native enrollment or a drain.

An inert integration test uses two actual project supervisor processes and one
account owner. It confirms OS suspension of A while B handles bounded socket
status/cancellation, confirms cleanup and commits its cancelled result locally.
Both projects issue custody in their own process, once, after actual account
claims/registrations and writer consumption. Central capacity stays held: local
terminal persistence does not invent authenticated account completion or release.

This remains uninstalled integration source. Native peer enrollment and current
revocation, pinned clock/boot/supervisor epochs, protected evidence publication,
completion/release transport, uncertain/orphan recovery, normal-client migration,
actual admitted Codex/Claude paths, containment and every independent/installed
release gate remain mandatory before activation. The two-process fixture proves
the tested isolation boundary with synthetic enrollment, not real-client cadence
or the complete concurrency acceptance criteria.

### Account enforcement of armed storage

`AccountService` and its deferred `AccountClaimJobs` now require protected armed
storage evidence for new eligible claims by default. Existing native caller,
subscription/billing, signature, policy, account capacity and unique-launch checks
still apply. Storage evidence only narrows admission; it grants no execution,
caller authentication or capacity release.

The trusted project writer can export its actual `claim_pending` lease through
`ProjectHistory.attest_result_lease`. Export rechecks stored state, keyed seals and
the original window; unused, bound and expired leases cannot produce this evidence.
The fixed project lane captures it after durable arming on its writer thread,
returns only an immutable byte reference, and drops that cached evidence after
actual claim binding. Each lane entry additionally reserves the full 16 KiB proof
budget. Account tickets retain at most one bounded proof and scope within their
configured per-project/global ticket quotas.

The account authorizer supplies a separately captured expected supervisor scope
and the sealed bytes as server-side context. No account request field selects a
proof, role, endpoint, key or reference mode. A native enrollment adapter must
independently derive that expected scope from the actual owning supervisor and
current permissions; taking it from the proof would remove the actor check.
Fetching/caching the protected writer's evidence must remain outside account
transactions and project control. The authenticated transport/enrollment for that
publication is still an activation gate; HMAC possession alone does not prove the
native requester or a protected runtime invocation.

Before capacity checks or launch-marker/intent/receipt consumption, the account
commit verifies a closed canonical proof of at most 16 KiB. It checks the keyed
state seal and original lease receipt, `claim_pending` state, complete expected
scope, exact canonical account request, project/runtime/run and original deadline.
The original keyed assignment digest and orchestration handle must also match the
authoritative account rows. The complete model timeout must fit strictly inside
the unchanged storage window. Assignment/orchestration columns participate in the
deferred capture comparison, so changes during signature work return stale rather
than consuming a different run. The account opens no project SQLite connection.

Missing, malformed/mismatched and expired/too-short storage produce fixed
`claim_storage_required`, `claim_storage_invalid` and `claim_storage_expired`
failures. They consume no launch claim, active slot or permanent claim receipt.
A known exact account receipt is retrieved before fresh storage/signature work,
including after lease expiry or clock rollback; it never registers or launches a
child again. Deferred jobs capture original immutable scope/evidence before work,
and cannot replace them through a duplicate with changed evidence.

The earlier unleased operation remains an explicitly named internal reference
setting for legacy-path fixtures. It requires an exact boolean in trusted Python
construction, must match the configured deferred owner, and is not selectable in
JSON. Before a new claim, supplied evidence is verified even in reference mode;
invalid evidence never falls back to an unleased claim. Known receipt retrieval
retains its earlier ordering before fresh evidence. Normal client migration uses the
required-storage configuration and bounded deferred owner. Existing direct-ledger
reference clients are not an authorized fallback when services fail.

Disposable tests use real project/account stores, actual fake-provider signatures
and deferred workers. They cover missing/forged seals, original receipt/window,
scope/request/assignment/orchestration mismatch, changed rows during verification,
known lost replies without project reads, bounded metadata and no claim/capacity
consumption on failure. Native provenance/revocation, independent supervisor
custody, real account/project transport, full latency/recovery/migration and all
installed/model/independent-review release gates remain mandatory. No endpoint or
installed runtime is enabled by this increment.

### Durable capacity before account claims

The project store now reserves body capacity without needing a future claim ID.
`reserve_result_lease` takes a closed `ResultLeaseBinding`: original supervisor
scope, orchestration/run/attempt and canonical account request ID/digest. It stores
only bounded metadata and a keyed assignment digest, with the full 1 MiB charged
before a result or account claim exists. Exact retries/reopens retain the original
creation and reservation expiry; neither policy changes nor binding refresh it.
The returned lease is storage metadata, not native caller or account authority.

Before the fixed frontend sends the account claim request, `arm_result_lease`
durably changes the lease to `claim_pending`. Pending claims stay charged across
expiry, disconnection and restart: a lost reply cannot establish that the request
was never consumed. Only unarmed unused leases can expire and free body capacity.
Their tombstones retain entry quota and cannot be reused. Bounded expiry sweeps
advance past corrupt rows, leave them charged and report a fixed failure, so one
bad row does not starve other selected leases. There is no caller-selected
"not sent", "not running" or cleanup flag that releases a pending claim.

After independently checking the actual account receipt and original request,
`bind_result_lease` binds the complete actual `AttemptBinding`, assignment and
original account deadline. That deadline must fit strictly inside the original
lease window. One transaction inserts the result reservation and binds the lease;
it transfers the existing charge, without double counting or new capacity. Exact
lost-reply/restart retries retrieve that binding. Changed claim, assignment, scope
or deadline conflicts. Account claim/release and native authority are not inferred
from these data values, a hash, or the presence of a row.

Leases share body and logical-entry limits with existing result reservations.
Private keyed seals protect mutable lease state and the actual claim binding.
Capacity checks validate at most the explicitly configured entry ceiling plus
one bounded row position before excluding bound/expired leases; malformed state
cannot free space. Neither assignments, results nor rejected content enter lease
SQLite metadata. The known-claim `reserve_result` reference and metadata-only
terminal commits cannot bypass an outstanding pre-claim lease.

The asynchronous project lane exposes `prepare_lease`, `arm` and `bind_claim` for
this sequence. It validates the supervisor scope without creating a placeholder
monitor claim. Input/result parsing and all storage stages stay on its original
writer; the control owner retains the immutable input reference across exact
storage retries. A newly prepared handle for a known pending/bound/expired lease
reports that existing state, rather than fresh readiness. A cancellation during
preparation stops the frontend before execution registration; existing monitor
refusal after a stop remains intact. The old `prepare` is a known-claim internal
reference and cannot provide the pre-claim ordering guarantee.

Project-history schema 3 added the fixed lease table/index; schema 4 also records
execution consumption as described above. Ordinary opens refuse older schemas
without migration writes. Only the explicit trusted drained-owner
upgrade option can migrate the exact recognized old schema, preserving signed
record bytes, counters, outbox and result artifacts in one transaction. Unknown
schema/trigger changes are refused. The option itself proves neither a drain nor
native authority; no request can select it.

Tests cover the actual fake-provider account handler minting its claim only after
lease reservation/arming, exact bind and lost replies, writer-process death mid-
bind and schema upgrade, mixed capacity limits, restart/expiry uncertainty, privacy
and inert-worker delivery. Native caller authentication, authenticated publication
of lease evidence to the account adapter, cross-owner execution custody, account-denial/
orphan reconciliation, scheduled lifecycle expiry/retirement and normal-client
migration remain separate mandatory integration/activation gates. No privileged
endpoint or installed runtime is enabled here.

### Fixed writer and result delivery

`result_persistence.ResultPersistenceLane` connects the registered bridge owner to
one prestarted project persistence thread. It opens and closes its `ProjectHistory`
on that thread. Assignment/result parsing, artifact I/O and SQL stay there; normal
control-owner methods use bounded metadata and immutable byte references, without
starting/joining threads, opening a store or waiting for a write. This is internal
composition; it enables no native listener, caller enrollment or normal client.

Trusted construction supplies the original `BridgeLivenessOwner`, fixed Codex pool,
history factory and explicit finite entry/retained-byte limits. Every retained entry
charges the maximum supported input, assignment and packet byte budgets; failed,
unknown and committed entries count too. These are retained serialized-byte bounds,
not a claim about the Python parser's peak allocation or measured live latency.

The known-claim reference `prepare` takes the pool's prepared, unlaunched handle, a complete
monitor binding and original wall deadline. It asynchronously validates the exact
immutable worker input and reserves result storage for its original assignment.
Repeated identical preparation retrieves the same local handle; changed scope or
deadline conflicts. Preparation failure cannot issue an execution ticket or start
model work. Service composition uses the lease sequence above to reserve before
consuming account launch authority. Account enforcement of that sequence remains
an integration gate.

After original execution registration, pool handoff and monitor binding, `bind`
checks the exact retained pool job, input reference and supervisor. `submit` then
takes the actual owner's once-selected return. It accepts no caller packet, result,
assignment, observation, record ID, key, timestamp or cleanup flag. Copied/foreign
handles and other threads are refused. Binding and returned-value snapshots avoid
mutable metadata aliases; the supported 1 MiB result body is passed by immutable
reference. Original owner stop/completion arbitration remains authoritative.

The writer uses existing `publish_result` to commit exact validated bytes, safe
fence/diagnostic facts, terminal metadata and the project outbox. The first selection
captures one observation time; explicit persistence retries retain it and the same
assignment/body. A lost commit reply can retrieve the same durable delivery without
another launch, changed fence, replacement result or refreshed retention. An ordinary
failure requires explicit persistence retry. An interrupted writer marks outstanding
jobs unknown and is never automatically replaced. Fixed failure codes contain no
exception strings, input, rejected answers or reasoning.

`delivery` returns copied durable metadata, not acceptance or a capacity release.
Only a committed job whose original execution is already released may retire its
local slot; the lane does not release account capacity. `close` stops new intake
without joining or interrupting an active write; already accepted writes drain on
the original writer. Orchestration closure/body retirement and expiry scheduling
still need lifecycle integration. Committed outbox records remain readable on a
subsequent history open; uncommitted in-memory selection is not reconstructed after
process loss.

Inert executable tests cover a blocked actual writer while another child's Unix
control cancellation and result intake progress, late polling after the deadline,
1 MiB body transfer, lost commit replies, exact retries, failed preparation, privacy,
unknown writer custody and nonblocking close. This does not prove native caller
authority, the account claim/terminal/release delivery sequence, complete crash
reconciliation, normal-client migration or real-model/two-project acceptance. Those
remain mandatory activation and release gates, with the existing review limits.

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

The earlier project-history schema 2 added only fixed result metadata tables. Upgrade requires
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

The default JSON summarizes the findings. `--full` adds per-module hashes and bounded
import/call-site metadata. Add `--compact` to render either report without indentation;
`check --full --compact` preserves every full-report field within the same 4 MiB limit,
including its final newline. This changes formatting only: conformance failures still
exit 1, and oversized output still fails with exit 2 before printing any partial report.
The report includes no source text or call arguments. Call counts are AST sites,
not regex line counts or execution counts. The report
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

## Bridge execution and independent local stop ownership

`OwnedExecutionRegistry.handoff` transfers one issued execution ticket to one
not-yet-started executor thread. The original issuing thread retains a registered
`ExecutionSupervisor` handle for bounded observations and stop requests. It loses
spawn/bind authority. Constructed/copied handles, foreign threads/processes and a
second transfer are refused. The actual raw run handle is retained in memory only;
the ledger's keyed handle binding and durable registration remain unchanged.

`bridge_liveness.BridgeLivenessOwner` connects those handles to the existing
`ProjectMonitor` and `ControlLoop`. Attachment compares project, run handle, run ID,
bridge claim and original absolute deadline against the complete registered monitor
binding. The owner applies latched timer/control stops before and after bounded
control I/O, including error/interruption paths. A failed clock latches monitoring
unavailable and stops other bound executions; a closed control loop can still
observe subsequent cleanup through this owner. Retained bindings are bounded by
the monitor's explicit attempt capacity.

The initial stop signals only the actual factory-created child's still-unreaped,
new-session process group. It needs no SQL, executor callback, process snapshot,
wait or join. Before the executor may reap that root, it revokes this signalling
eligibility under the registry lock. Repeated stops cannot signal a later reused
group. If cleanup entry fails, the bridge still performs its existing stop/wait
and preserves an original nonlocal interruption; it does not create a cleanup
credential. The tracked-tree executor and detached absolute watchdog remain.

Only the actual retained root wait and original pair's positive tracked-tree
cleanup advance terminal/cleanup observations. Stop acceptance alone never does.
Startup/work silence retains its fixed reason and fences as `timed_out`;
cancellation fences as `cancelled`, and monitor failure as `failed`. A pre-launch
or pre-bind stop remains conservatively held without fabricated completion.

Inert-child/socketpair tests exercise blocked frame callbacks and snapshots,
timer cutoffs, quiet/stale/recovery, two-child cancellation isolation, first-stop
precedence and cleanup faults. This internal composition is not enabled in normal
dispatch. Native enrollment, qualified/attributed provider-event intake, calibrated
timing, the Popen-internal creation gap, closed descendants, normal-client API
migration and real installed two-project acceptance remain required. These local
thread handles are not native authority or account-release credentials.

## Codex activity receipts and timer ordering

An attached bridge owner now enables a bounded activity buffer on its actual
registry-issued execution before the worker starts. Only that worker can publish
the fixed `startup` and `activity` categories. Only the original owner can drain
them. Publication and receipt-watermark sampling share the custody lock; the
monitor applies each bounded batch at its original monotonic receipt times, then
evaluates timers at that watermark. A later publication cannot predate that
observation. Delayed consumption cannot renew old work, and a valid queued
sequence cannot be cut off by polling today's time between its older records.
Existing stop decisions remain final. Overflow, clock rollback, malformed receipt
ordering and clock failure latch monitoring unavailable instead of dropping old
records to claim health. Overload, classifier failure and invalid receipts stop
only the affected attempt; an actual shared-clock failure still stops every
undecided attempt in that project. The detached absolute watchdog remains independent.

The buffer retains at most 256 category/timestamp pairs per execution. It stores
no event body, provider timestamp, message, command, reasoning or item identity.
The executor's classifier retains at most 4096 bounded item identities/stages in
memory for deduplication; these are never persisted or added to reports. These
limits are implementation ceilings, not measured real-client defaults.

For the opt-in Codex bridge path, the existing event/tool/model fences run first.
The first matching `thread.started` identifies the stream; the first subsequent
`turn.started` ends startup. Repeated startup does not count as work. Nonempty
reasoning/agent-message item stages and command start/completion count once per
increasing item stage. Repeated item stages, unchanged running-command updates,
usage, plan/file metadata and terminal assertions cannot keep a run alive. Model
rerouting or a reported failure disables further activity qualification. Without
per-update event sequence IDs, repeated updates are conservatively ignored rather
than claiming complete delta-level deduplication or progress proof.

The [official non-interactive documentation](https://learn.chatgpt.com/docs/non-interactive-mode)
describes JSONL thread/turn/item events and examples with item identities. It does
not establish this package's pinned 0.164.0 behavior, continuous reasoning cadence,
all path coverage or threshold calibration. Inert exec-protocol tests cover the
actual bridge reader, delayed receipts, a publication/timer race, invalid-tool
ordering, rerouting, ignored provider time and overload fencing. Two-child tests
also prove that one child's queue/classifier overload leaves the other running.
Real reviewed
client capability/cadence evidence remains required. Normal dispatch still does
not enable this composition, and native caller/run enrollment, closed containment,
service migration and all installed/release gates remain open.

## Fixed Codex worker return and owner result arbitration

`codex_worker.CodexWorkerPool` bootstraps fixed threads before control intake.
`prepare` reserves a slot and the full supported result budget before account
admission; it stores bounded immutable input bytes without parsing them. `bind`
then accepts the actual execution ticket for that prepared slot. Binding and
submission create no thread, parse no JSON, open no database and join no worker.
Submitting the same actual handle again cannot launch twice. Withdrawing an
unbound preparation frees local space only, never account capacity.

Each worker uses the existing Codex runner, validates its original task/route/
assignment/parent bounds, enforces the read-only and key-free contract, and shares
the legacy bridge's pure result-validation/fencing function. It retains at most
the supported 1 MiB result body plus 16 KiB of closed report metadata. Unknown
model text and invalid usage values do not enter metadata. Safe validator reasons
survive failed result delivery; rejected result text and input-bearing tracebacks
are not retained. Input, result and thread counts remain explicitly bounded;
these protocol reservations are not exact Python heap or latency guarantees.

Only the actual registered fixed loop publishes completion. Its reserved
category/timestamp slot and the owner's first-stop slot cannot be displaced by a full activity queue, and share
the original receipt-watermark ordering. A completion before a bound wins a later
owner poll/cancel; an earlier owner stop or elapsed bound wins over a late valid
result. Pool closure and handled process signals use that same ordered stop lane.
The signal guard uses the retained execution's pre-wait guard for worker-owned
children, so it cannot fall back to signalling a reaped root group. Fixed result parsing/validation remains part of this timed operation.
Physical child cleanup alone does not freeze that operation's timer; its terminal
and cleanup observations are published when fixed result preparation returns.

`BridgeLivenessOwner.result` checks the original binding, actual pool/handle and
completion custody, then selects immutable validated bytes or a small fixed stop
fence once. It parses no large result body and performs no persistence. A stopped
candidate's model body is discarded. Missing publication leaves unknown custody
and no deliverable success, marks that attempt's monitor unavailable immediately,
and discards the candidate body. Returned/unknown jobs remain charged until the
original released execution retires the slot; no timeout, result flag, copied
handle or fabricated packet releases it. Explicit body discard after durable
delivery/orchestration closure does not free uncertain execution capacity.

Inert executable tests cover completion/cancel/limit races, delayed observation,
slow result validation while control remains responsive, full-size result handoff,
safe rejection reasons, input bounds, slot charging and publication faults. These
are internal business-path proofs. Actual native caller/client/root custody,
authenticated persistence-lane handoff, cross-store result/outbox/release delivery,
orphan/drained recovery, normal CLI/hook/bridge API migration, both real Codex paths,
Claude and installed/release review remain required. No new listener or normal
dispatch path is enabled by constructing this pool or its delivery values.

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

### Fixed private process entry point

`signature_process` is an unused fixed helper entry point with preparation and
verification modes. The actual parent must establish the OS policy, exact owned
process, bounded input/output channels and deadline. Before its ready marker the
helper requires an actual fork refusal. That check and the marker are lifecycle
data; they authenticate no caller or complete confinement policy.

Preparation uses explicit input limits and audited metadata-only calculation.
Verification accepts only three fixed public-data files in a private owned working
directory, with no-follow opens, exact ownership/modes, single links and finite
sizes. It retains the checked descriptors, replaces stdin with the payload and
execs the fixed system SSH verifier in the same PID. Identity and namespace match
existing owner-record verification. No private key or nested SSH child is used.
Expected refusals and unexpected process faults exit without raw input or tracebacks.
The parent must supply only its intended standard streams, discard helper stderr
and keep exclusive scratch/file custody through reaping. Preopening retains file
identity; it does not freeze bytes against another writer.

No scheduler, service operation, native caller profile or installation invokes
this helper. Actual async ownership, prelaunch channel/memory reservation,
nonrenewable deadlines, cancellation, conservative orphan recovery and fresh
bounded atomic account checks remain integration requirements. The ready marker
and a process exit cannot replace those requirements.

### Owner-local admission evidence capture

`AccountOwnerViews.capture_admission_inputs` captures unverified raw evidence
from its already opened persistence owner. Explicit `AdmissionCaptureLimits`,
SQL work limits and a copied `AdmissionInputPolicy` are trusted configuration;
none is decoded from a service request or supplies installed-policy authority.
`AdmissionInputSnapshot` adds no normal account operation, listener, helper mode
or launch permission. The existing normal admission path is unchanged.
Ownership and well-formed missing/inactive-project failures retain `AccountError`
(`account_wrong_owner` or `account_project_not_found`). Capture validation,
quota and storage/work failures use `AdmissionCaptureError`, without a fallback.

One read transaction covers project status, owner history, record headers and
record values. Fixed status/type/length/integer metadata is read before owner
history and record values. Candidate rows and copied field bytes are charged
before fetching their raw content or signatures. Overflow refuses the capture;
it never silently drops restrictive evidence. Local identity and attestation
rows, global provider billing/headroom, and global run/probe admission history
are retained, including future observations and other providers/accounts.
Raw result bodies and unrelated event history are excluded. Project/source,
sequence, observation/ingestion times, exact content/digest and signature bytes
remain available for later decoding and family-specific ordering and citations.

The owner thread starts and ends its own read snapshot; an existing transaction
or foreign progress handler is refused. Settings and cursors are restored by the
cooperative SQL scope. Ambiguous transaction cleanup poisons/closes the owner
connection and returns no capture. A later database commit is visible to a new
capture, so a coherent earlier snapshot is not a fresh admission decision.

Current public-signer and filesystem observations precede the SQL scope. Their
existing size checks do not make file replacement atomic with database reads.
The row/field quotas and cooperative VM/deadline checks bound neither exact
Python/SQLite heap nor OS latency, global queued capacity or an installed service
response time. Conservatively charging every candidate history row can refuse
large histories; bounded projections/compaction and capacity reservation remain
integration work. Closed bounded decoding, owner-signature verification, exact
runtime/policy/generation freshness, atomic cap/bootstrap/claim commit checks,
durable recovery, native authority and normal-client migration remain required.

### Metadata-only admission calculations

`admission_metadata` contains the legacy admission, launch, headroom
and usage-window calculations, separately from provider observation/process code.
The existing `providers` entry points re-export these functions and constants.
Synthetic legacy vectors preserve decision order, restrictive and future evidence,
shared run caps, credit opt-in behavior, Claude bootstrap and project-local citations.

Reset-less restrictive observations remain live after a backward clock step until
the existing one-day age bound expires. An empty Claude unified-window map retains
an explicit rejection or warning under that same fallback bound. Known reset
times still expire normally; an elapsed explicit window does not restart the
fallback period. These are conservative restriction corrections, separately
covered from the unchanged synthetic legacy examples.
Exhausted observations also continue to protect the existing project-deletion
check until their fallback period expires.
Storage deduplication compares each reset-less observation's own expiry, including
aggregate statuses without windows. Conservative probe and overflow merges retain
an unscoped aggregate window with its original fallback expiry. The fallback is
inclusive at one day; its equivalent explicit reset is one millisecond later.
Still-eligible future-dated five-hour resets also participate in the earliest
shared cap window, so clock rollback cannot discard an earlier admission.
These offline corrections require independent acceptance before activation.
Installation and release gates remain open.

These functions calculate unsaved data over ordinary trusted Python values. They
verify no signatures, authenticate no caller and grant no launch permission. They
do not bound arbitrary caller objects. A future fixed helper must receive closed,
bounded decoded values; fresh atomic owner checks and all activation gates remain
mandatory. No service operation or helper mode is enabled by this extraction.

### Owner-local fixed worker pool

`SignatureWorkerPool` binds once to an actual owner-local `SignatureJobIntake`.
Explicit immutable limits configure up to 16 prestarted fixed worker threads,
one input and one completion mailbox per thread, one nonrenewable job deadline
and bounded helper I/O/cleanup waits. Bootstrap belongs before writer readiness,
outside independent cheap health/control: thread creation itself can block.
Normal owner methods select fairly, offer metadata, inspect, cancel and poll;
they create no thread/process, encode no large input, wait for no child and join
no live worker. The fixed workers receive data rather than a ledger or private key.

The pool reserves a complete protocol-copy share before offering work. With
captured field budget B and row budget r, that share is
`4*(B+27) + 4*(82+262*r) + 4*65536 + 20736` bytes. It covers counted concurrent
wire/decoded-field/response copies, bounded chunks and the three public scratch
files. It is not exact Python heap, JSON/Unicode object, allocator or kernel
accounting. Separate intake row/field quotas and process/mailbox/file caps apply.
The configured protocol ceiling is 512 MiB; existing intake products and 16 workers
bound the derived maximum share sum to 281,553,728 bytes. No operating defaults
or installed latency guarantees are selected by these configuration ceilings.

Fixed execution retains each actual Popen handle, checks kernel birth while
unreaped and reads its ready marker before input. Nonblocking channels enforce
finite bytes and one deadline across preparation and all SSH verifications.
After the established no-fork boundary, cleanup verifies the owned root's birth
and kills/waits for that process without process-table scans or group signals.
Returned input reservations, including cancelled results, remain through explicit
owner consumption/discard. Confirming actual cleanup and dropping worker-local
full data references precedes result publication; publication never releases a
cancelled reservation. A closing owner drains and discards known returned results.
Polls repeat unconsumed completion notices until explicit discard/cancellation,
so a later slot's clock fault cannot hide an earlier completion. A latched owner
clock failure suppresses positive results on subsequent reads and polls.

Missing readiness, ambiguous spawn, lost worker/completion or unconfirmed cleanup
retains its worker/slot, actual custody and all selected capacity. A late matching
token cannot clear that state, and no replacement worker or replay is automatic.
Other available project slots can continue. Closing does not acknowledge a drain
while an unknown job, returned input or queued intake remains.

Completion metadata must agree with the selected job: successful indices require
at least preparation plus one reaped verifier per successful index, and finish
time must fall between selection and a fresh owner sample. A completion remains
in its slot if that fresh clock sample fails; a later valid sample drains it only
as a clock failure. Ambiguous enqueue or result/intake publication faults fence
the slot and never replay confirmation. This is conservative in-memory fencing,
not transactional rollback or durable recovery across the two owner objects.
Cancellation cannot release a fenced slot's retained intake reservation.

The isolated regression fixtures inject cleanup, clock, completion, enqueue and
publication faults. Their teardown waits boundedly for every offered execution
before reaping all tracked actual roots and removing public scratch. Unknown
intake is never confirmed by fixture cleanup. A weak-reference check drops the
fixture's strong context reference before checking parked-worker custody.

This adapter is inactive in normal APIs/admission. Its experimental macOS helper
guard denies network, process creation and writes outside public scratch, while
retaining broad read access; it supplies no OS read-isolation claim. Linux and
hardware key formats remain unmeasured. Durable global claims/recovery, native
caller authority, fresh bounded atomic owner/policy/cap checks, normal-client
migration and combined integration/security/release review remain mandatory.

## Durable launch consumption in the candidate account owner

The account ledger uses schema 7. A separate `launch_claims` table records one
consumed launch per project/run and opaque handle. The existing validated
`dispatch_intent` and its consumption marker commit in the same explicit write
transaction. Preflight and claiming both refuse an existing marker. Event
retention, a clock rollback, a lost first response or a restart cannot recreate an
unused claim merely by removing its displayed intent. The marker contains only
bound identifiers, a fixed kind, timestamp and bridge identifier. It grants no
permission to execute, and does not change result consumption or agent binding.

The markers have no dependency on expiring records or bindings. Project backup
and isolated restoration verification include them; explicit project deletion
removes them. Schema 5 and 6 backups retain their exact earlier manifests. An
older runtime refuses schema 7, and a schema-7 store missing its consumption
table is refused instead of silently rebuilding an empty consumed set.

An upgrade from schema 6 requires a registered, unexpired, independently restored
backup matching every active project partition before migration writes. Schema 5
retains the same backup requirement for its own version; its lifecycle and launch
migration commit together or roll back to schema 5. Migration conservatively
fences every existing run, including pending unbound runs with no retained
intent. It preserves their records and signed statement bytes. Drained offline
upgrade, backup creation by the previous runtime and mixed-runtime retirement
remain installation gates; this candidate does not upgrade the installed owner.

This closes the legacy per-run consumption gap. It does not compose a fresh
admission check and claim into one operation or add scoped idempotent request
receipts. No account mutation RPC, native caller enrollment, listener or normal
CLI/hook/bridge API migration is enabled by this increment. Those operations,
bounded admission work, cross-store recovery and independent release review
remain required.


## Closed account claim operation in the uninstalled candidate

`account.claim_launch` accepts only a run handle and a provider-identity digest,
plus the existing request envelope. Its caller cannot choose a model, deadline,
role, SQL, executable, process or environment. The owner stores approved bridge
provider/model/effort/effective timeout/runtime when it opens the validated run.
A successful trusted before-launch observation binds its identity to that run;
starting another observation invalidates the previous binding even if it fails.

The reference owner operation verifies immutable signature inputs outside its
mutex. Its final explicit write transaction rechecks the open run, exact current
observation, admitted client, owner/signature inputs, account-wide restrictions
and clock. It commits either a fixed denial or the consumed marker and validated
intent together with the request receipt. The admission run cap is not charged
again. Scoped request identifiers, parameters and receipt integrity use owner-keyed
digests. An identical retry retrieves known durable metadata; changed parameters
conflict. A lost response, restart, event expiry or clock rollback cannot issue a
fresh claim. The client sends one exchange and never automatically retries or
starts a child. Neither the first nor a known receipt is executable permission.

Schema 8 adds the approved launch inputs, observation binding and permanent
receipts. A composite foreign key ties each claimed receipt to its exact consumed
marker/run/bridge identity. Restore refuses an orphan receipt, and deleting a
marker cannot leave its claimed receipt behind. Explicit project deletion removes
child receipts before markers. Current-schema opens check the expected table,
index and cleanup definitions; missing or weakened consumed-state schemas are
refused. Schema 5/6/7 backups retain their exact earlier manifests. Verified exact
backups precede atomic upgrades; existing claims survive and other old bindings
are conservatively fenced. Schemas 1-4 refuse automatic upgrade: a verified drained
staged migration remains required work before release.

The handler remains default-deny. Claim authorization requires a trusted adapter's
host operation scope and caller scope, separately from JSON. Fixture authorization
objects and callback contexts prove no actual native caller enrollment, fresh
caller environment or root custody. No listener, active profile, installation or
normal CLI/hook/bridge migration is enabled here. Signature preparation remains
synchronous in this reference call, and legacy JSON/filesystem/policy reads remain
inside the final lock. Bounded asynchronous admission-job integration, active-run
capacity/reconciliation, actual native authentication and the complete release
checks remain required before normal API activation.

### Account-store startup recovery

An initialized account store refuses a missing, empty or zero-version database.
Reopening never silently creates a replacement database or digest key. A durable
owner-only bootstrap marker permits retry of an interrupted first initialization;
the initialized marker takes precedence over any leftover bootstrap marker. The
marker is published by atomic rename only after its complete contents are fsynced,
then the directory is fsynced. Interrupted bootstrap scratch files stay untrusted.
The schema layout is created in one explicit transaction, including additive tables,
triggers and the version update. Interruption rolls it back before the next open.
Owner-key history initialization remains a separate, idempotent startup step;
this layout guarantee does not claim one transaction for all startup work.

Startup retains unregistered backup files as recovery material. They remain
unregistered and untrusted: the ordinary backup-verification API refuses them.
Recovery must explicitly verify and restore them; normal startup does not import
them. Orphan artifact and export cleanup continues. The standalone owner backup
tool preserves each supported schema's exact 5/6/7/8 manifest and checks a relational
restore, including the schema-8 receipt-parent uniqueness constraint. This does not
complete the staged legacy migration or cross-store release recovery gates.
Envelope hashing uses the owner's canonical ASCII escaping, including for Unicode
project paths, so the normal owner reader can verify these backups.

### Deferred signature-backed claim requests

`AccountService.begin` authorizes the same closed `account.claim_launch` request,
then returns either its immediate wire reply or a private owner-local ticket.
The ticket never crosses the client protocol and grants no execution authority.
A transport can retain its waiting connection outside the database request loop,
call `drive_claims`, and retrieve the original request-bound reply with
`take_claim_reply`. Health replies use the existing independent path. Configuring
deferred jobs makes synchronous claim handling refuse; `begin` also refuses a
claim when jobs are absent. Neither path falls back to synchronous verification.

`AccountClaimJobs` uses explicit startup limits and the existing prestarted fixed
signature pool. It reserves per-project ticket capacity before capture or writes.
Queued requests and completed, unconsumed replies count against the same quota.
The intake reserves row/byte capacity before reading inputs, rotates projects,
and keeps active or ambiguous worker custody. Request methods create no worker
thread or process and wait for no worker. Same scoped pending IDs share a ticket;
changed parameters conflict. After reply consumption, an explicit retry retrieves
the durable receipt without another signature job or launch claim.

Only an actually returned handle in this owner's registered pool can supply
verification indices. The owner retrieves its retained original inputs, then
rechecks current owner/signature rows, run, observation, account restrictions and
clock before the unchanged atomic intent/consumption/receipt commit. No request
accepts a caller's verification flags, indices, snapshot or callback. Cancellation
of active signature work stays pending until actual cleanup confirmation; this
does not claim cancellation of a model child. Commit/publication interruptions
fence the coordinator and retain reservations instead of replaying work.

Disposable tests use real fixed signature helpers, synthetic provider clients and
temporary owner keys. They exercise a second project's claim and health while the
first project's signature worker waits, invalid signatures, fresh foreign
restrictions, stale signed inputs, quotas, retries, cancellation and interrupted
commit/publication. They prove no real model execution or native enrollment.
Fault fencing permits owner-checked `request_close`, `drain` and metadata
inspection while permanently refusing new work. Draining never offers a job or
re-enters claim commit. The fixed pool cancels queued as well as active work,
releases only confirmed stopped inputs/completion metadata, and preserves unknown
custody. Coordinator ticket metadata is cleared only after actual pool closure;
durable receipts remain available for explicit retries after owner recovery.
The synchronous reference still uses legacy JSON and filesystem work, and the
helper's broad-read guard remains experimental. Deferred claim jobs use the
complete bounded preparation described below. Whole-owner latency evidence,
durable orphan reconciliation, active-run capacity,
actual native caller/fresh environment custody, normal-client migration and the
complete release gates remain required. No listener, profile or installed runtime
is enabled by this source integration.

### Bounded account socket ingress

`account_ingress.AccountIngress` separates accepted Unix-stream I/O from the
original account database owner. Trusted startup supplies exclusively transferred
connected sockets and bounded opaque authorization contexts. The component binds
no listener and discovers or enrolls no caller. The default-deny service remains
responsible for each complete request's authorization and digest binding. The
fixture listener and contexts establish no native identity or fresh environment.

Startup explicitly bootstraps one front thread after the fixed claim workers.
It handles nonblocking framing, reads, writes and absolute connection deadlines.
Fixed ordinary and reserved-health slot shares total at most 256. Each direction
is bounded to one 16 KiB frame including newline; partial traffic cannot extend
the at-most-60-second lifetime. Round-robin I/O steps, owner request batches and
the front poll interval are configured with hard ceilings. These are source
configuration bounds, not calibrated installed defaults. No request starts or
joins a thread, waits on socket I/O in the database owner, or opens another ledger.

The reserved health classification is a trusted startup input, never a request
field. That lane accepts only `account.health` and still requires authorization.
Health executes on the front thread and reports only transport availability with
`database_checked=false`. The trusted authorizer must be thread-safe and use
bounded, already verified in-memory state; slow native/signature enrollment
belongs elsewhere. Normal frames pass through bounded slots to `pump_owner` on
the original owner thread. Bounded raw capture/freshness reads and fixed filesystem
and policy observations still run there. This transport does not prove a complete
admission latency bound.

An attached socket holds its quota through framing and reply delivery. If it
disconnects after owner handoff, bounded metadata stays charged until the owner
acknowledges it. This prevents an unbounded disconnect-notice backlog during an
owner stall. Reserved health connections need no owner acknowledgement. Closed
malformed or extra frames execute no second request. EOF after handoff is treated
as disconnect; clients keep the connection open for their single response.

One ingress exclusively owns ticket delivery and coordinator shutdown. Binding
requires an empty, bootstrapped coordinator; other direct submit/take/cancel
consumers and replacement ingress instances are refused. The private issuer is
internal delivery custody and supplies no caller authority. Matching scoped IDs
share one ticket and fan out its exact bound reply. Dropping one subscriber leaves
the others intact. After the last subscriber leaves, only signature preparation
is cancelled, and detached ticket quota remains until the actual worker returns.
All published disconnect notices are reconciled before ordinary owner batches,
within the fixed connection ceiling. Each actual returned signature job checks
its bound ingress's subscriber liveness and deadlines again before committing.
A disconnect published before that decision suppresses the claim even when it
arrives during other owner work; completion can win a disconnect published later.
The metadata lock never spans the SQL commit. This is a delivery-liveness decision,
not native authorization, a worker-verification assertion or model cancellation.
Unknown worker custody is never released by a disconnect or timeout.
Once signature cancellation is latched, a new duplicate gets `claim_busy` while
that ticket remains reserved. It cannot join or restart cancelled preparation;
only an explicit retry after confirmed cleanup can request new work.
A committed claim and its receipt remain durable after lost delivery; explicit retry retrieves
known state without new signature work. These receipts grant no model execution.

Owner faults permanently stop ordinary admission while cheap health can remain
available. Front faults close I/O and latch unavailable.
All attachment lanes are fenced atomically before the front's final cleanup
snapshot, so a late reserved-health attachment cannot escape its cleanup.
Public owner-checked `request_close` and `drain` remain available after faults. Draining never offers or
commits a claim, joins no thread, and reports completion only after the actual
front thread and fixed pool stop. It leaves the service and SQL connection open.
Actual native enrollment/reserved-lane custody, fully bounded admission work,
active-account capacity and orphan reconciliation, complete C19 client migration,
independent review and all release gates remain required before activation.

### Complete deferred admission preparation

Configured claim jobs reserve their combined admission and signature row/field
share before capture. The original owner captures every candidate in the six
closed admission record families, including foreign-project/provider restrictions.
Headers and aggregate limits precede copying bodies; overflow refuses the job.
Local run-admission candidates are charged regardless of their source label.
A wrongly sourced local admission cannot disappear before cardinality/source
validation; malformed sources fail preparation instead of weakening the original
run-admission fence. Global account history retains its producer-source rules.
Local billing signature inputs are derived from that same immutable raw capture.
They are charged again conservatively against the combined share. Slot/project
ceilings still cover queued, active and returned data. Parsed metadata remains
with the same reserved job; field and protocol budgets are not exact Python heap
or operating-system memory quotas.

The prestarted fixed workers parse/validate all stored JSON and prepare ordered
billing/headroom vectors, local identity/admission indexes and reviewed-version
metadata. They also use the existing fixed signature execution. No worker opens
SQLite, and no request starts or joins a thread. The original owner accepts parsed
data only from its actual registered returned-worker registry, bound by identity
to the original retained admission capture and its exact derived signature rows.
Types, origin brands, parsed objects and completion flags supplied by a caller
remain insufficient. Neither parsing nor a valid signature authorizes a launch.

The final atomic transaction re-captures raw rows with bounded SELECT work and
compares every row, owner generation, public signer, project creation and policy
field exactly. Capture timestamps differ; all decision inputs must match.
The captured run/approved-launch/observation columns must also remain unchanged.
All three scalar rows are compared before eligibility checks, including missing
rows; a changed input cannot instead create a permanent denial receipt. Unchanged
inputs that have simply expired still receive the ordinary durable denial.
Fresh run state, observation age, wall clock and account restrictions are checked
before the same one-time intent/consumption/receipt commit. Ordered validated
metadata replaces stored-JSON parsing and SQL JSON extraction in this deferred
path. Repeated admitted-identity citations retain their original multiplicity;
indexing cannot silently turn an invalid multi-identity admission into a valid one.

New, deleted or changed candidates fail with the closed
`claim_preparation_stale` reason and no new claim. An explicit new request attempt
must obtain new bounded preparation; there is no automatic child or synchronous
verification retry. A known durable receipt still returns before preparation,
including after a lost reply or wall-clock rollback. Capturing all candidates is
conservative: unrelated history changes can require new preparation too. Fair
progress during sustained evidence churn still needs release evidence.

`SQLReadConnection.transaction_read_call` permits fixed owner SELECT work inside
an already-open owner transaction, preserving its writes, settings and caller-owned
commit/rollback. The ordinary `read_call` still refuses an outer transaction.
Transaction scopes reject ordinary commit/rollback, context-manager and SQL
transaction/PRAGMA controls. They remain cooperative trusted-code boundaries:
retained native/base-class capabilities are outside this contract, and no RPC
accepts a callback or SQL. SQLite's `query_only` setting alone is insufficient to
prevent commits, as its [documentation](https://www.sqlite.org/pragma.html#pragma_query_only)
explains. Scope faults restore settings or fence the connection; they never infer
that an unknown worker has stopped or replay a claim.
Account rollback uses trusted native cleanup even if restoration already closed
the connection. Cleanup errors cannot replace an operation's nonlocal interruption;
failed rollback fences/closes the connection and preserves returned-job custody.

Regressions exercise actual fixed helpers and Unix-wire claims, forbid legacy
owner JSON paths, test foreign restrictions and explicit stale retries, candidate
deletion, policy/run/observation changes, duplicate identity citations, mismatched
returned captures, overflow before helper work, transaction-control refusal,
rollback and public faulted drain. Native caller/session/fresh-environment/root
custody and reserved-lane enrollment remain unproved. Fixed public-key/config and
file-hardening observations still need whole-operation latency evidence. Active
capacity/orphan recovery, complete normal C19 migration, staged legacy migration,
reviewed installed D8 and all four liveness/release paths remain mandatory.

### Durable active-run capacity (uninstalled candidate)

The account owner reserves active capacity in the same transaction as the unique
launch marker and intent. The service claim and direct claim reference also commit
their keyed claim receipt there. The legacy bridge intent path has no service
claim receipt. All three paths enforce the installed candidate policy: two machine-wide slots
and two slots per eligible provider account. Requests and model output cannot
raise these limits. Indexed counts stop at the policy limit; a full lane returns
retryable `claim_busy` without a new receipt, marker or reservation. Known receipt
lookup precedes capacity checks and never repeats a launch.

Reservations retain their project/run/handle/bridge, account, runtime and policy
bindings across history expiry, wall-clock changes and restart. Project deletion
cannot erase unreconciled active capacity. A dropped socket, elapsed deadline,
missing local history or reported terminal result does not establish cleanup.
No public release endpoint is enabled. The internal fixed bridge now registers
one execution durably before creating its process. Registration cannot be repeated
after restart. Its registry ticket is issued only after the registration commit;
a rolled-back commit leaves no unreturned ticket blocking an explicit retry.
Claim creation captures the absolute monotonic deadline before its
wall-clock sample. The capacity row retains that reference and deadline with a
keyed association to the claiming ledger instance. Registration/commit delays consume
that original budget; later wall samples cannot extend it. A reopened owner cannot
recreate the clock association, even if registration never happened. Expired,
regressed or unauthenticated clock observations refuse launch and retain uncertainty.
Its owner-local registry creates and retains the actual Popen/tree
pair; caller-constructed tickets, process objects, trees and success flags cannot
substitute for that association.

The fixed factory requires a new process session. If registry handoff fails after
process creation, it retains the actual child, kills its still-owned group before
waiting and closes its pipes. The bridge enters cleanup before calling the factory
and can recover the registry's retained process if interruption prevents local
assignment. Tracking errors cannot skip that group kill or owned wait,
and cleanup errors cannot replace the original nonlocal interruption. Failed
handoff or incomplete tracking retains uncertain custody and held capacity;
these paths do not create a completion receipt or authorize another launch.

The tracker retains bounded positive exit/generation evidence separately from its
signalling set. Missing/unreadable process records, unresolved adoption candidates
(including possible reparenting), snapshot loss and identity overflow leave cleanup
unknown. Unverified candidates are never adopted or signalled. Release requires the registered original pair,
its owned root wait, positive evidence for tracked descendants and a durable
matching result binding. A short transaction commits both the released reservation
and a keyed completion receipt; registry acknowledgement follows that commit.
Lost acknowledgements retrieve the same receipt without another launch, including
after clock rollback. A new release refuses clock rollback and retains ready
custody. Result persistence does not by itself release capacity.

Polling cannot establish that every descendant was observed: a child that
daemonizes between snapshots can escape the tracked set and its original group.
The current positive receipt covers observed identities and cannot prove complete
account quiescence in that case. Native containment/custody proof remains a safety
gate before activating this release path. Ordinary short-lived descendants can
also retain capacity conservatively when their exit was never positively observed.
The Popen-internal creation interval before an actual process is returned remains
part of the native-launch containment gate.

Completion metadata survives record retention and restart; backups validate the
matching execution/completion parents. A restarted owner can retrieve an already
committed release, but cannot recreate lost process custody or release an unknown
or historical reservation from result flags. Strong native caller/profile/root
custody, cross-store delivery/reconciliation, actual client containment and all
concurrent-client release criteria remain separate mandatory activation gates.

Schema 9 requires verified schema-8 backups before migration. Existing consumed
or fenced launch markers become `legacy_unknown` reservations; migration does
not infer that historical execution never happened or invent account/cleanup
evidence. Unknown reservations count conservatively for every candidate account.
At the two-slot policy, two historical markers therefore block all new claims and
held projects cannot be deleted. Trusted drained migration and orphan reconciliation
must resolve this before a usable upgrade. Service-path execution/release integration
and recovery after an uncertain or committed registration also remain incomplete.
Migration and capacity backfill commit together or roll back together. Current
startup refuses missing/mismatched reservation coverage instead of rebuilding an
empty capacity pool. Sanitized and standalone owner backups retain the capacity
rows and validate their exact launch parents; older manifests remain unchanged.

### Capacity status and lost-reply recovery (uninstalled candidate)

`AccountClient.capacity_status(run_handle, bridge_run_id)` sends one closed
`account.capacity_status` read to the account owner. The existing default-deny
adapter must authorize the exact request for source `host`, its project/runtime,
operation and caller scope. Hook, app-server and owner-source grants do not admit
this operation. No PID, path, cleanup flag, process object or release instruction
is accepted. Native caller and endpoint authentication remain activation gates.
The caller scope is currently required adapter output; this dispatcher does not
derive or compare it with the original claimant. The future native adapter must
establish actual primary/run ownership before enabling the operation. A nonempty
scope or project match alone does not supply that authority.

The owner uses its existing connection and indexed project/handle/bridge binding.
It returns only `state`, `receipt_digest` and `released_at_ms`:

- `unknown`: no matching scoped reservation metadata; neither a free slot nor an
  absent process is proved.
- `held`: the reservation is unreleased, including after expiry, a terminal result
  or restart with lost cleanup custody. Receipt and release time remain null.
- `released`: the existing keyed completion receipt matches the reservation,
  execution registration and exact terminal binding. A missing or altered parent,
  release digest or completion is an error, never a repaired receipt.

Each explicit read obtains current metadata, even when a caller reuses its request
ID. The response digest binds the entire original request, including its run and
bridge attempt. After a dropped response or account-owner restart, another explicit
read retrieves the same committed release without creating an execution or touching
capacity. The client does not retry automatically or open SQLite if the service is
unavailable. An active owner transaction is not exposed or committed by this read.

The receipt retains the limits of its original evidence: current tracked-process
observations do not establish closed descendant containment. Reading a receipt is
not a new cleanup credential, a release request or permission to rerun a child.
Service-path execution and actual release delivery, native containment/custody,
orphan/drained migration and full normal-client migration remain mandatory.

### Original project-owned account admission/open (uninstalled candidate)

`AccountClient.open_host_run` sends one bounded `account.open_host_run` request
containing the independently enrolled scope, orchestration handle and original
input digest. The trusted server context supplies the full assignment, task,
route, parent grant and project-owned `history.open_scope` proof. Prospective
verifiers also bind the complete original worker context and retained project
result. JSON cannot supply admission, an accepted flag, timestamps or permissions.
The existing assignment validator and subscription admission evaluator remain
authoritative; this initial producer admits read-only Codex bridge routes only.

The configured account owner records a sealed pending request before signature
work. Its original start and observation deadline survive cancellation, a lost
reply and an explicit retry. Another request or assignment label cannot repeat
the same task stage and attempt. Trusted startup must supply bounded pending,
retained and copied-context limits. Unconfigured and synchronous open paths
refuse; there is no synchronous signature or direct-database client fallback.
Opens share the existing claim queue, per-project limits and registered fixed
signature-worker pool. Only an actual registered worker return can reach the
atomic admission commit. Current native authorization is checked again after
crypto, after all writes and before reply publication.

The account transaction retains minimal shared admission, launch inputs and host
metadata plus an immutable open receipt and original request journal. It creates
no central orchestration or copied project lifecycle/result history. Managed
preclaim provenance, claim storage, registration, registered provenance, denial
settlement and terminal delivery verify those original facts against the
independently supplied project lease. The immutable origin column distinguishes
project-owned and legacy runs; a missing or contradictory managed companion
refuses instead of falling back or reconstructing evidence. Original receipts
are metadata, not native identity, current permission, process custody or cleanup
proof. Neither opening nor terminal acknowledgement releases account capacity.

Account schema 10 adds this explicit origin and two sealed journals. Opening an
older database requires explicit trusted drained-owner upgrade authorization
and verified original partition backups; ordinary startup refuses before writing
database pragmas. The option itself proves no native drain. Migration preserves
old rows, blobs and schema-specific backup manifests, marks old host bindings
legacy, and invents no managed opens. Missing schema-10 objects are corruption,
not permission to recreate an empty journal. Actual native authorization and
endpoint activation, legacy drain/reconciliation, normal-client migration,
concurrent live runs and independent release validation remain mandatory.
