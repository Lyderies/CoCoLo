"""S1-H1b: the next step for each refusal, in plain words (PLAN H1b-1 to H1b-3).

Fixed text only: no path, name or value ever reaches it. A code has its own step when a specific
action helps; otherwise its family's (the first word of the code); otherwise DEFAULT.
"""
from __future__ import annotations

DEFAULT = "Run learning-status to see what is held, then try again."

EXACT = {
    'status_ledger_busy': "The collector's records were busy: run learning-status again in a moment.",  # S1-H1a-6c
    'result_capacity_full': "Close finished orchestrations (learning-close-orchestration), or wait for their "
                            "results to pass their retention.",
    'open_service_other_version': "The project's service is an earlier version's: when no run is active, start "
                                  "this version's with learning-start-project-service.",
    'open_service_not_running': "Start the project's service with learning-start-project-service, then try again.",
    'open_account_starting': "The account services are still starting: wait a moment and try again.",
    'open_account_closing': "The account services are stopping: wait for the collector to restart, then try again.",
    'billing_attestation_missing': "Attest billing for this project: run attest.py from the COCOLO folder "
                                   "(setup's billing step does this).",
    'claude_backend_not_subscription': "This child must use the Claude subscription: sign Claude Code in with the "
                                       "subscription account (not an API key), then try again.",
    'claude_model_override_present': "Remove the model override from the Claude settings or environment, then try "
                                     "again.",
    'provider_exhausted': "The provider's usage window is used up: wait until its reset time (learning-status "
                          "shows it), then try again.",
    'history_orchestration_other_scope': "This orchestration belongs to an earlier service or version: open a new "
                                         "orchestration (learning-open-orchestration).",
    'preparation_managed_configuration': "A managed Codex configuration applies here and this child cannot run under "
                                         "it: ask the configuration's owner for an approved setting.",
    'claim_busy': "Every run slot is in use: wait for a running child to finish, then try again.",
    'probe_busy': "The billing probe is busy: wait a moment, then try again.",
    'review_invalid_spec': "Fix the review spec: it needs exactly id, summary, packet (a folder inside the "
                           "project), context, objective, verify_objective, expected_output and evidence.",
    'review_no_verdict': "The child gave no verdict: run that review stage again (a recorded result without one "
                         "cannot pass operational QA).",
    'service_refused_busy': "The service is busy with other connections: wait a moment, then try again.",
}

FAMILY = {
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


def next_step(reason_code: object) -> str:
    """The plain next step for a refusal's fixed reason code."""
    from .contracts import RESULT_VALIDATION_REASON_CODES
    if type(reason_code) is not str:
        return DEFAULT
    if reason_code in RESULT_VALIDATION_REASON_CODES:
        return EXACT.get(reason_code) or RESULT_STEP
    return EXACT.get(reason_code) or FAMILY.get(reason_code.split('_', 1)[0], DEFAULT)
