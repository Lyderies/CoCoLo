"""RSF-S3 (split of test_rsf_s3_resumable): part project_kill."""
import json  # noqa: F401
import shutil  # noqa: F401
import unittest  # noqa: F401
from unittest import mock  # noqa: F401

import tests.support  # noqa: F401
from test_framework import requirements
from tests.rsf_s3_support import *  # noqa: F401,F403
from tests.rsf_s3_support import ResumableSupport


class ResumableProjectKillTests(ResumableSupport):
    __why_not_scenario__ = 'Isolated persistence owners and exact subprocess deaths; no installed services.'

    @requirements('RSF-S3-1', 'RSF-S3-2', 'RSF-S3-3', 'RSF-S3-4', 'RSF-S3-5')
    def test_project_killed_before_and_after_each_class_commit_reopens_twice_identically(self):
        self.interrupted_equivalence('project', PROJECT_CLASSES)
