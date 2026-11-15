from __future__ import annotations

import base64
import unittest

import tests.support  # noqa: F401
from codex_agents.owner_key_contracts import (
    OWNER_SIGNERS_MAX_BYTES, _owner_key_identity, checked_public_key, owner_public_key_from_line,
)


def public_value(key_type, *fields):
    # Serialization fixtures exercise the existing enrollment encoding policy;
    # they assert no signing identity or possession of a matching private key.
    parts = (key_type.encode("ascii"), *fields)
    blob = b"".join(len(part).to_bytes(4, "big") + part for part in parts)
    return key_type + " " + base64.b64encode(blob).decode("ascii")


class OwnerKeyContractTests(unittest.TestCase):
    def test_supported_canonical_public_formats_and_exact_stored_line(self):
        values = (
            public_value("ssh-ed25519", bytes(range(32))),
            public_value("sk-ssh-ed25519@openssh.com", bytes(range(32)), b"ssh:fixture"),
            public_value("ssh-rsa", b"\x01\x00\x01", b"\x01" + b"\x55" * 127),
            public_value("ecdsa-sha2-nistp256", b"nistp256", b"\x04" + bytes(range(64))),
            public_value("sk-ecdsa-sha2-nistp256@openssh.com", b"nistp256", b"\x04" + bytes(range(64)), b"ssh:fixture"),
        )
        for value in values:
            with self.subTest(key_type=value.split()[0]):
                checked, fingerprint = checked_public_key(value)
                self.assertEqual(value, checked)
                self.assertRegex(fingerprint, r"^SHA256:[A-Za-z0-9+/]{43}$")
                line = ("codex-agents-owner " + value + "\n").encode("ascii")
                self.assertEqual((checked, fingerprint), owner_public_key_from_line(line))

    def test_format_comments_options_and_noncanonical_base64_are_refused(self):
        value = public_value("sk-ssh-ed25519@openssh.com", bytes(range(32)), b"x")
        key_type, blob = value.split()
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        self.assertTrue(blob.endswith("="))
        # Flip unused padding bits: decoded bytes stay equal, spelling changes.
        position = len(blob.rstrip("=")) - 1
        changed = blob[:position] + alphabet[alphabet.index(blob[position]) + 1] + blob[position + 1:]
        self.assertEqual(base64.b64decode(blob), base64.b64decode(changed))
        cases = (None, True, [], {}, 1, "", value + " comment", value + "\n", " " + value,
                 value.replace(" ", "  ", 1), value.replace(" ", "\t", 1),
                 "codex-agents-owner " + value, value + "\n" + value, "PRIVATE " + value,
                 key_type + " " + changed, key_type + " !!!", value + "=" * 3,
                 "ssh-ed25519 " + "A" * OWNER_SIGNERS_MAX_BYTES, "ssh-ed25519 \ud800")
        for candidate in cases:
            with self.subTest(candidate=type(candidate).__name__), self.assertRaisesRegex(ValueError, "invalid owner public key"):
                checked_public_key(candidate)

    def test_malformed_key_fields_and_trailing_data_are_refused(self):
        values = (
            public_value("ssh-ed25519", b"short"),
            public_value("ssh-ed25519", bytes(32), b"extra"),
            public_value("ssh-rsa", b"\x80", b"\x01" + bytes(127)),
            public_value("ssh-rsa", b"\x01\x00\x01", b"\x01" + bytes(126)),
            public_value("ssh-rsa", b"\x01\x00\x01", b"\x00\x01" + bytes(127)),
            public_value("ecdsa-sha2-nistp256", b"wrong", b"\x04" + bytes(64)),
            public_value("sk-ssh-ed25519@openssh.com", bytes(32), b""),
            public_value("sk-ecdsa-sha2-nistp256@openssh.com", b"nistp256", b"\x04" + bytes(64), b""),
        )
        for value in values:
            with self.subTest(value=value.split()[0]), self.assertRaises(ValueError):
                checked_public_key(value)
        value = public_value("ssh-ed25519", bytes(32))
        kind, encoded = value.split()
        raw = base64.b64decode(encoded)
        for malformed in (raw[:-1], raw + b"\x00", raw + b"\x00\x00\x00\x00", b"\xff\xff\xff\xffx"):
            with self.assertRaises(ValueError):
                checked_public_key(kind + " " + base64.b64encode(malformed).decode("ascii"))

    def test_maximal_current_signer_line_is_supported_and_larger_is_refused(self):
        value = public_value("ssh-rsa", b"\x01\x00\x01", b"\x01" + b"\x55" * 3028)
        line = ("codex-agents-owner " + value + "\n").encode("ascii")
        self.assertEqual(OWNER_SIGNERS_MAX_BYTES, len(line))
        self.assertEqual(value, owner_public_key_from_line(line)[0])
        larger = public_value("ssh-rsa", b"\x01\x00\x01", b"\x01" + b"\x55" * 3029)
        with self.assertRaises(ValueError):
            checked_public_key(larger)
        for malformed in (line[:-1], line + b"\n", line.replace(b"codex-agents-owner", b"other-owner"), line.decode("ascii")):
            with self.assertRaises(ValueError):
                owner_public_key_from_line(malformed)

    def test_legacy_rsa_identity_remains_stable_while_new_read_rejects_padding(self):
        canonical = public_value("ssh-rsa", b"\x01\x00\x01", b"\x01" + b"\x55" * 127)
        padded = public_value("ssh-rsa", b"\x00\x01\x00\x01", b"\x00\x01" + b"\x55" * 127)
        canonical_line = ("codex-agents-owner " + canonical + "\n").encode("ascii")
        padded_line = ("codex-agents-owner " + padded + "\n").encode("ascii")
        self.assertEqual(_owner_key_identity(canonical_line), _owner_key_identity(padded_line))
        with self.assertRaises(ValueError):
            owner_public_key_from_line(padded_line)
