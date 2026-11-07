from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

import tests.test_signature_inputs as capture_fixture
from tests.support import ORCHESTRATION_ROOT

from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.signature_input_wire import SignatureWireError, SignatureWireLimits, encode_signature_inputs
from codex_agents.signature_inputs import SignatureInputSnapshot, signature_input_size
from codex_agents.signature_payloads import (
    MAX_SIGNATURE_PAYLOAD_BYTES, SIGNATURE_PAYLOAD_HEADER_BYTES, SignaturePayloadError,
    decode_signature_payloads, prepare_signature_payloads, signature_payload_response_limit,
)

NONCE = bytes(range(16))
VECTORS = Path(__file__).parent / "fixtures/learning/owner-record-payload-vectors.json"


def make_row(content, *, source="owner", signature=b"unverified", index=1):
    return ("record:" + str(index), index, content, hashlib.sha256(content).hexdigest(), source, signature)


def make_wire(project, rows, limits, *, owner=None, signer=b"RAW\x00LEGACY\xff"):
    fields = (project, "codex", owner, signer, rows)
    inputs = SignatureInputSnapshot(object(), *fields, signature_input_size(*fields))
    return encode_signature_inputs(inputs, NONCE, limits)


def frame(wire, row_count, entries):
    return (b"SIGPAY\x00\x01" + NONCE + hashlib.sha256(wire).digest()
            + struct.pack(">II", row_count, len(entries))
            + b"".join(struct.pack(">IH", index, len(payload)) + payload for index, payload in entries))


