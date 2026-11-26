"""S1-H1b: the next step for each refusal, in plain words (PLAN H1b-1 to H1b-3).

Fixed text only: no path, name or value ever reaches it. A code has its own step when a specific
action helps; otherwise its family's (the first word of the code); otherwise DEFAULT.
"""
from __future__ import annotations

DEFAULT = "Run learning-status to see what is held, then try again."

EXACT = {
    # S44a: the usage report's refusals, each naming what to check.
    'usage_project_root_invalid': "The project root is not a folder this command can read: run it from the project "
                                  "folder with --project-root ., or give the project's full path.",
    'usage_project_not_registered': "This state does not know that project: give the project root CoCoLo was set "
                                    "up in, then try again.",
    'usage_state_unavailable': "CoCoLo's learning state could not be opened safely (its folder or key file has the "
                               "wrong owner or permissions, or is missing): run learning-status for the details.",
    'usage_store_unavailable': "A usage store could not be read: try again in a moment, or run learning-status.",
    'usage_window_invalid': "Give --since and --until as dates, with since earlier than until and at most 366 "
                            "days apart.",
    'account_busy': "The account's records were busy: try again in a moment.",  # M4b E1a-2
    'open_account_busy': "The account's records were busy: try again in a moment.",  # M4b E1a-2
    'status_ledger_busy': "The collector's records were busy: run learning-status again in a moment.",  # S1-H1a-6c
    'runtime_upgrade_required': "The client and the collector must use the same runtime: update this project with its "
                                "setup update command, then try again.",  # RSF-S1
    'result_capacity_full': "Close finished orchestrations (learning-close-orchestration), or wait for their "
                            "results to pass their retention.",
    'result_capacity_busy': "Children still running in this project hold the space for results: wait for them "
                            "to finish, then dispatch again.",  # O10 RC-1
    'open_service_other_version': "The project's service is an earlier version's: when no run is active, start "
                                  "this version's with learning-start-project-service.",
    'open_service_not_running': "Start the project's service with learning-start-project-service, then try again.",
    'open_account_starting': "The account services are still starting: wait a moment and try again.",
    'open_account_closing': "The account services are stopping: wait for the collector to restart, then try again.",
    # OP-4: the project's opens in flight, or its runs opened and not yet claimed, are at their limit.
    'open_capacity_full': "This project's runs being opened, or opened and not yet claimed, are at their limit "
                          "(learning-status shows its open requests and pending bindings): wait for those runs to "
                          "start or finish, then open the run again.",
    'open_expired': "This open is older than the account keeps a finished open, and its record was removed: open "
                    "a new run for the task.",  # OP-2
    'billing_attestation_missing': "Attest billing for this project: run attest.py from the COCOLO folder "
                                   "(setup's billing step does this).",
    'claude_backend_not_subscription': "This child must use the Claude subscription: sign Claude Code in with the "
                                       "subscription account (not an API key), then try again.",
    'claude_model_override_present': "Remove the model override from the Claude settings or environment, then try "
                                     "again.",
    'provider_exhausted': "The provider's usage window is used up: wait until its reset time (learning-status "
                          "shows it), then try again.",
    # T7 S2 finding 1: a run not yet finished keeps the orchestration open; restarting the service changes nothing.
    'history_orchestration_pending': "A run of this orchestration has not finished: check each run with "
                                     "learning-run-status, wait for running children to end (the next service settles "
                                     "a lost one), then run learning-close-orchestration again.",
    'history_orchestration_other_scope': "This orchestration belongs to an earlier service or version: open a new "
                                         "orchestration (learning-open-orchestration).",
    'preparation_managed_configuration': "A managed Codex configuration applies here and this child cannot run under "
                                         "it: ask the configuration's owner for an approved setting.",
    'claim_busy': "Every run slot is in use: wait for a running child to finish, then try again.",
    'probe_cooldown': "A fresh billing probe is not allowed within five minutes of the last one that passed: wait "
                      "until then, then run provider-observe again.",
    'probe_busy': "The billing probe is busy: wait a moment, then try again.",
    'review_invalid_spec': "Fix the review spec: it needs exactly id, summary, packet (a folder inside the "
                           "project), context, objective, verify_objective, expected_output and evidence.",
    'review_no_verdict': "The child gave no verdict: run that review stage again (a recorded result without one "
                         "cannot pass operational QA).",
    'recheck_result_mismatch': "The result you handed is not the one the service stored: read the stored result "
                               "(learning-record-host-result shows it) and record again with that body.",
    'recheck_inputs_mismatch': "This run was opened with other original inputs than the ones you gave: record it "
                               "with the inputs of its own open.",
    'history_unrecordable_orchestration_closed': "This orchestration has ended, so this run's result can no longer "
                                                 "be recorded; its report stays as it was (unrecorded).",
    'history_unrecordable_other_runtime': "This service cannot verify this run's recording authority. Keep its stored "
                                          "result. Run: bash setup.sh --update.",
    'history_unrecordable_host_class': "This run belongs to the other host (Codex or Claude): record it from that "
                                       "host's own session.",
    'history_unrecordable_context_state': "This run's stored inputs are not in a state that can be recorded: run "
                                          "learning-status, and open the run again if it never finished.",
    'history_unrecordable_legacy_no_context': "This run was stored by an older version without its inputs, so only "
                                              "its primary can record it: record it with the original inputs.",
    'history_result_expired': "This result's recording window has ended, so it can no longer be recorded: dispatch the "
                              "work again.",
    # UX-HCU-1: a plain terminal is not a host; learning-status cannot fix that.
    'host_client_unknown': "This command runs only from the project's host: run it inside the Claude Code or Codex "
                           "session that runs CoCoLo for this project (or name a host that is running there).",
    'service_refused_busy':"The service is busy with other connections: wait a moment, then try again.",
    # RS-E E2a-6 (0.8.60 full-run fixes): an older service's wake publication after a newer one's; only counted.
    'wake_stale': "This start of the project's service sent a wake notice that is out of date or conflicts with the one "
                  "the collector already holds, so it was ignored: nothing to do; if the project is not being "
                  "started, run learning-status.",
}

