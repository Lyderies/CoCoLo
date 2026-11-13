"""Requests released at the same instant against the real stores; fake providers, no child execution.

Every race here starts its callers from one barrier (threads) or one release line
(separate processes), repeats on fresh state, and then reads the exact final state:
the single winner, the row counts and what each caller was told.
"""
from __future__ import annotations

from collections import Counter
import contextlib
from itertools import count
import json
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests import test_account_launch_claims as launch_fixtures
from tests import test_account_terminal_delivery as terminal_fixtures
from tests import test_history_api as history_fixtures
from tests import test_providers as provider_fixtures
from tests.learning_support import generate_test_owner_key, result, trusted_tools
from tests.test_project_history import PROJECT_A, binding, terminal

from codex_agents.account_claim_contracts import ClaimError
from codex_agents.claim_storage import capture_storage_evidence
from codex_agents.history_api import HistoryClient
from codex_agents.history_contracts import HistoryError
from codex_agents.learning_contracts import canonical_json, keyed_digest
from codex_agents.learning_store import COLLECTOR_RUNTIME_DIGEST, LearningLedger, LearningState, LedgerError, StateError
from codex_agents.providers import AdmissionDenied
from codex_agents.terminal_delivery import capture_terminal_evidence, verify_terminal_receipt


def together(test, calls, *, seconds=20, here=None):
    """Run every call from one barrier; return ("returned", value) or ("raised", error) per call.

    Each call has its own thread, except the one `here` names: that one runs on the
    calling thread, for stores that answer only the thread that opened them.
    """
    barrier = threading.Barrier(len(calls))
    outcomes = [None] * len(calls)

    def run(index):
        try:
            barrier.wait(10)
            outcomes[index] = ("returned", calls[index]())
        except BaseException as error:
            outcomes[index] = ("raised", error)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(len(calls)) if index != here]
    for thread in threads:
        thread.start()
    if here is not None:
        run(here)
    for thread in threads:
        thread.join(seconds)
        test.assertFalse(thread.is_alive(), "a racing request did not finish")
    return outcomes


def returned(outcomes):
    return [value for kind, value in outcomes if kind == "returned"]


def raised(outcomes):
    return [value for kind, value in outcomes if kind == "raised"]


