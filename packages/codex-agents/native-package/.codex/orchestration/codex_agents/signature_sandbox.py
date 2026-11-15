"""Linux confinement for the fixed signature helper; importing it changes nothing.

macOS runs the helper under sandbox-exec. Linux has no such wrapper, so the helper
confines itself before it reads any input, with two kernel features that need no
privilege and persist across exec into ssh-keygen:

- a seccomp filter that refuses new processes (fork, vfork, clone, clone3), sockets
  and io_uring with EPERM. This is required: without it the helper does not start,
  and the helper's own fork check then proves the refusal is real.
- a Landlock ruleset that refuses every file write outside the working directory
  and /dev/null, applied when the kernel has Landlock (Linux 5.13 or later).

Only x86_64 and aarch64 are supported; anything else fails closed.
"""

from __future__ import annotations

import ctypes
import errno
import os
import platform
import struct

_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_X32_SYSCALL_BIT = 0x40000000

# BPF opcodes: ld word absolute, jump if equal / greater-or-equal, return constant.
_LD_ABS = 0x20
_JEQ = 0x15
_JGE = 0x35
_RET = 0x06

# Audit architecture and the refused syscall numbers per architecture.
_ARCHES = {
    "x86_64": (0xC000003E, (56, 57, 58, 435, 41, 425)),   # clone fork vfork clone3 socket io_uring_setup
    "aarch64": (0xC00000B7, (220, 435, 198, 425)),        # clone clone3 socket io_uring_setup
}
_ARCHES["amd64"] = _ARCHES["x86_64"]
_ARCHES["arm64"] = _ARCHES["aarch64"]

# Landlock: the same syscall numbers on every architecture.
_LANDLOCK_CREATE_RULESET, _LANDLOCK_ADD_RULE, _LANDLOCK_RESTRICT_SELF = 444, 445, 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_WRITE_FILE = 1 << 1
_DIRECTORY_WRITES = sum(1 << bit for bit in range(4, 13))  # remove and make of every kind
_REFER = 1 << 13     # ABI 2
_TRUNCATE = 1 << 14  # ABI 3


class SignatureSandboxError(RuntimeError):
    """The helper could not be confined; it must not continue."""


def _libc():
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    library.prctl.restype = ctypes.c_int
    return library


def _filter(arch: str) -> bytes:
    audit, refused = _ARCHES[arch]
    deny = _SECCOMP_RET_ERRNO | errno.EPERM
    program = [(_LD_ABS, 0, 0, 4)]                      # seccomp_data.arch
    # Another architecture's calling convention could name other syscalls: refuse it.
    program.append((_JEQ, 1, 0, audit))
    program.append((_RET, 0, 0, deny))
    program.append((_LD_ABS, 0, 0, 0))                  # seccomp_data.nr
    if audit == _ARCHES["x86_64"][0]:
        program.append((_JGE, len(refused) + 1, 0, _X32_SYSCALL_BIT))
    for index, number in enumerate(refused):
        program.append((_JEQ, len(refused) - index, 0, number))
    program.append((_RET, 0, 0, _SECCOMP_RET_ALLOW))
    program.append((_RET, 0, 0, deny))
    return b"".join(struct.pack("=HBBI", *instruction) for instruction in program)


def _install_filter(library, arch: str) -> None:
    code = _filter(arch)
    instructions = ctypes.create_string_buffer(code, len(code))

    class _Program(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    program = _Program(len(code) // 8, ctypes.cast(instructions, ctypes.c_void_p))
    if library.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.byref(program), 0, 0) != 0:
        raise SignatureSandboxError()


def _landlock(library, directory: str) -> None:
    abi = library.syscall(_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                          ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION))
    if abi < 1:
        # No Landlock in this kernel, or it is disabled: seccomp alone applies.
        if ctypes.get_errno() in (errno.ENOSYS, errno.EOPNOTSUPP):
            return
        raise SignatureSandboxError()
    handled = _WRITE_FILE | _DIRECTORY_WRITES
    if abi >= 2:
        handled |= _REFER
    if abi >= 3:
        handled |= _TRUNCATE
    attributes = struct.pack("=Q", handled)
    ruleset = library.syscall(_LANDLOCK_CREATE_RULESET, ctypes.c_char_p(attributes),
                              ctypes.c_size_t(len(attributes)), ctypes.c_uint32(0))
    if ruleset < 0:
        raise SignatureSandboxError()
    try:
        file_rights = handled & (_WRITE_FILE | _TRUNCATE)
        for path, rights in ((directory, handled), ("/dev/null", file_rights)):
            descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                rule = struct.pack("=Qi", rights, descriptor)
                if library.syscall(_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset),
                                   ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                                   ctypes.c_char_p(rule), ctypes.c_uint32(0)) != 0:
                    raise SignatureSandboxError()
            finally:
                os.close(descriptor)
        if library.syscall(_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)) != 0:
            raise SignatureSandboxError()
    finally:
        os.close(ruleset)


def confine_signature_helper(directory: str = ".") -> None:
    """Confine this process and everything it execs; fails closed."""
    arch = platform.machine().lower()
    if arch not in _ARCHES:
        raise SignatureSandboxError()
    library = _libc()
    if library.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise SignatureSandboxError()
    _landlock(library, directory)
    _install_filter(library, arch)
