"""M3 slice 1: hardened git, the git version floor, and the candidate tree in a private object store.

Requirements (DESIGN-0.4.0-M3.md draft 3 §2-3, slice 1):
- M3-16 Every COCOLO git call is hardened: system and global configuration ignored, the
  repository's hooks, fsmonitor, filters, textconv, external diff, pager, gpg, credential helper and
  ssh command never run, no transport, and only plumbing (an allow-list of subcommands; anything
  that writes may only target COCOLO's private store). Git 2.50.1 or later, refused plainly below.
- M3-17 The candidate tree (base commit plus patch) is built in COCOLO's private object store: a
  private repository whose object directory is COCOLO's own and which reads the owner's objects
  through alternates, with a private index (`read-tree`, `apply --cached`, `write-tree`). The
  owner's working tree, index, refs, objects and configuration are never touched.
- M3-28 (R05, git part) Git missing, too old, or of an unknown version refuses with a reason and a
  plain sentence; so does a repository COCOLO cannot use.
- M3-29 Nothing of COCOLO's is left in the owner's repository (the before/after map below).
- X04 The candidate tree equals the base tree with exactly the patch's paths changed (oracle: the
  same patch applied by ordinary git in an independent throwaway clone gives the same tree id).
- G15 (slice-1 part) A repository whose configuration (and the caller's environment, and a global
  configuration file) sets every helper to a tripwire: no tripwire fires across every entry point.

All repositories are throwaway ones in temporary folders. Ordinary git is used only to build the
fixtures and the independent oracle, never on anything but those folders.
"""
from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT

from codex_agents import git_plumbing
from codex_agents import hardened_git as hg

