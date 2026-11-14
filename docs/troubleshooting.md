# Troubleshooting

Your only command for COCOLO is `./setup.sh`, run from the root of this repository. Nearly every fix
below is "run it again". See the [setup wizard](setup-wizard.md) for what each step does.

## "That passphrase did not unlock the key."

`attest.py` asks `Try again or Exit? [T/e]`; press Enter to try again. The line before the prompt names
the key it is unlocking, such as `~/.ssh/codex-agents-owner`. ssh-keygen's own prompt names a private
copy of that file in a temporary folder; that is expected.

## "That did not work: that private key file does not exist"

The key location question hides what you type, in case a passphrase is typed there by mistake, so a typo
is easy to miss. It asks again twice; paste the location to be sure. You rarely need to type it: a key
whose `.pub` file beside it matches the owner key on record is found by itself, whatever its name in
`~/.ssh`.

## "Collector status: not ready" or "provider observation unavailable" with `account_invalid_response`

Seen only with the first 0.3.0 test build: commands run from your own terminal (setup, status and the
billing attestation) were refused by the orchestrator's account service. This release lets your terminal
read status and observe providers. Update this repository (`git pull`), then run `./setup.sh` and choose
"Update the install and walk through every step".

## "the owner key must be a file in your own ~/.ssh folder"

Move the key into `~/.ssh` (mode 0600, no symbolic link, the folder writable only by you) and run again.
If it then says the key file has another link elsewhere, make a fresh copy in `~/.ssh` with `cp` (not
`mv`) and remove the old file.

## I lost my owner key passphrase

Run `./setup.sh`, pick the project and choose "Replace a lost owner key". It creates a new key with a new
passphrase, then signs for that project. If the wizard says the project's orchestrator differs from this
setup, choose "Update the install and walk through every step" first and skip the attestation step.

Replacing the key cancels every billing statement the old key signed, in every project, so each project's
children are refused with `billing_attestation_missing` until you choose "Re-run the billing attestation
only" there. A child whose run was opened before the replacement is refused at launch too. The old key
file stays in
`~/.ssh`; the new one is `codex-agents-owner-2` (or the next free number).

Only billing statements and grants not yet used are cancelled. QA, calibrations and promotions the old key
approved stay in force, so if the key may have been stolen rather than lost, review those too. A signed
learning release registry stops routing with `learning release registry signature is invalid`, because its
signature is the old key's: release those candidates again, or empty the registry, which is unsigned.

## "owner allowed-signers is not the owner key on record"

The owner key file in your records no longer matches the key the ledger recorded, so nothing can be signed
and billing statements stop counting. If you did not change it, a program may have, so find out what ran
before going further. To go on, choose "Replace a lost owner key" in `./setup.sh`.

## Codex reports the collector unreachable or "filesystem operation failed"

The command ran inside Codex's sandbox. Run `./setup.sh`, choose "Update the install and walk through every
step", and allow the rule in step 8 (Commands): the default there is "No, not now", so press Down once to
"Yes, allow these commands" before Enter. Then start a new Codex session in the project's folder.

## "input validation failed"

With a `field` beside it, a task, assignment, parent grant or route was refused. The `field` names where to
look, such as `task.family`; the value is never printed. A `field` naming a whole object (`task`,
`assignment`) means it is not an object or has a key the contract does not allow. The worked example in the
orchestrate-agents skill's `references/contracts.md` shows valid forms.

Without a `field`, the cause is something else: an unreadable or invalid input file, or a check that depends
on this machine or project.

## The Codex rule is "stale" after a Python or package upgrade

The rule names the exact Python build and runtime copy. Run `./setup.sh`, choose "Update the install and
walk through every step", and choose "Replace it" in step 8. The learning hooks name that Python too: the
update renders them again, so trust them again in Codex's `/hooks` afterwards.

## "This Codex session can write to ..."

The session guard refused the command. Quit that session and start Codex in the project's own folder:
never on a folder that contains a set-up project, the orchestrator clone, your home folder, a folder above
it or `/`. "Run Codex without --ephemeral" means the session left no record to check.

## "This Codex session runs without its sandbox (danger-full-access)"

Open a normal sandboxed Codex session on the project itself. A disabled permission profile cannot
launch managed children through the protected command rule. Do not disable the guard or grant more
filesystem access to resolve this refusal.

Codex 0.164 omits filesystem-policy entries when its sandbox is disabled. Older orchestrator builds
reported that case as an unrecognized session-record format. The updated diagnostic identifies the
sandbox-disabled mode directly; it continues to refuse it. An unknown format in a sandboxed session
still needs investigation before managed commands can run.

## Codex refuses with `host_client_unknown`

