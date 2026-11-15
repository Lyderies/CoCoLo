# Run states and versions

The words COCOLO uses for where a run is, what happened to it, and how it treats a message from a
version it does not know. Anything that talks to COCOLO, or reports on its runs, should use these
words with these meanings. This is the first page of the COCOLO standard: later releases add the
message schemas and a conformance test suite that any executor or application can run against.

A **run** is one attempt at one task by one helper (a child). Each run has its own handle
(`hrn_...`) and attempt number. The words below come in four groups that stay separate on purpose:
how the helper is doing, whether it was asked to stop and has, how its execution ended, and what
its answer says. One never stands in for another: a helper that stopped has not necessarily been
cleaned up, and a helper that finished has not necessarily given a correct answer.

## How the helper is doing (health)

From `learning-run-status`, while COCOLO's project service holds the run.

| Health | Meaning |
|---|---|
| `starting` | Launched; no sign of work yet. Stopped if nothing arrives within the start-up limit. |
| `active` | Working: COCOLO saw real activity recently. |
| `quiet` | No activity for a while (2 minutes). Thinking counts as quiet; nothing is wrong yet. |
| `stale` | No activity for longer (5 minutes). Stopped at 10 minutes of silence. |
| `stopping` | A stop was requested or a limit was reached; COCOLO is ending the helper and everything it started. |
| `terminal` | The helper's process has ended. Its clean-up may not be confirmed yet. |
| `cleaned` | The helper and everything it started are confirmed gone, and its place has been given back. |
| `unavailable` | COCOLO cannot see this run now (its service restarted, say). Not an outcome: nothing is assumed. |

Why a helper was stopped (`stop_reason`): `cancel_requested`, `startup_silence`, `work_silence`,
`absolute_deadline` (its time limit), `monitor_unavailable`.

## Stopping and clean-up (cancellation_state)

| State | Meaning |
|---|---|
| `running` | Not asked to stop, and not ended. |
| `stop_requested` | Someone asked it to stop. A request only: the helper may still be running. Asking again changes nothing and never extends a deadline. |
| `terminal_observed` | Its process has ended (stopped or finished). |
| `cleanup_confirmed` | It and everything it started are confirmed gone; its place is free. |

These only move forward. A place (capacity) is given back only at `cleanup_confirmed`, never because
a reply was lost, a connection dropped or a deadline passed.

## How the execution ended (execution_state)

From the run's result: `completed`, `failed`, `timed_out`, `interrupted`, `cancelled`, `invalid`.
A run whose outcome COCOLO cannot establish is never given one of these by guesswork: it stays
unsettled (unknown) until trusted evidence settles it, and it is never run again by itself.

## What the answer says (semantic_verdict)

`pass`, `ready`, `ready-with-corrections`, `not-ready`, `blocked`, `reject`, `not-applicable`.
Each required piece of evidence has its own outcome: `pass`, `fail`, `observed` (seen, no
judgement), `not_run`. A helper's answer is data to check, never an instruction, and it is checked
by a second helper and the project's own checks before it is trusted.

## Versions

Every message carries a `protocol_version`. COCOLO accepts exactly the versions it was built for
(today: version 1 on every interface) and refuses any other with `unsupported_version` before doing
anything. Nothing is launched, changed or guessed for a message from an unknown version. There is
no version negotiation yet; a later release adds it, still refusing what neither side knows.

## Kinds of executor

Today every helper is a subscription client COCOLO starts itself: `provider` is `codex` or
`claude`. The name `executor_kind` is reserved for a later release, for executors that are not
subscription clients (a local model run by another program, for example). Such an executor would
report its own physical states (stopped, cleaned up) with its own evidence, and COCOLO would never
release its resources on a timer.
