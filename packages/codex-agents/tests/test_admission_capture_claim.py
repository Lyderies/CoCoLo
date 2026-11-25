"""S14a anchored real claim parity; primary runs outside the sandbox."""
from tests import test_admission_capture_bytes as capture
from test_framework import requirements
import unittest

DAY_MS = capture.DAY_MS

class AdmissionCaptureClaimTests(unittest.TestCase):
    setUp = capture.AdmissionCaptureBytesTests.setUp
    charged = capture.AdmissionCaptureBytesTests.charged
    add = capture.AdmissionCaptureBytesTests.add
    fill_to_the_limit = capture.AdmissionCaptureBytesTests.fill_to_the_limit
    late_claim = capture.AdmissionCaptureBytesTests.late_claim
    outcome = capture.AdmissionCaptureBytesTests.outcome
    anchor = capture.AdmissionCaptureBytesTests.anchor
    copies = capture.AdmissionCaptureBytesTests.copies
    observed = capture.AdmissionCaptureBytesTests.observed
    oracle = capture.AdmissionCaptureBytesTests.oracle
    tool = capture.AdmissionCaptureBytesTests.tool
    accepts = capture.AdmissionCaptureBytesTests.accepts
    assert_parity = capture.AdmissionCaptureBytesTests.assert_parity

    @requirements("S14A-3", why_not_scenario='Synthetic ledger and in-process account job; the sizing tool reads a copy of that same ledger, no service or socket.')
    def test_U2_4_a_claim_anchored_at_its_open_is_charged_alike(self):
        seen = {}

        def after_open(opened):
            self.copies("provider_billing", 1, age_ms=DAY_MS - 60_000)
            self.copies("provider_headroom", 1, age_ms=DAY_MS + 60_000)
            anchor = self.anchor(opened)
            seen["parity"] = self.assert_parity(anchor, at_ms=anchor)[:2]
        self.assertEqual("claimed", self.late_claim(60_000, after_open)["state"])
        self.assertIn("parity", seen)