class SignaturePayloadTests(unittest.TestCase):
    def setUp(self):
        self.vectors = json.loads(VECTORS.read_text())["vectors"]
        self.vector = self.vectors[0]
        self.project = self.vector["project_id"]
        self.content = json.dumps(self.vector["record"]).encode("utf-8")
        self.payload = self.vector["payload_ascii"].encode("ascii")
        self.limits = SignatureWireLimits(8, 131_072)
        self.wire = make_wire(self.project, (make_row(self.content),), self.limits)

    def assert_payload_error(self, reason, operation):
        with self.assertRaises(SignaturePayloadError) as caught:
            operation()
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("CANARY", str(caught.exception))

    def test_all_pre_extraction_vectors_keep_exact_payloads_and_row_positions(self):
        for vector in self.vectors:
            with self.subTest(vector=vector["name"]):
                wire = make_wire(vector["project_id"], (make_row(json.dumps(vector["record"]).encode()),), self.limits)
                raw = prepare_signature_payloads(wire, self.limits)
                expected = vector["payload_ascii"].encode("ascii")
                self.assertEqual(frame(wire, 1, ((0, expected),)), raw)
                self.assertLessEqual(len(raw), signature_payload_response_limit(self.limits))
                batch = decode_signature_payloads(raw, wire, self.limits)
                self.assertEqual((NONCE, hashlib.sha256(wire).digest(), 1, ((0, expected),)),
                                 (batch.nonce, batch.input_digest, batch.row_count, batch.payloads))
                self.assertNotIn(vector["project_id"], repr(batch))
                for field in ("origin", "verified", "admission"):
                    self.assertFalse(hasattr(batch, field))
                with self.assertRaises(FrozenInstanceError):
                    batch.row_count = 0

    def test_mixed_bad_records_omit_only_legacy_ineligible_rows(self):
        contents = (self.content, self.content, b"{CANARY", self.content, b"[]", self.content,
                    b'{"record_type":"unknown"}', self.content)
        sources = ("external", "owner", "owner", "owner", "owner", "owner\x00", "owner", "owner")
        signatures = (b"x", b"", b"x", b"x", b"x", b"x", b"x", b"x")
        rows = tuple(make_row(content, source=source, signature=signature, index=i + 1)
                     for i, (content, source, signature) in enumerate(zip(contents, sources, signatures)))
        wire = make_wire(self.project, rows, self.limits)
        raw = prepare_signature_payloads(wire, self.limits)
        self.assertEqual(((3, self.payload), (7, self.payload)),
                         decode_signature_payloads(raw, wire, self.limits).payloads)
        for name in ("parse_bounded_json", "owner_record_payload"):
            for error_type in (TypeError, ValueError):
                with mock.patch("codex_agents.signature_payloads." + name, side_effect=error_type("CANARY")):
                    raw = prepare_signature_payloads(self.wire, self.limits)
                self.assertEqual((), decode_signature_payloads(raw, self.wire, self.limits).payloads)

    def test_missing_or_incompatible_signer_skips_records_without_parsing(self):
        rows = (make_row(self.content),)
        for owner, signer in ((None, None), ((1, b"different", "enrolled", 0), b"current")):
            wire = make_wire(self.project, rows, self.limits, owner=owner, signer=signer)
            with mock.patch("codex_agents.signature_payloads.parse_bounded_json", side_effect=AssertionError("parse")):
                raw = prepare_signature_payloads(wire, self.limits)
            self.assertEqual((), decode_signature_payloads(raw, wire, self.limits).payloads)
            self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
                frame(wire, 1, ((0, self.payload),)), wire, self.limits))
        empty = make_wire(self.project, (), self.limits)
        self.assertEqual(64, SIGNATURE_PAYLOAD_HEADER_BYTES)
        self.assertEqual(frame(empty, 0, ()), prepare_signature_payloads(empty, self.limits))

    def test_every_truncation_foreign_input_header_and_trailing_data_is_refused(self):
        raw = prepare_signature_payloads(self.wire, self.limits)
        for length in range(len(raw)):
            with self.subTest(length=length):
                self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
                    raw[:length], self.wire, self.limits))
        for offset in (0, 7, 8, 24):
            invalid = raw[:offset] + bytes((raw[offset] ^ 1,)) + raw[offset + 1:]
            self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
                invalid, self.wire, self.limits))
        for invalid in (raw + b"x", raw + raw, raw[:56] + struct.pack(">I", 2) + raw[60:],
                        raw[:60] + struct.pack(">I", 2) + raw[64:]):
            self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
                invalid, self.wire, self.limits))
        other = make_wire(self.project, (make_row(self.content, signature=b"other"),), self.limits)
        self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
            raw, other, self.limits))

    def test_duplicate_reordered_foreign_and_ineligible_indices_are_refused(self):
        rows = (make_row(self.content, index=1), make_row(self.content, index=2))
        wire = make_wire(self.project, rows, self.limits)
        for entries in (((0, self.payload), (0, self.payload)), ((1, self.payload), (0, self.payload)),
                        ((2, self.payload),)):
            self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
                frame(wire, 2, entries), wire, self.limits))
        for row in (make_row(self.content, source="external"), make_row(self.content, signature=None)):
            wire = make_wire(self.project, (row,), self.limits)
            self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
                frame(wire, 1, ((0, self.payload),)), wire, self.limits))

    def test_response_and_declared_counts_lengths_are_checked_before_copy(self):
        import codex_agents.signature_payloads as payloads

        oversized = b"x" * (signature_payload_response_limit(self.limits) + 1)
        with mock.patch.object(payloads, "decode_signature_inputs", side_effect=AssertionError("input copied")):
            self.assert_payload_error("signature_payload_limit", lambda: decode_signature_payloads(
                oversized, self.wire, self.limits))
        raw = prepare_signature_payloads(self.wire, self.limits)
        bad_count = raw[:60] + struct.pack(">I", 9) + raw[64:]
        self.assert_payload_error("signature_payload_limit", lambda: decode_signature_payloads(
            bad_count, self.wire, self.limits))
        observed = []
        original = payloads._Reader.take

        def guarded_take(reader, count):
            observed.append(count)
            if count > MAX_SIGNATURE_PAYLOAD_BYTES:
                raise AssertionError("oversized payload copied")
            return original(reader, count)

        for length in (257, 65535):
            invalid = raw[:68] + struct.pack(">H", length) + raw[70:]
            with mock.patch.object(payloads._Reader, "take", guarded_take):
                self.assert_payload_error("signature_payload_limit", lambda: decode_signature_payloads(
                    invalid, self.wire, self.limits))
        self.assertLessEqual(max(observed), 32)
        self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
            frame(self.wire, 1, ((0, b""),)), self.wire, self.limits))

    def test_payload_shape_scope_and_purpose_are_closed_but_not_authenticated(self):
        variants = (b"CANARY", self.payload.replace(self.project.encode(), b"prj_" + b"f" * 32),
                    self.payload.replace(b"authenticated_owner_record", b"owner_release"),
                    self.payload.replace(b'"feedback"', b'"unknown"'), self.payload + b" ")
        for payload in variants:
            self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
                frame(self.wire, 1, ((0, payload),)), self.wire, self.limits))
        # Valid correlation/shape is data, not authentication of a calculation.
        forged = json.loads(self.payload)
        forged["record_digest"] = "f" * 64
        forged_bytes = json.dumps(forged, sort_keys=True, separators=(",", ":")).encode()
        batch = decode_signature_payloads(frame(self.wire, 1, ((0, forged_bytes),)), self.wire, self.limits)
        self.assertEqual(((0, forged_bytes),), batch.payloads)
        self.assertFalse(hasattr(batch, "verified"))

    def test_maximum_configured_rows_and_format_ceiling(self):
        limits = SignatureWireLimits(4096, 64 * 1_048_576)
        wire = make_wire(self.project, tuple(make_row(self.content, index=i + 1) for i in range(4096)), limits)
        raw = prepare_signature_payloads(wire, limits)
        batch = decode_signature_payloads(raw, wire, limits)
        self.assertEqual(4096, len(batch.payloads))
        self.assertEqual((4095, self.payload), batch.payloads[-1])
        self.assertEqual(1_073_216, signature_payload_response_limit(limits))
        self.assertLessEqual(len(raw), signature_payload_response_limit(limits))

    def test_exact_types_bad_limits_and_callbacks_never_gain_authority(self):
        class CallbackTrap:
            def fail(self, *args):
                raise AssertionError("caller callback invoked")

            __len__ = __iter__ = __hash__ = __eq__ = __str__ = __repr__ = fail

        for value in (None, "CANARY", bytearray(b"x"), memoryview(b"x"), CallbackTrap()):
            self.assert_payload_error("signature_payload_invalid_response", lambda: decode_signature_payloads(
                value, self.wire, self.limits))
        for limits in (None, CallbackTrap()):
            with self.assertRaises(SignatureWireError):
                prepare_signature_payloads(self.wire, limits)
        object.__setattr__(self.limits, "max_rows", True)
        with self.assertRaises(SignatureWireError):
            signature_payload_response_limit(self.limits)
        self.assertEqual("signature_payload_invalid_response", str(SignaturePayloadError(CallbackTrap())))

    def test_limits_are_copied_and_unexpected_faults_inside_handlers_propagate(self):
        import codex_agents.signature_payloads as payloads

        original = payloads.parse_bounded_json

        def changed(raw):
            object.__setattr__(self.limits, "max_rows", 1)
            object.__setattr__(self.limits, "max_field_bytes", 1)
            return original(raw)

        with mock.patch.object(payloads, "parse_bounded_json", side_effect=changed):
            raw = prepare_signature_payloads(self.wire, self.limits)
        self.assertEqual(((0, self.payload),), decode_signature_payloads(
            raw, self.wire, SignatureWireLimits(8, 131_072)).payloads)
        for name in ("parse_bounded_json", "owner_record_payload"):
            for error_type in (AssertionError, MemoryError, RuntimeError, OSError, KeyboardInterrupt, SystemExit):
                with self.subTest(boundary=name, error=error_type.__name__):
                    error = error_type("CANARY-TRUSTED")
                    with mock.patch.object(payloads, name, side_effect=error):
                        with self.assertRaises(error_type) as caught:
                            prepare_signature_payloads(self.wire, SignatureWireLimits(8, 131_072))
                    self.assertIs(error, caught.exception)

    def test_actual_capture_payload_verifies_with_existing_authority_without_extra_state_access(self):
        fixture = capture_fixture.SignatureInputOwnerTests(
            "test_capture_matches_legacy_inputs_without_crypto_connections_or_writes")
        fixture.setUp()
        try:
            inputs = fixture.capture()
            wire = encode_signature_inputs(inputs, NONCE, self.limits)
            with mock.patch("sqlite3.connect", side_effect=AssertionError("extra database")), \
                    mock.patch.object(fixture.state, "digest_key", side_effect=AssertionError("private state key")):
                batch = decode_signature_payloads(prepare_signature_payloads(wire, self.limits), wire, self.limits)
            self.assertEqual(1, len(batch.payloads))
            index, payload = batch.payloads[0]
            authority = OwnerGrantAuthority(fixture.state.allowed_signers_path, signer_line=inputs.signer)
            self.assertTrue(authority._verify_payload(payload, inputs.rows[index][5]))
        finally:
            self.assertTrue(fixture.doCleanups(), "owned fixture cleanup failed")

    def test_fresh_helper_calculation_has_only_metadata_imports_and_no_io(self):
        script = """
import sys
sys.path.insert(0,sys.argv[1])
allowed={'codex_agents.contracts','codex_agents.learning_contracts','codex_agents.owner_record_payload',
         'codex_agents.owner_key_contracts','codex_agents.signature_inputs',
         'codex_agents.signature_input_wire','codex_agents.signature_payloads'}
class MetadataOnly:
    def find_spec(self,fullname,path=None,target=None):
        if (fullname.partition('.')[0] in {'sqlite3','_sqlite3'}
                or (fullname.startswith('codex_agents.') and fullname not in allowed)):
            raise AssertionError('unexpected dependency: '+fullname)
sys.meta_path.insert(0,MetadataOnly())
from codex_agents.signature_input_wire import SignatureWireLimits
from codex_agents.signature_payloads import prepare_signature_payloads,decode_signature_payloads
wire=sys.stdin.buffer.read(131099)
def refuse_io(event,args):
    if event in {'open','os.listdir','os.scandir','os.mkdir','os.remove','os.rename',
                 'os.system','os.fork','os.posix_spawn','os.exec','subprocess.Popen',
                 'socket.__new__','sqlite3.connect'}:
        raise AssertionError('unexpected calculation I/O: '+event)
sys.addaudithook(refuse_io)
limits=SignatureWireLimits(8,131072)
raw=prepare_signature_payloads(wire,limits)
batch=decode_signature_payloads(raw,wire,limits)
assert len(batch.payloads)==1 and not hasattr(batch,'verified')
assert {name for name in sys.modules if name.startswith('codex_agents.')}==allowed
sys.stdout.buffer.write(raw)
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c", script, str(ORCHESTRATION_ROOT)],
            input=self.wire, capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(prepare_signature_payloads(self.wire, self.limits), completed.stdout)
