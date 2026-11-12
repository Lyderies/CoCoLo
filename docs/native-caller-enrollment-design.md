# Native caller enrollment for local services

Status: not selected for 0.3.0. Owner decision, 2026-11-13: 0.3.0 keeps the 0.2.x
trust level for service callers and uses the capability enrollment described in
[the service API status](service-api-development.md#project-service-host-and-capability-enrollment-uninstalled-source-candidate).
Proof of the actual native primary process, as explored below, is a possible future
feature. Gates NE1 to NE8 are not 0.3.0 release gates. The independent security
review of the enrollment and host code is still required.

The rest of this document is kept as the record of that exploration. Where it says
a readable key or MAC cannot be used, it is describing the stronger native-identity
goal, which the capability does not claim to meet. The retained protected-entry
checks, role separation, custody rules for descriptors and the revocation table
remain useful input for the capability design and its review.

Write-capable children remain outside 0.3.0.

## Existing checks and the missing boundary

Retain these checks when moving normal CLI work behind services. Their current
guarantees must not be expanded merely by serializing their output.
Code references below are under `packages/codex-agents/native-package/.codex/orchestration/`;
module files are in its `codex_agents/` directory.

| Source | Retained check | What it does not establish for arbitrary IPC |
|---|---|---|
| `host_commands.render_rule`, `runtime_copy_problem` | Absolute isolated interpreter/protected runtime prefix, primary-command allowlist, project binding and exact installed-byte checks | Which process is speaking on a new connection, or who may enroll it |
| `cli._require_protected_runtime`, `_require_own_roots` | Protected-entry command restrictions, session guard and fixed project/state/policy roots | Proof that an arbitrary socket client passed this control flow |
| `session_guard.check_current_session` | Exactly one selected session record and acceptable recorded write roots | Native proof that the selected thread ID belongs to this requester; selection starts from the entry's environment |
| `hosts.detect_host_client`, `cli._require_host_owner`, `LearningLedger.host_owner` | Host-class consistency and project/handle ownership records | Distinction between two actual primaries of the same host class |
| `LearningLedger.bind_session` | Active-project checks, source/client/session consistency, root-level admission accounting | Authentication of the process that supplied those labels |
| `claude_host.check_backend`, `check_model_overrides` | Known backend/settings/override refusals at the entry | Stronger proof of a native parent's internal backend; the existing best-effort limit remains |
| `AccountService`, `HistoryService` | Default denial, operation/project/runtime/full-request checks before owner work | Native provenance of a configured authorizer's server-only output |

The source's fresh protected-entry environment is a launcher assumption that must
be established for the new issuer. The daemon's sanitized environment cannot stand
in for the invoking primary's restrictions. Host strings, session IDs, a readable
bearer, keyed digest, request hash or correctly shaped authorization object do not
close this gap.

`rule_status` reports rule status; it is not a per-command identity check. The
protected-copy branch and installed-root checks are conditional, and `learning-serve`
skips the copy/session checks. A Claude primary does not enter the Codex protected
branch. Its existing tool-policy and backend/settings checks do not supply the
missing native primary bootstrap.

Session lookup currently depends on `CODEX_THREAD_ID`; placing a recorded `tmpdir`
entry also reads the checking process's `TMPDIR`. Native enrollment must establish
whose context those values describe. The collector's root-admission path currently
uses its own `os.environ`, while the collector is launched with a sanitized
environment. Reusing that path as a caller-restriction verifier would not establish
the invoking primary's restrictions.

The current authorization objects carry project, runtime, request digest, source
and operations. They contain no authenticated primary-session, permission/supervisor
generation or run/attempt provenance. Adding similarly named JSON fields or
dataclass members alone is not a correction.

The existing same-user and read-access limits in [the security model](security-model.md)
remain. This is not a claim that every same-user program is prevented from opening
database files. It must protect the supported primary/child tool boundaries without
granting a child primary authority.
V1 also documents that a Codex primary that ignores bridging can create a native
child sharing its rule. That accepted limit is not an existing actor distinction;
any strengthening must be explicit. Supported Codex child paths remain bridged.

## Supported Codex interfaces to investigate

Official documentation checked on 2026-11-06 provides starting points, not a native
enrollment guarantee for the reviewed client:

- [Rules](https://learn.chatgpt.com/docs/agent-configuration/rules) match command
  argument prefixes. Scripts with environment assignments receive whole-shell
  evaluation. This describes execution policy, not an authenticated service principal.
- [Hooks](https://learn.chatgpt.com/docs/hooks) deliver JSON input; subagent hooks use
  the parent session ID, with separate agent IDs on some lifecycle events. Therefore
  a session ID alone cannot distinguish root and child. Hook coverage is not a
  complete enforcement boundary.
- [App Server](https://learn.chatgpt.com/docs/app-server) documents owned stdio,
  structured thread/turn/tool events, version-specific schema generation and
  experimental dynamic tools. A controller-owned native process/channel is another
  candidate source of context. Adopting that primary integration remains an open
  architecture choice; schema fields alone are not an enrollment proof.

Validate exact options and semantics on the reviewed client. Do not assume
`codex exec` isolation flags also apply to App Server, or drop startup guards just
to make a probe initialize. Generated schemas and a metadata handshake would not
prove native actor attribution, restrictions or authority.

## Candidate trust chain

The candidate to investigate is a fixed native **keeper** behind the protected
entry. It would retain the privileged service connection and execute only fixed
entry verification. Choosing, signing, distributing or installing such a keeper
is not decided here. Its bootstrap and real primary binding are required proofs,
not capabilities supplied by this proposed name.

1. An approved launcher starts an immutable, identified issuer for one installed
   project/runtime. Establish that launch provenance independently of JSON, argv
   claims, environment labels or a client-selected role. An arbitrary invocation
   of the same binary is not automatically a primary enrollment.
2. The service and issuer establish their actual native identities and current
   process generations in both directions. A path or UID is only supporting data.
   The service ties the issuer to the account's current trusted build/project
   registration. The client likewise verifies the intended service and boot epoch.
3. The issuer runs the retained protected-entry checks against the actual caller
   context through fixed, isolated code and bounded trusted work. A model cannot
   select an interpreter, script, verifier callback, source root or executable.
   A future native adapter must bind the actual root session and current permission
   context; looking up a supplied known session is insufficient.
4. The service issues a fresh challenge bound to that native channel and boot epoch.
   It accepts an enrollment only after the issuer/bootstrap/root proof is complete.
   It derives the permitted role and scope from trusted entry policy and provenance.
   A requested operation may narrow that scope; a requested role cannot enlarge it.
5. The service keeps the authenticated context internally. Existing JSON labels
   must match it. An opaque public handle is a scoped lookup identity, not standalone
   authority. Request validation and exact business checks still run after enrollment.
6. At use and commit, validate the applicable project, native root/run, runtime,
   supervisor and account/owner/policy generations. Expiry is not proof of unchanged
   authority. Define operation-specific revocation, including queued work and live
   connections, before enabling the adapter.
7. Lost replies and restarts reconcile durable business state under the original
   request identity. Re-enrollment never reissues a consumed launch permission or
   infers nonexecution from a missing local marker. Native context is not restored
   from an old readable handle after a service restart.

The current Python entry checks remain prerequisites within that chain. Merely
checking the identity of a generic interpreter does not identify which script or
guard control flow it executed. The candidate must prove fixed-code execution,
startup/library-injection constraints and protected runtime binding before an
issuer's observations can be trusted. No caller-provided `sandbox_ok`, `no_api_keys`
or `native=true` field is an enrollment proof.

The [bounded controller experiment](native-primary-controller-proposal.md#bounded-native-startup-evidence)
now observes one fresh ephemeral Codex root on owned stdio. It does not supply the
fixed native issuer, independently authenticated bootstrap or complete custody and
restriction proof. No candidate is selectable for privileged intake. The following
sequences still require those creation and authentication mechanisms; an approved
launcher is not an already established trust anchor:

| Candidate | Required creation sequence | Current refusal point |
|---|---|---|
| Fixed keeper | A trusted native host establishes the actual root and current restrictions; it creates a keeper and exclusive operation ingress bound to that root; an independently authenticated bootstrap registers that keeper with the service epoch | Existing rule invocation and Python session lookup do not establish that first native root observation or registration provenance |
| Direct client | A supported host/OS operation binds the exact fixed entry control flow and actual root restrictions to the service challenge and request | No such attestation operation is identified; recognizing an interpreter or the same installed binary is insufficient |
| Controller-owned transport | An owner-established controller creates the reviewed native client and exclusively owns its protocol channel; trusted native protocol events distinguish that controller-created root from every child request and supply current restriction context | Process ownership and schema fields alone do not establish actor attribution, dynamic-tool inheritance or effective restrictions; current-client isolation compatibility also remains unproved |
| Separate identity/native host integration | Explicitly provisioned policy grants a fixed authority a closed root-attestation operation; it authenticates the actual root and issues only scoped, current bindings | A different UID, signature or privileged executable cannot choose the requester's role by itself; provisioning and the attestation operation remain unspecified |

For each candidate, a positive proof must be paired with an invocation of identical
reviewed code from an unapproved process and a supported child. The latter must
not obtain primary service authority from known session/role/handle values. This
concerns the new API enrollment boundary; it does not claim OS protection against
the accepted same-user database-file access limit. Owner bootstrap remains a
separate explicit ceremony, with existing signed statements preserved.

Candidate peer-PID/audit-token and native code-identity APIs must be checked against
the target SDK and runtime. Relevant declarations are `sys/un.h` (`LOCAL_PEERPID`, `LOCAL_PEEREPID`,
`LOCAL_PEERTOKEN`), `bsm/libbsm.h` (`audit_token_to_pid`, `audit_token_to_pidversion`)
and Security's `SecCode.h` (`kSecGuestAttributeAudit`). Use the audit-token accessors;
its opaque representation is not a wire contract. Declaration of an API is not
proof of the required identity, freshness, code or per-message semantics.

## Public XPC code requirements

A separate disposable application-bundle experiment now establishes a usable
code-identity mechanism on the measured macOS host. A bundled XPC service accepted
two requests from the exact allowed client code hash. A different-code client
reached connection setup but never reached the application request handler. The
same unchanged second binary received a reply when its exact code hash was
allowed in a countercontrol. The native binaries remained unchanged during each
experiment. These were local ad hoc signatures, not owner-key signing operations.

Apple documents that
[`xpc_connection_set_peer_code_signing_requirement`](https://developer.apple.com/documentation/xpc/xpc_connection_set_peer_code_signing_requirement(_:_:))
checks the sender's code requirement for every incoming message. The installed
public SDK declares that API. The experiment configured requirements before peer
activation in both directions. This is a distinct public-libXPC experiment; it
does not retry, repair or accept the stopped hand-written Mach experiment.

This result does not select XPC or establish enrollment. An exact code requirement
does not distinguish two instances of the same reviewed executable, identify a
Codex or Claude root, prove the protected host checks ran, or authorize a business
operation. The experiment did not exercise endpoint delegation, queued mixed
senders, fork/exec transitions, native-client tool attribution, configuration
injection or operation/commit revocation. The requirement fixture was private
test input, not an installed trust source. Production code/build binding and
trusted requirement provisioning remain to be implemented and reviewed.

Bundled services are managed by `launchd`. All test client processes were waited;
the observed service PIDs were absent at later inspection. Their self-expiry
markers were not observed, and the parent could not reap those services. This
does not establish their termination cause or production cleanup ownership.

Any XPC candidate must retain the closed business protocol and bound untrusted
input before creating transport objects. Native transport decoding, resource
limits and service restart behavior require their own checks. Do not replace the
current default-deny adapters with a code-signature-only authorizer or expose a
generic XPC forwarding operation. The root/bootstrap proof and the operation
revocation rules below remain required.

## Code-object equality does not identify a process instance

Do not use `CFEqual` on two `SecCodeRef` values to bind a controller grant to one
native process. Apple's published
[`SecCode::equal` implementation](https://github.com/apple-oss-distributions/Security/blob/main/OSX/libsecurity_codesigning/lib/Code.cpp)
compares code-directory hashes, with a static-code fallback. That mutable source
reference is implementation evidence as retrieved, not an attestation of the
installed Security framework.

A separate bounded native control on this host created three directly owned,
live children: two running identical ad hoc signed code and one running different
code. Each code reference came from its owned process's kernel audit token using
`kSecGuestAttributeAudit`. Both same-code processes had distinct audit tokens but
their code objects compared equal. The different-code comparison was unequal.
All queries and no-network validity checks succeeded; all three children were
waited with zero exit status and no forced cleanup. No raw process identities or
audit tokens were retained in the evidence. This control used no XPC service or model request.

Code requirements can constrain executable identity. Controller-instance,
generation, actual root, service epoch and descriptor custody still need their
own proof. A signed statement or code-object equality must not fill those gaps.

The current public SDK also declares
[`xpc_peer_requirement_match_received_message`](https://developer.apple.com/documentation/xpc/xpc_peer_requirement_match_received_message)
from macOS 26. It checks a code requirement against a received message. No runtime
experiment here exercises that API, and neither its declaration nor a successful
code-requirement match would distinguish same-code instances. No minimum OS,
transport or authorizer is selected by this observation.

## Descriptor custody is part of authentication

Do not infer the author of queued stream bytes solely from a later peer-identity
query. A channel with inherited, duplicated or delegated writers requires a proof
that the identity applies to the exact request being authorized.

The keeper candidate therefore requires exclusive custody:

- Its service descriptor is never passed to the model, a provider client, an
  untrusted child, a plugin or an arbitrary subprocess. It offers closed business
  operations, not a generic byte-forwarding proxy.
- Trusted verification workers receive only their specifically created bounded
  input/output channels. Their spawn has an explicit descriptor allowlist; the
  service channel is closed in every child before untrusted code can run.
- A bridge executor obtains a distinct, independently authenticated scope for one
  admitted dispatch. It never borrows a primary channel to launch a child.
- No client-supplied descriptor or `SCM_RIGHTS` handoff is accepted as authority.
  A future need for delegation requires a separate narrowed protocol and review.
- Fork, exec, interpreter replacement, PID reuse, peer exit and endpoint replacement
  invalidate assumptions unless the specified native verifier proves continuity.
  Descriptor-close-on-exec alone does not prevent fork inheritance.

These are proof obligations for the proposed transport. They are not claims about
the current bridge's future enrollment descriptors. Existing watchdog descriptors
and their parent-death enforcement must be preserved during any spawn change.

Exclusive keeper-to-service custody is insufficient if a child can ask the keeper
to perform primary operations. The complete channel inventory must be resolved:

| Channel | Required creator, writers and binding | Required lifetime behavior |
|---|---|---|
| Primary operation ingress | The established root bootstrap creates it for exactly one primary/session/permission generation; the keeper derives context from that creation, never request labels | No inherited or delegated writer; root exit, exec or unverifiable restriction change closes intake and invalidates queued contexts |
| Keeper to project/account service | The authenticated keeper creates it for the registered project/runtime and verified service epoch; keeper code alone writes closed operations | No provider/client/plugin descriptor inheritance, generic forwarding or privilege from a received descriptor |
| Bridge executor enrollment | The trusted launch path creates a distinct context for one admitted run/attempt/dispatch | Cannot enroll another run or borrow a primary channel; launch consumption and later recovery have separate operation scopes |
| Hook/app-server activity ingress | Its own established producer binding permits only normalized activity/health for exact targets | No primary bootstrap, launch, cancellation or owner operation; a controller's bootstrap role is distinct from an activity producer |
| Verification worker pipes | Fixed trusted code creates bounded input/output pipes for one request digest and native-context generation | No service descriptor or ability to choose a role, target, executable or verifier; late output after invalidation is discarded |
| Owner operation ingress | An established owner-terminal entry and the applicable checked signed statement | No primary/child reuse, passphrase delivery or arbitrary signing API; key/epoch changes invalidate pending authority work |

Every applicable accepted stream needs negative cases for fork inheritance,
dup/dup2, exec, interpreter replacement, SCM_RIGHTS, parent exit with a surviving
writer, and endpoint replacement. Include complete and partial requests queued
by different writers before and after identity or revision changes. Checking the
peer again after reading cannot retrospectively authenticate those buffered bytes.
Unknown writer continuity refuses the affected request and channel.

## Internal context and closed roles

The authorizer may create its server-only context only after the complete trust
chain succeeds. At minimum it needs native issuer/process-generation identity,
service boot epoch, registered project/runtime identity, actual primary/session
provenance, current permission/authority revisions, operation scope and expiry.
Bridge contexts additionally identify exactly one run, attempt and dispatch.
These fields cannot be populated from otherwise identical caller JSON.

| Principal | Permitted direction | Required exclusion |
|---|---|---|
| Hook/app-server producer | Approved normalized activity or health operations under its own binding | Cannot enroll a primary or mint launch, cancellation or owner authority |
| Owning primary | Short project operations for its authenticated session and owned targets | No other primary/project scope; no owner-only operation through a primary entry |
| Bridge executor | One admitted dispatch's launch consumption, bounded observation and exact terminal submission | No unrelated run, new orchestration or primary channel |
| Read-only child | Its reviewed client/tool channels and returned result | No privileged service enrollment or inherited primary/executor descriptors |
| Owner terminal | Explicit owner business operations and checked signed statements | No passphrase or arbitrary signing/key operation through the API |

Owner bootstrap and owner-only IPC need their own established entry proof; a
`role: owner` message is not it. Preserve existing owner signatures and cold-start
ceremonies. Completing short setup/registration/attestation calls during another
run remains required; leaving all such work permanently behind a global drain is
not a solution to the concurrency requirement.

The owner and executor entries each need their own concrete bootstrap sequence,
initial anchor and refusal cases; neither inherits proof from the primary's
enrollment. The role-to-operation policy must distinguish an account metadata
read from an owner authority change. Existing source labels and permitted-operation
sets do not implement that future policy or an executor role by themselves.

The ordinary request envelope remains bounded/versioned and closed. Validate its
complete canonical bytes, permitted operation, immutable request identity and
project/run/attempt/runtime scope. Enrollment adds no arbitrary SQL, method,
database path, PID target, environment, producer or executable selection.

Native verification, protected-entry checks, provider observations and signature
work run in bounded trusted workers outside the database writer. Snapshot inputs
and revalidate their current revisions before authority changes. Reserve control
and terminal capacity. Local silence/deadline enforcement and cleanup cannot wait
on enrollment work or metadata publication. An accepted cancellation request still
does not mean cleanup has been confirmed.

The current `HistoryService.handle` invokes its authorizer on the persistence-owner
thread. A native verifier must not be installed directly in that callback. The
integration needs bounded preverification outside the owner, an immutable result
bound to the exact canonical request and native context, and a cheap revision check
at the owner operation. Both account and project paths need this separation.
`AccountService` also invokes authorization synchronously before its owner-thread
views, so moving only the history verifier is insufficient.
Bound unauthenticated connections, bytes, outstanding verification jobs, queue
depth and read/verification deadlines before starting expensive work. Refuse at
capacity; control and terminal work have separately reserved capacity. Worker
completion is not an authority commit, and a hash alone is not authentication.

## Revocation and recovery operations

The implementation must resolve the following dispositions before authorizer
activation. These rules constrain the protocol; no revocation source or cross-store
serialization mechanism is implemented by this document.

| Operation | Queued or not yet committed after relevant revocation | Executing, committed or reply lost |
|---|---|---|
| Enrollment and new reads | Refuse stale primary/permission, project/runtime, service or applicable policy generations; require fresh native bootstrap | A new service epoch cannot recover a context from a readable handle; a read snapshot never grants later authority |
| Admission, preparation and launch consumption | Revalidate actual primary/run scope and applicable project/runtime/supervisor/account/owner-policy revisions at the authoritative short write | Reconcile the original immutable request/claim; never mint a fresh permission because a reply is missing |
| Native spawn handoff | A revoked queued permit must not reach spawn; consumption alone does not prove that revocation and spawn are ordered | Account for outstanding prepared/consumed/native-spawn permits in the drain barrier; missing PID or metadata is not nonexecution or permission to relaunch |
| Owning-primary cancellation | Check actual primary ownership and target supervisor/attempt generation; stale external context cannot choose a replacement target | The supervisor retains independently scoped safety-cleanup authority; acknowledgement states whether cleanup is requested, underway or confirmed |
| Activity and health | Refuse stale producer/run bindings; a producer cannot obtain control authority by changing event labels | Local timers and tracked-process cleanup continue independently of enrollment and durable metadata queues |
| Terminal delivery and acknowledgement | Require the exact run/attempt/claim and a separately authenticated recovery scope; do not widen an expired launch context | Retain exact result/fence binding and idempotent receipt lookup across runtime changes; inability to reauthenticate cannot imply success, nonexecution or fresh launch authority |
| Owner registration, key and signed statements | Verify the applicable current owner/key/policy revision and explicit operation before commit | Lost replies reconcile the original operation; signing remains in the owner terminal, and replacement invalidates pending statements rather than restoring an old key |

Session/permission revisions need a trusted actual-root source; a polled record or
daemon environment is not that source. Project/runtime/account/owner revisions
need identified authoritative writers. Supervisor replacement must publish an
authenticated new generation and invalidate old target contexts. At each short
authority write, revocation and use need a defined serialization point. If their
facts span stores, an explicit receipt/handoff protocol must supply that ordering;
two SQLite commits do not create a common atomic boundary. The launch-to-spawn
gap and old-runtime terminal delivery remain migration/recovery design gates.
No operation may keep a transaction open while waiting for a native verifier,
signature worker, provider or child. Unsupported freshness or ordering refuses
new authority while preserving separately authorized local cleanup/reconciliation.

V1 permits a native Claude child opened before an owner-key change to start
afterwards. Keep that documented limit visible: new API authority must not silently
inherit a stale launch permission, and this design does not claim to stop an
already opened native tool call. Its exact disposition and native-control evidence
belong to the Claude phase after both Codex-child paths pass. NE5 cannot be claimed
for that path merely from the project/account API fixtures.

## Coverage and material alternatives

The [controller-owned primary proposal](native-primary-controller-proposal.md)
names a concrete alternative bootstrap operation: a fresh owner statement bound
to a verified controller that creates the native root itself. Its signing purpose,
startup isolation, native adapters and all-host coverage remain unselected and
unproved; the proposal grants no authority.

| Delegation path | Primary-side authority to establish | Executor/child separation |
|---|---|---|
| Codex primary → Codex child | Protected Codex entry and actual primary/session binding | Separate one-dispatch bridge context; child has no service authority |
| Claude primary → Codex child | Fresh Claude entry evidence and actual primary/session binding | Same bridge separation; this remains in the Codex phase |
| Codex primary → Claude child | Same Codex primary boundary | Bridge context; Claude liveness implementation follows the Codex phase |
| Claude primary → native Claude child | Authenticated parent-side entry and actual session binding | No primary power from child-supplied labels; native control remains a separate Claude-phase gate |

This table maps authorization obligations only. It does not investigate or select
native Claude activity/cancellation control ahead of the Codex phase. Both Codex
paths must pass first, and all four paths remain required for release.

| Alternative | Evidence required before selection | Material consequence |
|---|---|---|
| Fixed native keeper using the approved entry boundary | Actual launch/root proof, code/runtime/endpoint identity, exclusive channel custody and fresh restrictions | New trusted executable/build/signing and entry integration need review |
| Direct client connection to the service | Native, independently verifiable proof of the exact protected code/control flow and root context, not generic interpreter identity | No implicit acceptance merely because it is easier to connect |
| Controller-owned native client transport | Reviewed process/transport ownership and actual root/child context, isolation, admission and versioned tool semantics | Changes primary launch/control integration; all host paths still need evidence |
| Separate privilege/OS identity or trusted native host integration | Precise authority bootstrap, supported client/OS mechanism and preservation of all host paths and accepted limits | New privilege/profile/MCP/routing scope needs a concrete owner decision if required |

An unreviewed same-user socket, readable bearer/MAC key, argv check, random handle,
host string or authorization dataclass is not a fourth viable substitute.
If the existing launcher cannot supply the needed actual-root proof, stop that
activation path and present the supported alternatives. Do not silently adopt
new privileges, omit a provider path or declare a synthetic adapter sufficient.

## Proof dependencies before privileged integration

| Gate | Observable evidence |
|---|---|
| NE1: source boundary | Independent source review maps retained guards, their launcher assumptions, host-class versus actual-primary ownership and default-deny seams |
| NE2: bootstrap and root | Actual primary enrolls; arbitrary same-user/unreviewed process and child cannot enroll using known session/role/handle data; caller restrictions come from the protected entry |
| NE3: issuer and endpoint | Changed interpreter/script/runtime/build, substituted endpoint, process exit/exec/PID generation and unsupported native facts refuse; code identity alone cannot choose a role |
| NE4: custody | Named accepted streams and internal channels pass negative fork/exec/inheritance/duplication/delegation/queued-writer tests; no privileged descriptor reaches a provider or read-only child |
| NE5: freshness and revocation | Queued and established contexts handle session/permission, project, supervisor, runtime and applicable owner/policy changes; restart cannot resurrect authority; business retries never create fresh launch permission |
| NE6: bounded operation | Slow native/guard/signature work and overload cannot occupy the writer or reserved control path; local timers remain independent; request acceptance and cleanup remain distinct |
| NE7: client and recovery integration | Complete normal-client/authority-read census, no SQLite fallback, one global account/key, exact one-shot launch/outbox/replay and drained migration remain intact |
| NE8: release evidence | Codex-first path checks, diagnostic review/test install/fresh installed probe, combined security/regressions and real two-project installation pass |

Gate numbers are identifiers, not an executable sequence: NE2 cannot pass until
the relevant NE3 issuer/endpoint and NE4 ingress/service custody proofs hold. NE1
and a concrete candidate bootstrap specification precede that combined proof.
NE5 and NE6 may be falsified with synthetic fixtures, but their real integration,
NE7 and NE8 remain required before release. No production authorizer is enabled
to obtain experimental evidence.

The next bounded experiment must name its candidate, native primitive, exact
channel and expected refusal before execution:

| Experiment | Observable falsification | What it cannot establish |
|---|---|---|
| Bootstrap discrimination | The identical reviewed issuer invoked by an unapproved process or supported child must not obtain root scope; a positive actual-root case uses the specified authoritative creation operation | Fixture-supplied root labels, ordinary process creation and metadata handshakes cannot prove actual-root provenance |
| Named-stream writer custody | A connector delegates its endpoint to a known child, then queues its own frame; complete and partial earlier child frames must never gain connector authority from a later peer query | A private OS experiment is not an enrolled client or a production authorizer |
| Descriptor/epoch transitions | Fork/dup/exec/delegation, root exit, service restart and queued revision changes must invalidate the specified context without changing its request identity | Supplied fixture generations cannot prove a supported native host publishes real permission changes |
| Owner-thread isolation | A deliberately slow bounded verifier and full preauthentication queue must not occupy the persistence owner or reserved control/terminal capacity | Isolated scheduling logic cannot establish end-to-end client, crash or migration correctness |

If no supported actual-root creation/attestation operation can be named, stop the
candidate before a privileged prototype. Retain that unavailable proof and prepare
the exact alternative integration change, affected host paths and evidence for a
material owner decision when needed. Do not replace the missing operation with a
synthetic boolean, choose a different authority architecture silently or relax the
review/install/fresh installed probe gates. NE8 abbreviates rather than replaces
all L1–L17, C1–C19, D1–D8 and LIVE-1 through LIVE-5 acceptance criteria, existing
admission/billing/headroom/signature/result/verifier/replay checks and real
two-project QA. Retain the existing bridge admission/client/billing recheck before
launch. The reviewed diagnostic test installation precedes the fresh installed
Claude contract probe; offline source work does not waive either gate.
Private feasibility observations are not a passing gate or installed-client proof.
