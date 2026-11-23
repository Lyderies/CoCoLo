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
        for label,name,old,new in [('packaging executable',FILES[1],'MAX_CATALOGUE_BYTES =','MAX_CATALOGUE_BYTES = 1 #'),('unrelated comment',FILES[1],'import ast','import ast # unrelated'),('weaken size limit',FILES[2],'self.padded(2_097_152)','self.padded(1)'),('wrong count',FILES[2],'self.assertEqual(10, len','self.assertEqual(11, len'),('unpaired count',FILES[2],'self.assertEqual(10, len','self.assertEqual(9, len'),('helper edit',FILES[2],'def digest_of(record: dict)','def digest_of(record: object)')]:
            bad=copy.deepcopy(after)
            if old not in bad[name]:old='import json';new='import json # unrelated executable annotation'
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
