"""RSF-S3 (split of test_rsf_s3_resumable): part account."""
import json  # noqa: F401
import shutil  # noqa: F401
import unittest  # noqa: F401
from unittest import mock  # noqa: F401

import tests.support  # noqa: F401
from test_framework import requirements
from tests.rsf_s3_support import *  # noqa: F401,F403
from tests.rsf_s3_support import ResumableSupport


class ResumableAccountTests(ResumableSupport):
    __why_not_scenario__ = 'Isolated persistence owners and exact subprocess deaths; no installed services.'

    @requirements('RSF-S3-1', 'RSF-S3-2', 'RSF-S3-3', 'RSF-S3-4', 'RSF-S3-5')
    def test_account_killed_before_and_after_each_class_commit_reopens_twice_identically(self):
        self.interrupted_equivalence('account', ACCOUNT_CLASSES)

    @requirements('RSF-S3-1', 'RSF-S3-3', 'RSF-S3-4', 'RSF-S3-5')
    def test_account_last_short_done_and_publication_commits_reopen_twice_identically(self):
        self.final_boundary_equivalence('account', ACCOUNT_CLASSES)

    @requirements('RSF-S3-2', 'RSF-S3-4')
    def test_real_successor_after_sigkill_increments_epoch_and_stale_batch_cannot_commit(self):
        for kind, target in (('account', ACCOUNT_CLASSES[0]), ('project', PROJECT_CLASSES[0])):
            with self.subTest(kind=kind):
                source = self.account_fixture() if kind == 'account' else self.project_fixture()
                uninterrupted = self.clone(source, kind + '-takeover-uninterrupted')
                self.reopen(uninterrupted, kind)
                expected = self.snapshot(uninterrupted, kind)
                first = self.kill_at(source, kind, 'after', target, 64)
                stale = self.start_worker(source, kind, 'retain', target, 128)
                try:
                    second = self.read_worker(stale)
                    self.assertEqual('retain', second.get('boundary'), second)
                    self.assertNotEqual(first['pid'], second['pid'])
                    self.assertNotEqual(os.getpid(), second['pid'])
                    self.assertEqual(first['epoch'] + 1, second['epoch'])
                    stale.stdin.write('release\n')
                    stale.stdin.flush()
                    self.assertEqual({'released': True}, self.read_worker(stale))
                    third = self.kill_at(source, kind, 'after', target, COUNT)
                    self.assertNotEqual(second['pid'], third['pid'])
                    self.assertEqual(second['epoch'] + 1, third['epoch'])
                    before = self.snapshot(source, kind)
                    stale.stdin.write('commit\n')
                    stale.stdin.flush()
                    refused = self.read_worker(stale)
                    self.assertIn('Run: ./setup.sh --update', refused.get('refused', ''), refused)
                    self.assertTrue(refused['epoch_refused'])
                    self.assertEqual([1, 1], refused['staged'])
                    self.assertFalse(refused['in_transaction'])
                    stale.wait(timeout=5)
                    self.assertEqual(0, stale.returncode, stale.stderr.read())
                    self.assertEqual(before, self.snapshot(source, kind))
                finally:
                    if stale.poll() is None:
                        stale.kill()
                    stale.communicate(timeout=5)
                self.reopen(source, kind)
                self.assertEqual(expected, self.snapshot(source, kind))
                self.reopen(source, kind)
                self.assertEqual(expected, self.snapshot(source, kind))
