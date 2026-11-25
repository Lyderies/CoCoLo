"""The record of earlier releases (previous-releases.json) cannot vouch for itself.

Accepted digests are pinned in installer code, and a record may only name the files
compile_bundle produces. A forged record is refused with a clear reason and changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from claude_agents import packaging
from claude_agents.packaging import PackageError, compile_bundle, package_operation, released_bundles

SOURCE = Path(__file__).resolve().parents[1]
RELEASED_0_8_40 = "c3e80b3f92cc5e5728b848e384d820265f70b32031ed931b723f201c1b0309ae"
RELEASED_0_8_41 = "eb34209dacdac6d3fe00fcd8a3f4c055bb3cdf661683ea270abc527f95ff62d3"
MANIFEST = ".claude/claude-agents/install-manifest.json"
SETTINGS = ".claude/settings.json"
OUTSIDE_PATHS = (SETTINGS, ".claude/claude-agents/lib/claude_agents/contracts/__init__.py",
                 ".claude/claude-agents/install.lock", ".claude/agents/other_project_agent.md")


def digest_of(record: dict) -> str:
    files = {p: hashlib.sha256(c.encode()).hexdigest() for p, c in sorted(record["files"].items())}
    metadata = {"schema_version": 1, "namespace": "claude-agents", "package_version": record["package_version"],
                "provider_execution_enabled": False, "native_discovery_verified": False,
                "model_isolation": "not_provided", "files": files}
    return hashlib.sha256(packaging._canonical(metadata)).hexdigest()


class CatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.source = base / "source"
        (self.source / "tests" / "fixtures").mkdir(parents=True)
        shutil.copy(SOURCE / "pyproject.toml", self.source)
        shutil.copy(SOURCE / "previous-releases.json", self.source)
        shutil.copytree(SOURCE / "tests" / "fixtures" / "native", self.source / "tests" / "fixtures" / "native")
        shutil.copytree(SOURCE / "src", self.source / "src", ignore=shutil.ignore_patterns("__pycache__"))
        self.target = base / "target"
        subprocess.run(["/usr/bin/git", "init", "--quiet", "--template=", str(self.target)], check=True,
                       env={"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})

    def tearDown(self) -> None:
        self.temp.cleanup()

    @property
    def catalogue(self) -> Path:
        return self.source / "previous-releases.json"

    def records(self) -> list[dict]:
        return json.loads(self.catalogue.read_text())["releases"]

    def write(self, records: list[dict]) -> None:
        self.catalogue.write_text(json.dumps({"releases": records}))

    def forged(self, extra_path: str | None = None, **changes) -> dict:
        record = json.loads(json.dumps(self.records()[0]))
        record["release"] = "0.8.99"
        if extra_path:
            record["files"][extra_path] = '{"permissions":{"deny":[]}}\n'
        record["files"][".claude/agents/claude_agents_planner.md"] += "\nforged\n"
        record.update(changes)
        record["bundle_sha256"] = digest_of(record)  # a self-consistent forgery
        return record

    def snapshot(self) -> dict:
        return {p.relative_to(self.target).as_posix(): p.read_bytes()
                for p in self.target.rglob("*") if p.is_file() and ".git/" not in p.as_posix()}

    def refused(self, code: str) -> None:
        with self.assertRaises(PackageError) as caught:
            released_bundles(self.source)
        self.assertEqual(code, str(caught.exception))

    def test_the_accepted_digests_are_pinned_in_installer_code(self) -> None:
        self.assertEqual(frozenset({RELEASED_0_8_40, RELEASED_0_8_41}), packaging.PINNED_RELEASE_DIGESTS)

    def test_the_shipped_catalogue_is_accepted(self) -> None:
        self.assertEqual(22, len(released_bundles(self.source)))  # 0.8.40, 0.8.41, 0.8.42, 0.8.43, 0.8.44, 0.8.45, 0.8.46, 0.8.47, 0.8.48, 0.8.49, 0.8.50, 0.8.51, 0.8.52, 0.8.53, 0.8.54, 0.8.55, 0.8.56, 0.8.57, 0.8.58, 0.8.59, 0.8.60, 0.8.61 (last twenty-one: same bundle)

    def padded(self, size: int) -> None:
        body = json.dumps({"releases": self.records()}).encode()
        self.catalogue.write_bytes(body + b" " * (size - len(body)))

    def test_cl1_a_catalogue_up_to_2_mib_is_accepted(self) -> None:
        self.padded(2_097_152)
        self.assertEqual(22, len(released_bundles(self.source)))

    def test_cl1_a_catalogue_over_2_mib_is_refused(self) -> None:
        self.padded(2_097_152 + 1)
        self.refused("unsafe_file")

    def test_cl2_every_other_file_keeps_the_256_kib_limit(self) -> None:
        for size, ok in ((262_144, True), (262_145, False)):
            (self.source / "other.bin").write_bytes(b"x" * size)
            root = packaging._root(self.source)
            try:
                if ok:
                    self.assertEqual(size, len(packaging._read(root, "other.bin")))
                else:
                    with self.assertRaises(PackageError) as caught:
                        packaging._read(root, "other.bin")
                    self.assertEqual("unsafe_file", str(caught.exception))
            finally:
                import os
                os.close(root)

    def test_cl3_the_read_stays_bounded_when_the_file_grows_after_the_size_check(self) -> None:
        (self.source / "other.bin").write_bytes(b"x" * 10)
        root = packaging._root(self.source)
        try:
            with mock.patch.object(packaging.os, "fstat") as fstat:
                fstat.return_value = mock.Mock(st_mode=0o100644, st_size=10)
                (self.source / "other.bin").write_bytes(b"x" * 300_000)
                with self.assertRaises(PackageError) as caught:
                    packaging._read(root, "other.bin")
            self.assertEqual("oversized_file", str(caught.exception))
        finally:
            import os
            os.close(root)

    def test_an_appended_record_with_a_recomputed_digest_is_refused(self) -> None:
        self.write([*self.records(), self.forged()])
        self.refused("previous_release_not_pinned")

    def test_the_released_record_edited_with_a_recomputed_digest_is_refused(self) -> None:
        record = self.records()[0]
        record["files"][".claude/agents/claude_agents_planner.md"] += "\nforged\n"
        record["bundle_sha256"] = digest_of(record)
        self.write([record])
        self.refused("previous_release_not_pinned")

    def test_a_record_with_a_stale_digest_is_refused(self) -> None:
        record = self.records()[0]
        record["files"][".claude/agents/claude_agents_planner.md"] += "\nforged\n"
        self.write([record])
        self.refused("invalid_previous_release")

    def test_the_fixed_paths_are_exactly_what_compile_bundle_produces(self) -> None:
        self.assertEqual(set(dict(compile_bundle(self.source).files)), set(packaging.bundle_paths()))

    def test_a_record_naming_a_path_outside_the_fixed_set_is_refused(self) -> None:
        shipped = self.records()
        for path in OUTSIDE_PATHS:
            with self.subTest(path=path):
                forged = self.forged(path)
                self.write([*shipped, forged])
                with mock.patch.object(packaging, "PINNED_RELEASE_DIGESTS",
                                       packaging.PINNED_RELEASE_DIGESTS | {forged["bundle_sha256"]}):
                    self.refused("previous_release_path_refused")

    def test_a_forged_record_cannot_make_an_update_delete_a_path_outside_the_package(self) -> None:
        forged = self.forged(SETTINGS)
        self.write([*self.records(), forged])
        for path, content in forged["files"].items():
            (self.target / path).parent.mkdir(parents=True, exist_ok=True)
            (self.target / path).write_text(content)
        files = {p: hashlib.sha256(c.encode()).hexdigest() for p, c in sorted(forged["files"].items())}
        manifest = {"schema_version": 1, "namespace": "claude-agents", "package_version": forged["package_version"],
                    "provider_execution_enabled": False, "native_discovery_verified": False,
                    "model_isolation": "not_provided", "files": files, "bundle_sha256": forged["bundle_sha256"]}
        (self.target / MANIFEST).write_bytes(packaging._canonical(manifest))
        before = self.snapshot()
        for operation in ("update", "remove"):
            with self.subTest(operation=operation), self.assertRaises(PackageError) as caught:
                package_operation(self.source, self.target, operation, apply=True)
            self.assertEqual("previous_release_not_pinned", str(caught.exception))
            self.assertEqual(before, self.snapshot())
            self.assertTrue((self.target / SETTINGS).is_file())

    def test_a_forged_record_with_only_its_digest_pinned_is_stopped_by_the_path_check_end_to_end(self) -> None:
        forged = self.forged(SETTINGS)
        self.write([*self.records(), forged])
        for path, content in forged["files"].items():
            (self.target / path).parent.mkdir(parents=True, exist_ok=True)
            (self.target / path).write_text(content)
        files = {p: hashlib.sha256(c.encode()).hexdigest() for p, c in sorted(forged["files"].items())}
        manifest = {"schema_version": 1, "namespace": "claude-agents", "package_version": forged["package_version"],
                    "provider_execution_enabled": False, "native_discovery_verified": False,
                    "model_isolation": "not_provided", "files": files, "bundle_sha256": forged["bundle_sha256"]}
        (self.target / MANIFEST).write_bytes(packaging._canonical(manifest))
        before = self.snapshot()
        pinned = packaging.PINNED_RELEASE_DIGESTS | {forged["bundle_sha256"]}
        for operation in ("update", "remove"):
            with self.subTest(operation=operation), mock.patch.object(packaging, "PINNED_RELEASE_DIGESTS", pinned):
                with self.assertRaises(PackageError) as caught:
                    package_operation(self.source, self.target, operation, apply=True)
                self.assertEqual("previous_release_path_refused", str(caught.exception))
            self.assertEqual(before, self.snapshot(), operation)
            self.assertTrue((self.target / SETTINGS).is_file(), operation)

    def test_a_fresh_install_is_refused_while_the_catalogue_holds_a_forged_record(self) -> None:
        self.write([*self.records(), self.forged()])
        with self.assertRaises(PackageError) as caught:
            package_operation(self.source, self.target, "install", apply=True)
        self.assertEqual("previous_release_not_pinned", str(caught.exception))
        self.assertEqual({}, self.snapshot())


if __name__ == "__main__":
    unittest.main()
