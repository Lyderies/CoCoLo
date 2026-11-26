# Troubleshooting

Your only command for COCOLO is `./setup.sh`, run from the root of this repository. Nearly every fix
below is "run it again". See the [setup wizard](setup-wizard.md) for what each step does.

## `status_collector_unavailable` or `runtime_newer` after another project was updated

Another CoCoLo version holds the shared learning state. An already-installed 0.3.x client
cannot show the new repair message, so it may show `status_collector_unavailable` or
`runtime_newer` instead. New clients name the version holding the state and print the command
using the clone and project paths from the local install record.

In a plain terminal, use your actual CoCoLo clone and project paths in this command:

```sh
cd /path/to/CoCoLo && ./setup.sh /path/to/project
```

Choose Configure, then "Update the install and walk through every step". If a child run is
still active or unsettled, let it finish and try again. If the local install record is missing,
locate the clone you used to install CoCoLo; the message cannot supply its exact path.

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
read status and observe providers. Update this repository (`git pull`), then run `./setup.sh` and choose Configure, then
"Update the install and walk through every step".

## "the owner key must be a file in your own ~/.ssh folder"

Move the key into `~/.ssh` (mode 0600, no symbolic link, the folder writable only by you) and run again.
If it then says the key file has another link elsewhere, make a fresh copy in `~/.ssh` with `cp` (not
`mv`) and remove the old file.

## I lost my owner key passphrase

Run `./setup.sh`, pick the project and choose Configure, then "Replace a lost owner key". It creates a new key with a new
passphrase, then signs for that project. If the wizard says the project's orchestrator differs from this
setup, choose Configure, then "Update the install and walk through every step" first and skip the attestation step.

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
before going further. To go on, choose Configure, then "Replace a lost owner key" in `./setup.sh`.

## Codex reports the collector unreachable or "filesystem operation failed"

The command ran inside Codex's sandbox. Run `./setup.sh`, choose Configure, then "Update the install and walk through every
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

The rule names the exact Python build and runtime copy. Run `./setup.sh`, choose Configure, then "Update the install and
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

Your records are still in `~/Library/Application Support/codex-agents`. Run `./setup.sh`, choose Configure, then "Update
the install and walk through every step", and approve the move in step 5 (Collector) after checking the
creation date and owner key it shows. Until then every orchestrator command refuses. If it says the state
exists in both folders, keep the one in use, move the other out of both places, and run it again.

## "XDG_STATE_HOME must be inside a hidden folder in your home"

Unset `XDG_STATE_HOME`, or point it at a hidden folder in your home such as `~/.local/state`, and run
again. The default records folder is `~/.local/state/codex-agents`.

## "a child run is active; nothing was changed, try again when none is running"

The command needs the shared ledger to itself, and a child is running (or its service stopped and has
not yet settled it) in some project. Nothing was stopped. Wait for the child to finish, or ask the
primary to cancel it, and run the command again.

This message can still come from an already-installed older update command, or
from a different command that requires drained maintenance. Run the update from
the CoCoLo clone named in the local install record to use Update hand-over below.
The current update proceeds with a running child. An unreadable existing ledger
still refuses before any write, with `update_check_unavailable`; check that the
state folder is readable by this account and run the update again. The pre-check
is repeated immediately before hand-over and installation.

## Update hand-over

An update can proceed while a child runs. The updated project's old child ends
as `lost`; finished results remain available for recording by the currently
enrolled host. Other projects keep running, and their slots remain held until
their own update or the ordinary account recovery rule. Status lists their
locally verified update commands.

`predecessor_unverified` means the installed runtime's lineage could not be
verified. Nothing was changed. Use the project's recorded CoCoLo clone and keep
the installed files intact while investigating.

`predecessor_unproven` means a busy old service could not be confirmed as this
project's own service. Nothing was changed and its child keeps running. When
that run ends, the old service stops by itself; then run `./setup.sh --update`.
An idle service needs no signal proof: withdrawal of its hint lets it stop.
An installed service runs from its protected runtime copy under
`~/.codex/agent-orchestration/`; the update accepts that copy's
`project_host.py` as well as the project's own. If an idle service still cannot
be confirmed, the message says it has no child run in progress and names its
process number. Nothing was changed. Retry `./setup.sh --update`. If it is
refused again, collect `ps -o pid,lstart,command -p <pid>` and the setup output
and ask for help.

