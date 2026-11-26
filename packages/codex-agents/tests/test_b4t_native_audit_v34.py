"""T-43 (b) for the real 0.8.32 (v0.3.4) service: it never execs its own process during a claim.

Split from tests.test_b4t_native (S5 final fix): one archived World per module keeps each within the 60 s
scenario budget. COCOLO_TEST_B4T_SKIP_SOCKETS=1 is the builder's explicit no-socket gate only.
"""
import os
import unittest

import tests.support  # noqa: F401
from tests import test_b4t_native as native
from test_framework import requirements


@unittest.skipIf(os.environ.get('COCOLO_TEST_B4T_SKIP_SOCKETS') == '1',
                 'Primary must rerun: archived service audit requires sockets and PTY setup')
class ArchivedServiceAuditTests(native.ArchivedServiceAudit, unittest.TestCase):
    @requirements('RS-B-B4t-3','RS-B-B4t-9',why_not_scenario='Real 0.8.32 service through setup, claim, signed child launch and stop, with a per-pid audit hook.')
    def test_B4t_A1_real_V34_service_does_not_exec_during_a_claim(self):
        self.audit_revision('v0.3.4')
