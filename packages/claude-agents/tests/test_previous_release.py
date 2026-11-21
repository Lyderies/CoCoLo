"""An upgrade from the immediately previous released Claude bundle (0.8.40) is accepted when the
installed files match that bundle's recorded hashes; anything else stays refused."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from claude_agents import packaging
from claude_agents.packaging import PackageError, compile_bundle, package_operation

SOURCE = Path(__file__).resolve().parents[1]
# The bundle_sha256 recorded in the install manifest of projects on release 0.8.40 (taken from
# such a project's .claude/claude-agents/install-manifest.json, not from the data file).
RELEASED_0_8_40 = "c3e80b3f92cc5e5728b848e384d820265f70b32031ed931b723f201c1b0309ae"
MANIFEST = ".claude/claude-agents/install-manifest.json"


def previous_release():
    data = json.loads((SOURCE / "previous-releases.json").read_text())
    (release,) = [r for r in data["releases"] if r["bundle_sha256"] == RELEASED_0_8_40]
    return release


class PreviousReleaseUpgradeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.target = Path(self.temp.name).resolve() / "target"
        subprocess.run(["/usr/bin/git", "init", "--quiet", "--template=", str(self.target)], check=True,
                       env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    def install_0_8_40(self) -> None:
        """Write what release 0.8.40 installed, byte for byte, from the pinned release record."""
        release = previous_release()
        files = dict(release["files"])
        manifest = {"schema_version": 1, "namespace": "claude-agents",
                    "package_version": release["package_version"], "provider_execution_enabled": False,
                    "native_discovery_verified": False, "model_isolation": "not_provided",
                    "files": {p: __import__("hashlib").sha256(c.encode()).hexdigest()
                              for p, c in sorted(files.items())}}
        manifest["bundle_sha256"] = __import__("hashlib").sha256(packaging._canonical(manifest)).hexdigest()
        self.assertEqual(RELEASED_0_8_40, manifest["bundle_sha256"])
        files[MANIFEST] = packaging._canonical(manifest).decode()
        for path, content in files.items():
            destination = self.target / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content)

    def op(self, operation="update", apply=False):
        return package_operation(SOURCE, self.target, operation, apply=apply)

    def snapshot(self):
        return {p.relative_to(self.target).as_posix(): p.read_bytes()
                for p in self.target.rglob("*") if p.is_file() and ".git/" not in p.as_posix()}

    def test_upgrade_from_the_previous_release_is_accepted_and_applied(self) -> None:
        self.install_0_8_40()
        preview = self.op()
        self.assertEqual("preview", preview["result"])
        self.assertIn(MANIFEST, preview["changed_paths"])
        self.assertEqual("applied", self.op(apply=True)["result"])
        self.assertEqual(dict(compile_bundle(SOURCE).files)[".claude/agents/claude_agents_planner.md"],
                         (self.target / ".claude/agents/claude_agents_planner.md").read_bytes())
        self.assertEqual("verified", self.op("verify")["result"])

    def test_a_tampered_installed_file_is_refused_and_nothing_changes(self) -> None:
        self.install_0_8_40()
        tampered = self.target / ".claude/agents/claude_agents_planner.md"
        tampered.write_text(tampered.read_text() + "\nextra\n")
        before = self.snapshot()
        with self.assertRaises(PackageError) as caught:
            self.op(apply=True)
        self.assertEqual("managed_file_modified", str(caught.exception))
        self.assertEqual(before, self.snapshot())

    def test_an_unknown_manifest_is_refused(self) -> None:
        self.install_0_8_40()
        manifest = json.loads((self.target / MANIFEST).read_text())
        manifest["files"][".claude/agents/claude_agents_planner.md"] = "0" * 64
        manifest.pop("bundle_sha256")
        manifest["bundle_sha256"] = __import__("hashlib").sha256(packaging._canonical(manifest)).hexdigest()
        (self.target / MANIFEST).write_bytes(packaging._canonical(manifest))
        with self.assertRaises(PackageError) as caught:
            self.op()
        self.assertEqual("untrusted_manifest", str(caught.exception))

    def test_a_forged_manifest_claiming_the_previous_digest_is_refused(self) -> None:
        self.install_0_8_40()
        manifest = json.loads((self.target / MANIFEST).read_text())
        manifest["provider_execution_enabled"] = True  # digest field left as the released one
        (self.target / MANIFEST).write_bytes(packaging._canonical(manifest))
        with self.assertRaises(PackageError) as caught:
            self.op()
        self.assertEqual("untrusted_manifest", str(caught.exception))

    def test_the_pinned_record_is_the_released_bundle(self) -> None:
        release = previous_release()
        self.assertEqual("0.8.40", release["release"])
        self.assertEqual(9, len(release["files"]))

    def test_a_fresh_install_is_unchanged(self) -> None:
        self.assertEqual("applied", self.op("install", apply=True)["result"])
        self.assertEqual("verified", self.op("verify")["result"])
        self.assertEqual("unchanged", self.op("install", apply=True)["result"])


if __name__ == "__main__":
    unittest.main()
