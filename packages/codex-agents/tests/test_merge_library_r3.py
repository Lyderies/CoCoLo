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
