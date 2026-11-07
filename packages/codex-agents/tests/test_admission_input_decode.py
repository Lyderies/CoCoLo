from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import attest_provider, generate_test_owner_key, provider_fixture
from codex_agents.account_service import AccountOwnerViews
from codex_agents.admission_input_decode import AdmissionDecodeError, decode_admission_inputs
from codex_agents.admission_inputs import AdmissionCaptureLimits, AdmissionInputPolicy, admission_input_size
from codex_agents.learning_contracts import canonical_json, content_digest
from codex_agents.learning_store import LearningLedger, LearningState, authorize_collector_runtime
from codex_agents.providers import parse_codex_rate_limits
from codex_agents.sql_read_budget import SQLReadLimits

NOW = 10_000_000_000


class AdmissionInputDecodeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        state = LearningState(self.base / "state")
        authorize_collector_runtime(state, ORCHESTRATION_ROOT / "cli.py")
        key, public = generate_test_owner_key(self.base)
        state.install_owner_public_key(public)
        self.ledger = LearningLedger(state)
        self.addCleanup(self.ledger.close)
        projects = []
        for name in ("a", "b"):
            root = self.base / name
            root.mkdir()
            projects.append(self.ledger.register_project(root, now_ms=1))
        self.projects = projects
        attest_provider(self.ledger, projects[0], key, "codex", "a" * 64)
        records = parse_codex_rate_limits(
            json.loads(provider_fixture("codex-rate-limits.json")), digest_key=state.digest_key(),
            observed_at_ms=NOW, client=("/usr/bin/fixture-codex", "f" * 64), client_version="0.164.0",
        )
        for project in projects:
            self.ledger._store_provider_observations(project, records, timestamp=NOW)
        decision = {"provider": "claude", "decision": "pass", "reason_code": "admitted",
                    "admission_basis": "bootstrap", "account_fingerprint": "b" * 64,
                    "evidence_digests": [content_digest(records[0])], "observed_at_ms": NOW + 1}
        self.ledger._store_admission_locked(projects[1], decision, run_id="fixture-run", timestamp=NOW + 1)
        self.ledger._store_probe_admission_locked(projects[1], decision, timestamp=NOW + 1)
        self.limits = AdmissionCaptureLimits(64, 1_048_576)
        with mock.patch.object(self.ledger, "_clock_ms", return_value=NOW):
            self.snapshot = AccountOwnerViews(self.ledger).capture_admission_inputs(
                projects[0], "codex", AdmissionInputPolicy("0.164.0", "0.164.1", 60),
                self.limits, SQLReadLimits(10_000, 1000, 64 * 1_048_576),
            )

    def snapshot_with(self, **changes):
        value = replace(self.snapshot, **changes)
        return replace(value, retained_bytes=admission_input_size(
            value.project_id, value.provider, value.runtime_digest, value.policy, value.owner, value.signer, value.rows,
        ))

    def bad(self, snapshot, reason="admission_decode_invalid_input", limits=None):
        with self.assertRaises(AdmissionDecodeError) as caught:
            decode_admission_inputs(snapshot, self.limits if limits is None else limits)
        error = caught.exception
        self.assertEqual(reason, error.reason_code)
        self.assertEqual(reason, str(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_real_owner_capture_to_detached_decode_preserves_every_candidate_and_order(self):
        with mock.patch("sqlite3.connect", side_effect=AssertionError("SQLite in decoder")), \
                mock.patch.object(self.ledger._connection, "execute", side_effect=AssertionError("owner SQL in decoder")):
            parsed = decode_admission_inputs(self.snapshot, self.limits)
        self.assertEqual(self.snapshot.rows, tuple(row.raw for row in parsed))
        self.assertEqual({"provider_identity", "billing_attestation", "provider_billing", "provider_headroom",
                          "admission_decision", "probe_admission"}, {row.record["record_type"] for row in parsed})
        self.assertTrue(any(row.raw[1] == self.projects[1] for row in parsed))
        self.assertTrue(any(row.raw[10] for row in parsed))
        parsed[0].record["record_id"] = "changed-detached-copy"
        self.assertEqual(self.snapshot.rows[0][2], decode_admission_inputs(self.snapshot, self.limits)[0].record["record_id"])
        self.assertNotIn("changed-detached-copy", repr(parsed))

    def test_all_row_quotas_and_shapes_precede_any_json_decode(self):
        with mock.patch("codex_agents.admission_input_decode.parse_bounded_json", side_effect=AssertionError("decoded before preflight")):
            self.bad(self.snapshot, "admission_decode_limit", AdmissionCaptureLimits(1, self.limits.max_bytes))
            self.bad(self.snapshot, "admission_decode_limit", AdmissionCaptureLimits(64, self.snapshot.retained_bytes - 1))
            self.bad(replace(self.snapshot, rows=(*self.snapshot.rows[:-1], ("CANARY",))))
            self.bad(replace(self.snapshot, retained_bytes=self.snapshot.retained_bytes - 1))
        exact = AdmissionCaptureLimits(len(self.snapshot.rows), self.snapshot.retained_bytes)
        self.assertEqual(len(self.snapshot.rows), len(decode_admission_inputs(self.snapshot, exact)))

    def test_unknown_limits_and_header_types_fail_closed(self):
        self.bad(self.snapshot, "admission_decode_invalid_limits", object())
        limits = AdmissionCaptureLimits(1, 1)
        object.__setattr__(limits, "max_rows", True)
        self.bad(self.snapshot, "admission_decode_invalid_limits", limits)
        for changes in ({"provider": []}, {"captured_at_ms": True}, {"origin": object(), "runtime_digest": "wrong"},
                        {"signer": b"CANARY"}, {"owner": (True, b"CANARY", "enrolled", 1)}, {"rows": list(self.snapshot.rows)}):
            with self.subTest(fields=tuple(changes)):
                self.bad(replace(self.snapshot, **changes))

    def test_origin_and_signatures_never_become_verified_authority(self):
        parsed = decode_admission_inputs(replace(self.snapshot, origin=None), self.limits)
        self.assertEqual(len(self.snapshot.rows), len(parsed))
        signed = next(index for index, row in enumerate(self.snapshot.rows) if row[10])
        rows = list(self.snapshot.rows)
        row = list(rows[signed])
        row[10] = b"unverified-synthetic-signature"
        rows[signed] = tuple(row)
        result = decode_admission_inputs(self.snapshot_with(rows=tuple(rows)), self.limits)
        self.assertEqual(b"unverified-synthetic-signature", result[signed].raw[10])
        self.assertFalse(hasattr(result[signed], "verified"))

    def test_digest_identifier_type_and_observation_binding_reject_changed_rows(self):
        for field, value in ((2, "changed-record"), (4, "provider_billing"), (6, "0" * 64), (8, NOW + 5),
                             (0, True), (3, False), (9, -1), (7, "CANARY" * 40), (10, bytearray(b"signature"))):
            rows = list(self.snapshot.rows)
            row = list(rows[0])
            row[field] = value
            rows[0] = tuple(row)
            with self.subTest(field=field):
                # Malformed non-bytes signatures are handled without helper size accounting.
                if field in {0, 3, 9, 10}:
                    self.bad(replace(self.snapshot, rows=tuple(rows)))
                else:
                    self.bad(self.snapshot_with(rows=tuple(rows)))

    def test_strict_json_and_closed_records_reject_without_raw_error_context(self):
        baseline = self.snapshot.rows[0]
        original = json.loads(baseline[5])
        variants = [b'{"record_id":"CANARY","record_id":"duplicate"}', b'\xffCANARY',
                    b'{"x":NaN}', b'{"x":' + b'[' * 17 + b'0' + b']' * 17 + b'}',
                    canonical_json({**original, "CANARY": "unknown-field"}),
                    json.dumps(original, indent=2).encode()]
        for content in variants:
            row = list(baseline)
            row[5], row[6] = content, hashlib.sha256(content).hexdigest()
            rows = (tuple(row), *self.snapshot.rows[1:])
            with self.subTest(length=len(content)):
                self.bad(self.snapshot_with(rows=rows))

    def test_row_order_duplicate_and_scope_corruption_refuse_without_filtering(self):
        self.bad(self.snapshot_with(rows=tuple(reversed(self.snapshot.rows))))
        rows = list(self.snapshot.rows)
        row = list(rows[1])
        row[1], row[2] = rows[0][1], rows[0][2]
        rows[1] = tuple(row)
        self.bad(self.snapshot_with(rows=tuple(rows)))
        for kind, scope, source in (("provider_identity", self.projects[1], None),
                                    ("provider_headroom", None, "host")):
            rows = list(self.snapshot.rows)
            index = next(i for i, item in enumerate(rows) if item[4] == kind)
            row = list(rows[index])
            if scope is not None:
                row[1] = scope
            if source is not None:
                row[7] = source
            rows[index] = tuple(row)
            self.bad(self.snapshot_with(rows=tuple(rows)))


if __name__ == "__main__":
    unittest.main()
