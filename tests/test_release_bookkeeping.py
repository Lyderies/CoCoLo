"""TF-25: only an authenticated append and paired counts may rebind."""
import copy
from contextlib import contextmanager
import importlib.util
import json
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
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

    def admissions(self):
        paths=sorted((ROOT/'scripts/test-release-bindings').glob('*.json'),
                     key=lambda p:tuple(map(int,p.stem.split('.'))))
        self.assertTrue(paths,'missing admitted release bookkeeping')
        for path in paths:
            name=path.relative_to(ROOT).as_posix()
            introduction=subprocess.check_output(
                ['git','log','--reverse','--format=%H','HEAD','--',name],cwd=ROOT,text=True).split()[0]
            yield path,json.loads(path.read_text()),introduction

    @contextmanager
    def fixture_repo(self):
        # All writes and synthetic commits stay in this disposable repository.
        with tempfile.TemporaryDirectory(prefix='release-bookkeeping-fixture-') as folder:
            root=Path(folder)/'repo'
            subprocess.run(['git','clone','--quiet','--shared','--no-checkout',str(ROOT),str(root)],check=True)
            subprocess.run(['git','reset','--quiet','HEAD'],cwd=root,check=True)
            subprocess.run(['git','checkout','--quiet','HEAD','--','scripts'],cwd=root,check=True)
            _,_,introduction=list(self.admissions())[-1]
            subprocess.run(['git','checkout','--quiet',introduction,'--','packages/claude-agents'],cwd=root,check=True)
            for name in ('rebind_release_bookkeeping.py','test_framework.py','parallel_tests.py'):
                shutil.copyfile(ROOT/'scripts'/name,root/'scripts'/name)
            yield root

    def fixture_commit(self,root):
        subprocess.run(['git','add','--',*FILES,'scripts/test-release-bindings',
                        'packages/codex-agents/native-package/VERSION'],cwd=root,check=True)
        subprocess.run(['git','-c','user.name=TF-25 fixture','-c','user.email=tf25@example.invalid',
                        '-c','core.hooksPath=/dev/null','commit','--quiet','--no-gpg-sign','--allow-empty',
                        '-m','Disposable release bookkeeping witness'],cwd=root,check=True)
        return subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()

    def next_version(self,version):
        major,minor,patch=map(int,version.split('.'))
        return f'{major}.{minor}.{patch+1}'

    @requirements('TF-25','TF-23')
    def test_exact_primary_restoration_matches_installed_release(self):
        module=self.policy();anchor=module.source_hashes(self.sources(module.fw.RELEASE_BASE))
        for path,data,introduction in self.admissions():
            with self.subTest(release=path.stem):
                before=self.sources(data['before_commit']);expected=self.sources(introduction)
                self.assertEqual(path.stem,data['previous_release'])
                self.assertEqual(data['previous_release'],json.loads(expected[FILES[0]])['releases'][-1]['release'])
                self.assertEqual(anchor,data['before_sources'])
                self.assertEqual(data['before_sources'],module.source_hashes(before))
                self.assertEqual(data['after_sources'],module.source_hashes(expected))
                self.assertEqual(module.source_record(ROOT,data['source_commit'],data['previous_release']),
                                 json.loads(expected[FILES[0]])['releases'][-1])
                anchor=data['after_sources']
        for name in FILES:
            with self.subTest(file=name):
                self.assertEqual(expected[name],(ROOT/name).read_text(),'latest admitted bookkeeping not restored: '+name)
        self.assertEqual(14,len(module.checked_bindings(ROOT)))

    @requirements('TF-25')
    def test_correct_append_passes_and_every_unrelated_edit_is_rejected(self):
        module=self.policy()
        for path,data,introduction in self.admissions():
            with self.subTest(release=path.stem):
                self.check_transition_mutations(module,self.sources(data['before_commit']),self.sources(introduction))

    def check_transition_mutations(self,module,before,after):
        entries=json.loads(after[FILES[0]])['releases'];record=entries[-1];count=len(entries)
        self.assertEqual([],module.transition_errors(before,after,record))
        mutations=[]
        def catalogue(label,change):
            bad=copy.deepcopy(after);doc=json.loads(bad[FILES[0]]);change(doc);bad[FILES[0]]=json.dumps(doc,indent=1,sort_keys=True)+'\n';mutations.append((label,bad))
        catalogue('old record edit',lambda d:d['releases'][0].update(release='0.0.1'))
        catalogue('remove old record',lambda d:d['releases'].pop(0))
        catalogue('reorder old records',lambda d:d['releases'].reverse())
        catalogue('extra record',lambda d:d['releases'].append(copy.deepcopy(record)))
        catalogue('wrong source',lambda d:d['releases'][-1].update(source_commit='a1d25560'))
        catalogue('wrong release',lambda d:d['releases'][-1].update(release=self.next_version(record['release'])))
        catalogue('forged bundle',lambda d:d['releases'][-1]['files'].update({'forged':'x'}))
        catalogue('extra schema key',lambda d:d.update(unreviewed=True))
        for label,name,old,new in [('packaging executable',FILES[1],'_CATALOGUE_LIMIT = 2_097_152','_CATALOGUE_LIMIT = 1'),('unrelated comment',FILES[1],'import ast','import ast # unrelated'),('weaken size limit',FILES[2],'self.padded(2_097_152)','self.padded(1)'),('wrong count',FILES[2],f'self.assertEqual({count}, len',f'self.assertEqual({count+1}, len'),('unpaired count',FILES[2],f'self.assertEqual({count}, len',f'self.assertEqual({count-1}, len'),('helper edit',FILES[2],'def digest_of(record: dict)','def digest_of(record: object)')]:
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
        module=self.policy();path,data,introduction=list(self.admissions())[-1]
        previous=data['previous_release'];wrong=self.next_version(previous)
        with self.fixture_repo() as root:
            with self.assertRaisesRegex(ValueError,'source version'):
                module.source_record(root,data['source_commit'],wrong)
            record=module.source_record(root,data['source_commit'],previous)
            self.assertEqual(json.loads(self.sources(introduction)[FILES[0]])['releases'][-1],record)
            with self.assertRaisesRegex(ValueError,'immutable binding already exists'):
                module.write_new(root/'scripts/test-legacy-release-0850.json',{})
            binding=root/path.relative_to(ROOT);original=binding.read_bytes()
            command=[sys.executable,'-B',str(root/'scripts/rebind_release_bookkeeping.py'),
                     '--from',introduction,'--source',data['source_commit'],'--previous-release']
            overwrite=subprocess.run(command+[previous],cwd=root,capture_output=True,text=True)
            self.assertEqual(1,overwrite.returncode);self.assertIn('immutable binding already exists',overwrite.stderr)
            self.assertEqual(original,binding.read_bytes())
            invalid=subprocess.run(command+[wrong],cwd=root,capture_output=True,text=True)
            self.assertEqual(1,invalid.returncode);self.assertIn('source version',invalid.stderr)
            self.assertFalse((root/'scripts/test-release-bindings'/(wrong+'.json')).exists())

    @requirements('TF-25','TF-7')
    def test_claimed_pre_bump_commit_cannot_hide_old_record_edits(self):
        from unittest import mock
        module=self.policy();state=module.base_bindings(ROOT)
        for path,data,introduction in self.admissions():
            before=data['before_commit'];original=module.texts
            def forged(root,commit):
                values=original(root,commit)
                if commit in (before,introduction):
                    document=json.loads(values[FILES[0]]);document['releases'][0]['source_commit']='forged00'
                    values[FILES[0]]=json.dumps(document,indent=1,sort_keys=True)+'\n'
                return values
            with self.subTest(release=path.stem):
                with mock.patch.object(module,'texts',side_effect=forged):
                    with self.assertRaisesRegex(ValueError,'pre-bump bookkeeping sources'):
                        module.build(ROOT,before,data['source_commit'],data['previous_release'],state,
                                     introduction,data['before_sources'])
                result=module.build(ROOT,before,data['source_commit'],data['previous_release'],state,
                                    introduction,data['before_sources'],include_policy='test_owned_tests' in data)
                self.assertEqual(14,len(result['tests']))
                self.assertEqual(data,result)
            state.update(data['tests'])

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
        module=self.policy();_,latest,_=list(self.admissions())[-1]
        previous=self.next_version(latest['previous_release'])
        with self.fixture_repo() as root:
            version=root/'packages/codex-agents/native-package/VERSION'
            version.parent.mkdir(parents=True,exist_ok=True);version.write_text(previous+'\n')
            before_commit=self.fixture_commit(root)
            before=module.texts(root,before_commit);after=copy.deepcopy(before)
            record=module.source_record(root,before_commit,previous)
            catalogue=json.loads(before[FILES[0]]);entries=catalogue['releases'];count=len(entries)
            after[FILES[0]]=json.dumps({**catalogue,'releases':entries+[record]},indent=1,sort_keys=True)+'\n'
            last=entries[-1]
            annotation=last['release']+' (source '+last['source_commit']+')'
            self.assertIn(' and '+annotation+' compile to the same bundle',after[FILES[1]])
            after[FILES[1]]=after[FILES[1]].replace(' and '+annotation+' compile to the same bundle',
                ', '+annotation+' and '+previous+' (source '+before_commit[:8]+') compile to the same bundle')
            assertion=f'self.assertEqual({count}, len(released_bundles(self.source)))'
            self.assertEqual(2,after[FILES[2]].count(assertion))
            after[FILES[2]]=after[FILES[2]].replace(assertion,f'self.assertEqual({count+1}, len(released_bundles(self.source)))')
            same=0
            for row in reversed(entries+[record]):
                if row['bundle_sha256']!=record['bundle_sha256']:break
                same+=1
            words='zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty'.split()
            number=words[same] if same<len(words) else str(same)
            lines=after[FILES[2]].splitlines(keepends=True)
            for i,line in enumerate(lines):
                if f'self.assertEqual({count+1}, len(released_bundles(self.source)))  #' in line:
                    lines[i]=line.split('  #',1)[0]+'  # '+', '.join(row['release'] for row in entries+[record])+' (last '+number+': same bundle)\n'
            after[FILES[2]]=''.join(lines)
            self.assertEqual([],module.transition_errors(before,after,record))
            admitted=module.checked_bindings(root);state=module.base_bindings(root);state.update(admitted)
            for name,value in after.items():(root/name).write_text(value)
            result=module.build(root,before_commit,before_commit,previous,state,
                                expected_sources=latest['after_sources'],include_policy=True)
            self.assertEqual(set(admitted),set(result['tests']))
            key=next(k for k in state if k.endswith('test_the_shipped_catalogue_is_accepted'))
            self.assertNotEqual(state[key]['fingerprint'],result['tests'][key]['fingerprint'])
            command=[sys.executable,'-B',str(root/'scripts/rebind_release_bookkeeping.py'),
                     '--from',before_commit,'--source',before_commit,'--previous-release',previous]
            binding=root/'scripts/test-release-bindings'/(previous+'.json')
            for mutation in ('tampered latest admitted record','unrelated edit'):
                bad=copy.deepcopy(after)
                if mutation=='tampered latest admitted record':
                    document=json.loads(bad[FILES[0]]);document['releases'][-2]['source_commit']='forged00'
                    bad[FILES[0]]=json.dumps(document,indent=1,sort_keys=True)+'\n'
                else:bad[FILES[1]]+='\n# unrelated edit\n'
                for name,value in bad.items():(root/name).write_text(value)
                refused=subprocess.run(command,cwd=root,capture_output=True,text=True)
                with self.subTest(mutation=mutation):
                    self.assertEqual(1,refused.returncode)
                    self.assertIn('change outside permitted release bookkeeping',refused.stderr)
                    self.assertFalse(binding.exists())
            for name,value in after.items():(root/name).write_text(value)
            accepted=subprocess.run(command,cwd=root,capture_output=True,text=True)
            self.assertEqual(0,accepted.returncode,accepted.stderr)
            self.assertEqual(result,json.loads(binding.read_text()))
            self.fixture_commit(root)
            self.assertEqual(result['tests'],module.checked_bindings(root))

    @requirements('TF-25','TF-7')
    def test_immutable_binding_tampering_is_rejected(self):
        from unittest import mock
        module=self.policy();original=Path.read_text
        self.assertEqual(14,len(module.checked_bindings(ROOT)))
        for path,_,_ in self.admissions():
            def changed(file,*args,**kwargs):
                text=original(file,*args,**kwargs)
                if file==path:
                    data=json.loads(text);data['tests'][next(iter(data['tests']))]['fingerprint']='forged'
                    return json.dumps(data)
                return text
            with self.subTest(release=path.stem),mock.patch.object(Path,'read_text',changed):
                with self.assertRaisesRegex(ValueError,'immutable bookkeeping binding'):
                    module.checked_bindings(ROOT)
        self.assertEqual(14,len(module.checked_bindings(ROOT)))
