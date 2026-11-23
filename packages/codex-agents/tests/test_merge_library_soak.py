"""TF-18: explicit complete campaigns, selected with --profile soak only.

Run only this module for the complete real/model boundary inventory. The normal
crash modules retain their sampled runs and their own 60-second budgets.
"""
import unittest

from tests import test_merge_library_adversarial as _real
from tests import test_merge_library_model_sweep as _model


class CompleteRealCampaign(unittest.TestCase):
    __requirements__ = ('ML-11', 'ML-12', 'ML-18', 'TF-18', 'TF-19')
    __why_not_scenario__ = 'Complete syscall-boundary enumeration is an explicitly selected soak campaign.'
    setUp = _real.Adversarial.setUp


for _name in unittest.defaultTestLoader.getTestCaseNames(_real.Adversarial):
    if any(part in _name for part in ('kill_', 'fault_', 'edit_')):
        setattr(CompleteRealCampaign, _name, getattr(_real.Adversarial, _name))


class CompleteModelCampaign(_model.PersistenceSamples):
    __requirements__ = ('ML-11', 'ML-12', 'ML-19', 'TF-18', 'TF-19')
    __why_not_scenario__ = 'All discovered model fault/power-loss boundaries run only in the explicit soak layer.'
