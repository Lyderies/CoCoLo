"""A1.7 item 6 accepted residual (iii): a same-user stub that ignores SIGTERM. Run outside the builder sandbox.

Split from tests.test_b4t_native (S5 final fix): the real 30 s TERM bound this case witnesses fills half of the
60 s scenario budget by itself. COCOLO_TEST_B4T_SKIP_SOCKETS=1 is the builder's explicit no-socket gate only.
"""
import os
import unittest

import tests.support  # noqa: F401
from tests import test_b4t_native as native
from tests.test_project_start import StarterBase
from test_framework import requirements


@unittest.skipIf(os.environ.get('COCOLO_TEST_B4T_SKIP_SOCKETS') == '1',
                 'Primary must rerun: accepted same-user residual uses real Unix endpoints')
class AcceptedResidualKillTests(native.ResidualStub, StarterBase):
    setUp = native.NativeArchiveTests.setUp
    relaunch = native.NativeArchiveTests.relaunch

    @requirements('RS-B-B4t-3','RS-B-B4t-9',why_not_scenario='Real stub ignores TERM and is ended through the same native pin after the stop bound, then the real owner adopts.')
    def test_same_user_impostor_ignoring_TERM_is_KILLed_after_bound(self):
        self.run_stub(ignore_term=True)
