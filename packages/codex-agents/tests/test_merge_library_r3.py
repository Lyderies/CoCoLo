"""Decision 102: round-three witnesses with independent controls."""
import json
import subprocess
import unittest
from unittest import mock
from codex_agents import merge_library as ml
from tests.merge_library_support import Fixture, Trace, tree
from test_framework import requirements


class RoundThree(unittest.TestCase):
    __why_not_scenario__ = 'Exact stored-row corruption and failed admission commands need isolated boundary controls.'

    def fixture(self):
        f = Fixture(); self.addCleanup(f.close); return f

    @requirements('ML-1', 'ML-5')
    def test_p07_V1_stored_row_is_verified_before_commit_A(self):
        good = self.fixture(); trace = Trace()
        self.assertEqual('landed', good.land(good.batch({'a.txt': b'child'}), hook=trace).state)
        self.assertTrue(any(c.operation == 'sqlite_commit' and c.detail.get('commit') == 'A' for c in trace.calls))
        original = ml._Store.io
        for part in ('events', 'old_contents', 'new_contents', 'batch_digest'):
            with self.subTest(part=part):
                f = self.fixture(); batch = f.batch({'a.txt': b'child'}); before = tree(f.project); fired=[]; trace=Trace()
                def corrupt(store, operation, function, *args, **kwargs):
                    result = original(store, operation, function, *args, **kwargs)
                    if operation == 'sqlite_write' and store.phase == 'history' and not fired:
                        raw = store.db.execute('SELECT id,value FROM rows').fetchone()
                        if raw:
                            number, value = raw; row = json.loads(value)
                            if part == 'events': row['effective'][0][0] = 'different.txt'
                            elif part == 'old_contents': row['effective'][0][1] = None
                            elif part == 'new_contents': row['effective'][0][2] = None
                            else: row['batch'][6] = 'wrong-digest'
                            store.db.execute('UPDATE rows SET value=? WHERE id=?', (ml._json(row), number)); fired.append(part)
                    return result
                with mock.patch.object(ml._Store, 'io', corrupt): report=f.land(batch, hook=trace)
                self.assertEqual([part], fired)
                self.assertEqual(('refused','corrupt_history',False),(report.state,report.reason,report.recorded),'p07 V1 stored corruption was admitted')
                self.assertFalse(any(c.operation=='sqlite_commit' and c.detail.get('commit')=='A' for c in trace.calls))
                self.assertEqual(before,tree(f.project)); self.assertEqual((),ml.history(f.project,f.private))

    def failed_git(self, command):
        good=self.fixture(); self.assertEqual('landed',good.land(good.batch({'a.txt':b'child'})).state)
        f=self.fixture(); batch=f.batch({'a.txt':b'child'}); before=tree(f.project); trace=Trace(); fired=[]
        original=subprocess.run
        def run(argv,*args,**kwargs):
            if argv[0]=='git' and command in argv:
                fired.append(command); return subprocess.CompletedProcess(argv,128,b'',b'operational failure')
            return original(argv,*args,**kwargs)
        with mock.patch.object(ml.subprocess,'run',run): report=f.land(batch,hook=trace)
        self.assertEqual([command],fired)
        self.assertEqual(('refused','io',False),(report.state,report.reason,report.recorded),'p07 V2 failed '+command+' admitted')
        self.assertEqual(before,tree(f.project))
        self.assertFalse(any(c.operation=='sqlite_commit' and c.detail.get('commit')=='A' for c in trace.calls))

    @requirements('ML-12','ML-30')
    def test_p07_V2_failed_ls_files_is_not_empty_success(self): self.failed_git('ls-files')

    @requirements('ML-12','ML-30')
    def test_p07_V2_failed_check_attr_is_not_empty_success(self): self.failed_git('check-attr')

    @requirements('TF-19','ML-R91')
    def test_p08_V1_unreadable_folder_is_not_certified_empty(self):
        import os
        from tests import merge_library_support as support
        f=self.fixture();folder=f.project/'empty';folder.mkdir()
        support.check_empty_recorded_tree(self,f,'empty',{})
        original=os.scandir;fired=[]
        def denied(path):
            if str(path)==str(folder):fired.append(str(path));raise PermissionError('p08 denied listing')
            return original(path)
        with mock.patch.object(support.os,'scandir',denied):
            with self.assertRaisesRegex(PermissionError,'p08 denied listing'):
                support.check_empty_recorded_tree(self,f,'empty',{})
        self.assertEqual([str(folder)],fired)

    @requirements('TF-19','ML-12')
    def test_p08_V2_unresolved_row_needs_recovery_or_status_evidence(self):
        from tests import merge_library_support as support
        f=self.fixture();batch=f.batch();row=ml.HistoryRow(1,batch,'applying');report=ml.Report('needs_owner','io',1)
        with mock.patch.object(ml,'history',return_value=(row,)),mock.patch.object(support,'outcome_oracle') as oracle:
            for recovering,status in [((report,),()),((),(report,))]:
                with mock.patch.object(ml,'recover',return_value=recovering),mock.patch.object(ml,'status',return_value=status):
                    self.assertEqual((report,),support.recover_checked(self,f,batch,'forward'))
                    oracle.assert_called_once();oracle.reset_mock()
            with mock.patch.object(ml,'recover',return_value=()),mock.patch.object(ml,'status',return_value=()):
                with self.assertRaisesRegex(AssertionError,'unresolved row has no recovery or status evidence'):
                    support.recover_checked(self,f,batch,'forward')

    @requirements('ML-8','TF-19')
    def test_p09_final_reader_exception_fails_the_concurrency_witness(self):
        import os
        import threading
        from pathlib import Path
        from tests.test_merge_library_adversarial import Adversarial
        f=self.fixture();batch=f.batch({'a.txt':b'child'});original=Path.read_bytes
        for fail in (False,True):
            reads=[]
            def read(path):
                if path==f.project/'a.txt' and threading.current_thread()!=threading.main_thread():
                    reads.append(1)
                    if fail and len(reads)==2:raise PermissionError('p09 final reader boundary')
                return original(path)
            def land(batch,hook):
                hook(ml.IOCall('write','before','staging',detail={'data':b'ab','fd':123}))
                hook(ml.IOCall('checkpoint','after','cleanup'))
            case=Adversarial('test_ml_8_synchronized_reader_partial_write_and_boundaries');case.setUp=lambda:None;case.f=f
            result=unittest.TestResult()
            with mock.patch.object(f,'batch',return_value=batch),mock.patch.object(f,'land',land),mock.patch.object(Path,'read_bytes',read),mock.patch.object(os,'write',return_value=1):case.run(result)
            self.assertEqual([],result.errors);self.assertEqual(2,len(reads),'p09 did not reach exactly the final reader boundary')
            if fail:
                self.assertEqual(1,len(result.failures),'p09 old witness swallowed final reader exception')
                self.assertIn('p09 final reader boundary',result.failures[0][1])
            else:self.assertTrue(result.wasSuccessful(),result.failures)

    @requirements('ML-17','TF-13')
    def test_p10_blocked_sealers_leave_no_threads_in_the_test_process(self):
        import threading
        from tests import test_merge_library_contract as contract
        from tests.merge_library_support import child_run
        name='test_ml_17_concurrent_sealers_fifo_restart'
        control=unittest.TestResult();contract.Contract(name).run(control);self.assertTrue(control.wasSuccessful(),control.errors+control.failures)
        def probe():
            real_join=threading.Thread.join;stop=threading.Event()
            def blocked(*args,**kwargs):stop.wait(60)
            def short_join(thread,timeout=None):return real_join(thread,.02)
            result=unittest.TestResult()
            with mock.patch.object(ml,'enqueue',blocked),mock.patch.object(threading.Thread,'join',short_join):contract.Contract(name).run(result)
            self.assertEqual(1,len(result.failures),'p10 blocked enqueue was not reported')
            leaked=[t for t in threading.enumerate() if t is not threading.main_thread() and not t.daemon]
            self.assertEqual([],leaked,'p10 leaked non-daemon sealer threads')
        self.assertEqual(0,child_run(probe,timeout=3))
