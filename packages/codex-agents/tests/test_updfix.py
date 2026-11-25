"""UPDFIX (docs/requirements/updfix.md): an update that cannot go ahead is decided before anything is written,
and a second registration changes nothing.

UPDFIX-2 `learning-update-check`: read-only, says whether a child runs and whether the update would have to replace
the collector or the project's service. UPDFIX-5: registering a project on a running collector keeps the project's
wake key (L1.2).
"""

import hashlib
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_learning_store as store
from tests.support import ORCHESTRATION_ROOT

from codex_agents import learning_store, service_enrollment
from codex_agents.collector_paths import CollectorPaths

HOUR = 60 * 60 * 1000


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        self.fixture = store.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.project = self.fixture.project_a_id
        self.connection = self.fixture.ledger._connection

    def run_check(self, *, state_root=None):
        env = {key: value for key, value in os.environ.items()
               if not any(word in key.upper() for word in ("API_KEY", "ACCESS_TOKEN", "SECRET"))}
        for name in ("CODEX_THREAD_ID", "CODEX_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID"):
            env.pop(name, None)
        done = subprocess.run(
            [sys.executable, "-I", "-B", "-S", str(ORCHESTRATION_ROOT / "cli.py"), "learning-update-check",
             "--state-root", str(state_root or self.fixture.state_root),
             "--project-root", str(self.fixture.project_a)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=90, check=False)
        return done.returncode, json.loads(done.stdout or b"{}"), done.stderr.decode(errors="replace")

    def claim(self, handle="a", *, hours=1):
        """An unreleased claimed run whose deadline is still ahead: its child may be running."""
        now = time.time_ns() // 1_000_000
        reference = time.monotonic_ns()
        digest = handle * 64
        with self.fixture.ledger._account_transaction():
            self.connection.execute("INSERT INTO launch_claims VALUES(?,?,?,'claimed',?,?)",
                                    (self.project, digest, "run-" + handle, now, "brg_" + handle * 32))
            self.connection.execute(
                "INSERT INTO active_run_capacity(project_id,handle_digest,run_id,bridge_run_id,kind,provider,"
                "account_fingerprint,runtime_digest,policy_digest,claimed_at_ms,deadline_at_ms,clock_reference_ns,"
                "execution_deadline_ns,clock_owner_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.project, digest, "run-" + handle, "brg_" + handle * 32, "claimed", "codex", "7" * 64,
                 "6" * 64, "8" * 64, now, now + hours * HOUR, reference, reference + hours * HOUR * 1_000_000,
                 "9" * 64))

    def test_UPDFIX_2_no_state_at_all_is_not_a_refusal(self):
        code, report, _ = self.run_check(state_root=Path(self.fixture.temporary.name) / "no-such-state")
        self.assertEqual(0, code, report)
        self.assertEqual((False, False, False), (report["children_running"], report["would_replace"], report["refused"]))

    def test_UPDFIX_2_an_idle_ledger_and_a_run_admitted_but_never_claimed_are_not_children(self):
        code, report, _ = self.run_check()
        self.assertEqual(0, code, report)
        self.assertIs(False, report["children_running"])
        # D1: a run admitted whose binding is still pending has no child (it launches only after its claim).
        self.connection.execute(
            "INSERT INTO host_run_bindings(project_id,handle_digest,run_id,assignment_digest,task_digest,"
            "route_digest,configuration_digest,client_version,created_at_ms,expires_at_ms,binding_state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (self.project, "b" * 64, "pending", "b" * 64, "c" * 64, "d" * 64, "e" * 64, "fixture", 1, 2 ** 62,
             "pending")).close()
        code, report, _ = self.run_check()
        self.assertEqual(0, code, report)
        self.assertIs(False, report["children_running"])

    def test_UPDFIX_2_a_claimed_unreleased_run_is_a_child_that_may_be_running(self):
        self.claim()
        code, report, _ = self.run_check()
        self.assertEqual(0, code, report)
        self.assertIs(True, report["children_running"])
        # No collector runs here and no service of another version: nothing would be replaced, so not refused.
        self.assertEqual((False, False), (report["would_replace"], report["refused"]))

    def test_UPDFIX_2_a_live_child_is_seen_even_when_an_unreleased_earlier_version_slot_exists(self):
        # Review r1 HIGH: the ledger's legacy deadline functions were not registered on the check's read-only connection,
        # the SQL error was swallowed, and a live claimed child was reported as no child.
        now = time.time_ns() // 1_000_000
        other = self.fixture.project_b_id
        self.connection.execute("INSERT INTO launch_claims VALUES(?,?,?,?,?,?)",
                                (other, "c" * 64, "unknown", "legacy_fenced", now, None)).close()
        self.connection.execute("INSERT INTO active_run_capacity(project_id,handle_digest,run_id,kind,claimed_at_ms) "
                                "VALUES(?,?,?,?,?)", (other, "c" * 64, "unknown", "legacy_unknown", now)).close()
        self.claim()
        code, report, _ = self.run_check()
        self.assertEqual(0, code, report)
        self.assertIs(True, report["children_running"], report)
        self.assertIs(False, report["unreadable"], report)

    def test_UPDFIX_2_the_legacy_slot_functions_are_registered_by_the_collectors_own_function(self):
        import sqlite3
        from codex_agents.legacy_slots import register_legacy_functions
        connection = sqlite3.connect(":memory:")
        register_legacy_functions(connection)
        self.assertEqual("r", connection.execute("SELECT legacy_dispatch_run_id('{\"run_id\": \"r\"}')").fetchone()[0])

    def test_UPDFIX_2_a_ledger_that_exists_but_cannot_be_evaluated_refuses_with_a_next_step(self):
        # Decision 265: fail closed. An evaluation error is never swallowed into "no child".
        import cli
        import sqlite3
        with mock.patch("codex_agents.learning_source_image._drained", side_effect=sqlite3.OperationalError("no such function")):
            report = cli._update_check_report(self.fixture.state_root, self.fixture.project_a)
        self.assertEqual((True, True, "ledger_unreadable"), (report["unreadable"], report["refused"], report["reason"]))
        self.assertIn("nothing was changed", report["message"])
        self.assertIn("try again", report["message"])

    def test_UPDFIX_2_a_ledger_file_that_is_not_a_database_refuses(self):
        root = Path(self.fixture.temporary.name) / "garbage-state"
        root.mkdir(mode=0o700)
        database = root / "learning.sqlite3"
        database.write_bytes(b"this is not a sqlite database" * 100)
        os.chmod(database, 0o600)
        code, report, _ = self.run_check(state_root=root)
        self.assertEqual(0, code, report)
        self.assertEqual((True, True, "ledger_unreadable"), (report["unreadable"], report["refused"], report["reason"]))

    def test_UPDFIX_2_a_state_folder_with_no_ledger_is_not_a_refusal(self):
        root = Path(self.fixture.temporary.name) / "empty-state"
        root.mkdir(mode=0o700)
        code, report, _ = self.run_check(state_root=root)
        self.assertEqual(0, code, report)
        self.assertEqual((False, False, False), (report["unreadable"], report["refused"], report["children_running"]))

    # Review r2c (decision 269 and the fail-open, busy findings).

    def test_UPDFIX_2_a_ledger_of_an_older_release_without_the_capacity_tables_is_not_a_refusal(self):
        # Decision 269: an older ledger has no active_run_capacity table, so it cannot hold a child this check could see;
        # the collector's own drained stop at step 5 still refuses a running old child.
        self.fixture.ledger.close()
        import sqlite3
        raw = sqlite3.connect(self.fixture.state.database)
        raw.execute("DROP TABLE active_run_capacity")
        raw.commit()
        raw.close()
        code, report, _ = self.run_check()
        self.assertEqual(0, code, report)
        self.assertEqual((False, False, False, None), (report["children_running"], report["unreadable"],
                                                       report["refused"], report["reason"]), report)

    def test_UPDFIX_2_a_capacity_table_that_cannot_be_read_still_refuses(self):
        # Only a MISSING table proceeds. A table that exists but has the wrong shape is a schema failure: closed.
        self.fixture.ledger.close()
        import sqlite3
        raw = sqlite3.connect(self.fixture.state.database)
        raw.execute("DROP TABLE active_run_capacity")
        raw.execute("CREATE TABLE active_run_capacity(something_else TEXT)")
        raw.commit()
        raw.close()
        code, report, _ = self.run_check()
        self.assertEqual(0, code, report)
        self.assertEqual((True, True, "ledger_unreadable"), (report["unreadable"], report["refused"], report["reason"]))

    def test_UPDFIX_2_an_existing_ledger_in_a_folder_that_cannot_be_looked_into_refuses(self):
        # Review r2c: os.path.lexists hid a permission error, so an inaccessible existing ledger was treated as absent.
        root = Path(self.fixture.temporary.name) / "closed-state"
        root.mkdir(mode=0o700)
        (root / "learning.sqlite3").write_bytes(b"x")
        os.chmod(root, 0)
        self.addCleanup(os.chmod, root, 0o700)
        if os.access(root / "learning.sqlite3", os.R_OK):
            self.skipTest("this account can read through a mode 0 folder")
        code, report, _ = self.run_check(state_root=root)
        self.assertEqual(0, code, report)
        self.assertEqual((True, True, "ledger_unreadable"), (report["unreadable"], report["refused"], report["reason"]))

    def hold_exclusive(self, release_after):
        """A rollback-journal ledger with a writer holding an exclusive lock; released after `release_after` seconds
        (None: until the test releases it). Returns the release function."""
        import sqlite3
        import threading
        self.fixture.ledger.close()
        raw = sqlite3.connect(self.fixture.state.database)
        raw.execute("PRAGMA journal_mode=DELETE").fetchall()
        raw.close()
        writer = sqlite3.connect(self.fixture.state.database, isolation_level=None, check_same_thread=False)
        writer.execute("BEGIN EXCLUSIVE")
        released = threading.Event()

        def release():
            if not released.is_set():
                released.set()
                writer.execute("ROLLBACK")
                writer.close()
        self.addCleanup(release)
        if release_after is not None:
            timer = threading.Timer(release_after, release)
            timer.start()
            self.addCleanup(timer.cancel)
        return release

    def test_UPDFIX_2_a_brief_writer_lock_is_waited_for_not_refused(self):
        # Review r2c: normal collector maintenance holds the ledger briefly; that must not refuse an update.
        import cli
        self.hold_exclusive(0.4)
        with mock.patch.object(cli, "UPDATE_CHECK_BUSY_SECONDS", 3.0):
            report = cli._update_check_report(self.fixture.state_root, self.fixture.project_a)
        self.assertEqual((False, False, False), (report["unreadable"], report["refused"], report["children_running"]), report)

    def test_UPDFIX_2_a_busy_ledger_is_tried_once_more_then_refuses_with_the_plain_reason(self):
        import cli
        import sqlite3
        release = self.hold_exclusive(None)
        with mock.patch.object(cli, "UPDATE_CHECK_BUSY_SECONDS", 0.2):
            report = cli._update_check_report(self.fixture.state_root, self.fixture.project_a)
        self.assertEqual((True, True, "ledger_unreadable"), (report["unreadable"], report["refused"], report["reason"]))
        self.assertIn("try again", report["message"])
        # One retry, not an unbounded wait: a busy error on the first try, a clean answer on the second.
        calls = []

        def busy_once(connection, now, **kwargs):
            calls.append(now)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
        release()
        with mock.patch("codex_agents.learning_source_image._drained", side_effect=busy_once):
            report = cli._update_check_report(self.fixture.state_root, self.fixture.project_a)
        self.assertEqual(2, len(calls))
        self.assertEqual((False, False), (report["unreadable"], report["refused"]), report)

    def decide(self, *, health=None, other_host=None):
        """The command's own decision, in this process, with the collector's identity and the service's version given."""
        import cli
        from codex_agents.collector_client import CollectorClient
        with mock.patch.object(CollectorClient, "health", side_effect=health or (lambda self=None, *a, **k: {})), \
                mock.patch.object(cli, "other_version_host", return_value=other_host):
            return cli._update_check_report(self.fixture.state_root, self.fixture.project_a)

    def test_UPDFIX_2_refused_needs_both_a_child_and_a_replacement(self):
        from codex_agents.learning_store import CollectorBuildConflictError, CollectorError, CollectorUpgradeRequired
        older = CollectorUpgradeRequired("older", code="runtime_upgrade_required")
        conflict = CollectorBuildConflictError("other build", code="runtime_build_conflict")
        down = CollectorError("not running", code="collector_transport_unavailable")
        for children in (False, True):
            if children:
                self.claim()
            for label, kwargs, replace in (
                    ("same build, no other service", dict(), False),
                    ("an older collector", dict(health=older), True),
                    ("another build of this version", dict(health=conflict), True),
                    ("a service of another version", dict(other_host=Path("/service")), True),
                    ("no collector running", dict(health=down), False)):
                with self.subTest(children=children, case=label):
                    report = self.decide(**kwargs)
                    self.assertEqual((children, replace, children and replace),
                                     (report["children_running"], report["would_replace"], report["refused"]))

    def test_UPDFIX_2_the_check_changes_nothing(self):
        # The ledger's own contents (rows, size, mtime) and every other file of the state folder are unchanged. SQLite's -wal and
        # -shm sidecars are left out on purpose: reading a write-ahead ledger may create or touch them (updfix.md, UPDFIX-1).
        self.claim()
        state = self.fixture.state
        names = sorted(path for path in state.root.rglob("*") if path.is_file())
        before = {str(path): sha(path) for path in names if path.suffix not in (".sock",)
                  and not path.name.startswith("learning.sqlite3")}
        database_before = (state.database.stat().st_size, state.database.stat().st_mtime_ns)
        self.run_check()
        after_names = sorted(path for path in state.root.rglob("*") if path.is_file())
        self.assertEqual([str(path) for path in names], [str(path) for path in after_names])
        self.assertEqual(before, {str(path): sha(path) for path in after_names if path.suffix not in (".sock",)
                                  and not path.name.startswith("learning.sqlite3")})
        self.assertEqual(database_before, (state.database.stat().st_size, state.database.stat().st_mtime_ns))
        self.assertEqual(1, self.connection.execute(
            "SELECT COUNT(*) FROM active_run_capacity WHERE released_at_ms IS NULL").fetchone()[0])


