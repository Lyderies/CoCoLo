# Controller-owned primary bootstrap

Status: unselected architecture proposal for offline implementation and review.
No controller, signing purpose, owner command, primary profile or authenticated
service endpoint is enabled. This is a concrete alternative to the missing root
bootstrap in [native caller enrollment](native-caller-enrollment-design.md).

The proposed change is to launch a primary through a fixed trusted controller.
It would establish primary context by creating the native client and root itself,
rather than accepting a session ID from an arbitrary CLI connection. It must
preserve the existing same-user/read and native-parent-backend limits; it is not
OS protection against every unsandboxed program in the user's account.

## Proposed owner-visible change

An explicit owner command would start a primary for one registered project and
reviewed runtime. The command name and distribution/build mechanism remain
unselected. Existing interactive primaries are not silently enrolled or adopted.
The owner would authorize a fresh controller bootstrap in their terminal before
that primary receives project/account API authority.

That bootstrap would use a new, separately named signing purpose. The existing
billing attester must not be repurposed into an arbitrary signing service.
Passphrases and private-key operations stay in the owner terminal. An agent cannot
request signing through the project/account API. The owner-visible statement must
show the project/runtime, primary host, effective restrictions and bootstrap expiry,
with its native process/root binding derived by trusted code.

This is a material primary-startup and signing-protocol change. Preparing source,
fixtures and evidence offline does not adopt it. The exact implementation,
independent review, supported host coverage and owner decision are needed before
installation or use. It is not a request to approve a missing native primitive.

## Proposed bootstrap operation

1. The fixed controller verifies its own installed build, project registration and
   trusted native-client executable. It refuses substituted code, injection paths,
   unsafe roots and unsupported configuration. It starts no model turn.
2. It creates and exclusively owns a fresh native-client protocol channel. It
   creates exactly one new root with a fixed request identity and explicit primary
   configuration. It accepts the root only from the matching response on that
   owned channel, checking returned effective restrictions and parent/source
   metadata. No arbitrary resume, fork, imported history or supplied session is
   eligible for initial enrollment.
3. The account service supplies a fresh, bounded bootstrap challenge. The proposed
   owner statement binds the current account/project/runtime and service epoch,
   controller native process generation and reviewed build, owned client process
   generation, exact created root/thread/session binding, effective restriction
   digest, challenge, fixed operation scope and expiry.
4. Trusted terminal code independently reconstructs and displays that statement.
   The owner signs only those canonical bytes under the new purpose. No model
   prompt, arbitrary message, executable selection or caller-provided native fact
   is accepted as a statement to sign.
5. The service verifies the current owner key/history revision, challenge and
   signature outside its writer, then revalidates authoritative revisions in a
   short commit. It also verifies the actual connecting controller's native
   identity, fixed build and process generation. The signature is insufficient
   without that native connection and the complete custody/bootstrap proof.
6. A one-shot commit consumes the challenge and creates internal scope for that
   controller's actual root. A lost reply looks up the original request identity;
   it does not sign again, reissue authority or create another root.
7. Only after enrollment and existing admission checks may the controller accept
   primary business operations and start model work. The controller keeps its
   service descriptor away from the native client, provider subprocesses, tools,
   plugins and children.

This names a proposed initial authority operation: an explicit owner bootstrap
statement plus a verified fixed controller that creates the actual root. Its
canonical schema, namespace, cryptographic construction, native identity adapters
and commit protocol are not implemented or reviewed. Reusing a readable signed
statement from another process, service epoch, root or runtime must fail.

## Codex interface candidate

