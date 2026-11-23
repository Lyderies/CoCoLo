"""Decision 99 A: scoped r2 MED controls and named production-boundary evidence."""
import errno
import json
import os
import stat
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock
from codex_agents import merge_library as ml
from tests import merge_library_support as support


class RoundTwo(unittest.TestCase):
    __requirements__ = ('TF-18','TF-19','ML-12','ML-19','ML-20','ML-23')
    __why_not_scenario__ = 'Independent corrupt-state controls and exact injected boundaries are required to verify recovery and the oracle.'

    def fixture(self):
        f=support.Fixture(); self.addCleanup(f.close); return f

    def test_M1_fsync_and_fullfsync_each_need_a_sample(self):
        from generate_merge_samples import choose
        signature=('full_flush','history','before','project/a',None,None,None)
        points=[((signature,1),ml.IOCall('fsync','before','history')),((signature,2),ml.IOCall('fullfsync','before','history'))]
        self.assertEqual({'fsync','fullfsync'}, {row['operation'] for row in choose(points,'real')}, 'M1 generator merged flush signatures')
        for lane in ('real','model'):
            inventory={'samples':{lane:{'forward':[{'key':points[0][0]}]}}}
            with mock.patch.dict(os.environ,{'COCOLO_TEST_PROFILE':'ci'}), mock.patch.object(Path,'read_text',return_value=json.dumps(inventory)):
                with self.assertRaisesRegex(AssertionError,'new persistence boundary.*fullfsync'):
                    support.sample_boundaries(points,lane=lane)
            inventory['samples'][lane]['forward'].append({'key':points[1][0]})
            with mock.patch.dict(os.environ,{'COCOLO_TEST_PROFILE':'ci'}), mock.patch.object(Path,'read_text',return_value=json.dumps(inventory)):
                self.assertEqual(points,support.sample_boundaries(points,lane=lane))

    def test_M3_regular_file_cannot_be_an_emptied_folder(self):
        f=self.fixture(); batch=f.batch({'a.txt':b'child'}); support.put(f.project/'a.txt',b'child')
        info=(f.project/'z.txt').stat()
        row=ml.HistoryRow(1,batch,'landed',emptied_folders={'z.txt':(info.st_dev,info.st_ino)})
        report=ml.Report('landed',batch_no=1,recorded=True,paths=(ml.PathReport('a.txt','new'),))
        # Isolate the folder allowance from traversal errors: the independent
        # tree contains this regular file and the fake traversal returns no children.
        original=support.observed_tree
        def observed(fixture,*args,**kwargs):
            return {} if args and args[0]==f.project/'z.txt' else original(fixture,*args,**kwargs)
        with mock.patch.object(support,'observed_tree',observed):
            with self.assertRaisesRegex(AssertionError,'EL must be a directory'):
                support.check_end_state(self,f,batch,report,read_history=lambda:(row,))

    def test_M5_close_error_after_release_must_not_close_reused_descriptor(self):
        f=self.fixture(); store=ml._Store(f.project,f.private); self.addCleanup(store.close)
        fd=os.open(f.project,os.O_RDONLY); real_close=os.close; calls=[]; replacement=[]
        def release_then_fail(number):
            calls.append(number)
            real_close(number)
            reused=os.open(f.project,os.O_RDONLY)
            if reused != number:
                os.dup2(reused,number); real_close(reused)
            replacement.append(number)
            raise OSError(errno.EIO,'released fd before raising')
        try:
            with mock.patch.object(ml.os,'close',release_then_fail):
                with self.assertRaisesRegex(OSError,'released fd before raising'):
                    store.close_fd(fd,f.project)
            self.assertEqual([fd],calls,'M5 retried close after the syscall already released the descriptor')
            self.assertTrue(stat.S_ISDIR(os.fstat(fd).st_mode),'M5 reused descriptor was closed')
        finally:
            try: real_close(fd)
            except OSError: pass

    def unknown(self, report, fired, boundary):
        self.assertEqual([boundary],fired,'M6 wrong or missing read boundary')
        self.assertEqual(('unknown','io',False),(report.state,report.reason,report.recorded),boundary)

    def test_M6_rollback_initial_read_guard(self):
        f=self.fixture(); batch=f.batch(); ml._seed_history(f.project,f.private,(ml.HistoryRow(1,batch,'applying'),))
        fired=[]
        def unreadable(call):
            if call.operation=='sqlite_read' and call.when=='before':
                fired.append('rollback initial rows'); raise OSError(errno.EIO,'M6 rollback initial rows')
        report=ml.rollback(f.project,f.private,1,hook=unreadable)
        self.unknown(report,fired,'rollback initial rows')
        self.assertEqual(f.before,support.tree(f.project))

    def test_M6_undo_initial_read_guard(self):
        f=self.fixture(); landed=f.land(f.batch()); before=support.tree(f.project); fired=[]
        def unreadable(call):
            if call.operation=='sqlite_read' and call.when=='before':
                fired.append('undo initial rows'); raise OSError(errno.EIO,'M6 undo initial rows')
        report=ml.undo(f.project,f.private,landed.batch_no,hook=unreadable)
        self.unknown(report,fired,'undo initial rows'); self.assertEqual(before,support.tree(f.project))

    def test_M6_cleanup_initial_read_guard(self):
        f=self.fixture(); f.land(f.batch()); before=support.tree(f.project); fired=[]
        def unreadable(call):
            if call.operation=='sqlite_read' and call.when=='before':
                fired.append('cleanup initial rows'); raise OSError(errno.EIO,'M6 cleanup initial rows')
        reports=ml.cleanup(f.project,f.private,hook=unreadable)
        self.assertEqual(1,len(reports)); self.unknown(reports[0],fired,'cleanup initial rows'); self.assertEqual(before,support.tree(f.project))

    def test_M6_land_IO_handler_read_guard(self):
        f=self.fixture(); batch=f.batch(); original=ml._Store.rows; fired=[]
        def rows(store):
            if fired==['exchange']:
                fired.append('land handler rows'); raise OSError(errno.EIO,'M6 land handler rows')
            return original(store)
        def inject(call):
            if not fired and call.operation=='exchange' and call.when=='before':
                fired.append('exchange'); raise OSError(errno.EIO,'M6 exchange trigger')
        with mock.patch.object(ml._Store,'rows',rows): report=f.land(batch,hook=inject)
        self.assertEqual(['exchange','land handler rows'],fired,'M6 wrong handler boundary')
        self.assertEqual(('unknown','io',False),(report.state,report.reason,report.recorded))

    def test_M6_land_cancel_validation_read_guard(self):
        f=self.fixture(); batch=f.batch(); original_rows=ml._Store.rows; original_queue=ml._Store.qrows; fired=[]
        def queue(store):
            result=original_queue(store)
            if store.phase=='validation':
                for row in result: row['state']='cancelled'
            return result
        def rows(store):
            if store.phase=='validation':
                fired.append('land cancelled validation rows'); raise OSError(errno.EIO,'M6 validation rows')
            return original_rows(store)
        with mock.patch.object(ml._Store,'rows',rows),mock.patch.object(ml._Store,'qrows',queue): report=f.land(batch)
        self.unknown(report,fired,'land cancelled validation rows')

    def test_M6_cleanup_durable_reread_guard_continues_later_rows(self):
        f=self.fixture(); f.land(f.batch({'a.txt':b'one'})); f.land(f.batch({'second':b'two'},'second'))
        original=ml._Store.rows; clean=ml._clean_row; fired=[]; cleaned=[]
        def cleanup(store,row):
            cleaned.append(row['id'])
            if row['id']==1:
                fired.append('cleanup row'); raise OSError(errno.EIO,'M6 cleanup trigger')
            return clean(store,row)
        def rows(store):
            if fired==['cleanup row']:
                fired.append('cleanup durable reread'); raise OSError(errno.EIO,'M6 cleanup reread')
            return original(store)
        with mock.patch.object(ml,'_clean_row',cleanup),mock.patch.object(ml._Store,'rows',rows): reports=ml.cleanup(f.project,f.private)
        self.assertEqual(['cleanup row','cleanup durable reread'],fired,'M6 wrong reread boundary')
        self.assertEqual([1,2],cleaned)
        self.assertEqual(('unknown','io',False),(reports[0].state,reports[0].reason,reports[0].recorded))
        self.assertEqual('landed',reports[1].state)

    def test_M6_applying_cancel_enters_putback_and_terminal_retry_refuses(self):
        f=self.fixture(); batch=f.batch(); ticket=ml.enqueue(f.project,f.private,batch,policy=f.policy)
        ml._seed_history(f.project,f.private,(ml.HistoryRow(1,batch,'applying'),))
        original=ml._putback; fired=[]
        def putback(store,row,*args,**kwargs):
            fired.append((row['id'],row['state'],store.phase)); return original(store,row,*args,**kwargs)
        with mock.patch.object(ml,'_putback',putback): report=ml.cancel(f.project,f.private,ticket.ticket)
        self.assertEqual([(1,'applying','reverse')],fired,'M6 applying cancel did not reach putback')
        self.assertEqual(('reverted','cancelled'),(report.state,report.reason)); self.assertEqual(f.before,support.tree(f.project))
        fired.clear()
        with mock.patch.object(ml,'_putback',putback): retry=ml.retry(f.project,f.private,1,policy=f.policy)
        self.assertEqual(('refused','terminal',False),(retry.state,retry.reason,retry.recorded))
        self.assertEqual([],fired,'M6 terminal retry entered putback')
