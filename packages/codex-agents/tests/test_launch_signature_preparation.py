from __future__ import annotations

import json
from pathlib import Path
import threading
import unittest
from unittest import mock

import tests.test_providers as provider_fixture

from codex_agents.learning_contracts import canonical_json
from codex_agents.learning_engine import OwnerGrantAuthority
from codex_agents.learning_store import LedgerError
from codex_agents.providers import BILLING_MAX_AGE_MS


class LaunchSignaturePreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = provider_fixture.AdmissionTests("test_attested_disabled_credits_are_admitted_and_rechecked")
        self.fixture.setUp()
        self.addCleanup(lambda: self.assertTrue(self.fixture.doCleanups()))
        self.addCleanup(self.fixture.tearDown)
        self.ledger = self.fixture.ledger
        self.project = self.fixture.project_id
        identity = self.fixture.observe()[0]
        self.fixture.attest("codex", identity["account_fingerprint"])
        self.run = self.fixture.open_run()
        self.identity = self.fixture.observe()[0]
        clock = mock.patch.object(self.ledger, "_clock_ms", side_effect=lambda: self.fixture.clock + 1)
        clock.start()
        self.addCleanup(clock.stop)

    def launch(self):
        return self.ledger.launch_recheck(self.project, run_handle=self.run["run_handle"], identity=self.identity)

    def assert_no_claim(self):
        with self.ledger.repository(self.project) as repository:
            self.assertEqual([], repository.records("dispatch_intent"))
            self.assertEqual([], repository.records("dispatch_result"))

    def test_another_project_can_commit_while_launch_signature_verification_waits(self):
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        results, errors, registrations = [], [], []
        verify = OwnerGrantAuthority.verify_owner_record
        root = Path(self.fixture.temporary.name) / "project-during-launch-verification"
        root.mkdir()

        def blocked(authority, project, record, signature):
            self.assertFalse(self.ledger._connection.in_transaction)
            entered.set()
            if not release.wait(5):
                raise AssertionError("fixture verification was not released")
            return verify(authority, project, record, signature)

        def launch():
            try:
                results.append(self.launch())
            except BaseException as error:
                errors.append(error)

        def register():
            try:
                registrations.append(self.ledger.register_project(root, now_ms=provider_fixture.NOW))
            except BaseException as error:
                errors.append(error)
            finally:
                done.set()

        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=blocked):
            first, second = threading.Thread(target=launch), threading.Thread(target=register)
            first.start()
            try:
                self.assertTrue(entered.wait(2))
                second.start()
                self.assertTrue(done.wait(2), "another project waited on launch signature verification")
            finally:
                release.set()
                first.join(5)
                if second.ident is not None:
                    second.join(5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual([None], results)
        self.assertEqual([self.ledger.project_for_root(root)], registrations)
        self.assert_no_claim()

    def test_same_signer_new_owner_generation_during_verification_refuses_launch(self):
        verify = OwnerGrantAuthority.verify_owner_record

        def changed(authority, project, record, signature):
            result = verify(authority, project, record, signature)
            with self.ledger._account_transaction():
                self.ledger._connection.execute(
                    "INSERT INTO owner_key_history(signer_line,reason,recorded_at_ms) VALUES(?,'replaced',?)",
                    (self.fixture.state.allowed_signers_path.read_bytes(), provider_fixture.NOW),
                )
            return result

        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=changed), \
                self.assertRaisesRegex(LedgerError, "preparation is stale"):
            self.launch()
        self.assert_no_claim()

    def test_changed_attestation_inputs_during_verification_refuse_launch(self):
        verify = OwnerGrantAuthority.verify_owner_record
        identifier, sequence, content, digest, source, signature = self.ledger._attestation_rows_locked(self.project, "codex")[0]
        changed = json.loads(content)
        changed["observed_at_ms"] += 1
        changed["issued_at"] += 1
        changed_content = canonical_json(changed)
        variants = {"signature": signature + b"changed", "source": "provider", "content": changed_content,
                    "sequence": sequence + 1000, "digest": "0" * 64, "signer": b""}

        for field, value in variants.items():
            def changed_after_verifying(authority, project, record, signed):
                result = verify(authority, project, record, signed)
                with self.ledger._account_transaction():
                    if field == "signature":
                        self.ledger._connection.execute("UPDATE owner_attestations SET signature=? WHERE project_id=? AND subject_record_id=?",
                                                        (value, self.project, identifier))
                    elif field == "signer":
                        self.fixture.state.allowed_signers_path.write_bytes(value)
                    elif field == "content":
                        from hashlib import sha256
                        self.ledger._connection.execute("UPDATE records SET content=?,content_digest=? WHERE project_id=? AND record_id=?",
                                                        (value, sha256(value).hexdigest(), self.project, identifier))
                    else:
                        column = {"source": "source", "sequence": "source_sequence", "digest": "content_digest"}[field]
                        self.ledger._connection.execute("UPDATE records SET " + column + "=? WHERE project_id=? AND record_id=?",
                                                        (value, self.project, identifier))
                return result

            with self.subTest(field=field):
                try:
                    with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=changed_after_verifying), \
                            self.assertRaisesRegex(LedgerError, "preparation is stale|attestation is incompatible"):
                        self.launch()
                finally:
                    self.fixture.state.allowed_signers_path.write_bytes(self.ledger._owner_generation_locked()[1])
                    with self.ledger._account_transaction():
                        self.ledger._connection.execute("UPDATE records SET source_sequence=?,content=?,content_digest=?,source=? WHERE project_id=? AND record_id=?",
                                                        (sequence, content, digest, source, self.project, identifier))
                        self.ledger._connection.execute("UPDATE owner_attestations SET signature=? WHERE project_id=? AND subject_record_id=?",
                                                        (signature, self.project, identifier))
                self.assert_no_claim()

    def restrict_during_verification(self, changes, expected):
        verify = OwnerGrantAuthority.verify_owner_record
        root = Path(self.fixture.temporary.name) / "restricting-project"
        root.mkdir()
        other = self.ledger.register_project(root, now_ms=provider_fixture.NOW)
        original_project = self.fixture.project_id

        def changed(authority, project, record, signature):
            result = verify(authority, project, record, signature)
            try:
                self.fixture.project_id = other
                self.fixture.observe(**changes)
            finally:
                self.fixture.project_id = original_project
            return result

        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=changed):
            self.assertEqual(expected, self.launch())
        with self.ledger.repository(self.project) as repository:
            self.assertEqual(expected, repository.records("admission_decision")[-1]["reason_code"])
        self.assert_no_claim()

    def test_other_project_spend_control_during_verification_refuses_launch(self):
        self.restrict_during_verification({"rateLimits.spendControlReached": True}, "codex_spend_control_reached")

    def test_other_project_paid_usage_during_verification_refuses_launch(self):
        self.restrict_during_verification({"rateLimits.credits.unlimited": True}, "paid_usage_usable")

    def test_other_project_exhaustion_during_verification_refuses_launch(self):
        self.restrict_during_verification({"rateLimits.primary.usedPercent": 95}, "provider_exhausted")

    def test_billing_freshness_uses_clock_after_signature_verification(self):
        verify = OwnerGrantAuthority.verify_owner_record

        def delayed(authority, project, record, signature):
            result = verify(authority, project, record, signature)
            self.fixture.clock += BILLING_MAX_AGE_MS + 1
            return result

        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=delayed):
            self.assertEqual("billing_unverified", self.launch())
        self.assert_no_claim()

    def test_actual_run_consumed_during_verification_refuses_before_a_launch_decision(self):
        verify = OwnerGrantAuthority.verify_owner_record

        def consumed(authority, project, record, signature):
            result = verify(authority, project, record, signature)
            with self.ledger._account_transaction():
                self.ledger._connection.execute(
                    "UPDATE host_run_bindings SET consumed_at_ms=? WHERE project_id=?",
                    (self.fixture.clock + 1, self.project),
                )
            return result

        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=consumed), \
                self.assertRaisesRegex(LedgerError, "no longer open"):
            self.launch()
        self.assert_no_claim()

    def test_exactly_one_verification_and_no_locked_crypto_fallback(self):
        verify = OwnerGrantAuthority.verify_owner_record
        for valid in (True, False):
            calls = []

            def checked(authority, project, record, signature):
                calls.append(record["record_id"])
                self.assertFalse(self.ledger._connection.in_transaction)
                return verify(authority, project, record, signature) if valid else False

            with self.subTest(valid=valid), mock.patch.object(
                OwnerGrantAuthority, "verify_owner_record", autospec=True, side_effect=checked,
            ), mock.patch.object(self.ledger, "_attestation_signed_by_owner_locked", side_effect=AssertionError("locked crypto fallback")):
                self.assertEqual(None if valid else "billing_attestation_missing", self.launch())
            self.assertEqual(1, len(calls))
        self.assert_no_claim()

    def test_existing_write_transaction_and_invalid_run_refuse_before_crypto(self):
        with self.ledger._account_transaction(), \
                mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("crypto in outer transaction")), \
                self.assertRaisesRegex(LedgerError, "write transaction is already active"):
            self.launch()
        with mock.patch.object(OwnerGrantAuthority, "verify_owner_record", side_effect=AssertionError("crypto for unknown run")), \
                self.assertRaisesRegex(LedgerError, "unavailable"):
            self.ledger.launch_recheck(self.project, run_handle="hrn_" + "0" * 48, identity=self.identity)
        self.assert_no_claim()


if __name__ == "__main__":
    unittest.main()