class DuplicateHostResultRaceTests(unittest.TestCase):
    """The same run's result recorded twice at once through the account ledger's host adapter."""

    ROUNDS = 16

    def setUp(self):
        self.case = provider_fixtures.AdmissionTests("test_open_host_run_always_admits_inside_the_ledger")
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.ledger, self.project = self.case.ledger, self.case.project_id
        self.case.attest("codex", self.case.observe()[0]["account_fingerprint"])

    def opened(self, number):
        """One admitted, bound host run and the exact arguments its result must repeat."""
        self.case.observe()
        with mock.patch.object(self.ledger, "open_host_run", wraps=self.ledger.open_host_run) as opening:
            run = self.case.open_run()
        self.ledger.bind_host_agent(self.project, run_handle=run["run_handle"], agent_id=f"/root/racing_child_{number}")
        opened = opening.call_args.kwargs
        return run["run_handle"], {
            "assignment": opened["assignment"], "task": opened["task"], "route": opened["route"],
            "execution_configuration": opened["execution_configuration"], "result": result(),
            "observation": {"run_handle": run["run_handle"], "result_attempts": [
                {"status": "accepted", "reason_code": None, "receipt_id": None}]},
        }

    def count(self, sql, *parameters):
        return self.ledger._connection.execute(sql, parameters).fetchone()[0]

    def kept_files(self):
        return sorted(path.name for path in (self.case.state.artifacts / self.project).glob("host_result_*.json"))

    def test_the_same_result_committed_twice_at_once_is_recorded_exactly_once(self):
        for number in range(self.ROUNDS):
            with self.subTest(round=number):
                handle, arguments = self.opened(number)
                stamp = self.case.clock + 2
                # Even rounds commit at one instant, so both callers derive the same outcome and
                # the same kept file; odd rounds are one millisecond apart and derive two.
                stamps = (stamp, stamp) if number % 2 == 0 else (stamp, stamp + 1)
                events = self.count("SELECT COUNT(*) FROM records WHERE record_type='event'")
                outcomes = self.count("SELECT COUNT(*) FROM records WHERE record_type='outcome'")
                files = self.kept_files()
                raced = together(self, [
                    lambda at=at: self.ledger.commit_host_result(self.project, now_ms=at, **arguments) for at in stamps
                ])
                self.assertEqual(1, len(returned(raced)), raced)
                self.assertEqual([LedgerError], [type(error) for error in raised(raced)], raced)
                self.assertRegex(str(raised(raced)[0]), "assigned route|already consumed|already exists")
                committed = returned(raced)[0]
                self.assertFalse(self.ledger._connection.in_transaction)
                # One terminal event and one outcome joined the one start event of this run.
                self.assertEqual(events + 1, self.count("SELECT COUNT(*) FROM records WHERE record_type='event'"))
                self.assertEqual(outcomes + 1, self.count("SELECT COUNT(*) FROM records WHERE record_type='outcome'"))
                for record in (committed["event"], committed["outcome"]):
                    self.assertEqual(1, self.count("SELECT COUNT(*) FROM records WHERE project_id=? AND record_id=?",
                                                   self.project, record["record_id"]))
                digest = keyed_digest(self.case.state.digest_key(), handle)
                row = self.ledger._connection.execute(
                    "SELECT binding_state,consumed_at_ms FROM host_run_bindings WHERE handle_digest=?", (digest,)).fetchone()
                self.assertEqual("terminal", row["binding_state"])
                self.assertIn(row["consumed_at_ms"], stamps)
                # The winner's kept result is the only new file, registered once and complete.
                name = "host_result_" + committed["host_outcome_digest"]
                self.assertEqual(sorted([*files, name + ".json"]), self.kept_files())
                self.assertEqual(1, self.count("SELECT COUNT(*) FROM stored_files WHERE project_id=? AND file_id=?",
                                               self.project, name))
                self.assertEqual(number + 1, self.count("SELECT COUNT(*) FROM stored_files WHERE kind='artifact'"))
                self.assertEqual(canonical_json(result()),
                                 (self.case.state.artifacts / self.project / (name + ".json")).read_bytes())
                with self.assertRaisesRegex(LedgerError, "assigned route|already consumed"):
                    self.ledger.commit_host_result(self.project, now_ms=stamp + 2, **arguments)
                self.assertEqual(outcomes + 1, self.count("SELECT COUNT(*) FROM records WHERE record_type='outcome'"))
                self.case.clock = stamp + 2


class DuplicateBridgedTerminalRaceTests(unittest.TestCase):
    """A bridged child's returned result bound to its run twice at once."""

    ROUNDS = 3

    def test_a_dispatched_childs_result_recorded_twice_at_once_is_bound_exactly_once(self):
        for number in range(self.ROUNDS):
            with self.subTest(round=number):
                case = launch_fixtures.AccountLaunchClaimsTests("test_concurrent_same_run_claims_consume_exactly_once")
                case.setUp()
                try:
                    claimed = case.claim()

                    def record(case=case, claimed=claimed):
                        return case.ledger.record_dispatch_result(
                            case.project, run_handle=case.arguments["run_handle"], bridge_run_id=claimed["bridge_run_id"],
                            result=result(), fenced=False, reviewed=True, terminal_state=None)

                    with mock.patch.object(case.ledger, "_clock_ms", return_value=case.stamp):
                        raced = together(self, [record, record])
                    self.assertEqual(1, len(returned(raced)), raced)
                    self.assertEqual([LedgerError], [type(error) for error in raised(raced)], raced)
                    self.assertIn("already recorded", str(raised(raced)[0]))
                    self.assertEqual(claimed["bridge_run_id"], returned(raced)[0]["bridge_run_id"])
                    with case.ledger.repository(case.project) as repository:
                        recorded = repository.records("dispatch_result")
                    self.assertEqual(1, len(recorded))
                    rows = [tuple(row) for row in case.ledger._connection.execute(
                        "SELECT bridge_run_id,terminal_record_id FROM capacity_completions")]
                    self.assertEqual([(claimed["bridge_run_id"], recorded[0]["record_id"])], rows)
                    self.assertFalse(case.ledger._connection.in_transaction)
                finally:
                    case.tearDown()
                    self.assertTrue(case.doCleanups())


