"""TF-25: only an authenticated append and paired counts may rebind."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
from test_framework import requirements
FILES=('packages/claude-agents/previous-releases.json','packages/claude-agents/src/claude_agents/packaging.py','packages/claude-agents/tests/test_previous_release_catalogue.py')

class ReleaseBookkeeping(unittest.TestCase):
    __why_not_scenario__='Independent old/new Git sources and hostile bookkeeping mutations prove the narrow exception.'
    def policy(self):
        path=ROOT/'scripts/rebind_release_bookkeeping.py'
        self.assertTrue(path.exists(),'missing narrow release-bookkeeping validator')
        spec=importlib.util.spec_from_file_location('release_bookkeeping_fixture',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
    def sources(self,revision):
        return {name:subprocess.check_output(['git','show',revision+':'+name],cwd=ROOT,text=True) for name in FILES}

    @requirements('TF-25','TF-23')
    def test_exact_primary_restoration_matches_installed_release(self):
        expected=self.sources('30bf1fa9')
        self.assertEqual('0.8.49',json.loads(expected[FILES[0]])['releases'][-1]['release'])
        for name in FILES:
            with self.subTest(file=name):self.assertEqual(expected[name],(ROOT/name).read_text(),'installed 0.8.49 bookkeeping not restored: '+name)

    @requirements('TF-25')
    def test_correct_append_passes_and_every_unrelated_edit_is_rejected(self):
        module=self.policy();before=self.sources('c75eba51');after=self.sources('30bf1fa9')
        record=json.loads(after[FILES[0]])['releases'][-1]
        self.assertEqual([],module.transition_errors(before,after,record))
        mutations=[]
        def catalogue(label,change):
            bad=copy.deepcopy(after);doc=json.loads(bad[FILES[0]]);change(doc);bad[FILES[0]]=json.dumps(doc,indent=1,sort_keys=True)+'\n';mutations.append((label,bad))
        catalogue('old record edit',lambda d:d['releases'][0].update(release='0.0.1'))
        catalogue('remove old record',lambda d:d['releases'].pop(0))
        catalogue('reorder old records',lambda d:d['releases'].reverse())
        catalogue('extra record',lambda d:d['releases'].append(copy.deepcopy(record)))
        catalogue('wrong source',lambda d:d['releases'][-1].update(source_commit='a1d25560'))
        catalogue('wrong release',lambda d:d['releases'][-1].update(release='0.8.99'))
        catalogue('forged bundle',lambda d:d['releases'][-1]['files'].update({'forged':'x'}))
        catalogue('extra schema key',lambda d:d.update(unreviewed=True))
        for label,name,old,new in [('packaging executable',FILES[1],'_CATALOGUE_LIMIT = 2_097_152','_CATALOGUE_LIMIT = 1'),('unrelated comment',FILES[1],'import ast','import ast # unrelated'),('weaken size limit',FILES[2],'self.padded(2_097_152)','self.padded(1)'),('wrong count',FILES[2],'self.assertEqual(10, len','self.assertEqual(11, len'),('unpaired count',FILES[2],'self.assertEqual(10, len','self.assertEqual(9, len'),('helper edit',FILES[2],'def digest_of(record: dict)','def digest_of(record: object)')]:
            bad=copy.deepcopy(after)
            self.assertIn(old,bad[name]);bad[name]=bad[name].replace(old,new,1);mutations.append((label,bad))
        for label,bad in mutations:
            with self.subTest(mutation=label):self.assertTrue(module.transition_errors(before,bad,record),'accepted unrelated edit: '+label)
        self.assertEqual([],module.transition_errors(before,after,record))

    @requirements('TF-25','TF-7')
    def test_rebinding_retains_retirement_and_catches_later_fingerprint_edits(self):
        module=self.policy()
        versions=[{'baseline':'b','tests':{'kept':'old','untouched':'same'}} for _ in range(3)]
        records={'kept':{'fingerprint':'new'},'retired':{'fingerprint':'new'}}
        module.apply_fingerprints(versions,records)
        for version in versions:
            self.assertEqual({'kept':'new','untouched':'same'},version['tests'])
            self.assertNotIn('retired',version['tests'])
        import test_framework as fw
        inventory={'x':{'kept':dict(fingerprint='new',requirements=[],rationale=''),'untouched':dict(fingerprint='same',requirements=[],rationale='')}}
        args=dict(inventory=inventory,stages={'x':'unit'},layers={'x':'unit'},catalogue={},legacy=versions[0],ceiling=versions[1],previous=versions[2])
        self.assertEqual([],fw.validate_metadata(**args))
        inventory['x']['kept']['fingerprint']='later edit'
        self.assertIn('kept: legacy implementation changed; tag it',fw.validate_metadata(**args))

    @requirements('TF-25')
    def test_rebind_command_rejects_invalid_source_and_overwrite(self):
        module=self.policy()
        with self.assertRaisesRegex(ValueError,'source version'):
            module.source_record(ROOT,'b5318440','0.8.99')
        record=module.source_record(ROOT,'b5318440','0.8.49')
        self.assertEqual(json.loads(self.sources('30bf1fa9')[FILES[0]])['releases'][-1],record)
        with self.assertRaisesRegex(ValueError,'immutable binding already exists'):
            module.write_new(ROOT/'scripts/test-legacy-release-0850.json',{})

    @requirements('TF-25','TF-7')
    def test_claimed_pre_bump_commit_cannot_hide_old_record_edits(self):
        from unittest import mock
        module=self.policy();before=module.revision(ROOT,'c75eba51');after=module.revision(ROOT,'30bf1fa9')
        original=module.texts
        def forged(root,commit):
            data=original(root,commit)
            if commit in (before,after):
                document=json.loads(data[FILES[0]]);document['releases'][0]['source_commit']='forged00'
                data[FILES[0]]=json.dumps(document,indent=1,sort_keys=True)+'\n'
            return data
        with mock.patch.object(module,'texts',side_effect=forged):
            with self.assertRaisesRegex(ValueError,'pre-bump bookkeeping sources'):
                module.build(ROOT,before,'b5318440','0.8.49',module.base_bindings(ROOT),after)
        result=module.build(ROOT,before,'b5318440','0.8.49',module.base_bindings(ROOT),after)
        self.assertEqual(14,len(result['tests']))

    @requirements('TF-25','TF-7')
    def test_later_edits_to_each_admitted_file_still_fail_metadata(self):
        from unittest import mock
        module=self.policy();original=Path.read_text
        self.assertEqual(14,len(module.checked_bindings(ROOT)))
        for name in FILES:
            def changed(path,*args,**kwargs):
                value=original(path,*args,**kwargs)
                return value+' ' if path==ROOT/name else value
            with self.subTest(file=name),mock.patch.object(Path,'read_text',changed):
                with self.assertRaisesRegex(ValueError,'current bookkeeping sources'):
                    module.checked_bindings(ROOT)
        self.assertEqual(14,len(module.checked_bindings(ROOT)))

    @requirements('TF-25')
    def test_next_release_uses_the_same_narrow_procedure(self):
        from unittest import mock
        module=self.policy();before=self.sources('30bf1fa9');after=copy.deepcopy(before)
        record=module.source_record(ROOT,'0ca9c36f','0.8.50')
        catalogue=json.loads(after[FILES[0]]);catalogue['releases'].append(record)
        after[FILES[0]]=json.dumps(catalogue,indent=1,sort_keys=True)+'\n'
        after[FILES[1]]=after[FILES[1]].replace(' and 0.8.49 (source b5318440)',', 0.8.49 (source b5318440) and 0.8.50 (source 0ca9c36f)')
        after[FILES[2]]=after[FILES[2]].replace('self.assertEqual(10, len','self.assertEqual(11, len').replace('0.8.49 (last nine: same bundle)','0.8.49, 0.8.50 (last ten: same bundle)')
        self.assertEqual([],module.transition_errors(before,after,record))
        original=module.texts
        def sources(root,commit):return after if commit=='future-fixture' else original(root,commit)
        state=module.base_bindings(ROOT);state.update(module.checked_bindings(ROOT))
        with mock.patch.object(module,'texts',side_effect=sources):
            result=module.build(ROOT,'9e095c4c','0ca9c36f','0.8.50',state,'future-fixture',module.source_hashes(before))
        self.assertEqual(set(module.checked_bindings(ROOT)),set(result['tests']))
        self.assertNotEqual(state[next(k for k in state if k.endswith('test_the_shipped_catalogue_is_accepted'))]['fingerprint'],result['tests'][next(k for k in result['tests'] if k.endswith('test_the_shipped_catalogue_is_accepted'))]['fingerprint'])