The [App Server documentation](https://learn.chatgpt.com/docs/app-server) provides
owned stdio, root creation through `thread/start`, and experimental dynamic-tool
requests. It distinguishes a thread ID from the session-tree ID. A controller
could correlate the exact root it created with native protocol requests.

The reviewed client's generated schema is evidence about available data shapes,
not runtime attribution. Relevant shapes include:

- `ThreadStartResponse.thread`: distinct `id`, shared-tree `sessionId`, optional
  `parentThreadId`, source and effective configuration fields in the response.
- `DynamicToolCallParams`: native envelope `threadId`, `turnId`, `callId`,
  tool and untrusted arguments. There is no standalone root-authority credential.
- `ThreadStartParams.threadSource`: client-supplied analytics classification,
  which cannot establish a role.

For the proposed integration, root scope comes from controller creation state
and a verified owned native channel. It never comes from `sessionId` equality,
a tool's arguments, an analytics source, a parentless JSON object or a known
thread ID on a different connection. The controller rejects every unknown,
child, forked, resumed or mismatched thread for privileged primary operations.
Prove what actually arrives for supported child calls; optional parent metadata
alone cannot establish the negative case.

The native client receives closed tool operations, not raw service access or an
arbitrary RPC proxy. The controller derives project, primary, runtime and permitted
operation scope internally; supplied labels must match. The service independently
validates complete request bytes and authoritative target ownership.

## Configuration and custody prerequisites

A primary is not a bridged read-only child. Its owner-approved configuration
needs an explicit review contract; child-only isolation flags cannot simply be
assumed available on another native interface. No experiment may remove a
necessary startup guard to obtain initialization.

[Configuration layers](https://learn.chatgpt.com/docs/config-file/config-advanced)
include user/system settings even when project layers are untrusted. A named
profile overlays user configuration. Selecting an empty working directory or a
profile is therefore insufficient evidence of isolation.

Before a native initialization or root-creation experiment, specify exactly which
configuration is permitted, which hooks/plugins/MCP/provider/auth settings can
load, how fixed code verifies it, and how substitution or a change before use
refuses. An effective configuration response after unsafe startup would be too
late to justify that startup. Returning metadata is not evidence that those
settings were disabled. Do not change shared settings or account authentication
to make the experiment work.

The controller's ingress is the exclusively owned native protocol, not an open
primary-to-keeper proxy. Native child/tool processes must not receive its input,
service descriptor or a delegable primary channel. Test fork, dup, exec,
SCM_RIGHTS, partial/queued mixed-writer bytes, client exit and replacement on every
applicable accepted/internal channel. A later peer query cannot attribute earlier
buffered requests. Service and native-client identity are separate obligations.

## Coverage and refusal conditions

| Path | Required primary bootstrap | Remaining proof |
|---|---|---|
| Codex primary to Codex child | Controller-created Codex root | Native actor attribution, configuration, custody, separate admitted executor scope and real Codex checks |
| Claude primary to Codex child | Equivalent authenticated Claude-primary creation/entry | A supported auth-only root/child context mechanism; this must pass within the Codex phase |
| Codex primary to Claude child | Same verified Codex root | Claude liveness/control work follows both Codex-child paths |
| Claude primary to native Claude child | Verified Claude-primary boundary | Explicit native control and retained-limit disposition in the later Claude phase |

No equivalent Claude entry is selected here. Auth-only feasibility may be checked
for the Claude-primary-to-Codex path; Claude activity/cancellation implementation
must wait for the Codex phase. A Codex-only controller does not satisfy this release.

Refuse activation if the native identity, root/child attribution, fixed startup
configuration, descriptor custody or owner-entry proof is unavailable. An owner
signature does not fix an unverifiable controller or turn a child-supplied label
into an actual-root observation. Changing the signing purpose must not alter owner
key replacement, billing signatures, replay, shared caps or one-shot launch claims.

Session/permission changes, supervisor/runtime replacement and service restarts
need the [operation-specific revocation and recovery rules](native-caller-enrollment-design.md#revocation-and-recovery-operations).
New bootstrap cannot revive an old launch permission. Local cleanup and terminal
reconciliation retain separate bounded scopes and never wait for signing.

## Bounded native startup evidence

Private metadata-only experiments with the reviewed Codex 0.164.0 executable now
observe initialization and one newly created ephemeral root on owned stdio. The
matching `thread/start` response reports the requested directory, approval policy
`never`, a read-only sandbox with network disabled, no parent, no thread path and
no turns. No model turn, admitted routed child or privileged operation was requested.
These observations establish creation/response correlation in that experiment;
they do not establish a trusted native controller or caller enrollment.

The experimental process guard denies fork and network, allows execution of only
the exact client, and confines ordinary writes to private scratch. The successful
guard also included an exact directory service lookup and data access to the
existing `installation_id` file. The
[matching release source](https://github.com/openai/codex/blob/rust-v0.164.0/codex-rs/core/src/installation_id.rs)
opens that file read/write/create and locks it even when it already contains a
valid ID. This data exception permits writes; the experiment separately verified
that the valid existing file's bytes, identity, mode and timestamps were unchanged.
Disposable controls refused direct credential/configuration write opens, removal
and permission changes, helper execution, fork and outbound network. Delegated
directory-service behavior and a production permission profile remain unproved.

Root startup also attempted network access and process creation, which the guard
refused. Successful metadata therefore does not justify enabling those paths for
model execution. The root's `source` value did not match the assumed `appServer`
classification. Neither that label nor the client-supplied analytics source is an
authority primitive; the exact created root must remain bound to verified native
code and the exclusively owned channel.

A separate direct-exec control exposed another limit: the exact-executable rule
allows self-exec while PID and kernel birth time remain unchanged. A gated macOS
control observes `NOTE_EXEC` for that replacement and no such event without exec.
This is a possible revocation signal, not a synchronous authorization/commit fence
or an exec counter. Continuous native generation, queued frames, event delivery
races and loss of monitoring still need a fail-closed protocol. A birth-time check
alone cannot satisfy the exec-revocation requirement.

No production profile, owner signing purpose, primary workflow or service authority
is selected by these experiments. The fixed controller, independent native identity,
descriptor custody, actual child attribution and both primary-host paths remain
implementation and review gates.

## Bounded experiment before a selection

The first experiment must request no model turn, invoke no privileged business
operation and modify no shared configuration. It must state its fixed startup
guards before execution, then exercise owned initialization and creation of one
new root under those guards. Capture only approved metadata and exact reviewed
client/build provenance, never credentials, model content or raw private paths.

That experiment can establish creation/response correlation and process/channel
ownership in its measured environment. It cannot establish child-call attribution
without a separately admitted negative probe, signing safety without the reviewed
terminal implementation, or all-host compatibility from one provider.

Next source work, if prerequisites exist, is the fixed controller/terminal/account
bootstrap implementation with matching negative and crash/replay tests. Review
the exact statement schema, native adapters, descriptor and injection behavior,
root/child attribution and operation-specific revocation before enabling it.
No production authorizer is enabled to gather proof.

All diagnostic review/install/fresh installed Claude probe, L1–L17, C1–C19,
D1–D8, LIVE-1 through LIVE-5, client migration, real-client calibration, full
security/regression and real two-project release gates remain unchanged.
