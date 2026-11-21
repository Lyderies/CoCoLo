"""The kernel's atomic exchange and no-replace rename, relative to directory descriptors.

Requirements (DESIGN-0.4.0-M3.md draft 2 §3.2, spike S11):
- The merge replaces a file only by exchanging two directory entries in one kernel step:
  `renameatx_np(..., RENAME_SWAP)` on macOS (APFS), `renameat2(..., RENAME_EXCHANGE)` on Linux
  (3.15+, on ext4, xfs, btrfs, tmpfs and others; not every filesystem). It adds a file only by a
  rename the kernel refuses when anything is at the target (`RENAME_EXCL` / `RENAME_NOREPLACE`),
  which on a case-insensitive volume also refuses a letter-case or Unicode variant.
- A filesystem without them is found by a probe (`support_problem`) that creates two files under
  reserved names, exchanges them, checks the inodes really swapped, checks a no-replace rename
  refuses an existing name and moves to a free one, then removes both; it never touches other names.
  What a probe killed midway leaves is removed by `remove_leftovers` (the merge's recovery, which
  knows the probe's unique names from its journal).

S11 (2026-11-21): on this Mac (APFS, macOS 27.2, arm64) both work and a case variant is refused;
on FAT, ExFAT and HFS+ disk images both answer ENOTSUP (HFS+ has the no-replace rename but no
exchange). In the Linux CI container (kernel 7.2, aarch64; overlay, tmpfs, virtiofs) both work
through glibc's `renameat2`; the raw system call answers the same.
Standard library only (ctypes).
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import platform
import stat
import sys

if sys.platform == "darwin":
    _EXCHANGE, _NOREPLACE = 0x2, 0x4  # RENAME_SWAP, RENAME_EXCL (<stdio.h>)
else:
    _EXCHANGE, _NOREPLACE = 0x2, 0x1  # RENAME_EXCHANGE, RENAME_NOREPLACE (<linux/fs.h>)
# renameat2's system call number where glibc has no wrapper (before 2.28).
_LINUX_RENAMEAT2 = {"x86_64": 316, "aarch64": 276, "arm64": 276}
# The answers of a kernel or filesystem without the operation.
UNSUPPORTED = frozenset({errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.ENOSYS})

_function = None


def _library_call():
    global _function
    if _function is not None:
        return _function
    library = ctypes.CDLL(None, use_errno=True)
    arguments = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    if sys.platform == "darwin":
        function = library.renameatx_np
        function.argtypes, function.restype = arguments, ctypes.c_int
    elif hasattr(library, "renameat2"):
        function = library.renameat2
        function.argtypes, function.restype = arguments, ctypes.c_int
    else:
        number = _LINUX_RENAMEAT2.get(platform.machine())
        if number is None:
            def function(*_arguments):
                ctypes.set_errno(errno.ENOSYS)
                return -1
        else:
            syscall = library.syscall
            syscall.restype = ctypes.c_long

            def function(from_dir, from_name, to_dir, to_name, flags):
                return syscall(
                    ctypes.c_long(number), ctypes.c_int(from_dir), ctypes.c_char_p(from_name),
                    ctypes.c_int(to_dir), ctypes.c_char_p(to_name), ctypes.c_uint(flags),
                )
    _function = function
    return function


def _rename(from_dir: int, from_name: str, to_dir: int, to_name: str, flags: int) -> int:
    """The system call; 0 or the errno it set."""

    function = _library_call()
    ctypes.set_errno(0)
    if function(from_dir, os.fsencode(from_name), to_dir, os.fsencode(to_name), flags) == 0:
        return 0
    return ctypes.get_errno() or errno.EIO


def _checked(name: str) -> str:
    if type(name) is not str or name in ("", ".", "..") or "/" in name or "\0" in name:
        raise ValueError("a rename takes one name inside a directory")
    return name


def _call(from_dir: int, from_name: str, to_dir: int, to_name: str, flags: int) -> None:
    code = _rename(from_dir, _checked(from_name), to_dir, _checked(to_name), flags)
    if code:
        raise OSError(code, os.strerror(code), to_name)


def exchange(from_dir: int, from_name: str, to_dir: int, to_name: str) -> None:
    """Swap two existing directory entries in one kernel step (both must exist)."""

    _call(from_dir, from_name, to_dir, to_name, _EXCHANGE)


def rename_noreplace(from_dir: int, from_name: str, to_dir: int, to_name: str) -> None:
    """Rename, refused (EEXIST) when anything is at the target."""

    _call(from_dir, from_name, to_dir, to_name, _NOREPLACE)


def is_unsupported(error: OSError) -> bool:
    return error.errno in UNSUPPORTED


def support_problem(directory: int, prefix: str) -> str | None:
    """None when this directory's filesystem has both operations; otherwise a short reason.

    Creates `<prefix>a` and `<prefix>b` exclusively (a name already there is a problem, never
    touched) and removes only the names it created, each only while it is a one-byte file with
    the probe's content (by content, not inode: FAT volumes report unstable inode numbers).
    """

    first, second, moved = f"{prefix}a", f"{prefix}b", f"{prefix}c"
    created: dict[str, int] = {}
    try:
        for name, data in ((first, b"a"), (second, b"b")):
            try:
                descriptor = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory,
                )
            except OSError:
                return "a test file could not be created there"
            try:
                created[name] = os.fstat(descriptor).st_ino
                os.write(descriptor, data)
            finally:
                os.close(descriptor)
        try:
            exchange(directory, first, directory, second)
        except OSError as error:
            return "the filesystem has no atomic exchange" if is_unsupported(error) else "the exchange failed"
        if not (_inode(directory, first) == created[second] and _inode(directory, second) == created[first]):
            return "the filesystem did not exchange the files"
        created = {first: created[second], second: created[first]}
        try:
            rename_noreplace(directory, first, directory, second)
        except OSError as error:
            if error.errno != errno.EEXIST:
                return "the filesystem has no safe rename"
        else:
            return "the filesystem replaced a file it was told not to replace"
        try:
            rename_noreplace(directory, first, directory, moved)
        except OSError:
            return "the filesystem has no safe rename"
        if _inode(directory, moved) != created[first] or _inode(directory, first) is not None:
            return "the filesystem did not rename the file"
        created = {moved: created[first], second: created[second]}
        return None
    finally:
        for name in (first, second, moved):
            if name in created and _probe_file(directory, name):
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=directory)


def remove_leftovers(directory: int, prefix: str) -> list[str]:
    """Remove what an interrupted `support_problem(directory, prefix)` left behind (the prefix is the
    caller's unique one): each of its names only while it is a probe file or an empty file (created,
    not yet written). The names removed."""

    removed = []
    for name in (f"{prefix}a", f"{prefix}b", f"{prefix}c"):
        if _probe_file(directory, name, empty=True):
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=directory)
                removed.append(name)
    return removed


def _probe_file(directory: int, name: str, *, empty: bool = False) -> bool:
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except OSError:
        return False
    try:
        info = os.fstat(descriptor)
        if empty and stat.S_ISREG(info.st_mode) and info.st_size == 0:
            return True
        return stat.S_ISREG(info.st_mode) and info.st_size == 1 and os.read(descriptor, 2) in (b"a", b"b")
    finally:
        os.close(descriptor)


def _inode(directory: int, name: str) -> int | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False).st_ino
    except OSError:
        return None
