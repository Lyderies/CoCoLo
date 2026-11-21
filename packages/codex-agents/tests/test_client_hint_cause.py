"""S1-U10c: why a project's runtime hint cannot serve an open, decided before any socket is checked.

A unit test of client_runtime._hint_cause on a private hint file. It never touches a live service:
rewriting a running service's hint makes that service withdraw (found on Linux CI, 0.4.0).
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.support  # noqa: F401
from codex_agents import client_runtime


class HintCauseTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(os.path.realpath(folder.name))
        self.root.chmod(0o700)
        self.hint = self.root / "hint.json"
        self.enterContext(mock.patch.object(client_runtime, "runtime_hint_path", lambda state, root: self.hint))
        self.enterContext(mock.patch.object(client_runtime, "_canonical_project_root", lambda root: root))

    def cause(self, content=None, mode=0o600):
        if content is not None:
            self.hint.write_bytes(content)
            self.hint.chmod(mode)
        return client_runtime._hint_cause(object(), self.root)

    def test_each_hint_has_its_own_cause(self):
        current = json.dumps({"schema_version": 2}).encode()
        for label, content, mode, expected in (
                ("missing", None, 0o600, "open_service_not_running"),
                ("malformed", b"not json", 0o600, "open_service_not_running"),
                ("not an object", b"[]", 0o600, "open_service_not_running"),
                ("readable by others", current, 0o644, "open_service_hint_refused"),
                ("version 1", json.dumps({"schema_version": 1}).encode(), 0o600, "open_service_other_version"),
                ("current", current, 0o600, None)):
            with self.subTest(hint=label):
                self.assertEqual(expected, self.cause(content, mode))

    def test_a_linked_hint_is_refused(self):
        target = self.root / "elsewhere.json"
        target.write_bytes(b'{"schema_version": 2}')
        target.chmod(0o600)
        self.hint.symlink_to(target)
        self.assertEqual("open_service_hint_refused", self.cause())


if __name__ == "__main__":
    unittest.main()
