"""Review 040-s5-wizard-2 (c): the owner tools the wizard uses read project files within bounds, write
whole files, keep a Git root's spaces, and stop a provider client's group when they are stopped.

Runs Git and a fake client, so it is an integration test.
"""

import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"owner_tool_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load("install")
attest = load("attest")


class InstallBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(os.path.realpath(directory.name))

    def test_a_managed_file_larger_than_the_limit_is_refused_not_read_whole(self) -> None:
        (self.base / ".codex").mkdir()
        big = self.base / ".codex" / "big"
        with open(big, "wb") as handle:
            handle.truncate(install.WORKTREE_READ_LIMIT + 1)
        root = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, root)
        with self.assertRaises(install.InstallRefused):
            install._worktree_entry(root, ".codex/big")
        (self.base / ".codex" / "small").write_bytes(b"ok")
        self.assertEqual(("100644", b"ok"), install._worktree_entry(root, ".codex/small"))

    def test_a_short_write_is_completed(self) -> None:
        directory = os.open(self.base, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, directory)
        real_write = os.write

        def short_write(fd, data):
            return real_write(fd, bytes(data[:2]))
        with mock.patch.object(install.os, "write", short_write):
            install._write_new(directory, "settings.json", b"abcdef", 0o644)
        self.assertEqual(b"abcdef", (self.base / "settings.json").read_bytes())

    def test_the_git_root_keeps_its_trailing_spaces(self) -> None:
        project = self.base / "proj "
        project.mkdir()
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        self.assertEqual(str(project), install._git(project, "rev-parse", "--show-toplevel"))


class AttestBoundsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(os.path.realpath(directory.name))

    def test_copy_input_writes_the_whole_snapshot(self) -> None:
        (self.base / "checks.json").write_bytes(b"abcdef")
        real_write = os.write

        def short_write(fd, data):
            return real_write(fd, bytes(data[:2]))
        with mock.patch.object(attest.os, "write", short_write):
            attest.copy_input(self.base / "checks.json", self.base / "copy.json")
        self.assertEqual(b"abcdef", (self.base / "copy.json").read_bytes())

    def test_the_routing_read_is_bounded_and_never_follows_a_link(self) -> None:
        target = self.base / "project"
        (target / ".codex" / "orchestration").mkdir(parents=True)
        routing = target / ".codex" / "orchestration" / "provider-routing.json"
        (self.base / "elsewhere.json").write_text('{"providers": {"claude": {"enabled": true}}}')
        routing.symlink_to(self.base / "elsewhere.json")
        with self.assertRaises(attest.AttestError):
            attest.enabled_providers(target)
        routing.unlink()
        routing.write_text(" " * (attest.ROUTING_READ_LIMIT + 1))
        with self.assertRaises(attest.AttestError):
            attest.enabled_providers(target)
        routing.write_text('{"providers": {"codex": {"enabled": true}, "claude": {"enabled": true}}}')
        self.assertEqual(["codex", "claude"], attest.enabled_providers(target))

    def test_a_stopped_status_check_stops_the_clients_group(self) -> None:
        """The client runs in its own session; when the check is stopped, its whole group goes too."""
        marker = self.base / "client-pid"
        client = self.base / "codex"
        client.write_text(f"#!/bin/sh\necho $$ > '{marker}'\nexec /bin/sleep 30\n")  # clients get no PATH
        client.chmod(0o755)
        providers = attest.clone_module("providers")
        scratch = self.base / "scratch"
        scratch.mkdir(mode=0o700)
        with mock.patch.object(providers, "trusted_search_path", lambda: str(self.base)), \
                mock.patch.object(providers, "resolve_client", lambda name, untrusted=(): (str(client), "0" * 64)), \
                mock.patch.object(attest, "make_scratch", lambda prefix, project=None: scratch), \
                mock.patch.object(attest, "remove_scratch", lambda path: True):

            def stop_soon(signum, frame):
                raise SystemExit(143)
            previous = signal.signal(signal.SIGALRM, stop_soon)
            self.addCleanup(signal.signal, signal.SIGALRM, previous)
            signal.setitimer(signal.ITIMER_REAL, 1.0)
            with self.assertRaises(SystemExit):
                attest.client_status("codex")
        pid = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("the client is still running")

    def test_owner_tool_client_status_turns_a_termination_signal_into_a_clean_exit(self) -> None:
        """owner_tool installs the handlers attest.main uses, so SIGTERM unwinds (and cleans up)."""
        installed = []
        with mock.patch.object(attest, "_install_handlers", lambda: installed.append(True) or {}), \
                mock.patch.object(attest, "_restore_handlers", lambda previous: None), \
                mock.patch.object(attest, "client_status", lambda name, project=None: (10, b"")):
            self.assertEqual(10, attest.owner_tool(["client-status", "codex"]))
        self.assertEqual([True], installed)


if __name__ == "__main__":
    sys.exit(unittest.main())