The orchestrator tells Codex and Claude Code apart by the environment markers each client sets. A Codex
session started from inside a Claude Code session (for example from Claude Code's terminal tool) inherits
Claude's markers as well as its own, so the host check refuses. Quit it and start Codex from your own
terminal, in the project's folder.

## The Codex rule is "unsupported"

It works only in your account's real `~/.codex`: set `HOME` to your own home folder, unset `CODEX_HOME`
(or point it at `~/.codex`), replace a symlinked `~/.codex` with a real folder, and run `./setup.sh` again.

## "the learning state is still in its old macOS folder"

Your records are still in `~/Library/Application Support/codex-agents`. Run `./setup.sh`, choose "Update
the install and walk through every step", and approve the move in step 5 (Collector) after checking the
creation date and owner key it shows. Until then every orchestrator command refuses. If it says the state
exists in both folders, keep the one in use, move the other out of both places, and run it again.

## "XDG_STATE_HOME must be inside a hidden folder in your home"

Unset `XDG_STATE_HOME`, or point it at a hidden folder in your home such as `~/.local/state`, and run
again. The default records folder is `~/.local/state/codex-agents`.

## "a child run is active; nothing was changed, try again when none is running"

The command needs the shared ledger to itself, and a child is running (or ended without being settled
less than an hour ago) in some project. Nothing was stopped. Wait for the child to finish, or ask the
primary to cancel it, and run the command again.

## `ledger_upgrade_required`, or "these records were written by an earlier version"

Your orchestrator records come from an earlier version. Nothing was stopped or changed. Run `./setup.sh`
for the project, choose "Update the install and walk through every step", and answer "Yes, upgrade them"
in step 5 (Collector). It keeps a checked copy first and says where. Then run setup for every other
project on this machine: they share the records and need this version too.

## `ledger_upgrade_failed`

The records could not be upgraded and are as the earlier version left them; the message names a copy of
them too. Install the earlier version in this project again to keep working, keep the copy, and report
the problem.

## `registration_incomplete_after_upgrade` or `ledger_upgrade_unknown`

The first means the records were upgraded but registering the project did not finish: run `./setup.sh`
again and repeat step 5 (Collector). The second means the upgrade stopped and the records could not be
read afterwards: delete nothing, keep the copy the message names, and report the problem.

## "collector did not stop for maintenance", or "collector stopped too late for this command"

A command asked the shared collector to pause and it was slow to do so. Nothing was changed. If the
message says "it is still running" or "was started again", run the command again. If it says "is not
answering", the next command starts the collector again; run `learning-status` (or `./setup.sh`, verify)
and then repeat.

## `collector_not_started_after_registration`

The project was registered (and, if you approved it, the records upgraded), but the collector did not
start afterwards. Run `./setup.sh` again and repeat step 5 (Collector).

## Another project stopped working after this one was updated

Every project on the machine shares one set of records and one collector. Once one project is updated,
the others are refused (the collector "differs from the owner-authorized runtime", or a newer ledger)
until you run `./setup.sh` for each of them and choose "Update the install and walk through every step".

## "collector did not become healthy"

The shared collector was started and did not answer within eight seconds, which happens only on a very
busy machine or when it cannot start at all. It keeps starting by itself: run the command again. If it
keeps failing, run `./setup.sh` and choose "Update the install and walk through every step"; step 5
(Collector) says what is wrong.

## `claim_busy`

Usually two children are already running on this machine, or on the same provider account, which is this
release's limit. Wait for one to finish (or ask the primary to stop one), then ask
again. A place held by a run whose project service stopped is freed by itself one hour after that run's
deadline.

## `project_service_other_version`

The project was updated while its service was still running from before the update, and a child is
still active somewhere, so the old service was left alone. Try again when no child is running; the old
service is then replaced by itself.

## `learning_migration` stays `pending`

The project's learning history moves to its own store the first time the project is used after an
update, but only while no child is running anywhere. Use the project again when things are quiet, or
run `cli.py learning-migrate-project --project-root <root>`; it says what is in the way.

## A verifier is refused for a result recorded earlier

The project's service keeps what it needs to verify a recorded result only while it runs. It stops
after twelve idle hours, and when the project is updated or migrated. Verify and run operational QA in
the same working session as the work; otherwise run the work again.

## Hook ingestion is idle or has never received an event

`learning-status` separates a running collector from event delivery. `freshness.state: idle` means
hooks have delivered events before and there is no recent activity; it does not prove a hook failure.
`awaiting_hook_review_or_first_event` means no hook events have been recorded: on the main checkout,
open `/hooks` and review the hooks after installation. Linked worktrees load their main checkout's hooks;
the checkout guard ignores their events rather than attributing them to the main checkout.

`client_failures.hook_ingest_rejected.last_reason_code` holds only a fixed collector code. For example,
`record_conflict` means an event identifier had different contents; `capability_mismatch` or
`provenance_mismatch` means the existing ingest contract rejected the event. `transport_unavailable`
means delivery was not confirmed; it does not prove the collector refused the event. Counts are historical
and are not reset by an idle session. Lifecycle hooks fire only at their lifecycle boundaries, and
bridged children have hooks disabled: their run lifecycle comes from the managed host records instead.

`provider_attestations` shows `present`, `missing` or `identity_missing` for each provider, using the
latest observed account and admission's owner-verified project lookup. `unavailable` means the collector
could not report it. This is attestation status, not admission: fresh identity, billing, headroom and the
other gates still apply. For `billing_attestation_missing`, choose "Re-run the billing attestation only"
for that project in `./setup.sh`.

## Operational QA fails without test output

QA keeps stdout and stderr private. `learning-run-operational-checks` returns separate `diagnostics`
with a fixed reason code and exit code for each returned check; the receipt's check report is unchanged.
These are the checks' exit codes: a successful CLI call can still report a failing check. A cleanup
failure aborts the command before it returns per-check diagnostics; the command fails closed.
`nonzero_exit` means the wrapper failed, `deadline_elapsed` means it timed out, and
`temporary_storage_limit`, `temporary_storage_invalid` or `temporary_storage_unstable` names a storage
guard. `snapshot_rejected`, `launch_rejected`, `execution_rejected` and `snapshot_changed` identify the
stage without revealing paths or exception values. `runner_unavailable` is the fixed fallback when no
more specific reason applies. A terminated process may have a negative exit code;
large-file fixtures can exceed QA's resource limit. Inspect the wrapper locally, retaining the guard.
See the [in-process wrapper recipe and bytecode warning](manual-setup.md#operational-checks).
