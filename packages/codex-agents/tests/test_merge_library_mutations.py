"""TF-19: four production mutations must turn named requirement checks red."""
import errno
import inspect
import textwrap
import unittest
from unittest import mock

from codex_agents import merge_library as ml
from tests.merge_library_support import Fixture, check_end_state
from tests.merge_library_disk import MemoryDisk


class MutationProofs(unittest.TestCase):
    __requirements__ = ('ML-6', 'ML-12', 'ML-19', 'ML-30', 'TF-19')
    __why_not_scenario__ = 'Production mutations prove the authority, durability, two-commit and fault oracles reject the precise forbidden behavior.'

    def run_witness(self, name):
        from tests.test_merge_library_contract import Contract
        result = unittest.TestResult()
        Contract(name).run(result)
        self.assertFalse(result.errors, result.errors)
        return result

    def test_private_location_guard_mutation_is_caught_by_ML30_forgery(self):
        witness = 'test_ml_30_forgery_private_location'
        self.assertTrue(self.run_witness(witness).wasSuccessful())
        source = textwrap.dedent(inspect.getsource(ml._Store.__init__))
        original = 'if self.private == self.project or self.project in self.private.parents:'
        self.assertIn(original, source)
        namespace = {}
        exec(compile(source.replace(original, 'if False:'), ml.__file__, 'exec'), ml.__dict__, namespace)
        with mock.patch.object(ml._Store, '__init__', namespace['__init__']):
            self.assertTrue(self.run_witness(witness).failures, 'missing guard mutation survived')

    def test_third_history_commit_is_caught_by_ML6_order(self):
        witness = 'test_ml_6_normal_two_commit_order'
        self.assertTrue(self.run_witness(witness).wasSuccessful())
        original = ml._terminal
        def third(store, row, state, label):
            answer = original(store, row, state, label)
            if label == 'B' and answer:
                store.save([row], label='third')
            return answer
        with mock.patch.object(ml, '_terminal', third):
            self.assertTrue(self.run_witness(witness).failures, 'third transaction mutation survived')

    def test_missing_A_flush_is_caught_by_post_crash_staging_contents(self):
        from tests.test_merge_library_disk import DurableDiskTests
        result = unittest.TestResult()
        DurableDiskTests('test_skipping_flush_before_A_is_caught_after_model_power_loss').run(result)
        self.assertEqual(1, result.testsRun)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)

    def test_swallowed_EIO_landed_is_caught_by_the_single_end_state_check(self):
        def run():
            f = Fixture()
            self.addCleanup(f.close)
            batch = f.batch({'a.txt': b'child'})
            disk = f.disk = MemoryDisk(f.root)
            fired = []
            def fault(call):
                if not fired and call.operation == 'exchange' and call.phase == 'forward' and call.when == 'before':
                    fired.append(call)
                    raise OSError(errno.EIO, 'injected before publication')
                return disk(call)
            report = f.land(batch, hook=fault)
            self.assertEqual(1, len(fired))
            return f, batch, report, fired[0]
        f, batch, report, call = run()
        self.assertEqual('reverted', report.state)
        check_end_state(self, f, batch, report, call=call, fault=errno.EIO)
        original = ml._Store.io
        def swallow(store, op, function, *args, **kwargs):
            try:
                return original(store, op, function, *args, **kwargs)
            except OSError as error:
                if error.errno != errno.EIO or op != 'exchange' or store.phase != 'forward':
                    raise
                return original(store, op, function, *args, **kwargs)
        with mock.patch.object(ml._Store, 'io', swallow):
            f, batch, report, call = run()
            self.assertEqual('landed', report.state, 'mutation did not reach its forbidden outcome')
            with self.assertRaisesRegex(AssertionError, 'ordinary I/O failure cannot end landed'):
                check_end_state(self, f, batch, report, call=call, fault=errno.EIO)