class AccountTerminalAcceptanceRaceTests(unittest.TestCase):
    """One project terminal submitted to the account ledger by its owner thread and another thread at once.

    The account ledger accepts a project terminal only on the thread that opened it, so
    its service answers simultaneous submissions one after another on that thread.
    """

    ROUNDS = 4

    def accept(self, case):
        storage_binding, storage_evidence = capture_storage_evidence(case.expected, case.bound)
        return case.ledger._accept_project_terminal(
            case.binding.project_id, runtime_digest=COLLECTOR_RUNTIME_DIGEST, caller_scope=case.caller_scope,
            submission=case.submission, storage_binding=storage_binding, storage_evidence=storage_evidence,
            terminal_evidence=capture_terminal_evidence(case.proof))

    def completions(self, case):
        return [tuple(row) for row in case.ledger._connection.execute(
            "SELECT run_id,bridge_run_id,terminal_record_id,terminal_content_digest FROM capacity_completions")]

    def test_one_terminal_submitted_by_the_owner_thread_and_another_thread_at_once_commits_once(self):
        for number in range(self.ROUNDS):
            with self.subTest(round=number):
                case = terminal_fixtures.AccountTerminalDeliveryTests(
                    "test_exact_account_acceptance_and_local_acknowledgement_do_not_release_capacity")
                case.setUp()
                try:
                    raced = together(self, [lambda case=case: self.accept(case) for _ in range(2)], here=0)
                    (owner_kind, receipt), (other_kind, refusal) = raced
                    self.assertEqual(("returned", "raised"), (owner_kind, other_kind), raced)
                    # The other thread is refused with the fixed reason and writes nothing.
                    self.assertIs(ClaimError, type(refusal))
                    self.assertEqual("claim_terminal_unavailable", refusal.reason_code)
                    self.assertEqual(("recorded", False), (receipt["state"], receipt["known"]))
                    self.assertEqual(receipt, verify_terminal_receipt(
                        case.fixture.key, case.grant, case.delivery, canonical_json(receipt)))
                    self.assertEqual([(case.binding.run_id, case.binding.claim_id, case.delivery.record_id,
                                       case.delivery.record_content_digest)], self.completions(case))
                    self.assertEqual(1, case.ledger._connection.execute(
                        "SELECT COUNT(*) FROM account_cleanup_receipts").fetchone()[0])
                    self.assertFalse(case.ledger._connection.in_transaction)
                    # The owner's own repeat is the same receipt, marked as already known.
                    self.assertEqual({**receipt, "known": True}, self.accept(case))
                    self.assertEqual(1, len(self.completions(case)))
                    case.held()
                finally:
                    self.assertTrue(case.doCleanups())


