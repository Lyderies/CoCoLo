# Native caller enrollment for local services

Status: proposed design and proof order. No production issuer, enrollment adapter,
listener or authority change is enabled by this document. The current account and
history authorization adapters continue to default to denial.

This addresses the caller-authentication gate for the 0.3.0 project/account APIs.
It does not complete storage migration, native Claude control or release validation.
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

Candidate peer-PID/audit-token and native code-identity APIs must be checked against
the target SDK and runtime. Relevant declarations are `sys/un.h` (`LOCAL_PEERPID`, `LOCAL_PEEREPID`,
`LOCAL_PEERTOKEN`), `bsm/libbsm.h` (`audit_token_to_pid`, `audit_token_to_pidversion`)
and Security's `SecCode.h` (`kSecGuestAttributeAudit`). Use the audit-token accessors;
its opaque representation is not a wire contract. Declaration of an API is not
proof of the required identity, freshness, code or per-message semantics.

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

## Coverage and material alternatives

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

## Ordered exits before privileged integration

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

The next bounded experiment should falsify NE2–NE4 assumptions for the keeper
candidate before implementing privileged business intake. Private feasibility
observations are not a passing gate, installed-client evidence or authorization.
If a material native integration choice remains after that work, prepare its exact
reviewable change and evidence before requesting the owner's decision.
