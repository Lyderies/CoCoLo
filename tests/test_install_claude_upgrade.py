"""The umbrella installer upgrades a project whose Claude package is the previous release (0.8.40)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_install import git, installer, isolate_home, make_target  # noqa: E402

PACKAGE = Path(__file__).resolve().parents[1] / "packages" / "claude-agents"
MANIFEST = ".claude/claude-agents/install-manifest.json"


def canonical(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class ClaudeUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(dir=os.path.realpath(tempfile.gettempdir())))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        isolate_home(self, self.base / "home")
        self.target, self.codex_options = make_target(self.base)
        done = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(done["ok"], done)
        # Put the previous release's Claude files and manifest in place of the new ones.
        release = json.loads((PACKAGE / "previous-releases.json").read_text())["releases"][0]
        files = dict(release["files"])
        manifest = {"schema_version": 1, "namespace": "claude-agents",
                    "package_version": release["package_version"], "provider_execution_enabled": False,
                    "native_discovery_verified": False, "model_isolation": "not_provided",
                    "files": {p: hashlib.sha256(c.encode()).hexdigest() for p, c in sorted(files.items())}}
        manifest["bundle_sha256"] = hashlib.sha256(canonical(manifest)).hexdigest()
        files[MANIFEST] = canonical(manifest).decode()
        for path, content in files.items():
            (self.target / path).write_text(content)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "claude package 0.8.40")

    def test_preview_and_apply_upgrade_the_previous_claude_release(self) -> None:
        preview = installer.operate("install", self.target, codex_options=self.codex_options)
        self.assertTrue(preview["ok"], preview)
        self.assertEqual("preview", preview["claude"]["result"])
        self.assertEqual("update", preview["claude"]["operation"])
        applied = installer.operate("install", self.target, apply=True, codex_options=self.codex_options)
        self.assertTrue(applied["ok"] and applied["applied"], applied)
        git(self.target, "add", ".")
        git(self.target, "commit", "-qm", "upgrade")
        verified = installer.operate("verify", self.target)
        self.assertEqual("verified", verified["claude"]["result"], verified)

    def test_a_tampered_previous_file_is_refused_at_preview(self) -> None:
        path = self.target / ".claude/agents/claude_agents_planner.md"
        path.write_text(path.read_text() + "\nextra\n")
        git(self.target, "commit", "-qam", "tamper")
        preview = installer.operate("install", self.target, codex_options=self.codex_options)
        self.assertFalse(preview["ok"], preview)
        self.assertEqual("managed_file_modified", preview["claude"]["error"])

    def test_an_unknown_claude_manifest_is_refused_at_preview(self) -> None:
        path = self.target / MANIFEST
        manifest = json.loads(path.read_text())
        manifest["bundle_sha256"] = "0" * 64
        path.write_bytes(canonical(manifest))
        git(self.target, "commit", "-qam", "unknown")
        preview = installer.operate("install", self.target, codex_options=self.codex_options)
        self.assertFalse(preview["ok"], preview)
        self.assertEqual("untrusted_manifest", preview["claude"]["error"])


if __name__ == "__main__":
    unittest.main()
