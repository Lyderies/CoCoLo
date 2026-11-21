"""The Claude package reports its own writes (owner fix 1, finding r4-1 "observation is not write authority").

R1: an applied operation's reply names every path the package wrote, created or removed, with the sha256 it
    wrote, and every folder it created with its identity; the caller builds its undo journal from that.
R4: rollback_state is a structured field of the failure, set from the package's own state, never read from
    the error text (r4 verifier: "the producer is an exact string match").
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from claude_agents import packaging
from claude_agents.packaging import PackageError, package_operation

SOURCE = Path(__file__).resolve().parents[1]
SCRIPT = SOURCE / "scripts" / "package.py"


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_cli():
    spec = importlib.util.spec_from_file_location("claude_package_cli_writes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackageWritesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.target = Path(self.temp.name).resolve() / "target"
        subprocess.run(["/usr/bin/git", "init", "--quiet", "--template=", str(self.target)], check=True,
                       env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})

    def fail_link(self, at: int, before_failing=None):
        """A real failure inside the package: its `at`-th new-file link raises EIO (earlier ones really happen)."""
        real_link = os.link
        calls = []

        def link(*args, **kwargs):
            calls.append(args)
            if len(calls) == at:
                if before_failing:
                    before_failing()
                raise OSError(errno.EIO, "injected write failure")
            return real_link(*args, **kwargs)

        return patch.object(packaging.os, "link", side_effect=link)

    def test_an_applied_install_reports_every_path_it_wrote_with_the_digest_on_disk(self) -> None:
        """R1. Fails on 1d359e3b: the reply has no 'writes' (the caller inferred them by reading files back)."""
        report = package_operation(SOURCE, self.target, "install", apply=True)
        writes = {entry["path"]: entry for entry in report["writes"]}
        self.assertEqual(set(packaging.bundle_paths()) | {packaging.MANIFEST}, set(writes))
        for path, entry in writes.items():
            self.assertIsNone(entry["before"], path)
            self.assertEqual(sha((self.target / path).read_bytes()), entry["after"], path)
        self.assertNotIn(packaging.LOCK, writes)  # the lock and marker are gone again: not a net write
        self.assertNotIn(packaging.PENDING, writes)
        folders = {entry["path"]: entry for entry in report["folders_created"]}
        self.assertIn(".claude/claude-agents/lib/claude_agents", folders)
        for path, entry in folders.items():
            info = os.stat(self.target / path)
            self.assertEqual((info.st_dev, info.st_ino), (entry["device"], entry["inode"]), path)

    def test_an_apply_that_changes_nothing_reports_no_writes(self) -> None:
        """R1, r4 finding 'an already-current update creates a false journal entry'. Fails on 1d359e3b: no 'writes'."""
        package_operation(SOURCE, self.target, "install", apply=True)
        again = package_operation(SOURCE, self.target, "install", apply=True)
        self.assertEqual("unchanged", again["result"])
        self.assertEqual([], again["writes"])
        self.assertEqual([], again["folders_created"])

    def test_existing_agent_folders_are_never_reported_as_created(self) -> None:
        """r5 verifier guard: exercise the producer rather than supplying folder identities."""
        keep = self.target / ".claude/agents"
        keep.mkdir(parents=True)
        report = package_operation(SOURCE, self.target, "install", apply=True)
        created = {entry["path"] for entry in report["folders_created"]}
        self.assertNotIn(".claude", created)
        self.assertNotIn(".claude/agents", created)
        self.assertIn(".claude/skills", created)

    def test_reported_modes_are_the_modes_written_under_the_callers_umask(self) -> None:
        """The mode comes from the created descriptor, including the caller's umask."""
        previous = os.umask(0o077)
        try:
            report = package_operation(SOURCE, self.target, "install", apply=True)
        finally:
            os.umask(previous)
        for entry in report["writes"]:
            self.assertEqual(0o600, entry["mode"], entry["path"])
            self.assertEqual((self.target / entry["path"]).stat().st_mode & 0o7777, entry["mode"])

    def test_a_partial_remove_reports_a_mode_only_change_left_by_its_own_undo(self) -> None:
        """Restoring bytes internally can still change a saved mode; the umbrella must see that write."""
        package_operation(SOURCE, self.target, "install", apply=True)
        first = ".claude/agents/claude_agents_implementation_worker.md"
        (self.target / first).chmod(0o600)
        real_unlink = os.unlink
        failed = False

        def fail_once(name, *args, **kwargs):
            nonlocal failed
            if name == "claude_agents_planner.md" and not failed:
                failed = True
                raise OSError(errno.EIO, "injected removal failure")
            return real_unlink(name, *args, **kwargs)

        with patch.object(packaging.os, "unlink", side_effect=fail_once), self.assertRaises(PackageError) as caught:
            package_operation(SOURCE, self.target, "remove", apply=True)
        self.assertEqual("none", caught.exception.rollback_state)
        writes = {entry["path"]: entry for entry in caught.exception.writes}
        self.assertIn(first, writes)
        self.assertEqual(writes[first]["before"], writes[first]["after"])
        self.assertEqual((self.target / first).stat().st_mode & 0o7777, writes[first]["mode"])

    def test_a_real_failure_partway_reports_that_nothing_it_wrote_is_left(self) -> None:
        """R1 with a real mid-step failure (owner fix 6): the third new file fails inside the package after two were
        written; the package undoes its own two, so its failure says no write is left, rollback_state 'none', and
        names the folders it created. Fails on 1d359e3b: the failure carries no write report."""
        with self.fail_link(3), self.assertRaises(PackageError) as caught:
            package_operation(SOURCE, self.target, "install", apply=True)
        failure = caught.exception
        self.assertEqual("operation_failed", str(failure))
        self.assertEqual([], failure.writes)
        self.assertEqual("none", failure.rollback_state)
        self.assertIn(".claude/agents", {entry["path"] for entry in failure.folders_created})
        self.assertFalse(any((self.target / path).exists() for path in packaging.bundle_paths()))

    def test_a_failure_the_package_cannot_undo_reports_pending_and_what_it_left(self) -> None:
        """R4 producer-level (r4: 'give rollback_state a producer-level test'): a real failure partway, while another
        writer changes a file the package wrote, leaves the package's marker. The CLI says rollback_state 'pending'
        and its writes name the marker and the changed file with the digest the package wrote, not the other
        writer's. Fails on 1d359e3b: the reply has no 'writes'."""
        first = self.target / ".claude/agents/claude_agents_implementation_worker.md"
        cli = load_cli()
        out = io.StringIO()
        with self.fail_link(2, before_failing=lambda: first.write_bytes(b"another writer\n")), \
                patch("sys.argv", ["package.py", "install", "--target", str(self.target), "--apply"]), \
                contextlib.redirect_stdout(out):
            code = cli.main()
        reply = json.loads(out.getvalue())
        self.assertEqual(2, code)
        self.assertEqual("rollback_pending", reply["error"])
        self.assertEqual("pending", reply["rollback_state"])
        writes = {entry["path"]: entry for entry in reply["writes"]}
        marker = (self.target / packaging.PENDING).read_bytes()
        self.assertEqual(sha(marker), writes[packaging.PENDING]["after"])
        written = dict(packaging.compile_bundle(SOURCE).files)[first.relative_to(self.target).as_posix()]
        self.assertEqual(sha(written), writes[first.relative_to(self.target).as_posix()]["after"])
        self.assertEqual(b"another writer\n", first.read_bytes())

    def test_the_cli_reports_the_state_the_failure_carries_whatever_its_text(self) -> None:
        """R4: the field comes from the failure's own state, not its words. Fails on 1d359e3b: the CLI compares the
        text with 'rollback_pending' and says 'none'."""
        failure = PackageError("operation_failed")
        failure.rollback_state = "pending"
        cli = load_cli()
        out = io.StringIO()
        with patch.object(packaging, "package_operation", side_effect=failure), \
                patch("sys.argv", ["package.py", "update", "--target", "/nonexistent"]), \
                contextlib.redirect_stdout(out):
            cli.main()
        self.assertEqual("pending", json.loads(out.getvalue())["rollback_state"])


if __name__ == "__main__":
    unittest.main()