class ProjectStoreTerminalRaceTests(unittest.TestCase):
    """Terminal commits sent at once to one project store through its own process and wire API.

    A project store has one owner thread by construction, so simultaneous requests reach
    it as simultaneous connections to its service.
    """

    ROUNDS = 12
    CALLERS = 4

    def setUp(self):
        self.case = history_fixtures.HistoryProcessApiTests(
            "test_terminal_delivery_and_acknowledgement_replay_survive_process_restart")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.client, _, self.stop = self.case.start("racing", PROJECT_A)
        self.endpoint = self.case.base / "racing.sock"

    def commit(self, record, target):
        # Each caller has its own client and connection, as separate drivers would.
        return HistoryClient(self.endpoint, PROJECT_A, history_fixtures.RUNTIME, timeout=10).commit_terminal(record, target)

    def rows(self, table):
        connection = sqlite3.connect((self.case.base / "racing" / "history.sqlite3").as_uri() + "?mode=ro", uri=True)
        try:
            return connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
        finally:
            connection.close()

    def test_one_terminal_sent_by_several_callers_at_once_is_delivered_once_with_identical_replies(self):
        for number in range(1, self.ROUNDS + 1):
            with self.subTest(round=number):
                record, target = json.loads(terminal(number)), binding(number)
                raced = together(self, [lambda: self.commit(record, target) for _ in range(self.CALLERS)])
                self.assertEqual([], raised(raced))
                replies = returned(raced)
                self.assertEqual(self.CALLERS, len(replies))
                self.assertTrue(all(reply == replies[0] for reply in replies), replies)
                self.assertEqual(number, replies[0]["delivery_sequence"])
                self.assertEqual(record, replies[0]["dispatch_result"])
        pending = []
        while True:
            page = self.client.pending_terminals(after_sequence=len(pending), limit=10)["deliveries"]
            pending.extend(page)
            if not page:
                break
        self.assertEqual(list(range(1, self.ROUNDS + 1)), [delivery["delivery_sequence"] for delivery in pending])
        self.assertEqual([binding(number).claim_id for number in range(1, self.ROUNDS + 1)],
                         [delivery["binding"]["claim_id"] for delivery in pending])
        self.stop()
        self.assertEqual(self.ROUNDS, self.rows("terminal_outbox"))
        self.assertEqual(self.ROUNDS, self.rows("records"))

    def test_two_different_terminals_for_one_claim_sent_at_once_leave_one_and_refuse_the_other(self):
        for number in range(1, self.ROUNDS + 1):
            with self.subTest(round=number):
                target = binding(number)
                records = [json.loads(terminal(number)) for _ in range(2)]
                records[1].update(record_id=f"dispatch-other-{number}", reviewed=True)
                raced = together(self, [lambda record=record: self.commit(record, target) for record in records])
                self.assertEqual(1, len(returned(raced)), raced)
                self.assertEqual([HistoryError], [type(error) for error in raised(raced)], raced)
                self.assertEqual("history_conflict", raised(raced)[0].reason_code)
                kept = returned(raced)[0]
                winner = [kind for kind, _ in raced].index("returned")
                self.assertEqual(records[winner], kept["dispatch_result"])
                # The refused caller's record was not kept under its own identity either.
                with self.assertRaisesRegex(HistoryError, "history_not_found"):
                    self.client.read_record(records[1 - winner]["record_id"])
                self.assertEqual(kept, self.commit(records[winner], target))
                with self.assertRaisesRegex(HistoryError, "history_conflict"):
                    self.commit(records[1 - winner], target)
        self.stop()
        self.assertEqual(self.ROUNDS, self.rows("terminal_outbox"))
        self.assertEqual(self.ROUNDS, self.rows("records"))


# The two store calls `learning-configure-owner-key` makes (cli.py), held until one release line.
OWNER_KEY_PROCESS = r'''
import sys
from pathlib import Path
from codex_agents.learning_store import LearningState, StateError
state = LearningState(Path(sys.argv[1]))
public_key = Path(sys.argv[2]).read_text()
print("ready", flush=True)
sys.stdin.readline()
try:
    state.install_owner_public_key(public_key)
except StateError:
    print("refused", flush=True)
except OSError as error:
    print("filesystem:" + type(error).__name__, flush=True)
else:
    print("installed", flush=True)
'''


