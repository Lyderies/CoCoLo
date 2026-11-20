"""M3 slice 2: the merge into the owner's working directory, its exact undo, and crash recovery.

Requirements (DESIGN-0.4.0-M3.md draft 2 §3, draft 3 §2-3 slice 2; the owner's decision: COCOLO
merges verified work into the working directory itself, a file the owner changed since the child's
base is never overwritten, every merge keeps an exact undo):
- M3-21 The merge. Preconditions first, each refused with a fixed reason and nothing changed
  (§3.1): a merge, rebase, cherry-pick, revert or bisect in progress; a sparse or skip-worktree
  path, a submodule or nested repository, a symlink in the way; a path git converts (filter,
  text/eol, working-tree-encoding, ident, in the base or the candidate, `.gitattributes` or
  `info/attributes`; `core.autocrlf=true`); a filesystem without atomic exchange; too large; and
  the owner's HEAD no longer the candidate's base. Per file (§3.2): modify by atomic exchange then a
  check of the swapped-out file against the base (size, type, mode, bytes), exchanged back on any
  difference; add by a rename the kernel refuses if anything is there; delete by renaming away,
  checking, renaming back on a mismatch; parent folders created one at a time and recorded. The
  whole merge (§3.3): undo set stored and a durable journal written first, files in sorted order
  with progress after each, a final check of every target and every kept old copy, any mismatch
  rolls everything back. Git fenced (§3.4): `.git/index.lock` held with a token for the apply,
  waited for up to 10 s when the owner's git holds it, removed only when it holds COCOLO's token.
- M3-22 Exact undo (§3.5): bytes and modes of every touched file restored, deleted files
  re-created, added files and created folders removed; refused with nothing changed when the owner
  edited a merged file since; merges touching the same file undone newest first; the undo needs no
  git objects.
- M3-27 Crash recovery (§3.6): a merge (or undo) killed at any point is rolled back unless every file
  was already written, then completed; a path recovery cannot classify is left alone and reported.
- M3-29 Nothing of COCOLO's left in the owner's repository: `.git` identical after every case, no
  `.cocolo-merge-*` name left behind.
- The slice-2 adversarial review (NOT READY; F01-F11, S01, C01 and the small items), each finding a
  test written first: the owner's newest file always stays at its path or the result says where it
  is (a save by rename just before a delete, an in-place edit of COCOLO's new file, also in the
  instant before the swap back, a write into an old file after the final check, a real editor
  process saving while merges run); a SIGKILL at every file-system call of a merge and of an undo
  (not only the hook points) recovers to all or nothing with nothing left in the folder, in `.git`
  or in the state folder; a journal this COCOLO cannot read or does not trust is kept with its undo
  and never acted on; the fence is never left empty and never removes a lock that is not COCOLO's;
  a moved HEAD, a state-folder failure, new files' umask and a case-only rename are reported plainly.

The folder oracle (`snapshot`): every entry of the working tree (tracked, untracked, ignored) and of
`.git`, with type, mode, size, inode, modification time and content. Folder modification times are
left out (creating and removing a temporary name changes them; nothing else in the folder does).
A stopped merge must leave the oracle identical; a merged one may differ only at the patch's paths
and the folders it created. Every repository is a throwaway one in a temporary folder; ordinary
git builds the fixtures and the independent oracle there only.
"""
from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import errno
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT

from codex_agents import atomic_rename
from codex_agents import git_plumbing
from codex_agents import hardened_git as hg
from codex_agents import write_merge as wm

