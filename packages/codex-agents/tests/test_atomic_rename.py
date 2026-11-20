"""M3 slice 2, spike S11 kept as tests: the kernel's atomic exchange and no-replace rename.

Requirements (DESIGN-0.4.0-M3.md draft 2 §3.2, draft 3 slice 2):
- S11 / M3-21 The merge replaces a file only through the kernel's atomic exchange of two directory
  entries (macOS `renameatx_np(..., RENAME_SWAP)` on APFS, Linux `renameat2(..., RENAME_EXCHANGE)`),
  and adds a file only through a rename the kernel refuses when anything is at the target
  (`RENAME_EXCL` / `RENAME_NOREPLACE`), which also catches a letter-case variant on a
  case-insensitive volume. Both are called through ctypes, relative to directory descriptors.
- MG12 A filesystem without them is detected by a probe and refused; the probe leaves nothing behind.

Every file lives in a temporary folder; the macOS rows also attach throwaway disk images (FAT, HFS+)
in a temporary folder and detach them afterwards.
"""
from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import errno
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import TEMP_ROOT

from codex_agents import atomic_rename as ar


def case_insensitive(directory: Path) -> bool:
    probe = directory / "Case-Probe"
    probe.write_text("x")
    try:
        return (directory / "case-probe").exists()
    finally:
        probe.unlink()


class Folder(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)

    def write(self, name: str, data: bytes) -> int:
        (self.root / name).write_bytes(data)
        return os.lstat(self.root / name).st_ino


class AtomicRenameTests(Folder):
    def test_exchange_swaps_two_names_and_keeps_both_inodes(self) -> None:
        first, second = self.write("a", b"A"), self.write("b", b"BB")
        ar.exchange(self.fd, "a", self.fd, "b")
        self.assertEqual(b"BB", (self.root / "a").read_bytes())
        self.assertEqual(b"A", (self.root / "b").read_bytes())
        self.assertEqual(second, os.lstat(self.root / "a").st_ino)
        self.assertEqual(first, os.lstat(self.root / "b").st_ino)

    def test_exchange_needs_both_names(self) -> None:
        self.write("a", b"A")
        with self.assertRaises(OSError) as raised:
            ar.exchange(self.fd, "a", self.fd, "missing")
        self.assertEqual(errno.ENOENT, raised.exception.errno)
        self.assertEqual(b"A", (self.root / "a").read_bytes())

    def test_exchange_works_across_directory_descriptors(self) -> None:
        (self.root / "sub").mkdir()
        sub = os.open(self.root / "sub", os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, sub)
        self.write("a", b"A")
        (self.root / "sub" / "b").write_bytes(b"B")
        ar.exchange(self.fd, "a", sub, "b")
        self.assertEqual(b"B", (self.root / "a").read_bytes())
        self.assertEqual(b"A", (self.root / "sub" / "b").read_bytes())

    def test_noreplace_refuses_anything_at_the_target(self) -> None:
        self.write("a", b"A")
        self.write("b", b"B")
        (self.root / "folder").mkdir()
        os.symlink("nowhere", self.root / "link")
        for target in ("b", "folder", "link"):
            with self.subTest(target=target), self.assertRaises(OSError) as raised:
                ar.rename_noreplace(self.fd, "a", self.fd, target)
            self.assertEqual(errno.EEXIST, raised.exception.errno)
        self.assertEqual(b"A", (self.root / "a").read_bytes())
        self.assertEqual(b"B", (self.root / "b").read_bytes())
        ar.rename_noreplace(self.fd, "a", self.fd, "c")
        self.assertFalse((self.root / "a").exists())
        self.assertEqual(b"A", (self.root / "c").read_bytes())

    def test_noreplace_refuses_a_letter_case_variant_on_a_case_insensitive_volume(self) -> None:
        if not case_insensitive(self.root):
            self.skipTest("this temporary folder is on a case-sensitive filesystem")
        self.write("README.md", b"owner")
        self.write("temp", b"new")
        for variant in ("readme.md", "Readme.MD"):
            with self.subTest(variant=variant), self.assertRaises(OSError) as raised:
                ar.rename_noreplace(self.fd, "temp", self.fd, variant)
            self.assertEqual(errno.EEXIST, raised.exception.errno)
        self.assertEqual(b"owner", (self.root / "README.md").read_bytes())
        self.assertEqual(["README.md", "temp"], sorted(os.listdir(self.root)))

    def test_the_probe_accepts_this_filesystem_and_leaves_nothing(self) -> None:
        self.assertIsNone(ar.support_problem(self.fd, ".cocolo-merge-probe-"))
        self.assertEqual([], os.listdir(self.root))

    def test_unsupported_answers_are_reported_by_the_probe_and_the_calls(self) -> None:
        # The answers a kernel or filesystem gives without the operation: macOS ENOTSUP, Linux
        # EINVAL (flag unknown to the filesystem), EOPNOTSUPP, ENOSYS (no renameat2).
        for code in (errno.ENOTSUP, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
            with self.subTest(errno=errno.errorcode[code]):
                with mock.patch.object(ar, "_rename", return_value=code):
                    self.assertIsNotNone(ar.support_problem(self.fd, ".cocolo-merge-probe-"))
                    with self.assertRaises(OSError) as raised:
                        ar.exchange(self.fd, "x", self.fd, "y")
                    self.assertTrue(ar.is_unsupported(raised.exception))
                self.assertEqual([], os.listdir(self.root))

    def test_a_probe_that_swaps_nothing_is_refused(self) -> None:
        # A filesystem answering success without exchanging must not pass.
        with mock.patch.object(ar, "_rename", return_value=0):
            self.assertIsNotNone(ar.support_problem(self.fd, ".cocolo-merge-probe-"))
        self.assertEqual([], os.listdir(self.root))

    def test_names_must_be_single_components(self) -> None:
        for name in ("", ".", "..", "a/b", "a\0b"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                ar.exchange(self.fd, name, self.fd, "b")


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("hdiutil"), "macOS disk images only")
class DiskImageTests(unittest.TestCase):
    """Real filesystems without the operations: a FAT, an ExFAT and an HFS+ volume (S11, MG12)."""

    def attach(self, filesystem: str) -> Path:
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        image = Path(temporary.name) / "volume.dmg"
        mount = Path(temporary.name) / "mount"
        mount.mkdir()
        subprocess.run(
            ["hdiutil", "create", "-quiet", "-size", "8m", "-fs", filesystem, "-volname", "S11",
             "-layout", "NONE", str(image)], check=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        subprocess.run(
            ["hdiutil", "attach", "-quiet", "-nobrowse", "-noverify", "-mountpoint", str(mount), str(image)],
            check=True, timeout=120, stdin=subprocess.DEVNULL,
        )
        self.addCleanup(
            subprocess.run, ["hdiutil", "detach", "-quiet", "-force", str(mount)],
            timeout=120, stdin=subprocess.DEVNULL,
        )
        return mount

    def test_fat_exfat_and_hfs_plus_are_refused_and_left_clean(self) -> None:
        for filesystem in ("MS-DOS", "ExFAT", "HFS+"):
            with self.subTest(filesystem=filesystem):
                mount = self.attach(filesystem)
                before = sorted(os.listdir(mount))
                descriptor = os.open(mount, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    self.assertIsNotNone(ar.support_problem(descriptor, ".cocolo-merge-probe-"))
                finally:
                    os.close(descriptor)
                self.assertEqual(before, sorted(name for name in os.listdir(mount) if not name.startswith("._")))
                self.assertEqual([], [name for name in os.listdir(mount) if "cocolo" in name])


if __name__ == "__main__":
    unittest.main()