class WakeKeyKeptTests(unittest.TestCase):
    def setUp(self):
        self.fixture = store.LearningStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.paths = CollectorPaths(self.fixture.state.root)
        self.root = self.fixture.project_a
        self.server = learning_store.CollectorServer.__new__(learning_store.CollectorServer)
        self.server.ledger = self.fixture.ledger
        self.server.state = self.fixture.state

    def register(self):
        reply = self.server._dispatch({"protocol": learning_store.COLLECTOR_PROTOCOL_VERSION, "op": "register",
                                       "project_root": str(self.root),
                                       "runtime_digest": learning_store.COLLECTOR_RUNTIME_DIGEST})
        self.assertTrue(reply["ok"], reply)
        return reply

    def wake_file(self):
        return service_enrollment.wake_capability_path(self.paths, self.root)

    def test_UPDFIX_5_registering_again_keeps_the_wake_key_file_and_the_collectors_key(self):
        self.register()
        path = self.wake_file()
        first = (path.read_bytes(), os.stat(path).st_mode, os.stat(path).st_ino)
        project_id = self.fixture.project_a_id
        subject = str(service_enrollment._canonical_project_root(self.root))
        self.assertEqual((project_id, first[0]), service_enrollment.wake_keys(self.paths).lookup(subject))
        self.register()
        self.register()
        self.assertEqual(first, (path.read_bytes(), os.stat(path).st_mode, os.stat(path).st_ino))
        self.assertEqual((project_id, first[0]), service_enrollment.wake_keys(self.paths).lookup(subject))

    def test_UPDFIX_5_a_missing_or_invalid_wake_key_is_issued(self):
        self.register()
        path = self.wake_file()
        old = path.read_bytes()
        path.unlink()
        self.register()
        fresh = path.read_bytes()
        self.assertEqual(service_enrollment.KEY_BYTES, len(fresh))
        self.assertNotEqual(old, fresh)
        path.write_bytes(b"short")
        os.chmod(path, 0o600)
        self.register()
        self.assertEqual(service_enrollment.KEY_BYTES, len(path.read_bytes()))
        self.assertEqual(0o600, os.stat(path).st_mode & 0o777)
        path.chmod(0o644)  # a key another account could read is not kept
        self.register()
        self.assertEqual(0o600, os.stat(path).st_mode & 0o777)

    def test_UPDFIX_5_a_collector_start_still_issues_a_fresh_key(self):
        self.register()
        before = self.wake_file().read_bytes()
        service_enrollment.issue_wake_capability(self.paths, self.root, self.fixture.project_a_id)
        self.assertNotEqual(before, self.wake_file().read_bytes())


if __name__ == "__main__":
    unittest.main()
