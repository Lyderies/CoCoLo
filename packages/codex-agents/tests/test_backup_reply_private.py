"""bkfix (decisions 36, 192): a backup command's reply names the backup by a fixed relative name and id, never
an absolute path or the home folder, so an AI primary that reads the reply learns no private value."""

import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

import cli
from tests import test_learning_source_image_legacy as legacy
from tests.test_owner_backup_streamed import TOOL


class BackupReplyPrivateTests(unittest.TestCase):
    setUp = legacy.LearningSourceImageLegacyTests.setUp

    def cli_reply(self, command, file_id):
        argv = ["cli.py", command, "--state-root", str(self.source.state.root), "--project-root", "/x",
                "--file-id", file_id]
        if command == "learning-backup-project":
            argv += ["--expires-in-seconds", "3600"]
        out = io.StringIO()
        with mock.patch.object(cli, "_learning_ledger", return_value=(self.source, self.project)), \
                mock.patch.object(cli, "_close_ledger"), mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out):
            code = cli.main()
        self.assertEqual(0, code, out.getvalue())
        return out.getvalue()

    def assert_private(self, text):
        self.assertNotIn(str(self.source.state.root), text)
        self.assertNotIn(os.path.expanduser("~"), text)
        self.assertNotIn(str(self.source.state.backups), text)

    def test_BK_1_the_cli_backup_reply_names_the_backup_without_a_path(self):
        text = self.cli_reply("learning-backup-project", "bk_cli")
        self.assert_private(text)
        report = json.loads(text)
        self.assertNotIn("path", report)
        self.assertEqual("bk_cli.sqlite3", report["backup_name"])

    def test_BK_2_the_verify_reply_has_no_path_and_restore_finds_it_by_id(self):
        self.cli_reply("learning-backup-project", "bk_ver")
        text = self.cli_reply("learning-verify-backup", "bk_ver")
        self.assert_private(text)
        self.assertTrue(json.loads(text)["verified"])

    def test_BK_3_the_owner_script_reply_names_the_backup_without_a_path(self):
        schema = int(self.source._connection.execute("PRAGMA user_version").fetchone()[0])
        report = TOOL["backup_partition"](self.source, self.project, "bk_owner", schema_version=schema,
                                          expires_at_ms=self.source._clock_ms() + 3_600_000)
        self.assert_private(json.dumps(report))
        self.assertNotIn("path", report)
        self.assertEqual("bk_owner.sqlite3", report["backup_name"])

    def test_BK_4_the_cli_reply_carries_the_file_id_and_it_round_trips_to_verify(self):
        report = json.loads(self.cli_reply("learning-backup-project", "bk_rt"))
        self.assertEqual("bk_rt", report["file_id"])
        again = json.loads(self.cli_reply("learning-verify-backup", report["file_id"]))
        self.assertTrue(again["verified"])
        self.assertEqual(report["backup_sha256"], again["backup_sha256"])

    def test_BK_5_streamed_verification_refuses_a_file_with_an_extra_hard_link(self):
        self.cli_reply("learning-backup-project", "bk_link")
        path = self.source.state.backups / self.project / "bk_link.sqlite3"
        extra = path.with_name("extra-link")
        os.link(path, extra)
        with self.assertRaises(Exception) as caught:
            self.source.verify_sanitized_backup(self.project, file_id="bk_link")
        self.assertNotIn(str(self.source.state.root), str(caught.exception))
        os.unlink(extra)
        self.assertTrue(self.source.verify_sanitized_backup(self.project, file_id="bk_link")["verified"])

    def test_BK_6_error_replies_carry_fixed_reasons_without_paths_or_raw_text(self):
        secret = str(self.source.state.root)
        reason = TOOL["_reason"]
        for error in (OSError(13, "denied", secret + "/x"), ValueError(secret),
                      KeyError(secret), RuntimeError("fixed text")):
            self.assertNotIn(secret, reason(error))
        self.assertEqual("the backup could not be completed (RuntimeError)", reason(RuntimeError("fixed text")))
        # the folder check and a stuck cleanup name no path
        link = self.source.state.root / "a_link"
        os.symlink(self.source.state.root, link)
        with self.assertRaises(RuntimeError) as caught:
            TOOL["_private_folder"](link, owner_only=True)
        self.assertNotIn(secret, str(caught.exception))

    def test_BK_7_only_allowlisted_reasons_pass_through(self):
        secret = str(self.source.state.root)
        reason = TOOL["_reason"]
        self.assertNotIn(secret, reason(RuntimeError(f"cannot write {secret}/x")))
        self.assertNotIn(secret, reason(TOOL["QuiesceLeftStopped"](secret)))
        self.assertEqual("a backup with this file id already exists",
                         reason(RuntimeError("a backup with this file id already exists")))

    def test_BK_8_a_child_error_with_a_path_is_not_relayed(self):
        secret = str(self.source.state.root)
        real = __import__("subprocess").Popen

        def child(argv, *args, **kwargs):
            code = "import json,sys; print(json.dumps({'error': 'cannot open %s/t'})); sys.exit(2)" % secret
            return real([sys.executable, "-I", "-c", code], *args, **kwargs)
        schema = int(self.source._connection.execute("PRAGMA user_version").fetchone()[0])
        with mock.patch("subprocess.Popen", child), self.assertRaises(Exception) as caught:
            TOOL["backup_partition"](self.source, self.project, "bk_child", schema_version=schema,
                                     expires_at_ms=self.source._clock_ms() + 3_600_000)
        self.assertNotIn(secret, str(caught.exception))

    def test_BK_9_verify_of_a_missing_registered_file_gives_a_fixed_reason(self):
        self.cli_reply("learning-backup-project", "bk_gone")
        os.unlink(self.source.state.backups / self.project / "bk_gone.sqlite3")
        with self.assertRaises(Exception) as caught:
            self.source.verify_sanitized_backup(self.project, file_id="bk_gone")
        self.assertNotIn(str(self.source.state.root), str(caught.exception))
        self.assertNotIn("FileNotFoundError", type(caught.exception).__name__)

    def test_BK_10_the_cli_prints_a_fixed_reason_code_for_a_caught_exception(self):
        secret = str(self.source.state.root)
        argv = ["cli.py", "learning-verify-backup", "--state-root", secret, "--project-root", "/x",
                "--file-id", "bk_x"]
        out = io.StringIO()
        with mock.patch.object(cli, "_learning_ledger", side_effect=cli.LedgerError(f"broken {secret}/db")), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            code = cli.main()
        self.assertEqual(2, code)
        self.assertNotIn(secret, out.getvalue())
        self.assertFalse(json.loads(out.getvalue())["ready"])

    def test_BK_11_the_cli_shows_a_fixed_refusal_message_with_its_reason_code(self):
        argv = ["cli.py", "learning-verify-backup", "--state-root", str(self.source.state.root),
                "--project-root", "/x", "--file-id", "bk_x"]
        out = io.StringIO()
        with mock.patch.object(cli, "_learning_ledger",
                               side_effect=cli.LedgerError("a child run is active; nothing was changed")), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            self.assertEqual(2, cli.main())
        report = json.loads(out.getvalue())
        self.assertEqual("a child run is active; nothing was changed", report["error"])
        self.assertEqual("ledger_refused", report["reason_code"])
        # the same exception carrying a path is replaced (BK-10), and so is one naming the home folder
        out = io.StringIO()
        with mock.patch.object(cli, "_learning_ledger",
                               side_effect=cli.LedgerError("cannot open " + os.path.expanduser("~") + "/db")), \
                mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            cli.main()
        self.assertNotIn(os.path.expanduser("~"), out.getvalue())


if __name__ == "__main__":
    unittest.main()