`project_store_busy` means another program, such as a backup or records command,
is using this project's records. Nothing was changed. Let it finish, then run
`./setup.sh --update`. To inspect the holder yourself, run `lsof` on
`<state-root>/projects/<project-id>/history.lock` and the project's
`<state-root>/rt/<runtime-directory>/host.lock`. Keep lock files intact.

`update_busy` means another setup or records command holds
`<state-root>/collector-runtime.lock`. Nothing was changed. Let it finish, then
run `./setup.sh --update`.

`collector_unresponsive` means the collector holds its records lock but does
not answer. Nothing was changed. Wait a minute and retry `./setup.sh --update`.
If it still does not answer, inspect `lsof <state-root>/collector.lock` and the
collector's exit/status report. Verify its command and state root before ending
that one process with the operating system's process viewer. Never stop a
process by a guessed PID or a process-name pattern; the updater never signals
the collector.

An already-installed older client can still show its original compatibility
refusal; run the update from the clone named in its local install record.

After hand-over starts, `learning-status` names the unfinished phase. Run
`./setup.sh --update` for that project again to resume. If another project's
update is unfinished, finish the command named in the refusal first.
Do not remove the takeover journal, authorization marker, local store copies
or lock files to bypass recovery.

`collector_stop_failed` says whether the update was undone. When undone, the
collector starts again on next use. Otherwise collectors remain paused until
the update resumes. An unresponsive collector is never signalled by the update.
`service_stop_failed` leaves the hand-over resumable. A pre-B4t service receives
one identity-checked SIGTERM, with bounded escalation against that same process;
a newer service uses its authenticated stop operation. If escalation was needed,
the old child may still run and its slot stays held until it is proven gone.
`service_still_finishing` means the old service was told to stop by hint
withdrawal and is finishing its work first. Its child runs to its end, at most
at its deadline. Then run `./setup.sh --update` to finish; nothing else needs
doing. This is a resumable failure, so it never says nothing was changed.

`predecessor_lock_inherited` means this project's old service stopped, but
another program still holds its records lock. Nothing was taken over yet.
Inspect the lock with `lsof` as above, let that program finish, then run
`./setup.sh --update` again. Do not signal a guessed PID.

A run the update ended can sometimes not be re-checked by the new version (its
records were written by an older one and cannot be proven unchanged). Health then
says: "A run ended by the last update is still counted because its records could
not be checked. It is harmless, takes no run slot, and will not clear by itself."
This is `hold_unverified_takeover`. Nothing needs doing: the update never frees
such a record by guessing, the service tries its settlement once and then leaves it
unchanged, and it does not delay later runs or updates. It is not the
`hold_overdue` warning, which still means a held resource is genuinely late.
Do not edit the project's history file to remove it.

The service remembers up to 64 such runs at a time. If more than 64 keep coming
back, the oldest are forgotten, and each forgotten one is tried again locally on
every sweep. That is a local check only: it makes no account calls, changes
nothing, and learns the run is unverified again from the saved record.

An installer failure after hand-over rolls managed files back where possible;
the records already belong to the successor, and its journal names the next
step. Run the update again to finish. A record reported as "could not be
verified" stays in custody: the update never frees it by guessing. Missing
memory-lock evidence also keeps the hold. A recorded pending preparation-folder
removal continues to be retried by the successor.

Before any signal, the updater rechecks only the candidate's uid, birth time
and exact project launch line. macOS also checks pending exit/exec events; its
numeric-PID reuse interval remains (decision 323). Linux uses a pidfd where
supported; older kernels use the same birth recheck rule. No process descriptor
table or user-wide process list is read. Adoption requires the successor to
acquire the lifetime flocks; a held lock is never explained away.

