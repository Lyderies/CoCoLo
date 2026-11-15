"""Fixed private-helper entry point; importing it launches nothing.

The actual trusted parent must supply the reviewed OS policy, process/descriptor
custody, bounded channels, one deadline and conservative cleanup. A ready marker
is only lifecycle data; it proves no native caller, policy or account admission.
No scheduler or normal service operation invokes this entry point.
"""

from __future__ import annotations

import errno
import os
import stat
import struct
import sys

from .owner_key_contracts import OWNER_SIGNERS_MAX_BYTES
from .signature_input_wire import SignatureWireError, SignatureWireLimits
from .signature_payloads import (
    MAX_SIGNATURE_PAYLOAD_BYTES, SignaturePayloadError, prepare_signature_payloads,
)

SIGNATURE_PROCESS_READY = b"SIGRDY\x00\x01"
SIGNATURE_PROCESS_READY_BYTES = 17
_SSH = "/usr/bin/ssh-keygen"
_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
_REFUSE_CALCULATION_IO = frozenset({
    "open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rename",
    "os.system", "os.fork", "os.posix_spawn", "os.exec", "subprocess.Popen",
    "socket.__new__", "sqlite3.connect",
})


class SignatureProcessError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("signature_process_unavailable")


def _number(value: str) -> int:
    if not 1 <= len(value) <= 8 or not value.isascii() or not value.isdecimal():
        raise SignatureProcessError()
    return int(value)


def signature_process_arguments(arguments: list[str]) -> tuple[str, SignatureWireLimits | None]:
    """Closed arguments from the fixed parent, with no paths or commands."""

    if (type(arguments) is not list or len(arguments) not in (1, 3)
            or any(type(value) is not str for value in arguments)):
        raise SignatureProcessError()
    if arguments == ["verify"]:
        return "verify", None
    if len(arguments) == 3 and arguments[0] == "prepare":
        return "prepare", SignatureWireLimits(_number(arguments[1]), _number(arguments[2]))
    raise SignatureProcessError()


def _confine() -> None:
    # macOS applies its policy before exec; Linux confines here, before any input.
    if not sys.platform.startswith("linux"):
        return
    from .signature_sandbox import SignatureSandboxError, confine_signature_helper
    try:
        confine_signature_helper()
    except (SignatureSandboxError, OSError):
        raise SignatureProcessError() from None


def _require_fork_denial() -> None:
    # Run before adding the audit hook: this must be an actual OS refusal.
    # The fixed control child immediately exits and is waited for on failure.
    try:
        pid = os.fork()
    except OSError as error:
        if error.errno in (errno.EPERM, errno.EACCES):
            return
        raise SignatureProcessError() from None
    if pid == 0:
        os._exit(0)
    os.waitpid(pid, 0)
    raise SignatureProcessError()


def _write(data: bytes) -> None:
    view = memoryview(data)
    while view:
        count = os.write(1, view)
        if count == 0:
            raise SignatureProcessError()
        view = view[count:]


def _ready(mode: bytes) -> None:
    _write(SIGNATURE_PROCESS_READY + mode + struct.pack(">Q", os.getpid()))


def _refuse_calculation_io(event: str, arguments: tuple) -> None:
    if event in _REFUSE_CALCULATION_IO:
        raise SignatureProcessError()


def _prepare(limits: SignatureWireLimits) -> None:
    _ready(b"P")
    raw = sys.stdin.buffer.read(limits.max_wire_bytes + 1)
    sys.addaudithook(_refuse_calculation_io)
    _write(prepare_signature_payloads(raw, limits))


def _trusted_ssh() -> None:
    for path, directory in ((_SSH, False), ("/usr/bin", True), ("/usr", True)):
        value = os.lstat(path)
        correct_type = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
        if not correct_type or value.st_uid != 0 or value.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise SignatureProcessError()


def _open_public(directory: int, name: str, maximum: int) -> int:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        value = os.fstat(descriptor)
        if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
                or stat.S_IMODE(value.st_mode) != 0o600 or value.st_nlink != 1
                or not 1 <= value.st_size <= maximum):
            raise SignatureProcessError()
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _verify() -> None:
    # The parent creates this separate private working directory and fixed files.
    # Preopened descriptors retain the exact public files across exec; paths are
    # not reopened after a check. No model/client supplies any filename or argv.
    directory = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    descriptors = [directory]
    try:
        value = os.fstat(directory)
        if value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700:
            raise SignatureProcessError()
        payload = _open_public(directory, "payload", MAX_SIGNATURE_PAYLOAD_BYTES)
        descriptors.append(payload)
        signer = _open_public(directory, "allowed-signers", OWNER_SIGNERS_MAX_BYTES)
        descriptors.append(signer)
        signature = _open_public(directory, "signature", 16_384)
        descriptors.append(signature)
        _trusted_ssh()
        _ready(b"V")
        null = os.open("/dev/null", os.O_WRONLY | os.O_NOFOLLOW)
        descriptors.append(null)
        os.dup2(payload, 0, inheritable=True)
        os.dup2(null, 1, inheritable=True)
        os.dup2(null, 2, inheritable=True)
        os.set_inheritable(signer, True)
        os.set_inheritable(signature, True)
        os.execve(_SSH, [
            _SSH, "-Y", "verify", "-f", f"/dev/fd/{signer}",
            "-I", "codex-agents-owner", "-n", "codex-agents-owner-v1",
            "-s", f"/dev/fd/{signature}",
        ], _ENVIRONMENT)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def main(arguments: list[str] | None = None) -> int:
    """Fence only at the fixed process boundary, without raw diagnostic output."""

    try:
        mode, limits = signature_process_arguments(sys.argv[1:] if arguments is None else arguments)
        _confine()
        _require_fork_denial()
        if mode == "prepare":
            _prepare(limits)
        else:
            _verify()
    except (SignatureProcessError, SignatureWireError, SignaturePayloadError):
        return 2
    except BaseException:
        # Library functions still propagate unexpected faults. A private process
        # must terminate without echoing captured records in a traceback.
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
