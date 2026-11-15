from __future__ import annotations

import hashlib
import struct
import subprocess
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest import mock

import tests.test_signature_inputs as capture_fixture
from tests.support import ORCHESTRATION_ROOT

from codex_agents.signature_inputs import SignatureInputSnapshot, signature_input_size
from codex_agents.signature_input_wire import (
    SIGNATURE_INPUT_WIRE_OVERHEAD, SignatureWireError,
    SignatureWireLimits, decode_signature_inputs, encode_signature_inputs,
)

PROJECT = "prj_" + "0" * 32
NONCE = bytes(range(16))


def snapshot(*, rows=(), owner=None, signer=None, provider="codex", origin=None):
    return SignatureInputSnapshot(
        object() if origin is None else origin, PROJECT, provider, owner, signer, rows,
        signature_input_size(PROJECT, provider, owner, signer, rows),
    )


def row(*, signature=None):
    content = b"RAW\x00CONTENT\xff"
    return ("record:\x00\u03a9", 1, content, hashlib.sha256(content).hexdigest(), "owner\x00\u03bb", signature)


class SignatureInputWireTests(unittest.TestCase):
    def setUp(self):
        self.limits = SignatureWireLimits(8, 131_072)

    def assert_wire_error(self, reason, operation):
        with self.assertRaises(SignatureWireError) as caught:
            operation()
        self.assertEqual(reason, caught.exception.reason_code)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn("CANARY", str(caught.exception))

    def roundtrip(self, inputs):
        raw = encode_signature_inputs(inputs, NONCE, self.limits)
        decoded = decode_signature_inputs(raw, self.limits)
        self.assertEqual(NONCE, decoded.nonce)
        for field in ("project_id", "provider", "owner", "signer", "rows", "retained_bytes"):
            self.assertEqual(getattr(inputs, field), getattr(decoded, field))
        self.assertFalse(hasattr(decoded, "origin"))
        self.assertFalse(hasattr(decoded, "verified"))
        self.assertFalse(hasattr(decoded, "admission"))
        self.assertLessEqual(len(raw), inputs.retained_bytes + SIGNATURE_INPUT_WIRE_OVERHEAD)
        self.assertNotIn("RAW", repr(decoded))
        with self.assertRaises(FrozenInstanceError):
            decoded.provider = "other"
        return raw

    def test_raw_fields_unicode_null_empty_and_owner_generations_roundtrip(self):
        class OriginTrap:
            def __repr__(self):
                raise AssertionError("origin must stay local")

        for provider in ("codex", "claude"):
            for owner in (None, (7, b"RAW\x00LEGACY OWNER\xff", "enrolled", 0),
                          (9, b"RAW DIFFERENT KEY", "replaced", (1 << 63) - 1)):
                with self.subTest(provider=provider, owner=owner is not None):
                    self.roundtrip(snapshot(provider=provider, owner=owner, signer=b"RAW\x00CURRENT\xff",
                                            rows=(row(), row(signature=b""), row(signature=b"\x00\xff")),
                                            origin=OriginTrap()))

    def test_version_one_golden_frame_attains_the_derived_wire_allowance(self):
        inputs = snapshot(signer=b"x")
        limits = SignatureWireLimits(1, inputs.retained_bytes)
        raw = encode_signature_inputs(inputs, NONCE, limits)
        expected = (b"SIGJOB\x00\x01" + bytes(range(16)) + b"\x00\x00\x00\x24" + PROJECT.encode("ascii")
                    + b"\x00\x00\x01\x00\x00\x00\x01x\x00\x00\x00\x00")
        self.assertEqual(expected, raw)
        self.assertEqual(inputs.retained_bytes + 26, len(raw))
        self.assertEqual(limits.max_wire_bytes, len(raw))
        self.assertEqual(inputs.retained_bytes, decode_signature_inputs(raw, limits).retained_bytes)

    def test_maximum_rows_raw_fields_and_chunked_unicode_stay_bounded(self):
        rows = tuple((f"id:{index}", index + 1, b"x", "a" * 64, "owner", b"") for index in range(4096))
        inputs = snapshot(rows=rows)
        self.limits = SignatureWireLimits(4096, inputs.retained_bytes)
        self.roundtrip(inputs)
        extra = snapshot(rows=rows + (rows[-1],))
        self.assert_wire_error("signature_wire_limit", lambda: encode_signature_inputs(extra, NONCE, self.limits))
        content = bytes(range(256)) * 256
        fat_row = ("id:\x00\u03a9", (1 << 63) - 1, content, hashlib.sha256(content).hexdigest(),
                   "\U0001f600" * 50_000, bytes(range(256)) * 64)
        inputs = snapshot(rows=(fat_row,), owner=((1 << 63) - 1, b"K" * 4096, "replaced", 0),
                          signer=b"S" * 4096)
        self.limits = SignatureWireLimits(1, inputs.retained_bytes)
        self.roundtrip(inputs)
        generous = SignatureWireLimits(8, 64 * 1_048_576)
        for value in (snapshot(rows=(("id", 1, content + b"x", "a" * 64, "owner", None),)),
                      snapshot(rows=(("id", 1, b"x", "a" * 64, "owner", b"s" * 16_385),)),
                      snapshot(owner=(1, b"k" * 4097, "enrolled", 0)), snapshot(signer=b"k" * 4097)):
            self.assert_wire_error("signature_wire_limit", lambda: encode_signature_inputs(value, NONCE, generous))

    def test_every_truncation_trailing_data_and_invalid_header_is_refused(self):
        raw = self.roundtrip(snapshot(rows=(row(signature=b"x"),), owner=(1, b"key", "enrolled", 2)))
        for length in range(len(raw)):
            with self.subTest(length=length):
                self.assert_wire_error("signature_wire_invalid_input", lambda: decode_signature_inputs(
                    raw[:length], self.limits))
        for value in (raw + b"x", raw + raw, b"X" + raw[1:], raw[:7] + b"\x02" + raw[8:]):
            self.assert_wire_error("signature_wire_invalid_input", lambda: decode_signature_inputs(value, self.limits))
        empty = encode_signature_inputs(snapshot(), NONCE, self.limits)
        for offset in (64, 65, 66):
            invalid = empty[:offset] + b"\xff" + empty[offset + 1:]
            self.assert_wire_error("signature_wire_invalid_input", lambda: decode_signature_inputs(invalid, self.limits))
        invalid_utf8 = empty[:28] + b"\xff" + empty[29:]
        self.assert_wire_error("signature_wire_invalid_input", lambda: decode_signature_inputs(invalid_utf8, self.limits))

    def test_field_row_and_raw_limits_apply_before_large_field_copy(self):
        inputs = snapshot(rows=(row(signature=b"x"), row()))
        raw = encode_signature_inputs(inputs, NONCE, self.limits)
        small = SignatureWireLimits(8, inputs.retained_bytes - 1)
        self.assert_wire_error("signature_wire_limit", lambda: encode_signature_inputs(inputs, NONCE, small))
        self.assert_wire_error("signature_wire_limit", lambda: decode_signature_inputs(raw, small))
        self.assert_wire_error("signature_wire_limit", lambda: decode_signature_inputs(
            b"x" * (self.limits.max_wire_bytes + 1), self.limits))
        self.assert_wire_error("signature_wire_limit", lambda: encode_signature_inputs(
            inputs, NONCE, SignatureWireLimits(1, self.limits.max_field_bytes)))
        empty = encode_signature_inputs(snapshot(), NONCE, self.limits)
        bad_count = empty[:-4] + struct.pack(">I", self.limits.max_rows + 1)
        self.assert_wire_error("signature_wire_limit", lambda: decode_signature_inputs(bad_count, self.limits))
        import codex_agents.signature_input_wire as wire
        original = wire._Reader.take
        observed = []

        def bounded_take(reader, count):
            observed.append(count)
            if count > self.limits.max_field_bytes:
                raise AssertionError("oversized field copied before quota")
            return original(reader, count)

        bad_length = empty[:24] + b"\xff\xff\xff\xff" + empty[28:]
        with mock.patch.object(wire._Reader, "take", bounded_take):
            self.assert_wire_error("signature_wire_limit", lambda: decode_signature_inputs(bad_length, self.limits))
        self.assertEqual([8, 16, 4], observed)

    def test_wrong_types_invalid_values_and_false_retained_counts_do_not_gain_authority(self):
        inputs = snapshot(rows=(row(),))
        variants = (object(), replace(inputs, project_id="CANARY"), replace(inputs, provider="CANARY"),
                    replace(inputs, rows=list(inputs.rows)), replace(inputs, retained_bytes=True),
                    replace(inputs, retained_bytes=inputs.retained_bytes - 1),
                    replace(inputs, owner=(0, b"x", "enrolled", 1)),
                    replace(inputs, owner=(1, b"x", "CANARY", 1)),
                    replace(inputs, owner=(1, b"x", "enrolled", 1 << 63)),
                    replace(inputs, rows=(("id", 0, b"x", "a" * 64, "owner", None),)),
                    replace(inputs, rows=(("id", 1, b"x", "A" * 64, "owner", None),)),
                    replace(inputs, rows=(("id", 1, b"x", "a" * 64, "\ud800", None),)))
        for value in variants:
            with self.subTest(kind=type(value).__name__):
                self.assert_wire_error("signature_wire_invalid_input", lambda: encode_signature_inputs(
                    value, NONCE, self.limits))
        for nonce in (None, b"", b"x" * 15, bytearray(NONCE)):
            self.assert_wire_error("signature_wire_invalid_input", lambda: encode_signature_inputs(inputs, nonce, self.limits))
        for raw in (None, "CANARY", bytearray(b"x"), memoryview(b"x")):
            self.assert_wire_error("signature_wire_invalid_input", lambda: decode_signature_inputs(raw, self.limits))

    def test_wire_signed_integer_boundaries_and_callbacks_are_refused(self):
        inputs = snapshot(rows=(row(),))
        raw = encode_signature_inputs(inputs, NONCE, self.limits)
        sequence_offset = 71 + 4 + len(inputs.rows[0][0].encode("utf-8"))
        for value in (0, 1 << 63, (1 << 64) - 1):
            invalid = raw[:sequence_offset] + struct.pack(">Q", value) + raw[sequence_offset + 8:]
            self.assert_wire_error("signature_wire_invalid_input", lambda: decode_signature_inputs(invalid, self.limits))

        class CallbackTrap:
            def fail(self, *args):
                raise AssertionError("caller callback invoked")

            __len__ = __iter__ = __hash__ = __eq__ = __str__ = __repr__ = fail

        trap = CallbackTrap()
        variants = (replace(inputs, project_id=trap), replace(inputs, provider=trap),
                    replace(inputs, rows=trap), replace(inputs, owner=trap), replace(inputs, signer=trap),
                    replace(inputs, rows=((trap, 1, b"x", "a" * 64, "owner", None),)),
                    replace(inputs, rows=(("id", 1, trap, "a" * 64, "owner", None),)))
        for index, value in enumerate(variants):
            with self.subTest(index=index):
                self.assert_wire_error("signature_wire_invalid_input", lambda: encode_signature_inputs(value, NONCE, self.limits))
        self.assert_wire_error("signature_wire_invalid_limits", lambda: SignatureWireLimits(trap, 1))

    def test_limits_are_exact_frozen_revalidated_and_copied(self):
        for values in ((True, 1), (1, False), (0, 1), (4097, 1), (1, 64 * 1_048_576 + 1), (1, "CANARY")):
            self.assert_wire_error("signature_wire_invalid_limits", lambda: SignatureWireLimits(*values))
        with self.assertRaises(FrozenInstanceError):
            self.limits.max_rows = 1
        inputs = snapshot(rows=(row(),))
        import codex_agents.signature_input_wire as wire
        original = wire._encode_text_chunk

        def changed(text):
            object.__setattr__(self.limits, "max_field_bytes", 1)
            object.__setattr__(inputs, "rows", ())
            object.__setattr__(inputs, "retained_bytes", 0)
            return original(text)

        original_rows = inputs.rows
        with mock.patch.object(wire, "_encode_text_chunk", side_effect=changed):
            raw = encode_signature_inputs(inputs, NONCE, self.limits)
        self.assertEqual(original_rows, decode_signature_inputs(raw, SignatureWireLimits(8, 131_072)).rows)
        object.__setattr__(self.limits, "max_rows", True)
        self.assert_wire_error("signature_wire_invalid_limits", lambda: encode_signature_inputs(inputs, NONCE, self.limits))

    def test_programming_and_resource_faults_inside_actual_codec_handlers_propagate(self):
        inputs = snapshot(rows=(row(),))
        raw = encode_signature_inputs(inputs, NONCE, self.limits)
        for name, operation in (("encode", lambda: encode_signature_inputs(inputs, NONCE, self.limits)),
                                ("decode", lambda: decode_signature_inputs(raw, self.limits))):
            for error_type in (AssertionError, MemoryError, RuntimeError):
                with self.subTest(codec=name, error=error_type.__name__):
                    error = error_type("CANARY-TRUSTED")
                    with mock.patch("codex_agents.signature_input_wire.codecs." + name, side_effect=error):
                        with self.assertRaises(error_type) as caught:
                            operation()
                    self.assertIs(error, caught.exception)

    def test_actual_owner_capture_fields_roundtrip_without_an_extra_connection_or_key(self):
        fixture = capture_fixture.SignatureInputOwnerTests(
            "test_capture_matches_legacy_inputs_without_crypto_connections_or_writes")
        fixture.setUp()
        try:
            inputs = fixture.capture()
            with mock.patch("sqlite3.connect", side_effect=AssertionError("extra database")), \
                    mock.patch.object(fixture.state, "digest_key", side_effect=AssertionError("private state key")):
                self.roundtrip(inputs)
        finally:
            self.assertTrue(fixture.doCleanups(), "owned fixture cleanup failed")

    def test_fresh_helper_import_and_roundtrip_exclude_persistence_and_transport(self):
        script = """
import sys
sys.path.insert(0, sys.argv[1])
allowed = {'codex_agents.contracts', 'codex_agents.learning_contracts',
           'codex_agents.owner_key_contracts', 'codex_agents.signature_inputs',
           'codex_agents.signature_input_wire'}
class MetadataImportsOnly:
    def find_spec(self, fullname, path=None, target=None):
        if (fullname.partition('.')[0] in {'sqlite3', '_sqlite3'}
                or (fullname.startswith('codex_agents.') and fullname not in allowed)):
            raise AssertionError('unexpected owner/transport import: ' + fullname)
sys.meta_path.insert(0, MetadataImportsOnly())
from codex_agents.signature_inputs import SignatureInputSnapshot, signature_input_size
from codex_agents.signature_input_wire import SignatureWireLimits, encode_signature_inputs, decode_signature_inputs
project = 'prj_' + '0' * 32
inputs = SignatureInputSnapshot(object(), project, 'codex', None, b'x', (),
                                signature_input_size(project, 'codex', None, b'x', ()))
def refuse_io(event, arguments):
    if event in {'open', 'os.listdir', 'os.scandir', 'os.mkdir', 'os.remove', 'os.rename',
                 'os.system', 'os.fork', 'os.posix_spawn', 'os.exec',
                 'subprocess.Popen', 'socket.__new__', 'sqlite3.connect'}:
        raise AssertionError('unexpected wire I/O: ' + event)
sys.addaudithook(refuse_io)
limits = SignatureWireLimits(1, inputs.retained_bytes)
decoded = decode_signature_inputs(encode_signature_inputs(inputs, b'x' * 16, limits), limits)
assert decoded.signer == b'x' and not hasattr(decoded, 'origin')
assert {name for name in sys.modules if name.startswith('codex_agents.')} == allowed
print('signature-input-wire-isolated')
"""
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-S", "-c", script, str(ORCHESTRATION_ROOT)],
            text=True, capture_output=True, timeout=10, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("signature-input-wire-isolated\n", completed.stdout)
