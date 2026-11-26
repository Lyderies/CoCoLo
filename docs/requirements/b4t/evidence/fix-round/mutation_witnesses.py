"""Run only the five MED-1 witnesses with one deliberate product fault each.

Invoke from packages/codex-agents with repo/scripts on PYTHONPATH. No I/O occurs
at import. Exit zero means every mutation was detected by its named witness.
"""
def main():
    import io
    import os
    import sqlite3
    import unittest
    from unittest import mock
    from codex_agents.project_history import ProjectHistory
    from codex_agents.takeover_owner import OwnerOperations
    from codex_agents import memory_holds
    from tests import test_b4t_takeover as cases

    real_withdraw=ProjectHistory.withdraw_earlier_open_bindings
    def premature_withdraw(owner,*args,**kwargs):
        result=real_withdraw(owner,*args,**kwargs)
        with owner._transaction():
            owner._connection.execute("UPDATE project_holds SET state='released',release_kind='fenced' "
                                      "WHERE kind='open_binding' AND state='held'")
        return result
    real_adopt=OwnerOperations.adopt_ledger
    def adopt_other_project(ops,journal):
        real_adopt(ops,journal)
        with sqlite3.connect(ops.state.database) as db:
            db.execute('UPDATE active_run_capacity SET runtime_digest=? WHERE project_id<>?',
                       (journal['successor']['digest'],journal['project_id']))
    def release_without_inode(history,*args,**kwargs):
        with history._transaction():
            history._connection.execute("UPDATE project_holds SET state='released',release_kind='service_stopped' "
                                        "WHERE kind IN ('driver_entry','preparation') AND state='held'")
    real_stop=OwnerOperations.stop_collector
    def signal_collector(ops,journal):
        os.kill(os.getpid(),0)  # The witness guards this call; no signal escapes.
        return real_stop(ops,journal)
    rows=[
      ('T-4',cases.StoreTests,'test_B4t_2_a_result_committed_during_the_stop_is_judged_on_the_final_store',
       mock.patch.object(ProjectHistory,'_classify_takeover_custody',return_value=None)),
      ('T-12b',cases.CollectorStopTests,'test_B4t_3_collector_and_new_services_are_never_signalled',
       mock.patch.object(OwnerOperations,'stop_collector',signal_collector)),
      ('T-15',cases.StoreTests,'test_B4t_4_open_bindings_end_by_their_own_rules',
       mock.patch.object(ProjectHistory,'withdraw_earlier_open_bindings',premature_withdraw)),
      ('T-17',cases.AccountLineageTests,'test_B4t_4_another_projects_child_keeps_running',
       mock.patch.object(OwnerOperations,'adopt_ledger',adopt_other_project)),
      ('T-30',cases.MemoryTakeoverTests,'test_B4t_4_missing_journal_lock_evidence_keeps_memory_holds',
       mock.patch.object(memory_holds,'recover',release_without_inode))]
    missed=[]
    for label,cls,name,patch in rows:
        log=io.StringIO()
        with patch:
            result=unittest.TextTestRunner(stream=log,verbosity=0).run(unittest.TestSuite([cls(name)]))
        print(label,name,'RED (mutation detected)' if not result.wasSuccessful() else 'MISSED')
        if result.wasSuccessful():
            missed.append(label)
        else:
            for _,failure in result.failures+result.errors:
                print(failure.strip().splitlines()[-1][:240])
    return bool(missed)

if __name__=='__main__':
    raise SystemExit(main())