FIXTURE_ENV = {
    "PATH": os.defpath, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
REAL_GIT = git_plumbing.git_executable()
GIT_REASON = None
if REAL_GIT is None:
    GIT_REASON = "no trusted git binary on this machine"
elif hg.git_version_problem(REAL_GIT) is not None:
    GIT_REASON = "this machine's git is below COCOLO's floor"
needs_real_git = unittest.skipIf(GIT_REASON is not None, GIT_REASON or "")

# The default change, in the merge's (sorted) order: blob.bin add, gone.txt delete,
# new/deep/added.py add (two new folders), src/app.py modify, tool.sh modify (executable).
DEFAULT_CHANGES = {
    "blob.bin": "added", "gone.txt": "deleted", "new/deep/added.py": "added",
    "src/app.py": "modified", "tool.sh": "modified",
}
DEFAULT_FOLDERS = ("new", "new/deep")


def fixture_git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    """Ordinary git, for fixtures and the oracle in throwaway folders only."""

    return subprocess.run(
        [REAL_GIT or "git", "-C", str(root), *arguments], env=FIXTURE_ENV,
        capture_output=True, stdin=subprocess.DEVNULL, check=check, timeout=60,
    )


def entry_map(root: Path) -> dict[str, tuple]:
    result: dict[str, tuple] = {}
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            info = os.lstat(path)
            relative = str(path.relative_to(root))
            kind = stat.S_IFMT(info.st_mode)
            if stat.S_ISREG(info.st_mode):
                result[relative] = (
                    kind, stat.S_IMODE(info.st_mode), info.st_size, info.st_ino, info.st_mtime_ns,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
            elif stat.S_ISLNK(info.st_mode):
                result[relative] = (kind, info.st_ino, os.readlink(path))
            else:
                result[relative] = (kind, stat.S_IMODE(info.st_mode), info.st_ino)
    return result


def snapshot(project: Path) -> dict[str, dict[str, tuple]]:
    tree = entry_map(project)
    git = {key[len(".git/"):]: value for key, value in tree.items() if key.startswith(".git/")}
    tree = {key: value for key, value in tree.items() if key != ".git" and not key.startswith(".git/")}
    return {"tree": tree, "git": git}


def contents(project: Path) -> dict[str, tuple]:
    """Type, mode and content of every working-tree entry (no inode or time): for undo's exactness."""

    result = {}
    for key, value in snapshot(project)["tree"].items():
        if value[0] == stat.S_IFREG:
            result[key] = (value[0], value[1], value[5])
        elif value[0] == stat.S_IFLNK:
            result[key] = (value[0], value[2])
        else:
            result[key] = (value[0], value[1])
    return result


def leftovers(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob(".cocolo-merge-*"))


class Workspace(unittest.TestCase):
    """A throwaway folder: the owner's repository, COCOLO's private store and merge state."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base_folder = Path(self.temporary.name)
        self.project = self.base_folder / "project"
        self.stores = self.base_folder / "stores"
        self.stores.mkdir()
        self.state = self.base_folder / "merge-state"
        self.counter = 0

    def make_repository(self, root: Path | None = None, *, setup=None) -> str:
        root = root or self.project
        root.mkdir(parents=True)
        fixture_git(root, "init", "-q")
        (root / "README.md").write_text("hello\n")
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("def main():\n    return 1\n")
        (root / "src" / "keep.py").write_text("KEEP = True\n")
        (root / "tool.sh").write_text("#!/bin/sh\necho tool\n")
        (root / "tool.sh").chmod(0o755)
        (root / "gone.txt").write_text("to be deleted\n")
        (root / ".gitignore").write_text("*.log\n")
        if setup is not None:
            setup(root)
        fixture_git(root, "add", "-A")
        fixture_git(root, "commit", "-qm", "base")
        # The owner's own state the merge must leave alone: untracked, ignored, a dirty untouched file.
        (root / "notes.txt").write_text("owner notes\n")
        (root / "debug.log").write_text("ignored\n")
        (root / "src" / "keep.py").write_text("KEEP = 'owner edit, uncommitted'\n")
        return fixture_git(root, "rev-parse", "HEAD").stdout.decode().strip()

    @staticmethod
    def default_change(clone: Path) -> None:
        (clone / "src" / "app.py").write_text("def main():\n    return 2\n")
        (clone / "tool.sh").write_text("#!/bin/sh\necho tool 2\n")
        (clone / "new" / "deep").mkdir(parents=True)
        (clone / "new" / "deep" / "added.py").write_text("ADDED = 1\n")
        (clone / "blob.bin").write_bytes(bytes(range(256)) * 4)
        (clone / "gone.txt").unlink()

    def make_patch(self, base: str, change=None, root: Path | None = None) -> bytes:
        root = root or self.project
        self.counter += 1
        clone = self.base_folder / f"patch-source-{self.counter}"
        fixture_git(self.base_folder, "clone", "-q", "--no-local", str(root), str(clone))
        fixture_git(clone, "checkout", "-q", base)
        (change or self.default_change)(clone)
        fixture_git(clone, "add", "-A")
        patch = fixture_git(clone, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv").stdout
        shutil.rmtree(clone)
        return patch

    def candidate(self, base: str, change=None, root: Path | None = None) -> hg.CandidateTree:
        root = root or self.project
        patch = self.make_patch(base, change, root)
        result = hg.build_candidate_tree(root, self.stores / "store", base, patch, executable=REAL_GIT)
        self.assertTrue(result.ok, getattr(result, "message", ""))
        return result

    def merge(self, candidate: hg.CandidateTree, *, task: str = "task-1", root: Path | None = None, **options):
        options.setdefault("git_busy_wait", 0.3)
        return wm.merge_candidate(
            root or self.project, self.state, candidate, task=task, executable=REAL_GIT, **options,
        )

    def undo(self, merge_id: str, **options):
        options.setdefault("git_busy_wait", 0.3)
        return wm.undo_merge(self.project, self.state, merge_id, executable=REAL_GIT, **options)

    # --- assertions -------------------------------------------------------------------------

    def assert_unchanged(self, before: dict, *, root: Path | None = None) -> None:
        root = root or self.project
        after = snapshot(root)
        self.assertEqual(before["git"], after["git"])  # MG20 / M3-29
        self.assertEqual(before["tree"], after["tree"])
        self.assertEqual([], leftovers(root))

    def assert_stopped(self, result, reason: str, path: str | None = None) -> None:
        self.assertFalse(result.ok, result)
        self.assertEqual(reason, result.reason, result.message)
        self.assertIn("Nothing was changed", result.message)
        if path is not None:
            self.assertEqual(path, result.path)
            self.assertIn(path, result.message)

    def expected_files(self, base: str, patch: bytes) -> dict[str, tuple[bool, bytes]]:
        """Base plus the change, by ordinary git in an independent clone: path -> (executable, bytes)."""

        self.counter += 1
        clone = self.base_folder / f"oracle-{self.counter}"
        fixture_git(self.base_folder, "clone", "-q", "--no-local", str(self.project), str(clone))
        fixture_git(clone, "checkout", "-q", base)
        patch_file = self.base_folder / f"oracle-{self.counter}.patch"
        patch_file.write_bytes(patch)
        fixture_git(clone, "apply", "--index", str(patch_file))
        listed = fixture_git(clone, "ls-files", "-z").stdout.decode().split("\0")
        result = {}
        for name in filter(None, listed):
            path = clone / name
            result[name] = (bool(os.lstat(path).st_mode & 0o100), path.read_bytes())
        shutil.rmtree(clone)
        return result

    def assert_merged(self, before: dict, changes: dict[str, str], folders=(), *, root: Path | None = None) -> None:
        """Only the patch's paths and the created folders differ; .git is identical."""

        root = root or self.project
        after = snapshot(root)
        self.assertEqual(before["git"], after["git"])  # MG20: index, HEAD, refs, objects, config
        self.assertEqual([], leftovers(root))
        touched = set(changes) | set(folders)
        for key in set(before["tree"]) | set(after["tree"]):
            if key in touched:
                continue
            self.assertEqual(before["tree"].get(key), after["tree"].get(key), key)
        for path, change in changes.items():
            if change == "deleted":
                self.assertNotIn(path, after["tree"])
            else:
                self.assertIn(path, after["tree"])
        for folder in folders:
            self.assertEqual(stat.S_IFDIR, after["tree"][folder][0])


def git_dir_listing(project: Path) -> list[str]:
    return sorted(os.listdir(project / ".git"))


# --------------------------------------------------------------------------------------------
# M3-21: a clean merge, and the hardened git calls the merge adds
# --------------------------------------------------------------------------------------------

@needs_real_git
class CleanMergeTests(Workspace):
    def test_a_clean_merge_equals_base_plus_the_change_exactly(self) -> None:
        base = self.make_repository()
        patch = self.make_patch(base)
        candidate = hg.build_candidate_tree(self.project, self.stores / "store", base, patch, executable=REAL_GIT)
        tool_mode = stat.S_IMODE(os.lstat(self.project / "tool.sh").st_mode)
        before = snapshot(self.project)
        result = self.merge(candidate)
        self.assertTrue(result.ok, getattr(result, "message", result))
        self.assertEqual(DEFAULT_CHANGES, dict(result.changes))
        self.assertEqual(DEFAULT_FOLDERS, tuple(result.created_folders))
        self.assertEqual("task-1", result.task)
        self.assert_merged(before, DEFAULT_CHANGES, DEFAULT_FOLDERS)
        # Every tracked file equals ordinary git's base plus the change (bytes and executable bit);
        # the owner's uncommitted edit to an untouched file, untracked and ignored files are kept.
        expected = self.expected_files(base, patch)
        expected["src/keep.py"] = (False, b"KEEP = 'owner edit, uncommitted'\n")
        for name, (executable, data) in expected.items():
            self.assertEqual(data, (self.project / name).read_bytes(), name)
            self.assertEqual(executable, bool(os.lstat(self.project / name).st_mode & 0o100), name)
        self.assertFalse((self.project / "gone.txt").exists())
        self.assertEqual(b"owner notes\n", (self.project / "notes.txt").read_bytes())
        self.assertEqual(b"ignored\n", (self.project / "debug.log").read_bytes())
        # A modified file keeps the owner's permission bits.
        self.assertEqual(tool_mode, stat.S_IMODE(os.lstat(self.project / "tool.sh").st_mode))
        # Nothing staged or committed: git sees ordinary uncommitted edits.
        status = fixture_git(self.project, "status", "--porcelain").stdout.decode()
        for line in (" M src/app.py", " M tool.sh", " D gone.txt", "?? blob.bin", "?? new/"):
            self.assertIn(line, status)
        # The merge is kept for undo, and its journal says merged.
        kept = wm.kept_merges(self.state)
        self.assertEqual([result.merge_id], [record.merge_id for record in kept])
        self.assertEqual("merged", kept[0].state)

    def test_a_hard_link_elsewhere_keeps_the_owners_bytes(self) -> None:  # MG08
        base = self.make_repository()
        candidate = self.candidate(base)
        outside = self.base_folder / "outside-link"
        os.link(self.project / "src" / "app.py", outside)
        result = self.merge(candidate)
        self.assertTrue(result.ok, getattr(result, "message", result))
        self.assertEqual(b"def main():\n    return 1\n", outside.read_bytes())
        self.assertEqual(b"def main():\n    return 2\n", (self.project / "src" / "app.py").read_bytes())

    def test_the_merge_needs_no_write_into_git_and_leaves_no_names(self) -> None:  # MG20
        base = self.make_repository()
        candidate = self.candidate(base)
        listing = git_dir_listing(self.project)
        index = (self.project / ".git" / "index").read_bytes()
        result = self.merge(candidate)
        self.assertTrue(result.ok)
        self.assertEqual(listing, git_dir_listing(self.project))
        self.assertEqual(index, (self.project / ".git" / "index").read_bytes())
        self.assertFalse((self.project / ".git" / "index.lock").exists())


class MergeGitCallTests(unittest.TestCase):
    """The read-only plumbing the merge adds to the hardened allow-list, in fixed shapes only."""

    def test_the_merge_reads_are_allowed_in_fixed_shapes_only(self) -> None:
        oid = "0" * 40
        allowed = (
            ("diff-tree", "-r", "-z", "--raw", "--no-renames", "--full-index", oid, oid),
            ("check-attr", "-z", f"--source={oid}", "filter", "text", "--", "a.txt"),
            ("ls-files", "-z", "-v", "--", "a.txt"),
        )
        refused = (
            ("diff-tree", "-p", oid, oid), ("diff-tree", "-r", "-z", "--raw", "--ext-diff", oid, oid),
            ("diff-tree", "-r", "-z", "--raw", "--no-renames", "--full-index", "--textconv", oid, oid),
            ("check-attr", "-z", "filter", "--", "a.txt"), ("check-attr", "--all", "a.txt"),
            ("check-attr", "-z", "--source=HEAD", "filter", "--", "a.txt"),
            ("ls-files", "--others"), ("ls-files", "-z", "-v", "--with-tree=HEAD", "--", "a"),
        )
        seen = []

        def fake_output(argv, **options):
            seen.append(argv)
            return subprocess.CompletedProcess(argv, 0, "")

        with mock.patch.object(hg, "_bounded_git_output", fake_output):
            for arguments in allowed:
                with self.subTest(allowed=arguments):
                    hg.run_hardened_git("/usr/bin/true", Path("/"), *arguments)
            for arguments in refused:
                with self.subTest(refused=arguments), self.assertRaises(hg.GitCallRefused):
                    hg.run_hardened_git("/usr/bin/true", Path("/"), *arguments)
        self.assertEqual(len(allowed), len(seen))


# --------------------------------------------------------------------------------------------
# M3-21: the owner's changes and the preconditions, each refused with nothing changed
# --------------------------------------------------------------------------------------------

@needs_real_git
class OwnerChangeTests(Workspace):
    def test_an_owner_edit_to_a_touched_file_stops_the_merge(self) -> None:  # MG01
        base = self.make_repository()
        candidate = self.candidate(base)
        (self.project / "src" / "app.py").write_text("def main():\n    return 'owner'\n")
        before = snapshot(self.project)
        result = self.merge(candidate)
        self.assert_stopped(result, "write_merge_owner_changed", "src/app.py")
        self.assert_unchanged(before)

    def test_an_owner_mode_change_on_a_touched_file_stops_the_merge(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        (self.project / "tool.sh").chmod(0o644)
        before = snapshot(self.project)
        self.assert_stopped(self.merge(candidate), "write_merge_owner_changed", "tool.sh")
        self.assert_unchanged(before)

    def test_an_owner_commit_to_a_touched_file_stops_the_merge(self) -> None:  # MG02
        base = self.make_repository()
        candidate = self.candidate(base)
        (self.project / "src" / "app.py").write_text("def main():\n    return 'committed'\n")
        fixture_git(self.project, "commit", "-qam", "owner")
        before = snapshot(self.project)
        self.assert_stopped(self.merge(candidate), "write_merge_base_changed")
        self.assert_unchanged(before)

    def test_head_moved_without_touching_the_files_needs_a_candidate_on_the_new_head(self) -> None:  # MG03
        base = self.make_repository()
        old = self.candidate(base)
        (self.project / "README.md").write_text("hello again\n")
        fixture_git(self.project, "commit", "-qm", "owner", "--", "README.md")
        head = fixture_git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        before = snapshot(self.project)
        self.assert_stopped(self.merge(old), "write_merge_base_changed")
        self.assert_unchanged(before)
        # F10a: the caller rebuilds the candidate on the new HEAD (and re-tests); that one merges.
        fresh = self.candidate(head)
        result = self.merge(fresh, task="task-2")
        self.assertTrue(result.ok, getattr(result, "message", result))
        self.assert_merged(before, DEFAULT_CHANGES, DEFAULT_FOLDERS)

    def test_an_owner_deletion_of_a_touched_file_stops_the_merge(self) -> None:  # MG04
        base = self.make_repository()
        candidate = self.candidate(base)
        (self.project / "src" / "app.py").unlink()
        before = snapshot(self.project)
        self.assert_stopped(self.merge(candidate), "write_merge_owner_changed", "src/app.py")
        self.assert_unchanged(before)

    def test_an_add_over_anything_already_there_stops_the_merge(self) -> None:  # MG05
        for kind in ("file", "folder", "symlink"):
            with self.subTest(kind=kind):
                self.fresh_project()
                base = self.make_repository()
                candidate = self.candidate(base)
                target = self.project / "blob.bin"
                if kind == "file":
                    target.write_text("owner's own file\n")
                elif kind == "folder":
                    target.mkdir()
                else:
                    os.symlink("README.md", target)
                before = snapshot(self.project)
                result = self.merge(candidate)
                self.assert_stopped(
                    result, "write_merge_path_unsafe" if kind == "symlink" else "write_merge_path_taken", "blob.bin",
                )
                self.assert_unchanged(before)

    def fresh_project(self) -> None:
        """A fresh project, store and state for the next subtest."""

        for path in (self.project, self.stores / "store", self.state):
            if path.exists():
                shutil.rmtree(path)

    def test_a_letter_case_collision_stops_the_merge(self) -> None:  # MG06
        (self.base_folder / "x").touch()
        insensitive = (self.base_folder / "X").exists()
        (self.base_folder / "x").unlink()
        if not insensitive:
            self.skipTest("this temporary folder is on a case-sensitive filesystem")
        base = self.make_repository()

        def add_variant(clone):
            # Staged directly: on this volume the working tree cannot hold both names.
            blob = subprocess.run(
                [REAL_GIT, "-C", str(clone), "hash-object", "-w", "--stdin"], input=b"collides\n",
                env=FIXTURE_ENV, capture_output=True, check=True, timeout=60,
            ).stdout.decode().strip()
            fixture_git(clone, "update-index", "--add", "--cacheinfo", f"100644,{blob},readme.md")

        candidate = self.candidate(base, add_variant)
        before = snapshot(self.project)
        self.assert_stopped(self.merge(candidate), "write_merge_path_taken", "readme.md")
        self.assert_unchanged(before)

    def test_the_kernel_refuses_an_add_when_a_name_appears_during_the_merge(self) -> None:  # MG06
        base = self.make_repository()
        candidate = self.candidate(base, lambda clone: (clone / "new.txt").write_text("child\n"))
        before = snapshot(self.project)
        variant = "NEW.TXT" if (self.project / "README.MD").exists() else "new.txt"

        def hook(point, path):
            if point == "temp_written" and path == "new.txt":
                (self.project / variant).write_text("owner, just now\n")

        result = self.merge(candidate, fault_hook=hook)
        self.assert_stopped(result, "write_merge_path_taken", "new.txt")
        self.assertEqual(b"owner, just now\n", (self.project / variant).read_bytes())
        (self.project / variant).unlink()
        self.assertEqual(before["tree"], {
            key: value for key, value in snapshot(self.project)["tree"].items()
        })
        self.assertEqual([], leftovers(self.project))

    def test_symlinks_in_the_way_stop_the_merge(self) -> None:  # MG07
        for case in ("parent", "target"):
            with self.subTest(case=case):
                self.fresh_project()
                base = self.make_repository()
                candidate = self.candidate(base)
                elsewhere = self.base_folder / f"elsewhere-{case}"
                if case == "parent":
                    shutil.copytree(self.project / "src", elsewhere)
                    shutil.rmtree(self.project / "src")
                    os.symlink(elsewhere, self.project / "src")
                else:
                    (self.project / "src" / "app.py").unlink()
                    os.symlink("keep.py", self.project / "src" / "app.py")
                before = snapshot(self.project)
                outside = entry_map(elsewhere) if case == "parent" else None
                self.assert_stopped(self.merge(candidate), "write_merge_path_unsafe", "src/app.py")
                self.assert_unchanged(before)
                if outside is not None:
                    self.assertEqual(outside, entry_map(elsewhere))

    def test_an_owner_commit_between_prepare_and_fence_stops_the_merge(self) -> None:  # MG13
        base = self.make_repository()
        candidate = self.candidate(base)
        captured = {}

        def hook(point, path):
            if point == "prepared":
                (self.project / "README.md").write_text("owner commit during prepare\n")
                fixture_git(self.project, "commit", "-qm", "owner", "--", "README.md")
                captured["before"] = snapshot(self.project)

        result = self.merge(candidate, fault_hook=hook)
        self.assert_stopped(result, "write_merge_base_changed")
        self.assert_unchanged(captured["before"])
        self.assertEqual("stopped", wm.kept_merges(self.state, include_finished=True)[0].state)


@needs_real_git
class PreconditionTests(Workspace):
    def fresh(self, setup=None) -> str:
        for path in (self.project, self.stores / "store", self.state):
            if path.exists():
                shutil.rmtree(path)
        return self.make_repository(setup=setup)

    def test_git_operations_in_progress_stop_the_merge(self) -> None:  # MG10
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG", "rebase-merge", "rebase-apply"):
            with self.subTest(marker=marker):
                base = self.fresh()
                candidate = self.candidate(base)
                path = self.project / ".git" / marker
                if marker.startswith("rebase"):
                    path.mkdir()
                else:
                    path.write_text(base + "\n")
                before = snapshot(self.project)
                self.assert_stopped(self.merge(candidate), "write_merge_repo_busy")
                self.assert_unchanged(before)

    def test_sparse_and_skip_worktree_paths_stop_the_merge(self) -> None:
        for case in ("skip-worktree", "sparse-checkout"):
            with self.subTest(case=case):
                base = self.fresh()
                candidate = self.candidate(base)
                if case == "skip-worktree":
                    fixture_git(self.project, "update-index", "--skip-worktree", "src/app.py")
                else:
                    fixture_git(self.project, "config", "core.sparseCheckout", "true")
                before = snapshot(self.project)
                self.assert_stopped(self.merge(candidate), "write_merge_path_unsafe")
                self.assert_unchanged(before)

    def test_submodules_and_nested_repositories_stop_the_merge(self) -> None:
        # A nested repository inside a folder the change writes into.
        base = self.fresh()
        candidate = self.candidate(base)
        fixture_git(self.project / "src", "init", "-q")
        before = snapshot(self.project)
        self.assert_stopped(self.merge(candidate), "write_merge_path_unsafe", "src/app.py")
        self.assert_unchanged(before)
        # A gitlink (submodule entry) the change touches.
        commit = "1" * 40
        self.fresh()
        fixture_git(self.project, "update-index", "--add", "--cacheinfo", f"160000,{commit},vendor/lib")
        fixture_git(self.project, "commit", "-qm", "submodule")
        base = fixture_git(self.project, "rev-parse", "HEAD").stdout.decode().strip()

        def drop_gitlink(clone):
            fixture_git(clone, "rm", "-q", "--cached", "vendor/lib")

        candidate = self.candidate(base, drop_gitlink)
        before = snapshot(self.project)
        self.assert_stopped(self.merge(candidate), "write_merge_path_unsafe", "vendor/lib")
        self.assert_unchanged(before)

    def test_converted_paths_stop_the_merge(self) -> None:  # MG09
        attributes = {
            "filter": "src/app.py filter=lfs\n", "text": "src/app.py text\n", "text-auto": "src/*.py text=auto\n",
            "eol": "src/app.py eol=crlf\n", "encoding": "src/app.py working-tree-encoding=ISO-8859-1\n",
            "ident": "src/app.py ident\n",
        }
        for name, line in attributes.items():
            with self.subTest(attribute=name, where="base"):
                base = self.fresh(lambda root, line=line: (root / ".gitattributes").write_text(line))
                candidate = self.candidate(base)
                before = snapshot(self.project)
                self.assert_stopped(self.merge(candidate), "write_merge_transformed_path", "src/app.py")
                self.assert_unchanged(before)
        with self.subTest(where="candidate .gitattributes"):
            base = self.fresh()

            def change(clone):
                self.default_change(clone)
                (clone / ".gitattributes").write_text("tool.sh filter=lfs\n")

            candidate = self.candidate(base, change)
            before = snapshot(self.project)
            self.assert_stopped(self.merge(candidate), "write_merge_transformed_path", "tool.sh")
            self.assert_unchanged(before)
        with self.subTest(where="info/attributes"):
            base = self.fresh()
            candidate = self.candidate(base)
            (self.project / ".git" / "info").mkdir(exist_ok=True)
            (self.project / ".git" / "info" / "attributes").write_text("*.sh ident\n")
            before = snapshot(self.project)
            self.assert_stopped(self.merge(candidate), "write_merge_transformed_path", "tool.sh")
            self.assert_unchanged(before)
        with self.subTest(where="core.autocrlf"):
            base = self.fresh()
            candidate = self.candidate(base)
            fixture_git(self.project, "config", "core.autocrlf", "true")
            before = snapshot(self.project)
            self.assert_stopped(self.merge(candidate), "write_merge_transformed_path")
            self.assert_unchanged(before)

    def test_changes_the_merge_does_not_make_stop_it(self) -> None:
        for case in ("mode", "symlink"):
            with self.subTest(case=case):
                base = self.fresh()
                if case == "mode":
                    candidate = self.candidate(base, lambda clone: (clone / "tool.sh").chmod(0o644))
                    path = "tool.sh"
                else:
                    candidate = self.candidate(base, lambda clone: os.symlink("README.md", clone / "link"))
                    path = "link"
                before = snapshot(self.project)
                self.assert_stopped(self.merge(candidate), "write_merge_unsupported_change", path)
                self.assert_unchanged(before)

    def test_too_large_stops_the_merge(self) -> None:
        for limit, value in (("MAX_FILE_BYTES", 16), ("MAX_UNDO_BYTES", 40)):
            with self.subTest(limit=limit):
                base = self.fresh()
                candidate = self.candidate(base)
                before = snapshot(self.project)
                with mock.patch.object(wm, limit, value):
                    self.assert_stopped(self.merge(candidate), "write_merge_too_large")
                self.assert_unchanged(before)
        self.assertEqual(4 * 1_048_576, wm.MAX_FILE_BYTES)
        self.assertEqual(16 * 1_048_576, wm.MAX_UNDO_BYTES)

    def test_a_filesystem_without_atomic_exchange_stops_the_merge(self) -> None:  # MG12
        base = self.fresh()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        for code in (errno.ENOTSUP, errno.EINVAL):
            with self.subTest(errno=errno.errorcode[code]), \
                    mock.patch.object(atomic_rename, "_rename", return_value=code):
                self.assert_stopped(self.merge(candidate), "write_merge_fs_unsupported")
            self.assert_unchanged(before)

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("hdiutil"), "macOS disk images only")
    def test_a_project_on_a_fat_volume_is_refused(self) -> None:  # MG12, real filesystem
        image = self.base_folder / "fat.dmg"
        mount = self.base_folder / "fat"
        mount.mkdir()
        subprocess.run(["hdiutil", "create", "-quiet", "-size", "16m", "-fs", "MS-DOS", "-volname", "MG12",
                        "-layout", "NONE", str(image)], check=True, timeout=120, stdin=subprocess.DEVNULL)
        subprocess.run(["hdiutil", "attach", "-quiet", "-nobrowse", "-noverify", "-mountpoint", str(mount),
                        str(image)], check=True, timeout=120, stdin=subprocess.DEVNULL)
        self.addCleanup(subprocess.run, ["hdiutil", "detach", "-quiet", "-force", str(mount)],
                        timeout=120, stdin=subprocess.DEVNULL)
        root = mount / "project"
        base = self.make_repository(root)
        candidate = self.candidate(base, root=root)
        before = {key: value for key, value in contents(root).items() if not Path(key).name.startswith("._")}
        result = self.merge(candidate, root=root)
        self.assert_stopped(result, "write_merge_fs_unsupported")
        after = {key: value for key, value in contents(root).items() if not Path(key).name.startswith("._")}
        self.assertEqual(before, after)
        self.assertEqual([], leftovers(root))

    def test_the_merge_state_may_not_be_inside_the_repository(self) -> None:
        base = self.fresh()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        for state in (self.project / "cocolo-state", self.project / ".git" / "cocolo-state"):
            with self.subTest(state=state):
                result = wm.merge_candidate(self.project, state, candidate, task="t", executable=REAL_GIT)
                self.assert_stopped(result, "write_merge_state_unusable")
        self.assert_unchanged(before)

    def test_a_candidate_from_another_repository_is_refused(self) -> None:
        base = self.fresh()
        other = self.base_folder / "other"
        other_base = self.make_repository(other)
        foreign = hg.build_candidate_tree(
            other, self.stores / "other-store", other_base, self.make_patch(other_base, root=other),
            executable=REAL_GIT,
        )
        self.assertTrue(foreign.ok)
        before = snapshot(self.project)
        result = self.merge(foreign)
        self.assertFalse(result.ok)
        self.assertIn(result.reason, ("store_other_repository", "write_merge_base_changed"))
        self.assert_unchanged(before)
        self.assertTrue(base)


# --------------------------------------------------------------------------------------------
# M3-21 §3.4: the git fence
# --------------------------------------------------------------------------------------------

@needs_real_git
class FenceTests(Workspace):
    def test_owner_git_commands_are_refused_during_the_apply(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        (self.project / "README.md").write_text("owner edit to an untouched file\n")
        outcomes = {}

        def hook(point, path):
            if point == "file_done" and path == "blob.bin":
                lock = self.project / ".git" / "index.lock"
                outcomes["lock"] = lock.exists()
                for command in (("commit", "-qam", "owner"), ("checkout", "--", "README.md"),
                                ("stash", "-q"), ("reset", "-q", "--hard"), ("add", "README.md")):
                    completed = fixture_git(self.project, *command, check=False)
                    outcomes[command[0]] = (completed.returncode, b"index.lock" in completed.stderr)

        result = self.merge(candidate, fault_hook=hook)
        self.assertTrue(result.ok, getattr(result, "message", result))
        self.assertTrue(outcomes.pop("lock"))
        for command, (code, mentions_lock) in outcomes.items():
            self.assertNotEqual(0, code, command)
            self.assertTrue(mentions_lock, command)
        self.assertFalse((self.project / ".git" / "index.lock").exists())
        self.assertEqual(b"owner edit to an untouched file\n", (self.project / "README.md").read_bytes())
        self.assertEqual(base, fixture_git(self.project, "rev-parse", "HEAD").stdout.decode().strip())

    def test_the_owners_lock_is_waited_for_then_the_merge_stops(self) -> None:  # MG11
        base = self.make_repository()
        candidate = self.candidate(base)
        lock = self.project / ".git" / "index.lock"
        lock.write_text("owner's git\n")
        before = snapshot(self.project)
        started = time.monotonic()
        result = self.merge(candidate, git_busy_wait=0.5)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assert_stopped(result, "write_merge_git_busy")
        self.assert_unchanged(before)
        self.assertEqual("owner's git\n", lock.read_text())
        self.assertEqual(10.0, wm.GIT_BUSY_WAIT_SECONDS)

    def test_a_lock_released_while_waiting_lets_the_merge_go_on(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        lock = self.project / ".git" / "index.lock"
        lock.write_text("owner's git\n")
        timer = threading.Timer(0.3, lock.unlink)
        timer.start()
        self.addCleanup(timer.cancel)
        result = self.merge(candidate, git_busy_wait=5)
        self.assertTrue(result.ok, getattr(result, "message", result))
        self.assertFalse(lock.exists())


# --------------------------------------------------------------------------------------------
# M3-21 §3.2-3.3, §3.7: owner writes during the merge, errors, concurrency
# --------------------------------------------------------------------------------------------

@needs_real_git
class DuringMergeTests(Workspace):
    def test_an_owner_save_by_rename_between_exchange_and_check_rolls_back(self) -> None:  # MG21
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        saved = {}

        def hook(point, path):
            if point == "exchanged" and path == "src/app.py":
                temporary = self.project / "src" / ".app.py.swp"
                temporary.write_text("owner's save\n")
                os.rename(temporary, self.project / "src" / "app.py")
                saved["inode"] = os.lstat(self.project / "src" / "app.py").st_ino

        result = self.merge(candidate, fault_hook=hook)
        self.assert_stopped(result, "write_merge_owner_changed", "src/app.py")
        target = self.project / "src" / "app.py"
        self.assertEqual(b"owner's save\n", target.read_bytes())
        self.assertEqual(saved["inode"], os.lstat(target).st_ino)
        after = snapshot(self.project)
        self.assertEqual(before["git"], after["git"])
        for key in set(before["tree"]) | set(after["tree"]):
            if key != "src/app.py":
                self.assertEqual(before["tree"].get(key), after["tree"].get(key), key)
        self.assertEqual([], leftovers(self.project))

    def test_an_in_place_write_into_the_old_inode_rolls_back(self) -> None:  # MG22
        for point in ("exchanged", "file_done", "all_done"):
            with self.subTest(point=point):
                for path in (self.project, self.stores / "store", self.state):
                    if path.exists():
                        shutil.rmtree(path)
                base = self.make_repository()
                candidate = self.candidate(base)
                target = self.project / "src" / "app.py"
                inode = os.lstat(target).st_ino
                descriptor = os.open(target, os.O_RDWR)
                self.addCleanup(os.close, descriptor)
                before = snapshot(self.project)

                def hook(at, path, point=point, descriptor=descriptor):
                    if at == point and (path in (None, "src/app.py")):
                        os.pwrite(descriptor, b"OWNER", 0)

                result = self.merge(candidate, fault_hook=hook)
                self.assert_stopped(result, "write_merge_owner_changed", "src/app.py")
                self.assertEqual(inode, os.lstat(target).st_ino)
                self.assertEqual(b"OWNER" + b"def main():\n    return 1\n"[5:], target.read_bytes())
                after = snapshot(self.project)
                for key in set(before["tree"]) | set(after["tree"]):
                    if key != "src/app.py":
                        self.assertEqual(before["tree"].get(key), after["tree"].get(key), key)
                self.assertEqual(before["git"], after["git"])
                self.assertEqual([], leftovers(self.project))

    def test_permission_denied_while_writing_a_temp_stops_with_nothing_changed(self) -> None:  # MG23
        base = self.make_repository()
        candidate = self.candidate(base)
        (self.project / "src").chmod(0o555)
        self.addCleanup((self.project / "src").chmod, 0o755)
        before = snapshot(self.project)
        result = self.merge(candidate)
        self.assert_stopped(result, "write_merge_failed", "src/app.py")
        self.assert_unchanged(before)

    def test_disk_full_while_writing_a_temp_stops_with_nothing_changed(self) -> None:  # MG23
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        real = wm._write_all

        def full(descriptor, data):
            if len(data) > 100:
                real(descriptor, data[:50])
                raise OSError(errno.ENOSPC, "No space left on device")
            real(descriptor, data)

        with mock.patch.object(wm, "_write_all", full):
            result = self.merge(candidate)
        self.assert_stopped(result, "write_merge_failed", "blob.bin")
        self.assert_unchanged(before)

    def test_two_merges_at_once_are_serialized_and_the_second_rechecks_its_base(self) -> None:  # MG24
        base = self.make_repository()
        first = self.candidate(base)
        second = self.candidate(base, lambda clone: (clone / "src" / "app.py").write_text("second\n"))
        before = snapshot(self.project)
        holding, release = threading.Event(), threading.Event()
        results = {}

        def hook(point, path):
            if point == "fenced":
                holding.set()
                release.wait(30)

        one = threading.Thread(target=lambda: results.__setitem__("one", self.merge(first, fault_hook=hook)))
        two = threading.Thread(target=lambda: results.__setitem__("two", self.merge(second, task="task-2")))
        one.start()
        self.assertTrue(holding.wait(30))
        two.start()
        time.sleep(0.5)
        self.assertTrue(two.is_alive())  # waiting for the integration lock
        self.assertNotIn("two", results)
        release.set()
        one.join(60)
        two.join(60)
        self.assertTrue(results["one"].ok)
        # The second's base check sees the first merge's content in src/app.py: owner_changed.
        self.assert_stopped(results["two"], "write_merge_owner_changed", "src/app.py")
        self.assert_merged(before, DEFAULT_CHANGES, DEFAULT_FOLDERS)


# --------------------------------------------------------------------------------------------
# M3-22: exact undo
# --------------------------------------------------------------------------------------------

@needs_real_git
class UndoTests(Workspace):
    def test_undo_restores_bytes_modes_deleted_files_and_removes_added_files_and_folders(self) -> None:  # U01-U04
        base = self.make_repository()
        candidate = self.candidate(base)
        (self.project / "tool.sh").chmod(0o750)  # the owner's own permission bits on a touched file
        before = snapshot(self.project)
        before_contents = contents(self.project)
        merged = self.merge(candidate)
        self.assertTrue(merged.ok, getattr(merged, "message", merged))
        undone = self.undo(merged.merge_id)
        self.assertTrue(undone.ok, getattr(undone, "message", undone))
        self.assertEqual(merged.merge_id, undone.merge_id)
        self.assertIn("modification times", undone.not_restored)
        self.assertEqual(before_contents, contents(self.project))
        after = snapshot(self.project)
        self.assertEqual(before["git"], after["git"])
        # Files the merge did not touch keep their inode and time too.
        for key, value in before["tree"].items():
            if key not in DEFAULT_CHANGES:
                self.assertEqual(value, after["tree"][key], key)
        self.assertEqual([], leftovers(self.project))
        self.assertFalse((self.project / "new").exists())
        self.assertEqual("undone", wm.kept_merges(self.state, include_finished=True)[0].state)
        self.assert_stopped(self.undo(merged.merge_id), "write_undo_not_kept")

    def test_undo_leaves_a_created_folder_the_owner_has_used(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        merged = self.merge(candidate)
        (self.project / "new" / "deep" / "owner.txt").write_text("mine\n")
        undone = self.undo(merged.merge_id)
        self.assertTrue(undone.ok, getattr(undone, "message", undone))
        self.assertEqual(b"mine\n", (self.project / "new" / "deep" / "owner.txt").read_bytes())
        self.assertFalse((self.project / "new" / "deep" / "added.py").exists())
        self.assertIn("new/deep", undone.folders_kept)

    def test_undo_is_refused_after_an_owner_edit_with_nothing_changed(self) -> None:  # U05
        base = self.make_repository()
        merged = self.merge(self.candidate(base))
        merged_mode = stat.S_IMODE(os.lstat(self.project / "tool.sh").st_mode)
        for case in ("edit", "mode", "delete-added", "recreate-deleted"):
            with self.subTest(case=case):
                if case == "edit":
                    (self.project / "src" / "app.py").write_text("owner after merge\n")
                    path, undo = "src/app.py", lambda: (self.project / "src" / "app.py").write_text(
                        "def main():\n    return 2\n")
                elif case == "mode":
                    (self.project / "tool.sh").chmod(0o700)
                    path, undo = "tool.sh", lambda: (self.project / "tool.sh").chmod(merged_mode)
                elif case == "delete-added":
                    data = (self.project / "blob.bin").read_bytes()
                    (self.project / "blob.bin").unlink()
                    path, undo = "blob.bin", lambda data=data: (self.project / "blob.bin").write_bytes(data)
                else:
                    (self.project / "gone.txt").write_text("owner re-created\n")
                    path, undo = "gone.txt", lambda: (self.project / "gone.txt").unlink()
                before = snapshot(self.project)
                result = self.undo(merged.merge_id)
                self.assertFalse(result.ok)
                self.assertEqual("write_undo_owner_changed", result.reason)
                self.assertEqual(path, result.path)
                self.assertIn("Nothing was changed", result.message)
                self.assert_unchanged(before)
                undo()
        # With every edit put back, the undo goes through.
        self.assertTrue(self.undo(merged.merge_id).ok)

    def test_undo_goes_newest_first_across_merges(self) -> None:  # U06
        base = self.make_repository()
        before_contents = contents(self.project)
        first = self.merge(self.candidate(base), task="task-A")
        self.assertTrue(first.ok)
        # A second merge touching a file the first one changed (src/app.py).
        second = self.merge_on_top()
        self.assertTrue(second.ok, getattr(second, "message", second))
        after_both = snapshot(self.project)
        refused = self.undo(first.merge_id)
        self.assertFalse(refused.ok)
        self.assertEqual("write_undo_order", refused.reason)
        self.assertIn("task-B", refused.message)
        self.assertIn("Nothing was changed", refused.message)
        self.assert_unchanged(after_both)
        self.assertTrue(self.undo(second.merge_id).ok)
        self.assertTrue(self.undo(first.merge_id).ok)
        self.assertEqual(before_contents, contents(self.project))

    def merge_on_top(self):
        """A second merge whose base is the folder after the first: commit the first merge's
        result in the owner's repository (the owner's own commit), then merge a change on top."""

        fixture_git(self.project, "add", "-A", "--", *DEFAULT_CHANGES)
        fixture_git(self.project, "commit", "-qm", "owner commits the first merge")
        head = fixture_git(self.project, "rev-parse", "HEAD").stdout.decode().strip()
        candidate = self.candidate(head, lambda clone: (clone / "src" / "app.py").write_text(
            "def main():\n    return 3\n"))
        return self.merge(candidate, task="task-B")

    def test_undo_needs_no_git_objects(self) -> None:  # U08
        base = self.make_repository()
        before_contents = contents(self.project)
        merged = self.merge(self.candidate(base))
        self.assertTrue(merged.ok)
        # The owner rewrites history and collects garbage; COCOLO's private store goes too.
        (self.project / "README.md").write_text("rewritten\n")
        fixture_git(self.project, "commit", "-qm", "rewrite", "--", "README.md")
        fixture_git(self.project, "commit", "-q", "--amend", "-m", "rewritten history")
        fixture_git(self.project, "reflog", "expire", "--expire=now", "--all")
        fixture_git(self.project, "gc", "-q", "--prune=now")
        shutil.rmtree(self.stores / "store")
        undone = self.undo(merged.merge_id)
        self.assertTrue(undone.ok, getattr(undone, "message", undone))
        expected = dict(before_contents)
        expected["README.md"] = contents(self.project)["README.md"]
        self.assertEqual(expected, contents(self.project))

    def test_undo_refuses_unknown_merges_and_stopped_ones(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        self.assert_stopped(self.undo("0" * 32), "write_undo_not_kept")
        # A merge stopped after its journal was written (the owner's git held the lock).
        (self.project / ".git" / "index.lock").write_text("owner's git\n")
        stopped = self.merge(candidate)
        (self.project / ".git" / "index.lock").unlink()
        self.assertFalse(stopped.ok)
        kept = wm.kept_merges(self.state, include_finished=True)
        self.assert_stopped(self.undo(kept[0].merge_id), "write_undo_not_kept")


# --------------------------------------------------------------------------------------------
# M3-27: crash recovery (the merge process killed with SIGKILL at each point)
# --------------------------------------------------------------------------------------------

CHILD = r"""
import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from codex_agents import hardened_git as hg, write_merge as wm
spec = json.loads(sys.argv[1])
seen = []
def hook(point, path):
    seen.append((point, path))
    if [point, path] == spec["kill"]:
        os.kill(os.getpid(), signal.SIGKILL)
if spec["kind"] == "merge":
    c = spec["candidate"]
    candidate = hg.CandidateTree(c["tree"], c["base_commit"], c["base_tree"], Path(c["store"]), c["object_format"])
    result = wm.merge_candidate(Path(spec["project"]), Path(spec["state"]), candidate, task=spec["task"],
                                executable=spec["git"], fault_hook=hook)
else:
    result = wm.undo_merge(Path(spec["project"]), Path(spec["state"]), spec["merge_id"],
                           executable=spec["git"], fault_hook=hook)
print(json.dumps({"ok": result.ok, "points": seen}))
"""


@needs_real_git
class CrashRecoveryTests(Workspace):
    def run_child(self, spec: dict) -> subprocess.CompletedProcess:
        script = self.base_folder / "child.py"
        script.write_text(CHILD)
        spec = {**spec, "project": str(self.project), "state": str(self.state), "git": REAL_GIT}
        return subprocess.run(
            [sys.executable, "-I", "-B", str(script), json.dumps(spec), str(ORCHESTRATION_ROOT)],
            capture_output=True, timeout=120, stdin=subprocess.DEVNULL,
        )

    def candidate_spec(self, candidate: hg.CandidateTree) -> dict:
        return {"tree": candidate.tree, "base_commit": candidate.base_commit, "base_tree": candidate.base_tree,
                "store": str(candidate.store), "object_format": candidate.object_format}

    def reset(self) -> None:
        for path in (self.project, self.stores / "store", self.state):
            if path.exists():
                shutil.rmtree(path)

    def points(self, kind: str) -> list[list]:
        """Every fault point of an uninterrupted run, in order (a reference run in a fresh project)."""

        self.reset()
        base = self.make_repository()
        candidate = self.candidate(base)
        spec = {"kind": "merge", "candidate": self.candidate_spec(candidate), "task": "t", "kill": None}
        completed = self.run_child(spec)
        self.assertEqual(0, completed.returncode, completed.stderr)
        output = json.loads(completed.stdout)
        if kind == "merge":
            return output["points"]
        merge_id = wm.kept_merges(self.state)[0].merge_id
        completed = self.run_child({"kind": "undo", "merge_id": merge_id, "kill": None})
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)["points"]

    def test_a_merge_killed_at_any_point_recovers_to_all_or_nothing(self) -> None:  # MG14-MG19
        points = self.points("merge")
        names = [point for point, _ in points]
        for expected in ("prepared", "fenced", "directory_created", "temp_written", "exchanged",
                         "file_done", "all_done", "applied", "finished"):
            self.assertIn(expected, names)
        last_exchange = max(index for index, (point, path) in enumerate(points)
                            if point == "exchanged")
        for index, kill in enumerate(points):
            with self.subTest(kill=kill):
                self.reset()
                base = self.make_repository()
                candidate = self.candidate(base)
                before = snapshot(self.project)
                killed = self.run_child({"kind": "merge", "candidate": self.candidate_spec(candidate),
                                         "task": "t", "kill": kill})
                self.assertEqual(-signal.SIGKILL, killed.returncode, killed.stderr)
                outcomes = wm.recover(self.project, self.state)
                self.assertEqual(1, len(outcomes))
                self.assertFalse((self.project / ".git" / "index.lock").exists())
                if index >= last_exchange:
                    self.assertEqual("completed", outcomes[0].outcome)
                    self.assert_merged(before, DEFAULT_CHANGES, DEFAULT_FOLDERS)
                    self.assertEqual("merged", wm.kept_merges(self.state)[0].state)
                else:
                    self.assertEqual("rolled_back", outcomes[0].outcome)
                    self.assert_unchanged(before)
                    self.assertEqual((), wm.kept_merges(self.state))
                # Recovery is idempotent.
                self.assertEqual((), wm.recover(self.project, self.state))

    def test_an_undo_killed_at_any_point_recovers_to_all_or_nothing(self) -> None:  # U07
        points = self.points("undo")
        last_exchange = max(index for index, (point, _) in enumerate(points) if point == "exchanged")
        for index, kill in enumerate(points):
            with self.subTest(kill=kill):
                self.reset()
                base = self.make_repository()
                before_contents = contents(self.project)
                merged = self.merge(self.candidate(base))
                self.assertTrue(merged.ok)
                after_merge = snapshot(self.project)
                killed = self.run_child({"kind": "undo", "merge_id": merged.merge_id, "kill": kill})
                self.assertEqual(-signal.SIGKILL, killed.returncode, killed.stderr)
                outcomes = wm.recover(self.project, self.state)
                self.assertEqual(1, len(outcomes))
                self.assertFalse((self.project / ".git" / "index.lock").exists())
                self.assertEqual([], leftovers(self.project))
                if index >= last_exchange:
                    self.assertEqual("completed", outcomes[0].outcome)
                    self.assertEqual(before_contents, contents(self.project))
                else:
                    self.assertEqual("rolled_back", outcomes[0].outcome)
                    self.assert_unchanged(after_merge)
                    self.assertTrue(self.undo(merged.merge_id).ok)
                    self.assertEqual(before_contents, contents(self.project))

    def test_the_fence_is_removed_only_when_it_holds_cocolos_token(self) -> None:
        for owners in (False, True):
            with self.subTest(owner_replaced_the_lock=owners):
                self.reset()
                base = self.make_repository()
                candidate = self.candidate(base)
                before = snapshot(self.project)
                killed = self.run_child({"kind": "merge", "candidate": self.candidate_spec(candidate),
                                         "task": "t", "kill": ["fenced", None]})
                self.assertEqual(-signal.SIGKILL, killed.returncode, killed.stderr)
                lock = self.project / ".git" / "index.lock"
                self.assertTrue(lock.exists())
                if owners:
                    lock.unlink()
                    lock.write_text("owner's git\n")
                outcomes = wm.recover(self.project, self.state)
                self.assertEqual("rolled_back", outcomes[0].outcome)
                if owners:
                    self.assertEqual("owner's git\n", lock.read_text())
                    lock.unlink()
                else:
                    self.assertFalse(lock.exists())
                self.assert_unchanged(before)

    def test_a_merge_waits_for_recovery_first(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        killed = self.run_child({"kind": "merge", "candidate": self.candidate_spec(candidate),
                                 "task": "t", "kill": ["exchanged", "src/app.py"]})
        self.assertEqual(-signal.SIGKILL, killed.returncode)
        # The next merge recovers the interrupted one first (rolling it back), then merges.
        result = self.merge(candidate, task="again")
        self.assertTrue(result.ok, getattr(result, "message", result))
        self.assert_merged(before, DEFAULT_CHANGES, DEFAULT_FOLDERS)
        records = wm.kept_merges(self.state, include_finished=True)
        self.assertEqual(["interrupted", "merged"], sorted(record.state for record in records))

    def test_a_path_recovery_cannot_classify_is_left_and_reported(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        killed = self.run_child({"kind": "merge", "candidate": self.candidate_spec(candidate),
                                 "task": "t", "kill": ["exchanged", "gone.txt"]})
        self.assertEqual(-signal.SIGKILL, killed.returncode)
        # The owner re-creates the deleted file before recovery runs: renaming the kept copy back
        # would overwrite it, so both are kept and the owner is told.
        (self.project / "gone.txt").write_text("owner re-created\n")
        outcomes = wm.recover(self.project, self.state)
        self.assertEqual("needs_owner", outcomes[0].outcome)
        self.assertIn("gone.txt", outcomes[0].paths)
        self.assertEqual(b"owner re-created\n", (self.project / "gone.txt").read_bytes())
        self.assertFalse((self.project / ".git" / "index.lock").exists())
        kept = leftovers(self.project)
        self.assertEqual(1, len(kept))
        self.assertEqual(b"to be deleted\n", (self.project / kept[0]).read_bytes())
        # Everything else is back as it was.
        self.assertEqual(b"def main():\n    return 1\n", (self.project / "src" / "app.py").read_bytes())
        self.assertFalse((self.project / "blob.bin").exists())



# --------------------------------------------------------------------------------------------
# The adversarial review of slice 2 (NOT READY): every finding as a test. The rule they all
# check: the owner's newest file always stays at its path, or the result says plainly where it is.
# --------------------------------------------------------------------------------------------

def save_by_rename(path: Path, data: bytes) -> int:
    """An editor's save: write a temporary beside the file, rename it over the file; the new inode."""

    temporary = path.with_name(f".{path.name}.swp")
    temporary.write_bytes(data)
    os.rename(temporary, path)
    return os.lstat(path).st_ino


def rewrite_in_place(path: Path, data: bytes) -> None:
    """An editor's in-place save: the same inode, new bytes."""

    with open(path, "r+b") as handle:
        handle.seek(0)
        handle.write(data)
        handle.truncate()


def run_forked(work, arm=None) -> int:
    """Run `work()` in a forked child after `arm()` (which may make the child kill itself); the
    child's end: minus the signal that killed it, or its exit code (0 when `work` returned)."""

    pid = os.fork()
    if pid == 0:  # pragma: no cover -- the child
        code = 70
        try:
            if arm is not None:
                arm()
            work()
            code = 0
        except BaseException:  # noqa: BLE001
            code = 71
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    return -os.WTERMSIG(status) if os.WIFSIGNALED(status) else os.WEXITSTATUS(status)


def kill_self() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def state_names(state: Path) -> list[str]:
    return sorted(os.listdir(state)) if state.exists() else []


class ReviewWorkspace(Workspace):
    def reset(self) -> None:
        for path in (self.project, self.stores / "store", self.state):
            if path.exists():
                shutil.rmtree(path)

    def assert_others_unchanged(self, before: dict, *paths: str) -> None:
        after = snapshot(self.project)
        self.assertEqual(before["git"], after["git"])
        for key in set(before["tree"]) | set(after["tree"]):
            if key not in paths:
                self.assertEqual(before["tree"].get(key), after["tree"].get(key), key)

    def candidate_merge(self, candidate: hg.CandidateTree, **options):
        return lambda: self.merge(candidate, **options)

    @staticmethod
    def kill_at(point: str, path: str | None):
        def hook(at, where):
            if at == point and where == path:
                kill_self()
        return hook

    def race_before_rename_away(self, name: str, data: bytes, fired: list):
        """The owner saves `name` by rename in the instant before COCOLO renames it away."""

        real = atomic_rename.rename_noreplace

        def racing(from_dir, from_name, to_dir, to_name):
            if from_name == Path(name).name and to_name.startswith(wm.TEMP_PREFIX) and not fired:
                fired.append(save_by_rename(self.project / name, data))
            return real(from_dir, from_name, to_dir, to_name)

        return mock.patch.object(atomic_rename, "rename_noreplace", racing)


@needs_real_git
class OwnerRaceTests(ReviewWorkspace):
    def test_f01_a_save_by_rename_just_before_a_delete_stays_at_its_path(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        fired: list = []
        with self.race_before_rename_away("gone.txt", b"owner's new work\n", fired):
            result = self.merge(candidate)
        self.assertEqual(1, len(fired))
        self.assert_stopped(result, "write_merge_owner_changed", "gone.txt")
        target = self.project / "gone.txt"
        self.assertEqual(b"owner's new work\n", target.read_bytes())
        self.assertEqual(fired[0], os.lstat(target).st_ino)
        self.assertEqual([], leftovers(self.project))
        self.assert_others_unchanged(before, "gone.txt")
        self.assertEqual(["stopped"], [r.state for r in wm.kept_merges(self.state, include_finished=True)])

    def test_f01_a_save_by_rename_just_before_undo_removes_an_added_file_stays(self) -> None:
        base = self.make_repository()
        merged = self.merge(self.candidate(base))
        self.assertTrue(merged.ok)
        after_merge = snapshot(self.project)
        fired: list = []
        with self.race_before_rename_away("blob.bin", b"owner edited the added file\n", fired):
            result = self.undo(merged.merge_id)
        self.assertEqual(1, len(fired))
        self.assert_stopped(result, "write_undo_owner_changed", "blob.bin")
        self.assertEqual(b"owner edited the added file\n", (self.project / "blob.bin").read_bytes())
        self.assertEqual([], leftovers(self.project))
        self.assert_others_unchanged(after_merge, "blob.bin")
        self.assertEqual([merged.merge_id], [record.merge_id for record in wm.kept_merges(self.state)])

    def test_f02_an_in_place_edit_of_cocolos_new_file_stays_at_its_path(self) -> None:
        for point in ("exchanged", "all_done"):
            with self.subTest(point=point):
                self.reset()
                base = self.make_repository()
                candidate = self.candidate(base)
                before = snapshot(self.project)
                target = self.project / "src" / "app.py"

                def hook(at, path, point=point, target=target):
                    if at == point and path in (None, "src/app.py"):
                        rewrite_in_place(target, b"OWNER TYPED THIS\n")

                result = self.merge(candidate, fault_hook=hook)
                self.assertFalse(result.ok)
                # Reported plainly: the file is left as the owner saved it, nothing hidden.
                self.assertEqual("write_merge_needs_owner", result.reason, result.message)
                self.assertIn("src/app.py", result.message)
                self.assertNotIn("Nothing was changed", result.message)
                self.assertEqual(b"OWNER TYPED THIS\n", target.read_bytes())
                self.assertEqual([], leftovers(self.project))
                self.assert_others_unchanged(before, "src/app.py")

    def test_f02_an_edit_in_the_instant_before_the_swap_back_goes_back_to_its_path(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        target = self.project / "src" / "app.py"
        state = {"rolling_back": False, "edited": False}
        real = atomic_rename.exchange

        def hook(at, path):
            if at == "all_done":
                state["rolling_back"] = True
                raise OSError(errno.EIO, "Input/output error")

        def exchanging(from_dir, from_name, to_dir, to_name):
            if state["rolling_back"] and to_name == "app.py" and not state["edited"]:
                state["edited"] = True
                rewrite_in_place(target, b"OWNER TYPED THIS\n")
            return real(from_dir, from_name, to_dir, to_name)

        with mock.patch.object(atomic_rename, "exchange", exchanging):
            result = self.merge(candidate, fault_hook=hook)
        self.assertTrue(state["edited"])
        self.assertFalse(result.ok)
        self.assertEqual("write_merge_needs_owner", result.reason, result.message)
        self.assertIn("src/app.py", result.message)
        self.assertEqual(b"OWNER TYPED THIS\n", target.read_bytes())
        self.assertEqual([], leftovers(self.project))
        self.assert_others_unchanged(before, "src/app.py")

    def test_f06_a_write_into_the_old_file_after_the_final_check_rolls_back(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        target = self.project / "src" / "app.py"
        inode = os.lstat(target).st_ino
        descriptor = os.open(target, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        before = snapshot(self.project)

        def hook(at, path):
            if at == "applied":
                os.pwrite(descriptor, b"OWNER", 0)

        result = self.merge(candidate, fault_hook=hook)
        self.assert_stopped(result, "write_merge_owner_changed", "src/app.py")
        self.assertEqual(inode, os.lstat(target).st_ino)
        self.assertEqual(b"OWNER" + b"def main():\n    return 1\n"[5:], target.read_bytes())
        self.assertEqual([], leftovers(self.project))
        self.assert_others_unchanged(before, "src/app.py")
        self.assertEqual((), wm.kept_merges(self.state))

    def test_f09_head_moved_during_the_apply_rolls_back(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        tree = fixture_git(self.project, "rev-parse", "HEAD^{tree}").stdout.decode().strip()
        other = fixture_git(self.project, "commit-tree", tree, "-p", base, "-m", "elsewhere").stdout.decode().strip()
        before = snapshot(self.project)

        def hook(at, path):
            if at == "file_done" and path == "blob.bin":
                # Not refused by the fence: update-ref (and reset --soft) do not take index.lock.
                fixture_git(self.project, "update-ref", "HEAD", other)

        result = self.merge(candidate, fault_hook=hook)
        self.assert_stopped(result, "write_merge_base_changed")
        self.assertEqual(before["tree"], snapshot(self.project)["tree"])
        self.assertEqual([], leftovers(self.project))
        self.assertFalse((self.project / ".git" / "index.lock").exists())

    def test_f08_new_files_follow_the_umask_as_git_gives_them(self) -> None:
        base = self.make_repository()

        def change(clone: Path) -> None:
            (clone / "plain.txt").write_text("plain\n")
            (clone / "run.sh").write_text("#!/bin/sh\n")
            (clone / "run.sh").chmod(0o755)
            (clone / "made" / "inner.txt").parent.mkdir()
            (clone / "made" / "inner.txt").write_text("inner\n")

        candidate = self.candidate(base, change)
        previous = os.umask(0o077)
        try:
            result = self.merge(candidate)
        finally:
            os.umask(previous)
        self.assertTrue(result.ok, getattr(result, "message", result))
        self.assertEqual(0o600, stat.S_IMODE(os.lstat(self.project / "plain.txt").st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(self.project / "run.sh").st_mode))
        self.assertEqual(0o700, stat.S_IMODE(os.lstat(self.project / "made").st_mode))
        # What the merge does not keep on a file it replaces is said plainly in its result.
        for words in ("group", "access control", "extended attributes", "flags", "umask"):
            self.assertIn(words, result.message)
        self.assertTrue(self.undo(result.merge_id).ok)
        self.assertFalse((self.project / "plain.txt").exists())

    def test_a_case_only_rename_is_refused_with_its_own_sentence(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base, lambda clone: fixture_git(clone, "mv", "gone.txt", "Gone.txt"))
        before = snapshot(self.project)
        result = self.merge(candidate)
        if os.path.exists(str(self.project / "GONE.TXT")):  # a case-insensitive volume
            self.assert_stopped(result, "write_merge_unsupported_change", "Gone.txt")
            self.assertIn("`gone.txt`", result.message)
            self.assertIn("letter case", result.message)
            self.assert_unchanged(before)
        else:
            self.assertTrue(result.ok, getattr(result, "message", result))


EDITOR = r"""
import os, random, sys, time
root, stop, seed, delay = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
names = ("src/app.py", "gone.txt")
seq = dict.fromkeys(names, 0)
random.seed(seed)
while not os.path.exists(stop + ".go"):
    time.sleep(0.0002)
time.sleep(random.random() * delay)
while not os.path.exists(stop):
    name = random.choice(names)
    seq[name] += 1
    data = f"owner {name} seq {seq[name]}\n"
    path = os.path.join(root, name)
    try:
        if random.random() < 0.6:
            temporary = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.sw{seq[name]}")
            with open(temporary, "w") as handle:
                handle.write(data)
            os.rename(temporary, path)
        else:
            with open(path, "w") as handle:
                handle.write(data)
    except OSError:
        seq[name] -= 1
    time.sleep(random.random() * 0.004)
print(" ".join(f"{name}={count}" for name, count in seq.items()))
"""


@needs_real_git
class EditorStressTests(ReviewWorkspace):
    """S01: a real editor process saves two touched files (one modified, one deleted by the change)
    by rename and in place, in a tight random loop, while the merge runs."""

    ITERATIONS = 16

    def test_s01_the_owners_last_save_is_at_its_path_or_reported(self) -> None:
        script = self.base_folder / "editor.py"
        script.write_text(EDITOR)

        def change(clone: Path) -> None:
            (clone / "src" / "app.py").write_text("child app\n")
            (clone / "gone.txt").unlink()

        for iteration in range(self.ITERATIONS):
            with self.subTest(iteration=iteration):
                self.reset()
                base = self.make_repository()
                candidate = self.candidate(base, change)
                stop = self.base_folder / f"stop-{iteration}"
                go = Path(f"{stop}.go")
                editor = subprocess.Popen(
                    [sys.executable, "-I", "-B", str(script), str(self.project), str(stop), str(iteration),
                     "0.03"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                )
                try:
                    result = self.merge(candidate, fault_hook=lambda at, path: go.touch() if at == "prepared" else None)
                    time.sleep(0.01)
                finally:
                    stop.touch()
                    go.touch()
                    output = editor.communicate(timeout=60)[0].split()
                last = dict(item.split("=") for item in output)
                hidden = leftovers(self.project)
                for name, count in last.items():
                    if int(count) == 0:
                        continue
                    wanted = f"owner {name} seq {count}\n".encode()
                    path = self.project / name
                    at_path = path.exists() and path.read_bytes() == wanted
                    self.assertTrue(at_path, f"{name}: the owner's last save is not at its path "
                                    f"(result {getattr(result, 'reason', 'merged')}, hidden {hidden})")
                if not result.ok and "Nothing was changed" in result.message:
                    self.assertEqual([], hidden)
                if result.ok:
                    self.assertEqual([], hidden)


@needs_real_git
class ReviewRecoveryTests(ReviewWorkspace):
    def test_f01_recovery_puts_back_whatever_a_delete_moved_away(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        ended = run_forked(self.candidate_merge(candidate, fault_hook=self.kill_at("exchanged", "gone.txt")))
        self.assertEqual(-signal.SIGKILL, ended)
        # What the rename moved away was the owner's newer file (saved in the instant before it).
        hidden = leftovers(self.project)
        self.assertEqual(1, len(hidden))
        os.unlink(self.project / hidden[0])
        (self.project / hidden[0]).write_bytes(b"owner's newer gone.txt\n")
        outcomes = wm.recover(self.project, self.state)
        self.assertEqual(["rolled_back"], [outcome.outcome for outcome in outcomes])
        self.assertEqual(b"owner's newer gone.txt\n", (self.project / "gone.txt").read_bytes())
        self.assertEqual([], leftovers(self.project))

    def test_f03_a_merge_killed_while_writing_a_temporary_leaves_no_name(self) -> None:
        for where in ("before_mode", "half_written"):
            with self.subTest(where=where):
                self.reset()
                base = self.make_repository()
                candidate = self.candidate(base)
                before = snapshot(self.project)

                def arm(where=where):
                    if where == "before_mode":
                        os.fchmod = lambda descriptor, mode: kill_self()
                    else:
                        real = wm._write_all

                        def half(descriptor, data):
                            real(descriptor, data[: len(data) // 2])
                            kill_self()

                        wm._write_all = half

                self.assertEqual(-signal.SIGKILL, run_forked(self.candidate_merge(candidate), arm))
                outcomes = wm.recover(self.project, self.state)
                self.assertEqual(["rolled_back"], [outcome.outcome for outcome in outcomes])
                self.assert_unchanged(before)

    def test_f06_recovery_after_applied_rolls_back_a_write_into_an_old_copy(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        ended = run_forked(self.candidate_merge(candidate, fault_hook=self.kill_at("applied", None)))
        self.assertEqual(-signal.SIGKILL, ended)
        # An editor that held src/app.py open from before the merge writes into it (now the old copy).
        old = next(path for path in (self.project / "src").iterdir() if path.name.startswith(wm.TEMP_PREFIX))
        inode = os.lstat(old).st_ino
        with open(old, "r+b") as handle:
            handle.write(b"OWNER")
        outcomes = wm.recover(self.project, self.state)
        self.assertEqual(["rolled_back"], [outcome.outcome for outcome in outcomes])
        target = self.project / "src" / "app.py"
        self.assertEqual(inode, os.lstat(target).st_ino)
        self.assertEqual(b"OWNER" + b"def main():\n    return 1\n"[5:], target.read_bytes())
        self.assertEqual([], leftovers(self.project))
        self.assert_others_unchanged(before, "src/app.py")

    def test_recovery_after_applied_leaves_a_file_the_owner_replaced_since(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        ended = run_forked(self.candidate_merge(candidate, fault_hook=self.kill_at("applied", None)))
        self.assertEqual(-signal.SIGKILL, ended)
        target = self.project / "src" / "app.py"
        save_by_rename(target, b"owner saved after the merge\n")
        outcomes = wm.recover(self.project, self.state)
        self.assertEqual([("needs_owner", ("src/app.py",))], [(o.outcome, o.paths) for o in outcomes])
        self.assertEqual(b"owner saved after the merge\n", target.read_bytes())
        self.assertEqual([], leftovers(self.project))  # the old copy was exactly the base
        self.assertFalse((self.project / ".git" / "index.lock").exists())
        self.assert_merged(before, DEFAULT_CHANGES, DEFAULT_FOLDERS)  # the rest of the merge stands
        self.assertEqual(b"#!/bin/sh\necho tool 2\n", (self.project / "tool.sh").read_bytes())

    def test_f07_a_merge_killed_as_the_fence_appears_leaves_no_lock(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        lock = self.project / ".git" / "index.lock"

        def arm():
            for name in ("open", "link"):
                real = getattr(os, name)

                def call(*arguments, real=real, **options):
                    result = real(*arguments, **options)
                    if os.path.lexists(lock):
                        kill_self()
                    return result

                setattr(os, name, call)

        self.assertEqual(-signal.SIGKILL, run_forked(self.candidate_merge(candidate), arm))
        outcomes = wm.recover(self.project, self.state)
        self.assertEqual(["rolled_back"], [outcome.outcome for outcome in outcomes])
        self.assertFalse(lock.exists())
        self.assert_unchanged(before)
        self.assertEqual(0, fixture_git(self.project, "add", "README.md", check=False).returncode)

    def test_f11_a_lock_replaced_as_cocolo_releases_its_fence_is_left_alone(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        lock = self.project / ".git" / "index.lock"
        real_read = os.read
        fired: list = []

        def reading(descriptor, size):
            data = real_read(descriptor, size)
            if not fired and data.startswith(b"cocolo-merge ") and os.path.lexists(lock) \
                    and os.fstat(descriptor).st_ino == os.lstat(lock).st_ino:
                # The owner removed the lock git told them about, and their next git command took it.
                fired.append(True)
                lock.unlink()
                lock.write_bytes(b"git's own lock\n")
            return data

        with mock.patch.object(os, "read", reading):
            result = self.merge(candidate)
        self.assertEqual([True], fired)
        self.assertTrue(result.ok, getattr(result, "message", result))
        self.assertEqual(b"git's own lock\n", lock.read_bytes())
        lock.unlink()
        self.assertEqual([], [name for name in os.listdir(self.project / ".git") if "cocolo" in name])

    def test_a_merge_killed_while_probing_the_filesystem_leaves_no_probe(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)

        def arm():
            real = atomic_rename.exchange

            def exchanging(from_dir, from_name, to_dir, to_name):
                if "-probe-" in from_name:
                    kill_self()
                return real(from_dir, from_name, to_dir, to_name)

            atomic_rename.exchange = exchanging

        self.assertEqual(-signal.SIGKILL, run_forked(self.candidate_merge(candidate), arm))
        wm.recover(self.project, self.state)
        self.assert_unchanged(before)

    def test_the_merge_after_an_interrupted_one_reports_what_recovery_did(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        ended = run_forked(self.candidate_merge(candidate, fault_hook=self.kill_at("exchanged", "src/app.py")))
        self.assertEqual(-signal.SIGKILL, ended)
        result = self.merge(candidate, task="again")
        self.assertTrue(result.ok, getattr(result, "message", result))
        self.assertEqual(["rolled_back"], [outcome.outcome for outcome in result.recovered])
        undone = self.undo(result.merge_id)
        self.assertTrue(undone.ok)
        self.assertEqual((), undone.recovered)

    def test_f10_a_state_folder_failure_after_the_apply_is_reported_truthfully(self) -> None:
        for failing in ("applied", "merged"):
            with self.subTest(failing=failing):
                self.reset()
                base = self.make_repository()
                candidate = self.candidate(base)
                before = snapshot(self.project)
                real = wm._record

                def record(descriptor, entry, failing=failing):
                    if entry.get("e") == failing or (entry.get("e") == "state" and entry.get("state") == failing):
                        raise OSError(errno.ENOSPC, "No space left on device")
                    return real(descriptor, entry)

                with mock.patch.object(wm, "_record", record):
                    result = self.merge(candidate)
                self.assertFalse((self.project / ".git" / "index.lock").exists())
                if failing == "applied":
                    # Every file was in place and every old copy kept: it is put back, and said so.
                    self.assert_stopped(result, "write_merge_failed")
                    self.assertIn("state folder", result.message)
                    self.assertNotIn("could not read", result.message)
                    self.assert_unchanged(before)
                else:
                    # The folder is merged; only its record waits, and the result says so.
                    self.assertTrue(result.ok, getattr(result, "message", result))
                    self.assertIn("recorded", result.message)
                    self.assert_merged(before, DEFAULT_CHANGES, DEFAULT_FOLDERS)
                    outcomes = wm.recover(self.project, self.state)
                    self.assertEqual(["completed"], [outcome.outcome for outcome in outcomes])
                    self.assertEqual([result.merge_id], [r.merge_id for r in wm.kept_merges(self.state)])


@needs_real_git
class JournalTests(ReviewWorkspace):
    def test_f04_a_journal_this_cocolo_cannot_read_is_kept_with_its_undo(self) -> None:
        for variant in ("newer_format", "damaged_intent", "damaged_event"):
            with self.subTest(variant=variant):
                self.reset()
                base = self.make_repository()
                merged = self.merge(self.candidate(base))
                self.assertTrue(merged.ok)
                journal = self.state / f"merge-{merged.merge_id}.journal"
                undo = self.state / f"merge-{merged.merge_id}.undo"
                lines = journal.read_bytes().split(b"\n")
                if variant == "newer_format":
                    lines[0] = lines[0].replace(b'"format":1', b'"format":2', 1)
                elif variant == "damaged_intent":
                    lines[0] = lines[0].replace(b'"task"', b"'task\"", 1)
                else:
                    lines[1] = lines[1][:-3] + b"#" + lines[1][-2:]  # one byte of an early event
                damaged = b"\n".join(lines)
                journal.write_bytes(damaged)
                outcomes = wm.recover(self.project, self.state)
                self.assertEqual(damaged, journal.read_bytes())
                self.assertTrue(undo.is_dir())
                self.assertEqual([(merged.merge_id, "needs_owner")],
                                 [(outcome.journal_id, outcome.outcome) for outcome in outcomes])
                self.assertIn(journal.name, outcomes[0].message)
                # Later merges and recoveries keep it too, and the record lists it.
                wm.recover(self.project, self.state)
                self.assertTrue(journal.exists())
                self.assertTrue(undo.is_dir())
                records = wm.kept_merges(self.state, include_finished=True)
                self.assertEqual([(merged.merge_id, "unreadable")], [(r.merge_id, r.state) for r in records])
                self.assert_stopped(self.undo(merged.merge_id), "write_undo_not_kept")

    def test_f04_only_an_intent_without_its_line_end_counts_as_torn(self) -> None:
        self.make_repository()
        self.state.mkdir(mode=0o700)
        ident = "e" * 32
        (self.state / f"merge-{ident}.journal").write_bytes(b'{"format":1,"kind":"merge","id":"ee')
        (self.state / f"merge-{ident}.undo").mkdir(mode=0o700)
        (self.state / f"merge-{ident}.undo" / "0").write_bytes(b"base\n")
        self.assertEqual((), wm.recover(self.project, self.state))
        self.assertEqual([], [name for name in state_names(self.state) if ident in name])

    def test_a_line_torn_by_a_crash_is_read_after_recovery_marks_it(self) -> None:
        base = self.make_repository()
        candidate = self.candidate(base)
        before = snapshot(self.project)
        hook = self.kill_at("exchanged", "src/app.py")
        self.assertEqual(-signal.SIGKILL, run_forked(lambda: self.merge(candidate, fault_hook=hook)))
        journal = next(self.state.glob("merge-*.journal"))
        with open(journal, "ab") as handle:
            handle.write(b'{"e":"done","i')  # a line the kill tore
        outcomes = wm.recover(self.project, self.state)
        self.assertEqual(["rolled_back"], [outcome.outcome for outcome in outcomes])
        self.assert_unchanged(before)
        records = wm.kept_merges(self.state, include_finished=True)
        self.assertEqual(["interrupted"], [record.state for record in records])

    def test_f05_a_journal_naming_a_place_outside_the_project_is_never_acted_on(self) -> None:
        for variant in ("path", "temporary", "folder"):
            with self.subTest(variant=variant):
                self.reset()
                base = self.make_repository()
                self.assertTrue(self.merge(self.candidate(base)).ok)  # the state folder, made normally
                outside = self.base_folder / "outside"
                shutil.rmtree(outside, ignore_errors=True)
                outside.mkdir()
                victim = outside / "victim.txt"
                victim.write_bytes(b"the owner's file outside the project\n")
                data, info = victim.read_bytes(), os.lstat(victim)
                ident = "f" * 32
                path, temp = "zz.txt", f"{wm.TEMP_PREFIX}{ident}-0"
                folders: list = []
                empty = outside / "empty"
                empty.mkdir()
                if variant == "path":
                    path = "../outside/victim.txt"
                elif variant == "temporary":
                    temp = "../outside/victim.txt"
                else:
                    folders = ["../outside/empty"]
                intent = {
                    "format": 1, "kind": "merge", "id": ident, "seq": 99, "task": "forged",
                    "project": str(self.project), "root_ino": os.lstat(self.project).st_ino,
                    "git_dir": str(self.project / ".git"), "token": "0" * 32, "base_commit": base,
                    "tree": base, "folders": folders, "remove_folders": [],
                    "ops": [{"path": path, "change": "create", "label": "added", "temp": temp,
                             "before_sha": None, "before_size": None, "before_mode": None,
                             "after_sha": hashlib.sha256(data).hexdigest(), "after_size": len(data),
                             "after_mode": stat.S_IMODE(info.st_mode), "base_blob": None}],
                }
                events = [{"e": "temp", "i": 0, "ino": info.st_ino}]
                if variant == "folder":
                    events = [{"e": "folder", "i": 0, "ino": os.lstat(empty).st_ino}]
                journal = self.state / f"merge-{ident}.journal"
                journal.write_text("\n".join(json.dumps(item) for item in [intent, *events]) + "\n")
                outcomes = wm.recover(self.project, self.state)
                self.assertEqual(data, victim.read_bytes())
                self.assertTrue(empty.is_dir())
                self.assertTrue(journal.exists())
                self.assertEqual([(ident, "needs_owner")], [(o.journal_id, o.outcome) for o in outcomes])

    def test_the_undo_order_only_counts_merges_of_the_same_project(self) -> None:
        base = self.make_repository()
        other = self.base_folder / "other"
        other_base = self.make_repository(other)
        first = self.merge(self.candidate(base))
        self.assertTrue(first.ok)
        foreign = hg.build_candidate_tree(
            other, self.stores / "other-store", other_base, self.make_patch(other_base, root=other),
            executable=REAL_GIT,
        )
        self.assertTrue(foreign.ok)
        second = self.merge(foreign, root=other, task="other-project")
        self.assertTrue(second.ok, getattr(second, "message", second))
        undone = self.undo(first.merge_id)
        self.assertTrue(undone.ok, getattr(undone, "message", undone))


# --------------------------------------------------------------------------------------------
# M3-27: a SIGKILL at every file-system call (not only the hook points), then recovery
# --------------------------------------------------------------------------------------------

KILL_WRAPPED = {
    os: ("open", "write", "fchmod", "mkdir", "rmdir", "unlink", "fsync", "link", "rename", "replace"),
}


def arm_kill_at_call(limit: int, entry: str) -> None:
    """In the child: SIGKILL at the `limit`-th file-system call made from `entry` on."""

    import fcntl

    count = [0]

    def wrap(module, name: str) -> None:
        real = getattr(module, name)

        def call(*arguments, **options):
            count[0] += 1
            if count[0] == limit:
                kill_self()
            return real(*arguments, **options)

        setattr(module, name, call)

    real_entry = getattr(wm, entry)

    def armed(*arguments, **options):
        for name in KILL_WRAPPED[os]:
            wrap(os, name)
        wrap(fcntl, "fcntl")
        wrap(atomic_rename, "_rename")
        return real_entry(*arguments, **options)

    setattr(wm, entry, armed)


class KillEveryCall(ReviewWorkspace):
    def template(self) -> tuple[Path, hg.CandidateTree]:
        base = self.make_repository()
        candidate = self.candidate(base)
        template = self.base_folder / "template"
        template.mkdir()
        shutil.copytree(self.project, template / "project", symlinks=True)
        shutil.copytree(self.stores / "store", template / "store", symlinks=True)
        return template, candidate

    def restore(self, template: Path) -> None:
        self.reset()
        shutil.copytree(template / "project", self.project, symlinks=True)
        shutil.copytree(template / "store", self.stores / "store", symlinks=True)

    def check_state_folder(self, records) -> None:
        """Only the lock, journals, and the undo sets of merges still kept for undo."""

        kept = {record.merge_id for record in records if record.state in ("merged", "needs_owner")}
        for name in state_names(self.state):
            self.assertTrue(name == "merge.lock" or name.endswith(".journal") or name.endswith(".undo"), name)
            if name.endswith(".undo"):
                self.assertIn(name[len("merge-"):-len(".undo")], kept, name)

    def kill_every_call(self, kind: str) -> int:
        template, candidate = self.template()
        limit = 0
        while True:
            limit += 1
            self.assertLess(limit, 2000, "the run never finished")
            with self.subTest(kill_at_call=limit):
                self.restore(template)
                if kind == "undo":
                    merged = self.merge(candidate)
                    self.assertTrue(merged.ok)
                    work = lambda merged=merged: self.undo(merged.merge_id)  # noqa: E731
                    entry = "_undo_locked"
                else:
                    work = lambda: self.merge(candidate)  # noqa: E731
                    entry = "_merge_prepared"
                before = snapshot(self.project)
                ended = run_forked(work, lambda limit=limit, entry=entry: arm_kill_at_call(limit, entry))
                self.assertIn(ended, (0, -signal.SIGKILL))
                outcomes = wm.recover(self.project, self.state)
                outcome = outcomes[0].outcome if outcomes else "none"
                self.assertIn(outcome, ("none", "rolled_back", "completed"), outcomes)
                self.assertEqual([], leftovers(self.project))
                self.assertEqual(before["git"], snapshot(self.project)["git"])  # no lock, no fence file
                self.assertEqual((), wm.recover(self.project, self.state))
                records = wm.kept_merges(self.state, include_finished=True)
                states = [record.state for record in records]
                self.assertNotIn("unfinished", states)
                self.check_state_folder(records)
                finished = ("merged" if kind == "merge" else "undone") in states
                if outcome == "completed" or ended == 0:
                    self.assertTrue(finished, states)
                if kind == "merge":
                    if finished:
                        self.assert_merged(before, DEFAULT_CHANGES, DEFAULT_FOLDERS)
                    else:
                        self.assert_unchanged(before)
                elif finished:
                    self.assertEqual(contents(template / "project"), contents(self.project))
                else:
                    self.assert_unchanged(before)
                    self.assertTrue(self.undo(merged.merge_id).ok)
            if ended == 0:
                return limit


@needs_real_git
class KillEveryCallMergeTests(KillEveryCall):
    def test_c01_a_merge_killed_at_every_file_system_call_recovers_to_all_or_nothing(self) -> None:
        self.assertGreater(self.kill_every_call("merge"), 60)


@needs_real_git
class KillEveryCallUndoTests(KillEveryCall):
    def test_c01_an_undo_killed_at_every_file_system_call_recovers_to_all_or_nothing(self) -> None:
        self.assertGreater(self.kill_every_call("undo"), 60)


if __name__ == "__main__":
    unittest.main()