class OwnerKeyConfigurationRaceTests(unittest.TestCase):
    """Two first-time configurations of the owner public key for one state root, at once.

    The documented rule (`LearningState.install_owner_public_key`): only the first key is
    installed; the same key may be put back; another key is refused with a StateError.
    """

    THREAD_ROUNDS = 30
    PROCESS_ROUNDS = 60

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.base = Path(cls.temporary.name)
        cls.publics, cls.key_files = [], []
        for number in range(2):
            folder = cls.base / f"key-{number}"
            folder.mkdir()
            private_key, public_key = generate_test_owner_key(folder)
            cls.publics.append(public_key)
            cls.key_files.append(private_key.with_suffix(".pub"))
        cls.lines = [LearningState._owner_signer_content(public_key) for public_key in cls.publics]
        cls.sequence = count(1)

    def fresh_root(self):
        root = self.base / f"state-{next(self.sequence)}"
        LearningState(root)  # The empty, unconfigured root both requests find.
        return root

    def threads(self, keys):
        root = self.fresh_root()
        states = [LearningState(root) for _ in keys]
        raced = together(self, [
            lambda state=state, key=key: state.install_owner_public_key(self.publics[key])
            for state, key in zip(states, keys, strict=True)
        ])
        told = []
        for kind, value in raced:
            if kind == "returned":
                self.assertEqual(states[0].allowed_signers_path, value)
                told.append("installed")
            else:
                told.append("refused" if isinstance(value, StateError) else "filesystem:" + type(value).__name__)
        return root, told

    def processes(self, keys):
        root = self.fresh_root()
        started = [subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c",
             f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + OWNER_KEY_PROCESS,
             str(root), str(self.key_files[key])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for key in keys]
        try:
            for process in started:
                self.assertTrue(select.select([process.stdout], [], [], 20)[0], "fixture process did not start")
                self.assertEqual("ready", process.stdout.readline().strip())
            for process in started:
                process.stdin.write("go\n")
            for process in started:
                process.stdin.flush()
            told = []
            for process in started:
                output, errors = process.communicate(timeout=20)
                self.assertEqual(0, process.returncode, errors)
                told.append(output.strip())
            return root, told
        finally:
            for process in started:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def final_state(self, root, keys, told):
        """What is wrong with one finished round, as short fixed labels; empty when consistent."""
        wrong = []
        state = LearningState(root)
        in_force = state.allowed_signers_path.read_bytes()
        holders = sorted({key for key in keys if self.lines[key] == in_force})
        if len(holders) != 1:
            wrong.append("signer file is not exactly one of the racing keys")
        if 0o600 != state.allowed_signers_path.stat().st_mode & 0o777:
            wrong.append("signer file is not owner-only")
        if [path.name for path in root.iterdir() if path.name.startswith(".owner-allowed-signers")]:
            wrong.append("a temporary signer file was left behind")
        winners = [key for key, answer in zip(keys, told, strict=True) if answer == "installed"]
        if not winners:
            wrong.append("no caller was told its key was installed")
        if any(self.lines[key] != in_force for key in winners):
            wrong.append("a caller was told its key was installed while another key is in force")
        if len(set(winners)) > 1:
            wrong.append("callers with different keys were both told their key was installed")
        ledger = LearningLedger(state)
        try:
            history = [(bytes(row[0]), row[1]) for row in ledger._connection.execute(
                "SELECT signer_line,reason FROM owner_key_history ORDER BY sequence")]
        finally:
            ledger.close()
        if history != [(in_force, "enrolled")]:
            wrong.append("the ledger did not record exactly the key in force as the one enrolled key")
        if state.owner_key_report()["state"] != "ok":
            wrong.append("the owner key report is not ok")
        # After the race the documented sequential rule holds for every racing key.
        for key in sorted(set(keys)):
            try:
                state.install_owner_public_key(self.publics[key])
                again = "installed"
            except StateError:
                again = "refused"
            if again != ("installed" if self.lines[key] == in_force else "refused"):
                wrong.append("a later configuration did not follow the first-key rule")
        if state.allowed_signers_path.read_bytes() != in_force:
            wrong.append("a later configuration changed the key in force")
        return wrong

    def assert_consistent(self, racing, keys, rounds):
        seen, answers = Counter(), Counter()
        for _ in range(rounds):
            root, told = racing(keys)
            answers[tuple(sorted(told))] += 1
            seen.update(self.final_state(root, keys, told))
        self.assertEqual({}, dict(seen), f"inconsistent rounds out of {rounds}; answers seen: {dict(answers)}")

    def test_two_different_keys_configured_at_once_leave_the_one_key_whose_caller_was_told_it_won(self):
        self.assert_consistent(self.threads, (0, 1), self.THREAD_ROUNDS)

    def test_the_same_key_configured_twice_at_once_is_in_force_once_and_recorded_once(self):
        self.assert_consistent(self.threads, (0, 0), self.THREAD_ROUNDS)

    def test_two_processes_configuring_different_keys_at_once_leave_the_one_key_whose_caller_was_told_it_won(self):
        self.assert_consistent(self.processes, (0, 1), self.PROCESS_ROUNDS)

    def test_the_caller_that_loses_a_configuration_race_gets_the_answer_a_later_caller_would(self):
        # After a first key is installed the function has two answers: the signer path for
        # the same key, a StateError for another. A caller that raced gets one of them too.
        for keys, expected in (((0, 0), ("installed", "installed")), ((0, 1), ("installed", "refused"))):
            with self.subTest(keys=keys):
                answers = Counter()
                for _ in range(self.THREAD_ROUNDS):
                    answers[tuple(sorted(self.threads(keys)[1]))] += 1
                self.assertEqual({expected: self.THREAD_ROUNDS}, dict(answers))