FAMILY = {
    'withdraw': "The open could not be withdrawn: check learning-run-status; use a new run after it ends.",
    'history': "The project's records refused this: run learning-status; if it repeats and no run is active, "
               "restart the project's service (learning-start-project-service).",
    'claim': "The account refused the launch claim: run learning-status for held slots, then dispatch again with a "
             "new run (never retry a fenced one).",
    'preparation': "The child's workspace could not be prepared: check the project is committed and readable, then "
                   "dispatch a new run.",
    'open': "The account refused the run's open: run learning-status, then open the run again.",
    'probe': "The billing probe did not complete: wait a moment and run provider-observe again.",
    'run': "Check the run with learning-run-status first: only if it says the run has ended, classify it "
           "abandoned (learning-classify-host-run-abandoned) and dispatch a new one.",
    'account': "The account services did not answer as expected: run learning-status; if it says faulted or "
               "unknown, restart the collector.",
    'dispatch': "The dispatch could not be completed: run learning-run-status for this run, then decide whether to "
                "dispatch a new one.",
    'observation': "The provider observation did not complete: run provider-observe again.",
    'service': "The service would not take the connection: wait a moment and try again; if it repeats, run "
               "learning-status.",
    'admission': "The provider refused admission: run provider-observe and check the provider's account state.",
    'claude': "Claude Code is not in the state this child needs: check its sign-in and settings, then try again.",
    'provider': "The provider is not available for this child: run provider-observe to see why.",
    'billing': "Billing evidence is missing or stale: run provider-observe, and attest billing if asked.",
    'codex': "Codex is not in the state this child needs: check its sign-in and configuration, then try again.",
    'client': "The client changed since the run was opened: open a new run from the same client.",
    'paid': "Paid usage would be used: check the provider's billing settings and attestation.",
    'result': "The result could not be recorded: run learning-status, then record it again.",
    'review': "The review stopped at a step: fix what that step's reason says, then run the same stage again.",
    'window': "The provider's usage window is used up: wait for its reset, then try again.",
}


# A result that fails its contract (these codes share no family word).
RESULT_STEP = ("The result does not meet its contract: correct it as the reason says; validate-result returns a "
               "repair_receipt only when a structural repair is allowed.")


def next_step(reason_code: object, retry_at: object = None) -> str:
    """The plain next step for a refusal's fixed reason code."""
    from .contracts import RESULT_VALIDATION_REASON_CODES
    if type(reason_code) is not str:
        return DEFAULT
    if reason_code == 'probe_cooldown':
        from datetime import datetime
        from .billing_probe_contracts import valid_probe_retry
        if valid_probe_retry(retry_at):
            local = datetime.fromisoformat(retry_at).astimezone().strftime('%H:%M')
            return ("A fresh billing probe is not allowed within five minutes of the last one that passed: "
                    f"wait until {local}, then run provider-observe again.")
    if reason_code in RESULT_VALIDATION_REASON_CODES:
        return EXACT.get(reason_code) or RESULT_STEP
    return EXACT.get(reason_code) or FAMILY.get(reason_code.split('_', 1)[0], DEFAULT)
