# Using it

Your only command is `./setup.sh`. The orchestrator CLI is for the agent: you ask the primary session for
the skill, and it routes the work, opens each run through admission, starts the child, has the result
verified, runs operational QA and reports back, or stops with a `reason_code`.

## From Claude Code

Open the project and ask for the `agent-orchestration` skill, for example: "Use the agent-orchestration
skill to have the routed reviewer check the error handling in `src/`."

It routes the task, opens the run, spawns a Claude child natively or dispatches a Codex child through
the bridge, verifies the result, runs operational QA and reports.

## From Codex

1. From your own terminal, go to the project's own folder. Never start Codex on a folder above the
   project, on this repository or on your home folder ([why](security-model.md#where-to-open-codex)).
2. Start `codex -p orchestrator` if step 7 of the wizard created the profile (needed for Claude
   children), otherwise `codex`.
3. Ask for the `orchestrate-agents` skill.

The skill first runs `host-command` and stops, with the reason, unless the Codex commands rule is
`installed` and the session guard says `ok`.

Do not start Codex from inside a Claude Code session, for example from Claude Code's terminal tool: it
inherits Claude Code's environment markers, and the host check then refuses with `host_client_unknown`.

## Read-only patch workflow

Children in this release are read-only. The primary writes code in the project and delegates bounded
research and independent review. A coding route can ask a read-only implementation worker for a proposed
patch in its result; the primary checks and applies that patch, runs the project checks and requests
independent verification. A patch in a child's reply has not changed the project.

A project agent mapped to `workspace-write` is not eligible for a Codex child yet. Routing skips that
Codex candidate and tries the remaining eligible providers in policy order; it denies the task if none
is eligible. In a Codex-only project that family is unavailable until its mapping is read-only. There is
no automatic replacement with a packaged role. The project owner can align its mapping for read-only
patch authorship; the primary can meanwhile write code and delegate eligible research and review tasks.
The provider, model, effort and role still come from `learning-route`.

## When a task is denied

A denial comes back as a `reason_code`, and no child starts. Admission fails closed, for example on an
API key in the environment, an unreviewed client version, a missing attestation, unknown or paid-usage
billing, exhausted headroom or a run cap ([full list](security-model.md#what-v1-enforces)). Common
messages and their fixes are in [troubleshooting](troubleshooting.md); nearly every fix is "run
`./setup.sh` again".

## Result-contract diagnostics

A client can finish successfully while its answer violates the assignment's result contract.
The bridge still returns `outcome: invalid_result` and a failed fenced replacement. Its
`bridge_report.result_validation_reason_code` names the bounded validator rule, such as
`missing_evidence` or `duplicate_criterion`. An untyped validation error uses
`contract_validation_failed`; rejected text, field names and exception messages are never included.
The reason does not authorize repair, retry, model escalation or acceptance.
Raw child results must leave admission `unverified` and cannot attest their own timeout.
Those claims are fenced with `inconsistent_axes` before result binding; actual bridge-observed
timeouts keep their authoritative timed-out outcome.

Bridge prompts carry the same allowed values and consistency rules as `result-preflight`,
with the literal result shape and its string-array types. A finished review can use completed
execution with a `not-ready` verdict; the sample `pass` value is not a requested conclusion.
The prompt also shows the legacy status projection: a `blocked` verdict uses status `blocked`.
Keep finding severity and corrections in evidence details, without adding output fields.

The trusted bridge stores the reason in a version-3 `dispatch_result` record, alongside the
unchanged result digest and fence. Recording that exact host result returns the stored reason
under `dispatch_diagnostics`. A primary cannot supply or replace this diagnostic. Version-1 and
version-2 records remain readable with their original bytes; absent historical observations stay
unknown. Historical rejected answers cannot be reconstructed from the fenced replacement. This record-format addition needs
the updated runtime, but does not rewrite ledger tables or change outcome/verifier digests.

For Claude, `bridge_report.client_diagnostics` records a bounded process exit code, whether a final
result frame was observed, its allowlisted subtype, and the most recent non-null assistant error code.
Unknown codes use fixed `unrecognized_error` or `unrecognized_result` values; raw messages and
output text are discarded. A null exit code means no client exit was observed, including when
the bridge stopped the child itself. The stored observations return under
`dispatch_diagnostics.client_diagnostics` at host commitment. They describe observed protocol
facts and do not establish a failure's cause, admission, verification or acceptance.

The existing `missing_result` outcome also covers nonzero client exits. A received error frame
and a nonzero exit can therefore coexist with that outcome; the separate observations identify
those facts while preserving the exact failed replacement and terminal binding. Error fields follow
the [documented Claude message types](https://code.claude.com/docs/en/agent-sdk/typescript#sdkassistantmessage).

## Keeping it working

- Run `./setup.sh` again to update the install, verify it, re-run the billing attestation or change the
  owner key passphrase.
- Re-attest whenever your billing settings change.
- Close every Codex session in the project before installing or updating.
- After every install or update (a Python upgrade needs one), trust the orchestrator's learning hooks
  again in Codex's `/hooks` on a main checkout; linked worktrees load their main checkout's hooks.
  The wizard tells you when.