The owner-only state root has the existing trust level: a same-user program
that presents the exact service arguments can receive a signal itself. The
entry hash attests the file on disk, not the running code. Exec after the last
check can replace the program in the same process before the signal arrives.
These are the accepted A1.7/TL residuals, separate from macOS PID reuse. A real
predecessor service starts children as separate processes rather than execing
itself. Later services use the stop operation without signals.

State roots on network file systems without real cross-process flocks are not
supported. Use a local file system for the protected state root.

## `ledger_upgrade_required`, or "these records were written by an earlier version"

Your orchestrator records come from an earlier version. Nothing was stopped or changed. Run `./setup.sh`
for the project, choose Configure, then "Update the install and walk through every step", and answer "Yes, upgrade them"
in step 5 (Collector). It keeps a checked copy first and says where. Then run setup for every other
project on this machine: they share the records and need this version too.

## `ledger_upgrade_failed`

The records could not be upgraded and are as the earlier version left them; the message names a copy of
them too, and setup says what stopped it when that is a plain fixed message. Install the earlier version
in this project again to keep working, keep the copy, and report the problem with that message.

## `ledger_upgrade_capacity`

The upgrade stopped before it wrote anything: every project's records are backed up before the upgrade,
and those backups do not fit. Either there is not enough free disk space (setup says how much is needed
and how much is free: free some and run setup again), or one project's backup would be over the 16 GiB
limit (setup says which project, by its position, and its size: keep using the earlier version and
report it). Release 0.3.0 refused any project over 128 MiB here; 0.3.1 streams the backups, so size
alone no longer stops an upgrade.

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
until you run `./setup.sh` for each of them and choose Configure, then "Update the install and walk through every step".

## "collector did not become healthy"

The shared collector was started and did not answer within 20 seconds, which happens only on a very
busy machine or when it cannot start at all. It keeps starting by itself: run the command again. If it
keeps failing, run `./setup.sh` and choose Configure, then "Update the install and walk through every step"; step 5
(Collector) says what is wrong.

## A dispatch refused with `preparation_...` or `run_client_changed`

The project's service prepares a child before launching it, and says which step stopped it:

- `preparation_managed_configuration`: managed (enterprise or MDM) settings for the provider's client
  are present on this machine, which COCOLO does not run children under;
- `preparation_client_missing` or `preparation_client_unsupported`: the provider's client was not found,
  or lacks an option a child needs; update or reinstall the client;
- `preparation_client_changed` or `run_client_changed`: the client changed while the child was being
  prepared (an update, say); dispatch a fresh run;
- `preparation_export_failed`: the committed files the child reads could not be exported; check that
  every path the assignment reads is committed;
- `preparation_backend_refused`: the client is set to a metered (API) backend;
- `preparation_invalid_input` or `preparation_not_authorized`: the run's inputs were refused; open a
  fresh run;
- `preparation_full` or `preparation_busy`: the project's service had no free preparation slot; wait for
  a run to finish, or run `./setup.sh` to restart the service, then open a fresh run;
- `preparation_failed_at_<step>` or `preparation_unavailable`: an unexpected error at that step of
  preparing (for example `preparation_failed_at_workspace`); report it with the reason;
- `preparation_worker_busy` or `preparation_worker_failed`: the prepared run could not be handed to its
  worker; run `./setup.sh` to restart the project's service, then open a fresh run;
- `run_account_call_failed`: a call to the account service failed; run `learning-status`, then open a
  fresh run.

Release 0.3.1 and earlier said only `run_unavailable` for all of these.

## `account_closed_without_reply`