FIXTURE_ENV = {
    "PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
REAL_GIT = git_plumbing.git_executable()


def fixture_git(root: Path, *arguments: str, check: bool = True, env=None) -> subprocess.CompletedProcess:
    """Ordinary git, for building fixtures and the oracle only."""

    return subprocess.run(
        [REAL_GIT or "git", "-C", str(root), *arguments], env=env or FIXTURE_ENV,
        capture_output=True, stdin=subprocess.DEVNULL, check=check, timeout=60,
    )


def folder_map(root: Path) -> dict[str, tuple]:
    """Every entry under root (the working tree and .git): type, mode, size, inode, mtime and content."""

    result: dict[str, tuple] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        for name in sorted(names + files):
            path = Path(directory) / name
            info = os.lstat(path)
            relative = str(path.relative_to(root))
            if stat.S_ISREG(info.st_mode):
                content = hashlib.sha256(path.read_bytes()).hexdigest()
            elif stat.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            else:
                content = None
            result[relative] = (
                stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_size,
                info.st_ino, info.st_mtime_ns, content,
            )
    root_info = os.lstat(root)
    result["."] = (root_info.st_ino, root_info.st_mtime_ns)
    return result


def make_fake_git(directory: Path, output: str, status: int = 0) -> str:
    path = directory / "fake-git"
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {status}\n")
    path.chmod(0o755)
    return str(path)


def needs_real_git(test):
    reason = None
    if REAL_GIT is None:
        reason = "no trusted git binary on this machine"
    elif hg.git_version_problem(REAL_GIT) is not None:
        reason = "this machine's git is below COCOLO's floor"
    return unittest.skipIf(reason is not None, reason or "")(test)


class Workspace(unittest.TestCase):
    """A throwaway folder holding the owner's repository, the private store's parent, and an oracle."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "project"
        self.stores = self.base / "stores"
        self.stores.mkdir()
        self.store = self.stores / "task-1"

    def make_repository(self, root: Path, *, object_format: str = "sha1") -> str:
        root.mkdir(parents=True)
        fixture_git(root, "init", "-q", f"--object-format={object_format}")
        (root / "README.md").write_text("hello\n")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("def main():\n    return 1\n")
        (root / "src" / "keep.py").write_text("KEEP = True\n")
        (root / "tool.sh").write_text("#!/bin/sh\necho tool\n")
        (root / "tool.sh").chmod(0o755)
        (root / "gone.txt").write_text("to be deleted\n")
        fixture_git(root, "add", "-A")
        fixture_git(root, "commit", "-qm", "base")
        return fixture_git(root, "rev-parse", "HEAD").stdout.decode().strip()

    def make_patch(self, root: Path, base: str) -> bytes:
        """A patch from base: modify, add (in a new folder), delete, mode change and a binary file."""

        clone = self.base / "patch-source"
        fixture_git(self.base, "clone", "-q", "--no-local", str(root), str(clone))
        fixture_git(clone, "checkout", "-q", base)
        (clone / "src" / "app.py").write_text("def main():\n    return 2\n")
        (clone / "new").mkdir()
        (clone / "new" / "added.py").write_text("ADDED = 1\n")
        (clone / "blob.bin").write_bytes(bytes(range(256)) * 4)
        (clone / "gone.txt").unlink()
        (clone / "tool.sh").chmod(0o644)
        fixture_git(clone, "add", "-A")
        patch = fixture_git(clone, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv").stdout
        shutil.rmtree(clone)
        return patch

    def oracle_tree(self, root: Path, base: str, patch: bytes) -> tuple[str, set[str]]:
        """The tree ordinary git builds from the same base and patch in an independent clone."""

        clone = Path(tempfile.mkdtemp(dir=self.base, prefix="oracle-")) / "clone"
        fixture_git(self.base, "clone", "-q", "--no-local", str(root), str(clone))
        fixture_git(clone, "checkout", "-q", base)
        patch_file = clone.parent / "oracle.patch"
        patch_file.write_bytes(patch)
        fixture_git(clone, "apply", "--index", str(patch_file))
        tree = fixture_git(clone, "write-tree").stdout.decode().strip()
        changed = set(fixture_git(
            clone, "diff", "--cached", "--name-only", "--no-renames", "--no-ext-diff", "-z", base,
        ).stdout.decode().split("\0")) - {""}
        return tree, changed

    def store_git(self, *arguments: str) -> subprocess.CompletedProcess:
        """Read the private store with ordinary git (it reads the owner's objects as alternates)."""

        objects = str(self.project / ".git" / "objects").replace("\\", "\\\\").replace('"', '\\"')
        env = {**FIXTURE_ENV, "GIT_DIR": str(self.store),
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": f'"{objects}"'}
        return subprocess.run(
            [REAL_GIT, *arguments], env=env, capture_output=True, stdin=subprocess.DEVNULL,
            check=True, timeout=60, cwd=self.base,
        )


# --------------------------------------------------------------------------------------------
# M3-16: the hardened runner
# --------------------------------------------------------------------------------------------

class HardenedRunnerTests(Workspace):
    def test_every_call_carries_the_hardened_settings(self) -> None:
        options = list(hg.HARDENED_OPTIONS)
        settings = {options[index + 1] for index, item in enumerate(options[:-1]) if item == "-c"}
        for setting in (
            "core.hooksPath=/dev/null", "core.fsmonitor=false", "core.sshCommand=false",
            "credential.helper=", "protocol.allow=never", "core.attributesFile=/dev/null",
            "commit.gpgSign=false", "tag.gpgSign=false", "log.showSignature=false",
            "core.protectHFS=true", "core.protectNTFS=true", "submodule.recurse=false",
        ):
            self.assertIn(setting, settings)
        for flag in ("--no-pager", "--no-optional-locks", "--literal-pathspecs"):
            self.assertIn(flag, options)
        environment = hg.HARDENED_ENVIRONMENT
        for key, value in (
            ("GIT_CONFIG_NOSYSTEM", "1"), ("GIT_CONFIG_GLOBAL", "/dev/null"),
            ("GIT_ATTR_NOSYSTEM", "1"), ("GIT_TERMINAL_PROMPT", "0"),
            ("GIT_NO_REPLACE_OBJECTS", "1"), ("GIT_ALLOW_PROTOCOL", ""),
            ("GIT_NO_LAZY_FETCH", "1"),
        ):
            self.assertEqual(value, environment[key], key)
        for inherited in ("HOME", "GIT_DIR", "GIT_INDEX_FILE", "GIT_EXTERNAL_DIFF", "GIT_SSH_COMMAND"):
            self.assertNotIn(inherited, environment)

    def test_only_allowed_plumbing_runs(self) -> None:
        private = hg.PrivateRepository(
            git_dir=self.store, index_file=self.store / "index-x", alternates=(),
        )
        refused = [
            ("status",), ("diff",), ("checkout", "HEAD"), ("commit", "-m", "x"), ("merge", "x"),
            ("stash",), ("reset", "--hard"), ("add", "."), ("log",), ("fetch",), ("gc",),
            ("update-ref", "refs/heads/x", "0" * 40), ("hash-object", "-w", "/etc/hosts"),
            ("config", "core.fsmonitor", "x"), ("config", "--unset", "x"), ("config", "--add", "a", "b"),
            ("apply", "/tmp/p"), ("apply", "--index", "/tmp/p"), ("apply", "--cached", "--unsafe-paths", "/tmp/p"),
            ("apply", "--cached", "--3way", "/tmp/p"), ("apply", "--cached"),
            ("read-tree", "-u", "HEAD"), ("read-tree", "--recurse-submodules", "HEAD"),
            ("write-tree", "--prefix=x"),
        ]
        with mock.patch.object(git_plumbing.subprocess, "Popen") as popen, \
                mock.patch.object(git_plumbing.subprocess, "run") as run:
            for arguments in refused:
                with self.subTest(arguments=arguments), self.assertRaises(hg.GitCallRefused):
                    hg.run_hardened_git("/usr/bin/true", self.base, *arguments, private=private)
            # Writing plumbing never runs against a repository other than the private store.
            for arguments in (("read-tree", "HEAD"), ("apply", "--cached", "/tmp/p"), ("write-tree",)):
                with self.subTest(without_private=arguments), self.assertRaises(hg.GitCallRefused):
                    hg.run_hardened_git("/usr/bin/true", self.base, *arguments)
            popen.assert_not_called()
            run.assert_not_called()

    def test_the_callers_environment_is_never_inherited(self) -> None:
        hostile = {
            "GIT_DIR": "/nonexistent/hostile", "GIT_INDEX_FILE": "/nonexistent/index",
            "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='/nonexistent/x'",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.pager", "GIT_CONFIG_VALUE_0": "x",
        }
        captured = {}

        def fake_popen(argv, **options):
            captured.update(options["env"])
            raise OSError("stop here")

        with mock.patch.dict(os.environ, hostile), \
                mock.patch.object(git_plumbing.subprocess, "Popen", fake_popen), \
                self.assertRaises(OSError):
            hg.run_hardened_git("/usr/bin/true", self.base, "rev-parse", "HEAD")
        for key in hostile:
            self.assertNotIn(key, captured)
        self.assertEqual("/dev/null", captured["GIT_CONFIG_GLOBAL"])


# --------------------------------------------------------------------------------------------
# M3-16 / M3-28: the version floor
# --------------------------------------------------------------------------------------------

class VersionTests(Workspace):
    def test_version_text_is_parsed(self) -> None:
        for text, expected in (
            ("git version 2.54.0 (Apple Git-157)", (2, 54, 0)),
            ("git version 2.50.1", (2, 50, 1)),
            ("git version 2.50.1.windows.1", (2, 50, 1)),
            ("git version 2.51.0.rc1", (2, 51, 0)),
            ("git version 2.50", (2, 50, 0)),
            ("git version 3.0.0\n", (3, 0, 0)),
            ("version 2.54.0", None), ("git version banana", None), ("", None),
            ("git version 2.54.0\nsurprise", None),
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, hg.parse_git_version(text))

    def test_the_floor_is_2_50_1(self) -> None:
        self.assertEqual((2, 50, 1), hg.GIT_VERSION_FLOOR)
        for output, reason in (
            ("git version 2.49.0", "git_too_old"),
            ("git version 2.50.0", "git_too_old"),
            ("git version 1.99.9", "git_too_old"),
            ("git version 2.50.1", None),
            ("git version 2.54.0 (Apple Git-157)", None),
            ("git version something", "git_version_unknown"),
        ):
            with self.subTest(output=output):
                fake = make_fake_git(self.base, output)
                problem = hg.git_version_problem(fake)
                if reason is None:
                    self.assertIsNone(problem)
                else:
                    self.assertFalse(problem.ok)
                    self.assertEqual(reason, problem.reason)
        old = hg.git_version_problem(make_fake_git(self.base, "git version 2.49.0"))
        self.assertIn("2.50.1", old.message)
        self.assertIn("2.49.0", old.message)

    def test_missing_or_broken_git_is_refused_plainly(self) -> None:
        self.assertEqual("git_missing", hg.git_version_problem(None).reason)
        self.assertEqual("git_missing", hg.git_version_problem(str(self.base / "no-such-git")).reason)
        failing = make_fake_git(self.base, "git version 2.54.0", status=1)
        self.assertEqual("git_version_unknown", hg.git_version_problem(failing).reason)
        for problem in (hg.git_version_problem(None), hg.git_version_problem(failing)):
            self.assertTrue(problem.message.endswith("."))
            self.assertNotIn("Traceback", problem.message)

    @needs_real_git
    def test_build_refuses_old_git_with_nothing_changed(self) -> None:
        base = self.make_repository(self.project)
        patch = self.make_patch(self.project, base)
        before = folder_map(self.project)
        fake = make_fake_git(self.base, "git version 2.49.0")
        result = hg.build_candidate_tree(self.project, self.store, base, patch, executable=fake)
        self.assertFalse(result.ok)
        self.assertEqual("git_too_old", result.reason)
        self.assertEqual(before, folder_map(self.project))
        self.assertFalse(self.store.exists())
        with mock.patch.object(hg, "git_executable", return_value=None):
            missing = hg.build_candidate_tree(self.project, self.store, base, patch)
        self.assertEqual("git_missing", missing.reason)
        self.assertFalse(self.store.exists())


# --------------------------------------------------------------------------------------------
# M3-17 / M3-29 / X04: the candidate tree in the private store
# --------------------------------------------------------------------------------------------

class CandidateTreeTests(Workspace):
    def assert_store_has_no_leftovers(self) -> None:
        if not self.store.exists():
            return
        leftovers = [name for name in os.listdir(self.store)
                     if name.startswith(("index", "patch")) or name.endswith(".lock")]
        self.assertEqual([], leftovers)

    def build(self, base: str, patch: bytes, **options):
        return hg.build_candidate_tree(self.project, self.store, base, patch, executable=REAL_GIT, **options)

    @needs_real_git
    def test_candidate_equals_base_plus_exactly_the_patch(self) -> None:
        base = self.make_repository(self.project)
        patch = self.make_patch(self.project, base)
        before = folder_map(self.project)
        result = self.build(base, patch)
        self.assertTrue(result.ok, getattr(result, "message", ""))
        self.assertEqual(before, folder_map(self.project))  # M3-17, M3-29
        expected_tree, expected_paths = self.oracle_tree(self.project, base, patch)
        self.assertEqual(expected_tree, result.tree)  # X04
        self.assertEqual(base, result.base_commit)
        self.assertEqual(
            fixture_git(self.project, "rev-parse", f"{base}^{{tree}}").stdout.decode().strip(),
            result.base_tree,
        )
        self.assertEqual("sha1", result.object_format)
        changed = set(self.store_git(
            "diff-tree", "-r", "--name-only", "--no-renames", "-z", result.base_tree, result.tree,
        ).stdout.decode().split("\0")) - {""}
        self.assertEqual(
            {"src/app.py", "new/added.py", "blob.bin", "gone.txt", "tool.sh"}, changed,
        )
        self.assertEqual(expected_paths, changed)
        # The new objects live in the private store, not in the owner's repository.
        self.assertEqual(
            "tree", self.store_git("cat-file", "-t", result.tree).stdout.decode().strip(),
        )
        self.assertNotEqual(0, fixture_git(self.project, "cat-file", "-e", result.tree, check=False).returncode)
        self.assertEqual(self.store.resolve(), Path(result.store).resolve())
        self.assert_store_has_no_leftovers()

    @needs_real_git
    def test_a_patch_that_does_not_apply_is_refused_with_nothing_changed(self) -> None:
        base = self.make_repository(self.project)
        good = self.make_patch(self.project, base)
        # The first hunk applies; a later one names context the base does not have.
        bad = good + (
            b"diff --git a/src/keep.py b/src/keep.py\n"
            b"--- a/src/keep.py\n+++ b/src/keep.py\n"
            b"@@ -1 +1 @@\n-NOT THE BASE CONTENT\n+KEEP = False\n"
        )
        before = folder_map(self.project)
        result = self.build(base, bad)
        self.assertFalse(result.ok)
        self.assertEqual("patch_does_not_apply", result.reason)
        self.assertIn("nothing was changed", result.message)
        self.assertEqual(before, folder_map(self.project))
        self.assert_store_has_no_leftovers()
        # Patches git itself refuses (a .git path, an escaping path) are refused the same way.
        for hostile in (
            b"diff --git a/.git/config b/.git/config\nnew file mode 100644\n--- /dev/null\n"
            b"+++ b/.git/config\n@@ -0,0 +1 @@\n+[core]\n",
            b"diff --git a/../escape b/../escape\nnew file mode 100644\n--- /dev/null\n"
            b"+++ b/../escape\n@@ -0,0 +1 @@\n+x\n",
        ):
            with self.subTest(hostile=hostile[:40]):
                refused = self.build(base, hostile)
                self.assertEqual("patch_does_not_apply", refused.reason)
                self.assertEqual(before, folder_map(self.project))
                self.assert_store_has_no_leftovers()

    @needs_real_git
    def test_empty_oversized_and_invalid_inputs_are_refused(self) -> None:
        base = self.make_repository(self.project)
        patch = self.make_patch(self.project, base)
        before = folder_map(self.project)
        cases = (
            (base, b"", "patch_empty"),
            (base, b"x" * (hg.MAX_PATCH_BYTES + 1), "patch_too_large"),
            (base, "not bytes", "patch_invalid"),
            ("HEAD", patch, "base_commit_invalid"),
            (base[:12], patch, "base_commit_invalid"),
            ("--output=/tmp/x" + "0" * 25, patch, "base_commit_invalid"),
            ("f" * 40, patch, "base_commit_missing"),
            (fixture_git(self.project, "rev-parse", f"{base}^{{tree}}").stdout.decode().strip(),
             patch, "base_commit_missing"),
        )
        for commit, data, reason in cases:
            with self.subTest(reason=reason, commit=commit[:16]):
                result = self.build(commit, data)
                self.assertFalse(result.ok)
                self.assertEqual(reason, result.reason)
                self.assertEqual(before, folder_map(self.project))
                self.assert_store_has_no_leftovers()

    @needs_real_git
    def test_the_store_may_not_be_inside_the_owners_repository(self) -> None:
        base = self.make_repository(self.project)
        patch = self.make_patch(self.project, base)
        before = folder_map(self.project)
        for store in (self.project / "cocolo-store", self.project / ".git" / "cocolo",
                      self.project / "src" / "store", self.project):
            with self.subTest(store=str(store)):
                result = hg.build_candidate_tree(self.project, store, base, patch, executable=REAL_GIT)
                self.assertFalse(result.ok)
                self.assertEqual("store_inside_repository", result.reason)
                self.assertEqual(before, folder_map(self.project))
        # Relative, symlinked, parentless and foreign stores are refused too.
        link = self.stores / "link"
        link.symlink_to(self.stores / "elsewhere")
        (self.stores / "elsewhere").mkdir()
        stray = self.stores / "stray"
        stray.mkdir()
        (stray / "file").write_text("not a store\n")
        for store, reason in (
            (Path("relative/store"), "store_unusable"),
            (link, "store_unusable"),
            (self.base / "missing-parent" / "store", "store_unusable"),
            (stray, "store_unusable"),
        ):
            with self.subTest(store=str(store)):
                result = hg.build_candidate_tree(self.project, store, base, patch, executable=REAL_GIT)
                self.assertEqual(reason, result.reason)
        self.assertEqual(before, folder_map(self.project))

    @needs_real_git
    def test_the_store_is_reused_for_its_project_only(self) -> None:
        base = self.make_repository(self.project)
        patch = self.make_patch(self.project, base)
        first = self.build(base, patch)
        self.assertTrue(first.ok)
        # A second build (the re-test on a moved HEAD) reuses the same private store.
        (self.project / "README.md").write_text("hello again\n")
        fixture_git(self.project, "commit", "-qam", "owner commit")
        head = fixture_git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        before = folder_map(self.project)
        second = self.build(head, patch)
        self.assertTrue(second.ok, getattr(second, "message", ""))
        self.assertEqual(self.oracle_tree(self.project, head, patch)[0], second.tree)
        self.assertEqual(before, folder_map(self.project))
        # Another project's repository cannot use this store.
        other = self.base / "other"
        other_base = self.make_repository(other)
        refused = hg.build_candidate_tree(other, self.store, other_base, patch, executable=REAL_GIT)
        self.assertEqual("store_other_repository", refused.reason)
        self.assert_store_has_no_leftovers()

    @needs_real_git
    def test_sha256_repository_and_unusual_folder_names(self) -> None:
        self.project = self.base / "pro:ject \"quoted\""
        base = self.make_repository(self.project, object_format="sha256")
        patch = self.make_patch(self.project, base)
        before = folder_map(self.project)
        result = self.build(base, patch)
        self.assertTrue(result.ok, getattr(result, "message", ""))
        self.assertEqual("sha256", result.object_format)
        self.assertEqual(64, len(result.tree))
        self.assertEqual(self.oracle_tree(self.project, base, patch)[0], result.tree)
        self.assertEqual(before, folder_map(self.project))

    @needs_real_git
    def test_repositories_cocolo_cannot_use_are_refused(self) -> None:
        base = self.make_repository(self.project)
        patch = self.make_patch(self.project, base)
        bare = self.base / "bare.git"
        fixture_git(self.base, "clone", "-q", "--bare", str(self.project), str(bare))
        partial = self.base / "partial"
        partial_base = self.make_repository(partial)
        fixture_git(partial, "config", "remote.origin.promisor", "true")
        fixture_git(partial, "config", "extensions.partialclone", "origin")
        not_a_repository = self.base / "plain"
        not_a_repository.mkdir()
        for root, commit in ((bare, base), (partial, partial_base), (not_a_repository, base),
                             (self.project / "src", base)):
            with self.subTest(root=root.name):
                before = folder_map(root)
                result = hg.build_candidate_tree(root, self.store, commit, patch, executable=REAL_GIT)
                self.assertFalse(result.ok)
                self.assertEqual("repository_unusable", result.reason)
                self.assertEqual(before, folder_map(root))
                self.assertFalse(self.store.exists())


# --------------------------------------------------------------------------------------------
# G15 (slice 1): the tripwire repository over every entry point
# --------------------------------------------------------------------------------------------

class TripwireTests(Workspace):
    """A repository whose own configuration names a program for every helper git knows.

    The tripwire records that it ran and, as a clean filter, transforms content, so a filter that
    ran would also show in the candidate's bytes. The caller's environment and a global
    configuration file are armed too. The traps are proven live with ordinary git first.
    """

    def setUp(self) -> None:
        super().setUp()
        self.marker = self.base / "tripwire-ran"
        tripwire = self.base / "tripwire.sh"
        tripwire.write_text(
            f'#!/bin/sh\necho "$0 $*" >> "{self.marker}"\n'
            "tr 'a-z' 'A-Z'\nexit 0\n"
        )
        tripwire.chmod(0o755)
        self.tripwire = str(tripwire)
        self.base_commit = self.make_repository(self.project)
        (self.project / ".gitattributes").write_text(
            "* filter=trip diff=trip merge=trip\n*.py filter=trip diff=trip\n"
        )
        fixture_git(self.project, "add", ".gitattributes")
        fixture_git(self.project, "commit", "-qm", "attributes")
        self.base_commit = fixture_git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        self.patch = self.make_patch(self.project, self.base_commit)
        self.expected_tree = self.oracle_tree(self.project, self.base_commit, self.patch)[0]
        hooks = self.base / "hooks"
        hooks.mkdir()
        for directory in (hooks, self.project / ".git" / "hooks"):
            for name in ("post-index-change", "reference-transaction", "post-checkout",
                         "pre-commit", "post-commit", "pre-auto-gc", "post-rewrite"):
                (directory / name).write_text(f'#!/bin/sh\necho "hook {name}" >> "{self.marker}"\n')
                (directory / name).chmod(0o755)
        attributes = self.base / "global-attributes"
        attributes.write_text("* filter=trip diff=trip\n")
        included = self.base / "included.config"
        included.write_text(f"[core]\n\tfsmonitor = {self.tripwire}\n")
        settings = {
            "core.hooksPath": str(hooks),
            "core.fsmonitor": self.tripwire,
            "core.pager": self.tripwire,
            "core.editor": self.tripwire,
            "core.askPass": self.tripwire,
            "core.sshCommand": self.tripwire,
            "core.attributesFile": str(attributes),
            "core.alternateRefsCommand": self.tripwire,
            "filter.trip.clean": self.tripwire,
            "filter.trip.smudge": self.tripwire,
            "filter.trip.process": self.tripwire,
            "filter.trip.required": "true",
            "diff.trip.textconv": self.tripwire,
            "diff.trip.command": self.tripwire,
            "diff.external": self.tripwire,
            "merge.trip.driver": self.tripwire,
            "gpg.program": self.tripwire,
            "gpg.ssh.program": self.tripwire,
            "commit.gpgSign": "true",
            "log.showSignature": "true",
            "credential.helper": f"!{self.tripwire}",
            "uploadpack.packObjectsHook": self.tripwire,
            "include.path": str(included),
            "pager.cat-file": self.tripwire,
            "sequence.editor": self.tripwire,
        }
        for key, value in settings.items():
            fixture_git(self.project, "config", key, value)
        global_config = self.base / "global.config"
        global_config.write_text(
            f"[core]\n\tfsmonitor = {self.tripwire}\n\thooksPath = {hooks}\n"
            f"[filter \"trip\"]\n\tclean = {self.tripwire}\n"
        )
        self.hostile_environment = {
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "",
            "GIT_EXTERNAL_DIFF": self.tripwire, "GIT_PAGER": self.tripwire,
            "GIT_EDITOR": self.tripwire, "GIT_ASKPASS": self.tripwire, "SSH_ASKPASS": self.tripwire,
            "GIT_SSH_COMMAND": self.tripwire, "GIT_SSH": self.tripwire,
            "GIT_CONFIG_PARAMETERS": f"'core.fsmonitor'='{self.tripwire}'",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(hooks),
            "GIT_DIR": str(self.project / ".git"),
            "GIT_WORK_TREE": str(self.project),
            "GIT_INDEX_FILE": str(self.project / ".git" / "index"),
            "GIT_OBJECT_DIRECTORY": str(self.project / ".git" / "objects"),
            "GIT_TRACE": str(self.base / "trace-written"),
            "HOME": str(self.base),
        }
        # The traps are live for ordinary git use (shown on a copy, so the project stays clean).
        armed = self.base / "armed-copy"
        shutil.copytree(self.project, armed, symlinks=True)
        (armed / "src" / "app.py").write_text("changed\n")
        armed_env = {**FIXTURE_ENV, "GIT_CONFIG_GLOBAL": "/dev/null"}
        fixture_git(armed, "status", "--porcelain", check=False, env=armed_env)
        fixture_git(armed, "diff", check=False, env=armed_env)
        self.assertTrue(self.marker.exists(), "the tripwire repository is not armed")
        self.marker.unlink()

    def assert_never_ran(self) -> None:
        self.assertFalse(
            self.marker.exists(), self.marker.read_text() if self.marker.exists() else "",
        )
        self.assertFalse((self.base / "trace-written").exists())

    @needs_real_git
    def test_no_tripwire_fires_on_any_entry_point(self) -> None:
        before = folder_map(self.project)
        bad_patch = self.patch + (
            b"diff --git a/src/keep.py b/src/keep.py\n--- a/src/keep.py\n+++ b/src/keep.py\n"
            b"@@ -1 +1 @@\n-NOT THE BASE\n+KEEP = False\n"
        )
        with mock.patch.dict(os.environ, self.hostile_environment):
            with self.subTest(entry="git_version_problem"):
                self.assertIsNone(hg.git_version_problem(REAL_GIT))
                self.assert_never_ran()
            with self.subTest(entry="build_candidate_tree, default git"):
                with mock.patch.object(hg, "git_executable", return_value=REAL_GIT):
                    built = hg.build_candidate_tree(self.project, self.store, self.base_commit, self.patch)
                self.assertTrue(built.ok, getattr(built, "message", ""))
                self.assert_never_ran()
            with self.subTest(entry="build_candidate_tree, reused store"):
                again = hg.build_candidate_tree(
                    self.project, self.store, self.base_commit, self.patch, executable=REAL_GIT,
                )
                self.assertTrue(again.ok)
                self.assertEqual(built.tree, again.tree)
                self.assert_never_ran()
            with self.subTest(entry="build_candidate_tree, patch refused"):
                refused = hg.build_candidate_tree(
                    self.project, self.store, self.base_commit, bad_patch, executable=REAL_GIT,
                )
                self.assertEqual("patch_does_not_apply", refused.reason)
                self.assert_never_ran()
            with self.subTest(entry="run_hardened_git on the owner's repository"):
                for arguments in (("rev-parse", "HEAD"), ("cat-file", "-p", self.base_commit),
                                  ("ls-tree", "-r", self.base_commit), ("config", "--get", "core.bare")):
                    completed = hg.run_hardened_git(REAL_GIT, self.project, *arguments)
                    self.assertEqual(0, completed.returncode, arguments)
                self.assert_never_ran()
        self.assertEqual(before, folder_map(self.project))
        # No filter ran: the candidate holds the patch's bytes exactly, not transformed ones.
        added = self.store_git("cat-file", "blob", f"{built.tree}:new/added.py").stdout
        self.assertEqual(b"ADDED = 1\n", added)
        self.assertEqual(self.expected_tree, built.tree)
        self.assert_never_ran()


if __name__ == "__main__":
    unittest.main()
