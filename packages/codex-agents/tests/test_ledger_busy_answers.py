"""M4b E1a-2 on the account side: a busy ledger is answered as busy, never as another failure
(DESIGN-0.4.0-M4b-E1a.md revisions 2 to 3c).

Integration stage: a real ledger and account service (the test_account_api fixture). Each case makes
the ledger busy at one boundary, after the earlier steps have succeeded, and checks the decoded reply.
"""
import threading
import time
import types
import unittest

from tests.test_account_api import RUNTIME, TOKEN, AccountApiTests

from codex_agents import ledger_waits, refusal_steps
from codex_agents.account_api import AccountError, account_request_digest
from codex_agents.sql_read_budget import SQLReadError


def busy(*_args, **_kwargs):
    raise SQLReadError("sql_read_busy")


class LedgerBusyAnswerTests(AccountApiTests):
    def reason(self, request, service=None):
        with self.assertRaises(AccountError) as refused:
            self.call(request, service=service)
        return refused.exception.reason_code

    def hold_lock(self):
        held, release = threading.Event(), threading.Event()

        def holder():
            with self.ledger._lock:
                held.set()
                release.wait(30)
        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(5))
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        return release

    def test_E1a_2_an_authorizer_meeting_a_busy_ledger_is_answered_busy_not_unauthorized(self):
        from codex_agents.account_service import AccountService
        service = AccountService(RUNTIME, self.views, authorizer=busy)
        self.addCleanup(service.close)
        self.assertEqual("account_busy", self.reason(self.request(), service))

    def test_E1a_2_a_held_ledger_answers_account_busy_within_the_request_deadline(self):
        self.hold_lock()
        started = time.monotonic()
        # The owner-key read takes the ledger lock (health does not: it reads published notes).
        reason = self.reason(self.request("account.owner_key"), self.owner_service())
        waited = time.monotonic() - started
        self.assertEqual("account_busy", reason)
        self.assertLess(waited, ledger_waits.LEDGER_REQUEST_WAIT_MS / 1000 + 0.3)
        self.assertIsNone(getattr(ledger_waits._request, "deadline", None))  # restored after the request

    def test_E1a_2_the_account_hosts_authorizer_passes_busy_through(self):
        from codex_agents.account_host import AccountServiceHost
        entry = types.SimpleNamespace(kind="account", enrolled=types.SimpleNamespace(
            role="account", host_client="codex-host-v1", subject=str(self.roots[0]), context=None,
            request_digest=None, bound_project=None, project_bound=False))  # E2a-5: Enrolled's new fields (unbound)
        request = self.request()
        entry.enrolled.request_digest = account_request_digest(request)
        host = types.SimpleNamespace(_frontend=types.SimpleNamespace(context=lambda token: entry),
                                     _views=types.SimpleNamespace(_ledger=types.SimpleNamespace(project_for_root=busy)))
        with self.assertRaises(SQLReadError):
            AccountServiceHost._authorize(host, request, TOKEN)

    def test_E1a_2_a_deferred_billing_reply_keeps_account_busy(self):
        ticket = object()
        reply = types.SimpleNamespace(reason="account_busy", key=(self.projects[0], RUNTIME, "c" * 64, "probe-1"))
        self.service._billing_jobs = types.SimpleNamespace(_jobs={ticket: None}, take=lambda t, _ingress=None: reply)
        from codex_agents.account_api import AccountRequest, decode_account_response
        raw = self.service.take_claim_reply(ticket)
        request = AccountRequest(1, "account.billing_probe", "probe-1", self.projects[0], RUNTIME, {})
        with self.assertRaises(AccountError) as refused:
            decode_account_response(raw, request)
        self.assertEqual("account_busy", refused.exception.reason_code)

    def test_E1a_2_claim_completion_answers_busy_and_records_no_lane_fault(self):
        from codex_agents import account_claim_jobs
        self.assertEqual("account_busy", account_claim_jobs.completion_reason(SQLReadError("sql_read_busy"), "claim"))
        self.assertEqual("open_account_busy", account_claim_jobs.completion_reason(SQLReadError("sql_read_busy"), "open"))
        self.assertEqual("claim_unavailable", account_claim_jobs.completion_reason(RuntimeError("x"), "claim"))
        self.assertFalse(account_claim_jobs.completion_fault(SQLReadError("sql_read_busy")))
        self.assertTrue(account_claim_jobs.completion_fault(RuntimeError("x")))

    def test_E1a_2_a_busy_open_is_answered_open_account_busy_at_the_shared_boundary(self):
        from codex_agents import account_service
        self.assertEqual("open_account_busy", account_service.busy_reason("account.open_host_run"))
        self.assertEqual("account_busy", account_service.busy_reason("account.claim_launch"))
        self.assertEqual("account_busy", account_service.busy_reason(None))  # not yet decoded

    def test_E1a_2_every_busy_answer_has_its_own_next_step(self):
        for reason in ("account_busy", "open_account_busy"):
            with self.subTest(reason=reason):
                self.assertIn("try again in a moment", refusal_steps.next_step(reason))

    def test_E1a_2_provenance_adapters_pass_busy_through(self):
        from codex_agents import account_host_open, account_run_provenance
        for module in (account_host_open, account_run_provenance):
            with self.subTest(module=module.__name__):
                self.assertTrue(module.passes_busy(SQLReadError("sql_read_busy")))
                self.assertFalse(module.passes_busy(SQLReadError("sql_read_deadline")))

    def test_E1a_4_a_wait_that_succeeds_in_time_answers_as_today(self):
        release = self.hold_lock()
        threading.Timer(0.2, release.set).start()
        try:
            self.call(self.request("account.owner_key"), service=self.owner_service())
        except AccountError as refused:  # this fixture's run is unknown: refused for its own reason, not busy
            self.assertNotEqual("account_busy", refused.reason_code)
        self.assertEqual({}, self.ledger.lock_refusals())


def load_tests(loader, tests, pattern):
    """Only this module's own tests: the inherited account API tests run in test_account_api."""
    suite = unittest.TestSuite()
    for name in loader.getTestCaseNames(LedgerBusyAnswerTests):
        if name in LedgerBusyAnswerTests.__dict__:
            suite.addTest(LedgerBusyAnswerTests(name))
    return suite


if __name__ == "__main__":
    unittest.main()
