"""ML real kills campaign; unchanged checks partitioned for TF-8."""
import unittest
from tests import test_merge_library_adversarial as source


class Campaign(unittest.TestCase):
    setUp = source.Adversarial.setUp


for _name in unittest.defaultTestLoader.getTestCaseNames(source.Adversarial):
    if source.campaign_area(_name) == 'kills':
        setattr(Campaign, _name, getattr(source.Adversarial, _name))