The account service closed the connection without answering, usually because the collector stopped
while the call was in flight; the next command starts it again. Try the command again. Run
`learning-status`: `collector_previous_exit` says how the last collector stopped (`signal`, `requested`,
`state_removed`, `thread_ended`, `stop_forced`, or `error` with the error's type and code locations). Report an
`error` with those fields. `stop_forced` means a stop took longer than 15 s and was ended; its
`stalled_in` names the step it was stuck in. Report that too. Releases before 0.3.3 said `account_invalid_response` for this.

On 0.8.28, an `error` of `SQLReadError` passing through `bind_session` was an account read colliding
with a hook's session bind. Codex package 0.8.29 fixes it: update.

## A billing probe refused with `observation_unavailable` or `probe_..._failed`

The probe names the step that stopped it before launching anything:
- `observation_unavailable` (after one automatic retry): the Claude client could not be read; check it
  is installed and signed in, then try again;
- `probe_preparation_failed` or `probe_preparation_unknown`: preparing the probe failed (unknown when
  its cleanup could not be confirmed, which keeps its slot);
- `probe_worker_failed` or `probe_step_failed`: the probe's worker or another step failed. Report it
  with the reason. A probe that failed this way before its model started frees its worker, so the next
  probe can run (0.3.3);
- `probe_worker_unavailable`: the machine's probe worker is held by a probe whose cleanup could not be
  confirmed, or has stopped, and only a restart frees it. Restart the collector (`setup.sh` repairs it),
  then probe again.

## "the account is stuck … and waits to restart itself until it can prove the processes it started have ended"

An account lane (signature checks, billing probes or provider observations) holds work whose end it could not
confirm, so it restarts its own services, but only once it has proved that no process it started can still be
running. The line names each process it is waiting for and the command that checks it (`ps -p <pid>`,
`pgrep -g <pid>` for a process group, `pgrep -s <pid>` for a session). It checks again every minute and
restarts by itself once they are gone. If one stays listed, look at it: a leftover COCOLO helper can be stopped
(`kill <pid>`). Once nothing is listed, restart the collector (`./setup.sh` repairs it). A lane that "may have
started a process whose id it does not have" cannot name one: check `ps -ax` for a leftover COCOLO helper
before restarting the collector.

## `probe_busy`

The Claude billing probe needs one of its account's six run slots, and none was free, or another probe
for the account was running. Nothing was admitted. Try again when a run has finished. Right after an
upgrade from 0.2.x, runs that version recorded count as running for a day after they started (it
never recorded when one finished), so they can hold slots until then; see `claim_busy`. Release 0.3.1
and earlier reported this as `probe_unavailable`.

## `admission_capture_limit` or `admission_capture_work_limit`

Before admitting a child or a billing probe, the account service reads the provider readings and earlier
decisions that can still change the decision: about the last day of them, plus older ones still in force.
These reasons mean even that did not fit in one bounded check (4,096 records and 2 MiB), so nothing was
admitted. It happens only under very heavy use: about ten times the readings of a busy day. Readings stop
counting after a day, and usage windows when they reset (up to a week), so try again later; if it keeps
happening, report it with the reason. A dispatch refused this way never launched and holds no slot; it
closes by itself, and `learning-classify-host-run-abandoned` then answers `already_closed`. On 0.3.2 such
a run could stay open on a project with long history (the command answered `history_conflict`); after
updating to 0.3.3 the project's next service closes it by itself.

Release 0.3.2 held a quarter of a megabyte, about one busy day of readings, so a dispatch on a busy
account could be refused with this reason even though its open was admitted. Release 0.3.1 and earlier
read the whole history, so a project with long use refused every run and reported `account_unavailable`
instead. Update to 0.3.3 or later.

## `claim_busy`

Usually six children are already running on this machine, or on the same provider account, which is this
release's limit (a project may run three of them; a fourth from one project is `preparation_full`). Wait for one to finish (or ask the primary to stop one), then ask
again. A place held by a run whose project service stopped or crashed is freed by that project's next
service, which starts with the project's next command, as soon as nothing of the run is still running;
`learning-run-status` then reports that run as `run_lost` (abandoned, outcome unknown); in the first moments of
the new service it may answer `run_settling` (`ready: false`, no outcome yet): ask again shortly. If it keeps saying so for minutes, `learning-status` shows `project_service.warm_up`: `failing` names the check
(`listing` or `sweep`) and a fixed failure code; the service retries by itself, and stopping it so the next command starts a fresh one is safe. Only when the
service cannot tell (its records of the run are missing or damaged) is the place freed one hour after
that run's deadline. After an upgrade from 0.2.x, runs that version left can hold slots for a day; see
`earlier_version_runs` under `learning_migration` stays `pending`, below.

`learning-status` shows what this project holds on the account in `account_holds`: run slots, run
bindings (runs opened but not yet started) and billing probes, each with how many are held, how many are
past due (freed at the next check), and when the next can be freed. `account_holds.summary` says it in one
line, for example `Account holds: 1 run slot held (next can be freed at 2027-01-15 08:05 UTC; deadline
2027-01-15 08:00 UTC), 0 bindings, 0 probes.` An earlier collector does not report them
(`not reported by this collector version`).

## `project_service_other_version`

This project's earlier service still owns its records. Follow the update command
from its local install record, using that CoCoLo clone. The update verifies and
stops the predecessor automatically before adoption; see Update hand-over above.
An unverifiable or unresponsive service remains held with a plain refusal. An
ordinary project use does not itself perform an owner-authorized hand-over.

## `learning_migration` stays `pending`

The project's learning history moves to its own store the first time the project is used after an
update, but only while no child is running anywhere. Use the project again when things are quiet, or
run `cli.py learning-migrate-project --project-root <root>`; it says what is in the way.

After an upgrade from 0.2.x, the runs that version recorded count as running for a day after they
started. 0.2.x never recorded when one finished, and it enforced no deadline on a native Claude child,
so a day is a cautious rule. They do not hold back an update of COCOLO itself: they never ran under its
collector. `learning-status` and
`learning-migrate-project --status` show them as `earlier_version_runs`: `holding` is how many still
count, and `finished_by_ms` is when the last stops counting (milliseconds since 1970). From then on, the
move can start and their slots are free. Nothing has to be done.

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
other gates still apply. For `billing_attestation_missing`, choose Configure, then "Re-run the billing attestation only"
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

## A dispatch refused with `result_capacity_full`

The project's store has no free place for another result. Each project keeps up to 64 live
results: those of open orchestrations, and those still inside their seven-day retention.

- Close orchestrations you have finished with (`learning-close-orchestration`). Their results retire
  within seconds, and a new dispatch then fits.
- `learning-status` shows `project_capacity.results`: how many results are live, and the limit.
- Before 0.3.4, every result a project had ever stored counted, so a project could record only 64 in
  its whole life and then failed every child with `run_failed`. Updating fixes that; a full store
  recovers by itself at the project service's first cleanup.

## A dispatch refused with `result_capacity_busy`

Nothing is full: children still running in this project hold the space their results may need (about
two at once). Wait for them to finish, then dispatch again. `learning-status` shows
`project_capacity.results_in_flight` against how many fit. Closing orchestrations does not help here.

## A run's open refused with `open_capacity_full` (or `open_busy` before 0.4.0)

This project already has as many runs being opened, or opened and not yet claimed, as the account allows:
up to four opens in progress at once, and up to 4,096 runs opened and not yet started.

- `learning-status` shows `account_capacity.open_requests` (opens in progress) and
  `account_capacity.pending_bindings` (runs opened and not yet claimed), each with its limit.
- Wait for those runs to start or finish, then open the run again. An open that never finishes stops
  counting at its own deadline, and an opened run that is never claimed at its expiry (a day).
- Before 0.4.0, every open request a project had ever made counted, so a project could open only 256 runs
  in its whole life and then every open failed with `open_busy`. Updating fixes that: finished requests
  no longer count, and finished opens are removed a week after their run's expiry.

## A replayed open refused with `open_expired`

The open was finished more than a week ago and the account has removed its record. A run handle is never
reopened: open a new run for the task.

## An orchestration will not close: `history_orchestration_pending`

A run of the orchestration has not finished, or its result was never recorded.

- If `dispatch` lost or refused its reply after the child finished, dispatch the same handle once
  more while the project's service still runs; it returns the same result, which you then record.
- From 0.3.4, once the account has released the run, close works even if its result was never
  recorded, and the run is counted abandoned. A run still running blocks close until it ends.
