"""Learning CLI tests, part keys_upgrade (CI round 6 split of test_learning_cli, test alignment 126; tests moved verbatim)."""
from __future__ import annotations

import contextlib
import io
import json
import base64
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents import learning_store
from codex_agents.contracts import ContractError
from codex_agents.learning_contracts import (
    candidate_base_digest,
    canonical_json,
    content_digest,
)
from codex_agents.learning_adapters import load_client_capabilities
from codex_agents.learning_store import (
    CollectorClient,
    CollectorServer,
    CollectorError,
    CollectorMaintenance,
    CollectorRestartError,
    LearningLedger,
    LearningState,
    authorize_collector_runtime,
    authorized_collector_runtime,
)
from cli import _RestartingLedger, _close_ledger
from cli import main as cli_main
from codex_agents.learning_engine import generate_candidate, runtime_fixture_digest
from codex_agents.providers import AdmissionDenied
from codex_agents.bridge import BridgeRefused
from tests.learning_support import (
    DIGEST_A,
    DIGEST_B,
    DIGEST_C,
    GRADER_CONFIG,
    assignment,
    champion_configuration,
    event_record,
    full_usage,
    admit_provider,
    fake_provider_tools,
    generate_test_owner_key,
    OWNER_SIGNATURE_NAMESPACE,
    trusted_tools,
    sign_owner_payload,
    result,
    routing_fields,
    verification_outcome,
)
from tests.learning_cli_support import LearningCliBase  # noqa: F401