class ClaudeBootstrapRaceTests(unittest.TestCase):
    """The one unverified-billing Claude admission per account, asked for twice at once."""

    ROUNDS = 10

    def setUp(self):
        self.case = provider_fixtures.AdmissionTests("test_claude_bootstrap_admits_one_run_until_it_records_billing")
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.ledger = self.case.ledger

    def account(self, number):
        """A Claude account no run or probe was ever admitted for, attested by the owner."""
        fingerprint = self.case.observe("claude", email=f"fixture-race-{number}@example.invalid")[0]["account_fingerprint"]
        self.case.attest("claude", fingerprint)
        return fingerprint

    def decisions(self, fingerprint, record_type="admission_decision"):
        rows = self.ledger._connection.execute(
            "SELECT content FROM records WHERE record_type=? AND source='lifecycle'", (record_type,)).fetchall()
        records = [json.loads(bytes(row[0])) for row in rows]
        return [record for record in records if record["account_fingerprint"] == fingerprint]

    def both_evaluated_first(self):
        """Hold each opener after its unlocked admission check until both have passed it."""
        evaluate, barrier = self.ledger.evaluate_admission, threading.Barrier(2)

        def evaluated_together(*arguments, **options):
            decision = evaluate(*arguments, **options)
            barrier.wait(10)
            return decision

        return mock.patch.object(self.ledger, "evaluate_admission", side_effect=evaluated_together)

    def assert_one_bootstrap(self, raced, fingerprint, *, bindings):
        self.assertEqual(1, len(returned(raced)), raced)
        self.assertEqual([AdmissionDenied], [type(error) for error in raised(raced)], raced)
        self.assertEqual("billing_unverified", raised(raced)[0].reason_code)
        stored = self.decisions(fingerprint)
        passed = [record for record in stored if record["decision"] == "pass"]
        self.assertEqual(["bootstrap"], [record["admission_basis"] for record in passed])
        self.assertIsNotNone(passed[0]["run_id"])
        self.assertEqual([("deny", "billing_unverified", None)],
                         [(record["decision"], record["reason_code"], record["run_id"])
                          for record in stored if record["decision"] != "pass"])
        self.assertEqual(bindings + 1, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM host_run_bindings").fetchone()[0])
        self.assertEqual(1, self.ledger._connection.execute(
            "SELECT COUNT(*) FROM host_run_bindings WHERE run_id=?", (passed[0]["run_id"],)).fetchone()[0])
        self.assertFalse(self.ledger._connection.in_transaction)

    def bindings(self):
        return self.ledger._connection.execute("SELECT COUNT(*) FROM host_run_bindings").fetchone()[0]

    def test_two_runs_opened_at_once_on_a_new_claude_account_share_one_bootstrap(self):
        orchestration = self.ledger.open_orchestration(
            self.case.project_id, client_version="codex-host-v1", now_ms=provider_fixtures.NOW)
        for number in range(self.ROUNDS):
            with self.subTest(round=number):
                fingerprint, bindings = self.account(number), self.bindings()
                stamp = self.case.clock + 1
                # Odd rounds only start together; even rounds also both pass the unlocked check first.
                held = self.both_evaluated_first() if number % 2 == 0 else contextlib.nullcontext()
                with mock.patch.object(self.ledger, "_clock_ms", return_value=stamp), held:
                    raced = together(self, [lambda: self.case._open_run("claude", orchestration, stamp)] * 2)
                self.assert_one_bootstrap(raced, fingerprint, bindings=bindings)
                opened = returned(raced)[0]
                self.assertEqual("bootstrap", self.ledger.host_run_admission(
                    self.case.project_id, opened["run_handle"])["admission_basis"])
                # Until that run records billing the account stays closed to another run.
                with self.assertRaises(AdmissionDenied) as denied:
                    self.case.open_run("claude")
                self.assertEqual("billing_unverified", denied.exception.reason_code)
                self.assertEqual(bindings + 1, self.bindings())

    def test_two_projects_opening_at_once_on_one_new_claude_account_share_one_bootstrap(self):
        first = self.case.project_id
        root = Path(self.case.temporary.name) / "second-project"
        root.mkdir()
        second = self.ledger.register_project(root, now_ms=1)
        orchestrations = {project: self.ledger.open_orchestration(
            project, client_version="codex-host-v1", now_ms=provider_fixtures.NOW) for project in (first, second)}
        for number in range(self.ROUNDS):
            with self.subTest(round=number):
                fingerprint = self.account(number)
                with mock.patch.object(self.case, "project_id", second):
                    self.assertEqual(fingerprint, self.account(number))
                bindings, stamp = self.bindings(), self.case.clock + 1
                # The fixture reads its project when it opens, so each caller's arguments are
                # captured first and the two real opens then name their own project.
                arguments = {}
                for project in (first, second):
                    with mock.patch.object(self.ledger, "open_host_run") as opening, \
                            mock.patch.object(self.case, "project_id", project):
                        self.case._open_run("claude", orchestrations[project], stamp)
                    arguments[project] = opening.call_args.kwargs

                def open_in(project, arguments=arguments):
                    return self.ledger.open_host_run(project, **arguments[project])

                held = self.both_evaluated_first() if number % 2 == 0 else contextlib.nullcontext()
                with mock.patch.object(self.ledger, "_clock_ms", return_value=stamp), held:
                    raced = together(self, [lambda project=project: open_in(project) for project in (first, second)])
                self.assert_one_bootstrap(raced, fingerprint, bindings=bindings)

    def test_a_run_and_a_billing_probe_at_once_on_a_new_claude_account_share_one_bootstrap(self):
        orchestration = self.ledger.open_orchestration(
            self.case.project_id, client_version="codex-host-v1", now_ms=provider_fixtures.NOW)
        events = [{"status": "allowed", "rateLimitType": "five_hour", "utilization": 0.1, "isUsingOverage": False,
                   "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled"}]
        for number in range(self.ROUNDS):
            with self.subTest(round=number):
                fingerprint, bindings = self.account(number), self.bindings()
                stamp = self.case.clock + 1

                def probe():
                    return self.ledger.observe_claude_billing(
                        self.case.project_id, project_root=self.case.tools, environ=provider_fixtures.CLEAN)

                with trusted_tools(self.case.tools), mock.patch.object(self.ledger, "_clock_ms", return_value=stamp), \
                        mock.patch("codex_agents.claude_host.check_backend"), \
                        mock.patch("codex_agents.bridge.claude_billing_probe", return_value=(events, None)) as launched:
                    raced = together(self, [lambda: self.case._open_run("claude", orchestration, stamp), probe])
                passed = {record_type: [record["admission_basis"] for record in self.decisions(fingerprint, record_type)
                                        if record["decision"] == "pass"]
                          for record_type in ("admission_decision", "probe_admission")}
                bases = [*passed["admission_decision"], *passed["probe_admission"]]
                self.assertEqual(1, bases.count("bootstrap"), passed)
                (run_kind, run_value), (probe_kind, probe_value) = raced
                if passed["admission_decision"] == ["bootstrap"]:
                    # The run holds the bootstrap: the probe is refused and its client never starts.
                    self.assertEqual(("returned", "raised"), (run_kind, probe_kind), raced)
                    self.assertIsInstance(probe_value, AdmissionDenied)
                    self.assertEqual("billing_unverified", probe_value.reason_code)
                    self.assertEqual(([], 0), (passed["probe_admission"], launched.call_count))
                else:
                    # The probe holds the bootstrap and starts once. The run is refused, unless it
                    # was judged after the probe recorded clean billing: then it stands on that evidence.
                    self.assertEqual((["bootstrap"], 1, "returned"),
                                     (passed["probe_admission"], launched.call_count, probe_kind), raced)
                    self.assertEqual("launched", probe_value["probe"])
                    if run_kind == "returned":
                        self.assertEqual(["evidence"], passed["admission_decision"])
                    else:
                        self.assertIsInstance(run_value, AdmissionDenied)
                        self.assertEqual("billing_unverified", run_value.reason_code)
                        self.assertEqual([], passed["admission_decision"])
                self.assertEqual(bindings + len(passed["admission_decision"]), self.bindings())
                self.assertFalse(self.ledger._connection.in_transaction)
                self.case.clock = stamp


if __name__ == "__main__":
    unittest.main()
