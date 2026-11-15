from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests.test_signature_inputs as capture_fixture
from tests.support import ORCHESTRATION_ROOT

from codex_agents.learning_contracts import parse_bounded_json
from codex_agents.owner_record_payload import owner_record_payload
from codex_agents.signature_input_wire import SignatureWireError, SignatureWireLimits, encode_signature_inputs
from codex_agents.signature_payloads import decode_signature_payloads
from codex_agents.signature_process import (
    SIGNATURE_PROCESS_READY, SIGNATURE_PROCESS_READY_BYTES, SignatureProcessError,
    main, signature_process_arguments,
)

BOOTSTRAP = ("import sys;sys.path.insert(0,sys.argv[1]);"
             "from codex_agents.signature_process import main;"
             "raise SystemExit(main(sys.argv[2:]))")


class SignatureProcessArgumentTests(unittest.TestCase):
    def test_closed_bounded_arguments_with_no_caller_callbacks(self):
        self.assertEqual(("verify", None), signature_process_arguments(["verify"]))
        mode, limits = signature_process_arguments(["prepare", "4096", "67108864"])
        self.assertEqual(("prepare", 4096, 67108864), (mode, limits.max_rows, limits.max_field_bytes))

        class CallbackTrap:
            def fail(self, *args):
                raise AssertionError("caller callback invoked")

            __len__ = __iter__ = __eq__ = __str__ = __repr__ = fail

        for arguments in (None, (), CallbackTrap(), [], [CallbackTrap()], ["verify", "CANARY"],
                          ["other"], ["prepare", "1"], ["prepare", "1", "1", "CANARY"],
                          ["prepare", "9" * 10000, "1"], ["prepare", "\u0661", "1"],
                          ["prepare", "-1", "1"], ["prepare", "+1", "1"],
                          ["prepare", "1.0", "1"], ["prepare", "", "1"]):
            with self.assertRaises(SignatureProcessError):
                signature_process_arguments(arguments)
        for arguments in (["prepare", "0", "1"], ["prepare", "4097", "1"],
                          ["prepare", "1", "67108865"]):
            with self.assertRaises(SignatureWireError):
                signature_process_arguments(arguments)

    def test_process_boundary_fences_faults_without_echo_or_spawn_on_bad_args(self):
        with mock.patch("codex_agents.signature_process._confine"), \
                mock.patch("codex_agents.signature_process._require_fork_denial") as denial:
            self.assertEqual(2, main(["CANARY-PRIVATE"]))
            denial.assert_not_called()
        for error_type in (AssertionError, RuntimeError, MemoryError, OSError, KeyboardInterrupt, SystemExit):
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(error=error_type.__name__), contextlib.redirect_stdout(stdout), \
                    contextlib.redirect_stderr(stderr), \
                    mock.patch("codex_agents.signature_process._confine"), \
                    mock.patch("codex_agents.signature_process._require_fork_denial"), \
                    mock.patch("codex_agents.signature_process._prepare", side_effect=error_type("CANARY-PRIVATE")):
                self.assertEqual(3, main(["prepare", "8", "65536"]))
            self.assertEqual(("", ""), (stdout.getvalue(), stderr.getvalue()))


MACOS_POLICY = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()
LINUX = sys.platform.startswith("linux")


@unittest.skipUnless(MACOS_POLICY or LINUX,
                     "requires the disposable macOS no-fork test policy, or Linux self-confinement")
class SignatureProcessExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = capture_fixture.SignatureInputOwnerTests(
            "test_capture_matches_legacy_inputs_without_crypto_connections_or_writes")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.inputs = self.fixture.capture()
        self.limits = SignatureWireLimits(8, 65536)
        self.wire = encode_signature_inputs(self.inputs, bytes(range(16)), self.limits)
        self.row = self.inputs.rows[0]
        self.payload = owner_record_payload(self.inputs.project_id, parse_bounded_json(self.row[2]))
        self.scratch = self.fixture.base / "fixed-worker"
        self.scratch.mkdir(mode=0o700)
        for name, data in (("payload", self.payload), ("allowed-signers", self.inputs.signer),
                           ("signature", self.row[5])):
            self.write(name, data)
        self.profile = ('(version 1) (allow default) (deny network*) (deny file-write*) (deny process-fork) '
                        '(allow file-write* (subpath ' + json.dumps(str(self.scratch)) + ')) '
                        '(allow file-write* (literal "/dev/null"))')

    def write(self, name, data):
        path = self.scratch / name
        path.write_bytes(data)
        path.chmod(0o600)

    def run_helper(self, arguments, *, data=None, restricted=True):
        command = [sys.executable, "-I", "-B", "-S", "-c", BOOTSTRAP, str(ORCHESTRATION_ROOT), *arguments]
        if restricted and MACOS_POLICY:
            command = ["/usr/bin/sandbox-exec", "-p", self.profile, *command]
        process = subprocess.Popen(
            command, cwd=self.scratch, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "TMPDIR": str(self.scratch)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True, start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(data, timeout=5)
        except BaseException:
            process.kill()
            process.communicate(timeout=5)
            raise
        self.assertEqual(b"", stderr)
        return process, stdout

    def assert_ready(self, process, raw, mode):
        self.assertEqual(17, SIGNATURE_PROCESS_READY_BYTES)
        self.assertEqual(SIGNATURE_PROCESS_READY + mode + struct.pack(">Q", process.pid), raw[:17])

    def test_restricted_prepare_has_same_owned_pid_and_exact_bound_response(self):
        process, raw = self.run_helper(["prepare", "8", "65536"], data=self.wire)
        self.assertEqual(0, process.returncode)
        self.assert_ready(process, raw, b"P")
        batch = decode_signature_payloads(raw[17:], self.wire, self.limits)
        self.assertEqual(((0, self.payload),), batch.payloads)
        self.assertLessEqual(len(raw), 17 + 64 + 262 * 8)

    def test_restricted_same_pid_exec_verifies_real_public_signature(self):
        process, raw = self.run_helper(["verify"])
        self.assertEqual(0, process.returncode)
        self.assert_ready(process, raw, b"V")
        self.assertEqual(17, len(raw))
        self.assertEqual({"payload", "allowed-signers", "signature"}, {path.name for path in self.scratch.iterdir()})

    def test_wrong_project_and_parseable_corrupted_signature_are_refused(self):
        other = "prj_" + ("1" * 32 if self.inputs.project_id != "prj_" + "1" * 32 else "2" * 32)
        self.write("payload", owner_record_payload(other, parse_bounded_json(self.row[2])))
        process, raw = self.run_helper(["verify"])
        self.assertGreater(process.returncode, 0)
        self.assert_ready(process, raw, b"V")
        self.write("payload", self.payload)
        armor = self.row[5].splitlines()
        binary = bytearray(base64.b64decode(b"".join(armor[1:-1])))
        binary[-1] ^= 1
        encoded = base64.b64encode(binary)
        corrupted = armor[0] + b"\n" + b"\n".join(encoded[i:i+70] for i in range(0, len(encoded), 70)) + b"\n" + armor[-1] + b"\n"
        self.write("signature", corrupted)
        process, raw = self.run_helper(["verify"])
        self.assertGreater(process.returncode, 0)
        self.assert_ready(process, raw, b"V")

    def test_symlink_hardlink_fifo_wrong_mode_and_oversize_refuse_before_ready(self):
        path = self.scratch / "signature"
        path.unlink()
        path.symlink_to(self.scratch / "allowed-signers")
        process, raw = self.run_helper(["verify"])
        self.assertNotEqual(0, process.returncode)
        self.assertEqual(b"", raw)
        path.unlink()
        os.link(self.scratch / "allowed-signers", path)
        process, raw = self.run_helper(["verify"])
        self.assertNotEqual(0, process.returncode)
        self.assertEqual(b"", raw)
        path.unlink()
        os.mkfifo(path, mode=0o600)
        process, raw = self.run_helper(["verify"])
        self.assertNotEqual(0, process.returncode)
        self.assertEqual(b"", raw)
        path.unlink()
        self.write("signature", self.row[5])
        for name, maximum in (("payload", 256), ("allowed-signers", 4096), ("signature", 16384)):
            original = (self.scratch / name).read_bytes()
            for data in (b"", b"x" * (maximum + 1)):
                self.write(name, data)
                process, raw = self.run_helper(["verify"])
                self.assertNotEqual(0, process.returncode)
                self.assertEqual(b"", raw)
            self.write(name, original)
            (self.scratch / name).chmod(0o644)
            process, raw = self.run_helper(["verify"])
            self.assertNotEqual(0, process.returncode)
            self.assertEqual(b"", raw)
            (self.scratch / name).chmod(0o600)
        self.scratch.chmod(0o755)
        process, raw = self.run_helper(["verify"])
        self.assertNotEqual(0, process.returncode)
        self.assertEqual(b"", raw)

    @unittest.skipUnless(MACOS_POLICY, "on Linux the helper always confines itself")
    def test_unrestricted_helper_refuses_before_ready(self):
        process, raw = self.run_helper(["prepare", "8", "65536"], data=self.wire, restricted=False)
        self.assertEqual(2, process.returncode)
        self.assertEqual(b"", raw)

    def test_bad_arguments_and_input_are_fenced_without_content(self):
        process, raw = self.run_helper(["CANARY-PRIVATE"])
        self.assertEqual(2, process.returncode)
        self.assertEqual(b"", raw)
        for data in (b"CANARY-PRIVATE", b"x" * 65563):
            process, raw = self.run_helper(["prepare", "8", "65536"], data=data)
            self.assertEqual(2, process.returncode)
            self.assert_ready(process, raw, b"P")
            self.assertEqual(17, len(raw))


CONFINEMENT_PROBE = """import errno,json,os,socket,sys
sys.path.insert(0,sys.argv[1])
from codex_agents.signature_sandbox import confine_signature_helper
confine_signature_helper()
def attempt(action):
    try:
        action()
    except OSError as error:
        return errno.errorcode.get(error.errno, error.errno)
    return "allowed"
def fork():
    if os.fork() == 0:
        os._exit(0)
outside = sys.argv[2]
print(json.dumps({
    "fork": attempt(fork),
    "socket": attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)),
    "unix_socket": attempt(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)),
    "write_inside": attempt(lambda: open("inside", "w").close()),
    "write_outside": attempt(lambda: open(outside, "w").close()),
}))
"""


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux self-confinement")
class LinuxConfinementTests(unittest.TestCase):
    def test_the_confined_helper_cannot_start_processes_open_sockets_or_write_elsewhere(self):
        with tempfile.TemporaryDirectory() as scratch, tempfile.TemporaryDirectory() as elsewhere:
            os.chmod(scratch, 0o700)
            outside = os.path.join(elsewhere, "written")
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-S", "-c", CONFINEMENT_PROBE, str(ORCHESTRATION_ROOT), outside],
                cwd=scratch, capture_output=True, timeout=30, check=False)
            self.assertEqual(0, completed.returncode, completed.stderr)
            seen = json.loads(completed.stdout)
            self.assertEqual(("EPERM", "EPERM", "EPERM", "allowed"),
                             (seen["fork"], seen["socket"], seen["unix_socket"], seen["write_inside"]))
            # Landlock is enforced where the kernel has it; seccomp alone applies otherwise.
            landlock = subprocess.run(
                [sys.executable, "-I", "-c", "import ctypes;l=ctypes.CDLL(None,use_errno=True);"
                 "l.syscall.restype=ctypes.c_long;print(l.syscall(444,None,ctypes.c_size_t(0),ctypes.c_uint32(1)))"],
                capture_output=True, text=True, timeout=30, check=True)
            if int(landlock.stdout) >= 1:
                self.assertIn(seen["write_outside"], ("EACCES", "EPERM"))
                self.assertFalse(os.path.exists(outside))