class LearningCliTests(LearningCliBase):
    def test_replacing_the_owner_key_through_the_cli(self) -> None:
        try:
            before = self._run("learning-status")["owner_key"]
            self.assertEqual((before["state"], before["replacements"]), ("ok", 0))
            directory = Path(self.temporary.name) / "replacement"
            directory.mkdir()
            key, public = generate_test_owner_key(directory)
            self.assertNotIn("fingerprint", before)  # D36: status shows no key fingerprint
            self.assertIsNone(before["last_replaced_at_ms"])
            statement = b"owner statement"
            allowed = self.state_root / "owner-allowed-signers"

            def accepted(private: Path, name: str) -> bool:
                signature = sign_owner_payload(private, statement, name)
                try:
                    return subprocess.run(
                        ["ssh-keygen", "-Y", "verify", "-f", str(allowed), "-I", "codex-agents-owner",
                         "-n", OWNER_SIGNATURE_NAMESPACE, "-s", str(signature)],
                        input=statement, capture_output=True, check=False).returncode == 0
                finally:
                    signature.unlink(missing_ok=True)

            self.assertTrue(accepted(self.owner_private_key, "old-before"))
            replaced = self._run(
                "learning-replace-owner-key", "--public-key", f"{key}.pub"
            )["owner_key"]
            self.assertEqual((replaced["state"], replaced["replacements"]), ("ok", 1))
            self.assertNotIn("fingerprint", replaced)
            # L2.2: the replacement really changed the key: the old key's signature is refused, the new one's accepted.
            self.assertIsInstance(replaced["last_replaced_at_ms"], int)
            self.assertTrue(accepted(key, "new-after"))
            self.assertFalse(accepted(self.owner_private_key, "old-after"))
            after = self._run("learning-status")["owner_key"]
            self.assertEqual(after, replaced)
            fields = public.split()
            self.assertEqual(
                (self.state_root / "owner-allowed-signers").read_text(),
                f"codex-agents-owner {fields[0]} {fields[1]}\n",
            )
        finally:
            self._stop_collector()

    def test_status_and_key_replacement_carry_no_private_value_anywhere(self) -> None:
        # Review 040-screens-r1 MED-1 (owner decision 36): a sentinel scan over the whole output (stdout and
        # stderr, not one field) of status and the replacement, and over every saved file that is not the
        # ledger or the signer file (which hold the public key by design): no fingerprint, key text or home path.
        try:
            directory = Path(self.temporary.name) / "replacement"
            directory.mkdir()
            key, public = generate_test_owner_key(directory)
            sentinels = {"home path": str(Path.home())}
            for name, pub in (("old", self.owner_private_key.with_suffix(".pub")), ("new", Path(f"{key}.pub"))):
                listed = subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True, check=True).stdout
                fingerprint = listed.split()[1]
                self.assertTrue(fingerprint.startswith("SHA256:"), listed)
                sentinels[f"{name} fingerprint"] = fingerprint
                sentinels[f"{name} fingerprint body"] = fingerprint.split(":", 1)[1]
                sentinels[f"{name} key text"] = pub.read_text().split()[1]

            def output(*command: str) -> str:
                completed = subprocess.run(
                    [sys.executable, str(ORCHESTRATION_ROOT / "cli.py"), command[0], "--state-root", str(self.state_root),
                     "--project-root", str(self.project), *command[1:]],
                    text=True, capture_output=True, env=self._environment(), check=False)
                self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
                return completed.stdout + "\n--stderr--\n" + completed.stderr

            reports = {"status before": output("learning-status"),
                       "replacement": output("learning-replace-owner-key", "--public-key", f"{key}.pub"),
                       "status after": output("learning-status")}
            for report, text in reports.items():
                for name, value in sentinels.items():
                    with self.subTest(report=report, sentinel=name):
                        self.assertNotIn(value, text)
                        self.assertNotIn("fingerprint", json.loads(text.split("\n--stderr--\n")[0]).get("owner_key", {}))
            saved = [path for path in self.state_root.rglob("*")
                     if path.is_file() and path.name != "owner-allowed-signers"
                     and not path.name.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3"))
                     and "sqlite" not in path.name]
            self.assertTrue(saved)
            for path in saved:
                data = path.read_bytes()
                for name, value in sentinels.items():
                    with self.subTest(saved=path.name, sentinel=name):
                        self.assertNotIn(value.encode(), data)
            # The owner's own terminal output still names the key where it matters: status says whether a key is
            # set and how often it was replaced, which is what the owner needs to proceed or troubleshoot.
            owner_key = json.loads(reports["status after"].split("\n--stderr--\n")[0])["owner_key"]
            self.assertEqual(("ok", 1), (owner_key["state"], owner_key["replacements"]))
            self.assertIsInstance(owner_key["last_replaced_at_ms"], int)
        finally:
            self._stop_collector()

    def test_replace_key_report_has_exactly_the_three_allowed_fields(self) -> None:
        # Review 040-screens-r2 MED-2 (owner decision 36): the report is a whitelist, so a backend that grows or
        # leaks a field (a fingerprint, key text, a path, a passphrase) never reaches the owner's terminal.
        sentinels = ("SHA256:SENTINELFINGERPRINT0123", "AAAASENTINELKEYTEXT", "sentinel-passphrase-value",
                     "/srv/sentinel/owner-key", "-----BEGIN OPENSSH PRIVATE KEY-----SENTINEL")
        extra = {"fingerprint": sentinels[0], "public_key": sentinels[1], "passphrase": sentinels[2],
                 "path": sentinels[3], "private_key": sentinels[4], "signer_line": sentinels[1]}
        code, text = self._replace_key_report(
            {"state": "ok", "replacements": 2, "last_replaced_at_ms": 1234, **extra})
        self.assertEqual(0, code, text)
        report = json.loads(text)
        self.assertEqual({"ready", "owner_key"}, set(report))
        self.assertEqual({"state", "replacements", "last_replaced_at_ms"}, set(report["owner_key"]))
        self.assertEqual({"state": "ok", "replacements": 2, "last_replaced_at_ms": 1234}, report["owner_key"])
        for value in sentinels:
            self.assertNotIn(value, text)

    def test_replace_key_report_missing_a_field_is_a_clean_error_not_a_traceback(self) -> None:
        # Review 040-screens-r2 LOW: a backend that omits a whitelisted field gives the CLI's error report.
        code, text = self._replace_key_report({"state": "ok", "replacements": 1})
        self.assertEqual(2, code, text)
        report = json.loads(text)
        self.assertIs(False, report["ready"])
        self.assertIn("error", report)
        self.assertNotIn("Traceback", text)

    def test_key_replacement_output_carries_no_private_key_text_or_passphrase(self) -> None:
        # Review 040-screens-r2 LOW: the whole-output scan also seeds the private key's text and a passphrase
        # (in the environment the CLI runs in); neither may appear in the replacement's stdout or stderr.
        try:
            directory = Path(self.temporary.name) / "private-scan"
            directory.mkdir()
            key, _public = generate_test_owner_key(directory)
            private_body = "".join(key.read_text().splitlines()[1:-1])
            self.assertGreater(len(private_body), 40)
            passphrase = "sentinel-passphrase-value-r2"
            completed = subprocess.run(
                [sys.executable, str(ORCHESTRATION_ROOT / "cli.py"), "learning-replace-owner-key",
                 "--state-root", str(self.state_root), "--project-root", str(self.project),
                 "--public-key", f"{key}.pub"],
                text=True, capture_output=True,
                env=self._environment(SSH_KEY_PASSPHRASE=passphrase, ORCH_OWNER_PASSPHRASE=passphrase),
                check=False)
            self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
            output = completed.stdout + completed.stderr
            for value in (private_body, passphrase, str(key)):
                self.assertNotIn(value, output)
        finally:
            self._stop_collector()

    def test_registration_upgrades_an_earlier_versions_ledger_only_when_approved(self) -> None:
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        ledger = LearningLedger(LearningState(self.state_root, create=False))
        records = ledger._connection.execute("SELECT count(*) FROM records").fetchone()[0]
        downgrade_ledger_to_six(ledger)
        ledger.close()
        try:
            # Asked first: nothing is stopped, copied or changed until the owner approves.
            refused = self._run("learning-register", expected=2)
            self.assertEqual(("ledger_upgrade_required", 6, False), (
                refused["reason_code"], refused["ledger_schema_version"], refused["ready"]))
            self.assertIn("--upgrade-ledger", refused["error"])
            self.assertFalse((self.state_root / "ledger-before-upgrade").exists())
            self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
            upgraded = self._run("learning-register", "--upgrade-ledger")
            self.assertEqual((True, 6, learning_store.LEDGER_SCHEMA_VERSION, self.project_id), (
                upgraded["ready"], upgraded["ledger_upgraded_from"], upgraded["collector_schema_version"],
                upgraded["project_id"]))
            copy = Path(upgraded["ledger_copy"])
            self.assertEqual(self.state_root.resolve() / "ledger-before-upgrade", copy.parent)
            kept = sqlite3.connect(copy)
            try:
                self.assertEqual((6, records), (kept.execute("PRAGMA user_version").fetchone()[0],
                                                kept.execute("SELECT count(*) FROM records").fetchone()[0]))
            finally:
                kept.close()
            self.assertEqual(1, len(list((self.state_root / "backups" / self.project_id).glob("upgrade_schema6_*.sqlite3"))))
            # Approving again when there is nothing left to upgrade changes nothing.
            again = self._run("learning-register", "--upgrade-ledger")
            self.assertEqual((True, None, None), (again["ready"], again["ledger_upgraded_from"], again["ledger_copy"]))
            self.assertEqual([copy], list(copy.parent.iterdir()))
            self.assertTrue(self._run("learning-status")["active"])
        finally:
            self._stop_collector()

    def test_a_project_backup_is_streamed_so_its_size_never_meets_a_memory_bound(self) -> None:
        # Release 0.3.0 built each project's upgrade backup as one document in memory
        # and refused any over 128 MiB, so a busy project could never be upgraded. The
        # backup is now written and checked row by row: building that document at all
        # is a regression.
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        ledger = LearningLedger(LearningState(self.state_root, create=False))
        records = ledger._connection.execute("SELECT count(*) FROM records").fetchone()[0]
        downgrade_ledger_to_six(ledger)
        ledger.close()
        whole = mock.patch.object(learning_store, "_snapshot_project_partition",
                                  side_effect=AssertionError("a whole partition was built in memory"))
        try:
            with whole:
                upgraded = self._register_here("--upgrade-ledger")
            self.assertEqual((True, 6), (upgraded["ready"], upgraded["ledger_upgraded_from"]))
            [backup] = self._upgrade_backups()
            self.assertEqual(".sqlite3", backup.suffix)
            self.assertEqual(0o600, backup.stat().st_mode & 0o777)
            self._stop_collector()
            ledger = LearningLedger(LearningState(self.state_root, create=False))
            try:
                with whole:
                    verified = ledger.verify_sanitized_backup(self.project_id, file_id=backup.stem)
            finally:
                ledger.close()
            self.assertEqual((True, 6, records), (
                verified["verified"], verified["ledger_schema_version"], verified["counts"]["records"]))
        finally:
            self._stop_collector()

    def test_an_upgrade_that_cannot_fit_refuses_before_writing_any_backup(self) -> None:
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        ledger = LearningLedger(LearningState(self.state_root, create=False))
        try:
            earlier, later = self._later_large_project(ledger)
            downgrade_ledger_to_six(ledger)
            # The earlier project fits; the later one is made too large by a limit
            # between their sizes, so an upgrade that sized projects one at a time
            # would already have written the earlier one's backup.
            active = [row[0] for row in ledger._connection.execute(
                "SELECT project_id FROM projects WHERE status='active' ORDER BY project_id")]
            sizes = {project: learning_store.project_backup_bytes(ledger._connection, project, 6)
                     for project in active}
        finally:
            ledger.close()
        self.assertTrue(all(sizes[later] > size for project, size in sizes.items() if project != later))
        self.assertLess(active.index(earlier), active.index(later))
        try:
            for patch, expected in (
                    # The later project alone is over the per-project limit.
                    (mock.patch.object(learning_store, "MAX_PROJECT_BACKUP_BYTES", sizes[later] - 1),
                     {"project_number": active.index(later) + 1, "projects": len(active),
                      "needed_bytes": sizes[later], "limit_bytes": sizes[later] - 1}),
                    # Every project fits, but not all of them on this disk.
                    (mock.patch.object(learning_store, "_free_backup_bytes", return_value=1),
                     {"project_number": None, "projects": len(active), "available_bytes": 1})):
                with self.subTest(expected=sorted(expected)), patch:
                    refused = self._register_here("--upgrade-ledger", expected=2)
                self.assertEqual(("ledger_upgrade_capacity", False), (refused["reason_code"], refused["ready"]))
                for name, value in expected.items():
                    self.assertEqual(value, refused["capacity"][name], name)
                self.assertIn("nothing was changed", refused["error"])
                self.assertEqual([], self._upgrade_backups())
                self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
                kept = sqlite3.connect(self.state_root / "learning.sqlite3")
                try:
                    self.assertEqual(0, kept.execute(
                        "SELECT count(*) FROM stored_files WHERE file_id LIKE 'upgrade_schema6_%'").fetchone()[0])
                finally:
                    kept.close()
            # With room, the same records upgrade, every project backed up.
            upgraded = self._register_here("--upgrade-ledger")
            self.assertEqual((True, 6), (upgraded["ready"], upgraded["ledger_upgraded_from"]))
            self.assertEqual(len(active), len(self._upgrade_backups()))
        finally:
            self._stop_collector()

    def test_a_failed_upgrade_names_its_cause_only_in_plain_fixed_words(self) -> None:
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        try:
            for message, shown in (("project backup cannot represent records exactly",
                                    "project backup cannot represent records exactly"),
                                   # A path, or anything that could identify a project or account, is never shown.
                                   ("cannot read /srv/records/private/ledger", None),
                                   ("project prj_0123456789abcdef0123456789abcdef is not active", None)):
                ledger = LearningLedger(LearningState(self.state_root, create=False), allow_schema_upgrade=True)
                if learning_store.ledger_schema_behind(ledger.state) is None:
                    downgrade_ledger_to_six(ledger)
                ledger.close()
                with self.subTest(message=message), \
                        mock.patch.object(LearningLedger, "_initialize_launch_claims_locked", autospec=True,
                                          side_effect=learning_store.LedgerError(message)):
                    report = self._register_here("--upgrade-ledger", expected=2)
                self.assertEqual(("ledger_upgrade_failed", shown), (report["reason_code"], report["cause"]))
                self.assertNotIn("/srv/records", json.dumps(report))
                self.assertNotIn("prj_0123456789abcdef", json.dumps(report))
        finally:
            self._stop_collector()

    def test_a_failure_after_the_upgrade_is_not_reported_as_records_left_unchanged(self) -> None:
        import cli
        from tests.test_account_launch_claims import downgrade_ledger_to_six

        def register(patched, effect):
            ledger = LearningLedger(LearningState(self.state_root, create=False), allow_schema_upgrade=True)
            downgrade_ledger_to_six(ledger)
            ledger.close()
            stdout = io.StringIO()
            arguments = ["cli.py", "learning-register", "--state-root", str(self.state_root),
                         "--project-root", str(self.project), "--upgrade-ledger"]
            with mock.patch.object(sys, "argv", arguments), contextlib.redirect_stdout(stdout), \
                    mock.patch.object(cli, patched, side_effect=effect):
                code = cli_main()
            self.assertEqual(2, code, stdout.getvalue())
            return json.loads(stdout.getvalue())

        try:
            # The schema change is committed, then authorising the runtime fails.
            after = register("authorize_collector_runtime", OSError("disk"))
            self.assertEqual(("registration_incomplete_after_upgrade", 6), (
                after["reason_code"], after["ledger_schema_version"]))
            self.assertIn("were upgraded", after["error"])
            self.assertNotIn("as the earlier version left them", after["error"])
            self.assertTrue(Path(after["ledger_copy"]).is_file())
            self.assertIsNone(learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
            # The copy itself cannot be made: nothing was upgraded, and the report says so plainly.
            stdout = io.StringIO()
            ledger = LearningLedger(LearningState(self.state_root, create=False), allow_schema_upgrade=True)
            downgrade_ledger_to_six(ledger)
            ledger.close()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(cli, "copy_ledger_before_upgrade",
                                      side_effect=learning_store.LedgerError("the ledger could not be copied before its upgrade")):
                self.assertEqual(2, cli_main())
            uncopied = json.loads(stdout.getvalue())
            self.assertEqual(("ledger_upgrade_failed", None, None), (
                uncopied["reason_code"], uncopied["ledger_copy"], uncopied["state_moved"]))
            self.assertNotIn("copy", uncopied["error"])  # There is none to keep.
            # The upgrade and the registration went through and the collector then did not start:
            # still one report, with the copy, never a bare error.
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(cli, "ensure_collector", side_effect=cli.CollectorError("did not become healthy")):
                self.assertEqual(2, cli_main())
            unstarted = json.loads(stdout.getvalue())
            # Registration completed: it is not described as unfinished, and the cause is kept.
            self.assertEqual(("collector_not_started_after_registration", "did not become healthy"), (
                unstarted["reason_code"], unstarted["cause"]))
            self.assertIn("were upgraded and this project is registered", unstarted["error"])
            self.assertTrue(Path(unstarted["ledger_copy"]).is_file())
            # SQLite itself failing inside the migration is reported the same way as any other failure there.
            ledger = LearningLedger(LearningState(self.state_root, create=False))
            downgrade_ledger_to_six(ledger)
            ledger.close()
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(LearningLedger, "_initialize_launch_claims_locked", autospec=True,
                                      side_effect=sqlite3.OperationalError("disk I/O error")):
                self.assertEqual(2, cli_main())
            self.assertEqual("ledger_upgrade_failed", json.loads(stdout.getvalue())["reason_code"])
            self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
            self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
            # The upgrade went through, registration failed, and the ledger cannot then be read.
            readings = iter([6, 6])
            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(cli, "authorize_collector_runtime", side_effect=OSError("disk")), \
                    mock.patch.object(cli, "ledger_schema_behind", side_effect=lambda state: next(
                        readings, None) or (_ for _ in ()).throw(learning_store.LedgerError("unreadable"))):
                self.assertEqual(2, cli_main())
            unknown = json.loads(stdout.getvalue())
            self.assertEqual("ledger_upgrade_unknown", unknown["reason_code"])
            self.assertIn("do not delete anything; keep the copy", unknown["error"])
            self.assertTrue(Path(unknown["ledger_copy"]).is_file())
            ledger = LearningLedger(LearningState(self.state_root, create=False))
            downgrade_ledger_to_six(ledger)
            ledger.close()
            # The migration fails inside its transaction: the earlier records are as they were.
            initialize = LearningLedger._initialize_launch_claims_locked

            def fail(ledger, **arguments):
                initialize(ledger, **arguments)
                raise learning_store.LedgerError("fixture migration failure")

            stdout = io.StringIO()
            with mock.patch.object(sys, "argv", ["cli.py", "learning-register", "--state-root", str(self.state_root),
                                                 "--project-root", str(self.project), "--upgrade-ledger"]), \
                    contextlib.redirect_stdout(stdout), \
                    mock.patch.object(LearningLedger, "_initialize_launch_claims_locked", autospec=True, side_effect=fail):
                self.assertEqual(2, cli_main())
            before = json.loads(stdout.getvalue())
            self.assertEqual("ledger_upgrade_failed", before["reason_code"])
            self.assertIn("as the earlier version left them", before["error"])
            self.assertEqual(6, learning_store.ledger_schema_behind(LearningState(self.state_root, create=False)))
        finally:
            self._stop_collector()

    def test_a_ledger_is_found_and_copied_by_its_own_path_whatever_characters_that_holds(self) -> None:
        base = Path(self.temporary.name)
        for folder, decoy in (("st%41te", "stAte"), ("st?mode=rwc", "st"), ("st#x", "st")):
            root = base / folder
            state = LearningState(root)
            LearningLedger(state).close()
            connection = sqlite3.connect(state.database)
            connection.execute("PRAGMA user_version=6")
            connection.close()
            # What the path would name if it were read as a URI by hand: a current ledger.
            (base / decoy).mkdir(mode=0o700, exist_ok=True)
            other = sqlite3.connect(base / decoy / "learning.sqlite3")
            other.execute(f"PRAGMA user_version={learning_store.LEDGER_SCHEMA_VERSION}")
            other.execute("CREATE TABLE IF NOT EXISTS decoy(x)")
            other.commit()
            other.close()
            with self.subTest(folder=folder):
                self.assertEqual(6, learning_store.ledger_schema_behind(state))
                copy = learning_store.copy_ledger_before_upgrade(state, 6)
                self.assertEqual(root.resolve() / "ledger-before-upgrade", copy.parent)
                kept = sqlite3.connect(copy)
                try:
                    self.assertEqual(6, kept.execute("PRAGMA user_version").fetchone()[0])
                    self.assertIsNone(kept.execute("SELECT 1 FROM sqlite_master WHERE name='decoy'").fetchone())
                finally:
                    kept.close()
        # A ledger that is there but cannot be read is an error, never taken for a current one.
        broken = LearningState(base / "broken")
        LearningLedger(broken).close()
        broken.database.write_bytes(b"not a database, and long enough to be read as one" * 100)
        with self.assertRaisesRegex(learning_store.LedgerError, "cannot be read"):
            learning_store.ledger_schema_behind(broken)


if __name__ == "__main__":
    unittest.main()
