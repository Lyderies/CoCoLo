"""Decision 102: exact round-three rejection and witness-sensitivity controls."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest import mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import test_framework as fw
from test_framework import requirements


class RoundThree(unittest.TestCase):
    __why_not_scenario__ = 'Metadata and witness defects require independent invalid-evidence and mutation controls.'

    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); self.root=Path(temp.name)

    def module(self,name,source):
        path=self.root/(name+'.py'); path.write_text(source)
        module=types.ModuleType(name); module.__file__=str(path);sys.modules[name]=module
        self.addCleanup(sys.modules.pop,name,None);exec(compile(source,str(path),'exec'),vars(module));return module

    @requirements('TF-14')
    def test_p01_M2_submodule_aliases_and_relative_imports(self):
        pkg=self.root/'pkg';pkg.mkdir();(pkg/'__init__.py').write_text('')
        (pkg/'fixtures.py').write_text('from pathlib import Path\nGOLDEN_PATH=Path("goldens/value")\nORDINARY=Path("input")\n')
        for prefix in ('from pkg import fixtures as f','from . import fixtures as f','from .fixtures import GOLDEN_PATH as expected'):
            with self.subTest(prefix=prefix):
                expression='expected' if 'GOLDEN_PATH' in prefix else 'f.GOLDEN_PATH'
                source=prefix+'\nvalue='+expression+'.read_text()'
                errors=fw.golden_bypasses(source,filename=pkg/'test_case.py')
                self.assertTrue(any('direct golden' in e for e in errors),'p01 M2 submodule golden escaped: '+prefix)
                ordinary=source.replace('GOLDEN_PATH','ORDINARY')
                self.assertEqual([],fw.golden_bypasses(ordinary,filename=pkg/'test_case.py'))

    @requirements('TF-7')
    def test_p01_closure_fixture_state_invalidates_metadata(self):
        module=self.module('test_r3_closure','import unittest\nfrom types import SimpleNamespace\ndef make():\n    state=SimpleNamespace(expected=1)\n    def test_value(self): self.assertEqual(1,state.expected)\n    return state,test_value\nstate,method=make()\nclass Case(unittest.TestCase): pass\nCase.test_value=method\n')
        case=module.Case('test_value');before=fw.describe_test(case);result=unittest.TestResult();case.run(result);self.assertTrue(result.wasSuccessful())
        module.state.expected=2;after=fw.describe_test(case);result=unittest.TestResult();case.run(result);self.assertEqual(1,len(result.failures))
        self.assertTrue(before['dependency_values'] is None or after['dependency_values'] is None or before!=after,'p01 closure behavior changed but metadata stayed eligible')
        simple=self.module('test_r3_simple','import unittest\ndef make(expected):\n    def test_value(self): self.assertEqual(1,expected)\n    return test_value\nclass Case(unittest.TestCase): pass\nCase.test_value=make(1)\n')
        a=fw.dependency_fingerprint(simple.Case('test_value'));self.assertIsNotNone(a)
        simple.Case.test_value=simple.make(2);self.assertNotEqual(a,fw.dependency_fingerprint(simple.Case('test_value')))

    @requirements('TF-7')
    def test_p01_positional_and_keyword_defaults_are_dependencies(self):
        for signature in ('value=EXPECTED','*, value=EXPECTED'):
            with self.subTest(signature=signature):
                source='import unittest\nEXPECTED=1\ndef helper('+signature+'):\n    return value\nclass Case(unittest.TestCase):\n    def test_value(self): self.assertEqual(1,helper())\n'
                module=self.module('test_r3_defaults',source);case=module.Case('test_value');before=fw.describe_test(case)
                result=unittest.TestResult();case.run(result);self.assertTrue(result.wasSuccessful());self.assertIsNotNone(before['dependency_values'])
                module.EXPECTED=2
                if signature.startswith('*'): module.helper.__kwdefaults__={'value':module.EXPECTED}
                else: module.helper.__defaults__=(module.EXPECTED,)
                result=unittest.TestResult();case.run(result);self.assertEqual(1,len(result.failures))
                self.assertNotEqual(before,fw.describe_test(case),'p01 default-only behavior change kept identical metadata')

    @requirements('TF-8','TF-9')
    def test_p02_retirement_witness_rejects_each_status_blind_mutant(self):
        from test_test_framework import ReviewCoreTests
        name='test_A8_incomplete_and_timed_out_modules_do_not_retire_debt'
        control=unittest.TestResult();ReviewCoreTests(name).run(control);self.assertTrue(control.wasSuccessful(),control.errors+control.failures)
        original=fw.measurements
        for field,value in [('timed_out',False),('returncode',0)]:
            with self.subTest(field=field):
                def ignore(rows,*args,**kwargs): return original([{**r,field:value} for r in rows],*args,**kwargs)
                result=unittest.TestResult()
                with mock.patch.object(fw,'measurements',ignore): ReviewCoreTests(name).run(result)
                self.assertEqual([],result.errors)
                self.assertEqual(1,len(result.failures),'p02 old witness accepted status-blind '+field)
                self.assertIn('p02 '+field,result.failures[0][1])

    @requirements('TF-1','TF-9')
    def test_p04_shard_witness_rejects_duplicate_original_worker_records(self):
        from test_test_framework_guards import RunnerIntegrationTests
        method='test_r2_M7_real_runner_shards_and_import_failure'; original=RunnerIntegrationTests.run_fixture
        control=unittest.TestResult();RunnerIntegrationTests(method).run(control);self.assertTrue(control.wasSuccessful(),control.failures+control.errors)
        def duplicate(self,*args,**kwargs):
            code,output,report=original(self,*args,**kwargs)
            if '--shard' in kwargs.get('extra_args',()): report['workers'].append(copy.deepcopy(report['workers'][0]))
            return code,output,report
        result=unittest.TestResult()
        with mock.patch.object(RunnerIntegrationTests,'run_fixture',duplicate):RunnerIntegrationTests(method).run(result)
        self.assertEqual([],result.errors)
        self.assertEqual(1,len(result.failures),'p04 old witness accepted duplicate original workers')
        self.assertIn('p04 repeated worker outcome',result.failures[0][1])

    @requirements('TF-11')
    def test_p04_unix_witness_rejects_permitted_async_operations(self):
        from test_test_framework_guards import WorkerTests
        name='test_inherited_socket_methods_and_asyncio_have_no_guard_exemption'; original=WorkerTests.probe
        control=unittest.TestResult();WorkerTests(name).run(control);self.assertTrue(control.wasSuccessful(),control.failures+control.errors)
        for api in ('open_unix_connection','start_unix_server'):
            with self.subTest(api=api):
                def permit(self,body,*args,**kwargs):
                    extra='import asyncio\nasync def allowed(*args,**kwargs): return None\nasyncio.'+api+'=allowed\n'
                    kwargs['bootstrap']=kwargs.get('bootstrap','')+'\n'+extra
                    return original(self,body,*args,**kwargs)
                result=unittest.TestResult()
                with mock.patch.object(WorkerTests,'probe',permit): WorkerTests(name).run(result)
                self.assertEqual([],result.errors)
                self.assertEqual(1,len(result.failures),'p04 old Unix witness accepted permitted '+api)
                self.assertIn('p04 Unix operation allowed',result.failures[0][1])

    @requirements('TF-11')
    def test_p05_connected_socket_io_is_sticky_in_both_layers(self):
        from test_test_framework_guards import WorkerTests
        methods={'send':"b'x'",'sendall':"b'x'",'recv':'1','recv_into':'bytearray(1)', 'recvfrom':'1','recvfrom_into':'bytearray(1)','recvmsg':'1','recvmsg_into':'[bytearray(1)]'}
        for layer in ('unit','component'):
            for method,args in methods.items():
                if not hasattr(__import__('socket').socket,method):continue
                with self.subTest(layer=layer,method=method):
                    bootstrap="import socket,builtins; builtins.PAIR=socket.socketpair(); builtins.PAIR[1].sendall(b'x')"
                    body='import builtins\ntry:\n    builtins.PAIR[0].'+method+'('+args+')\nexcept Exception:\n    pass\nfinally:\n    [s.close() for s in builtins.PAIR]'
                    code,output,result=WorkerTests().probe(body,layer,bootstrap=bootstrap)
                    self.assertNotEqual(0,code,'p05 connected socket I/O was permitted: '+method)
                    self.assertTrue(any(v['operation']=='use a socket (socket.'+method+')' for v in result['violations']))
        code,output,result=WorkerTests().probe("import socket\na,b=socket.socketpair()\na.sendall(b'x'); self.assertEqual(b'x',b.recv(1));a.close();b.close()",'scenario')
        self.assertEqual(0,code,output);self.assertEqual([],result['violations'])

    @requirements('TF-1','TF-5')
    def test_p05_CI_selects_trusted_bases_for_supported_events(self):
        from test_ci_stages import jobs
        workflow=(ROOT/'.github/workflows/ci.yml').read_text();self.assertIn('schedule:',workflow)
        def git(*args):return subprocess.check_output(['git',*args],cwd=self.root,text=True,stderr=subprocess.DEVNULL,timeout=5).strip()
        git('init','-b','main');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid');git('commit','--allow-empty','-m','base');base=git('rev-parse','HEAD');git('commit','--allow-empty','-m','tip');tip=git('rev-parse','HEAD')
        for job in ('unit','integration','full'):
            shell=textwrap.dedent(jobs(workflow)[job].split('run: |',1)[1].split('      - name:',1)[0])
            for event,ref,pr,push,want in [('pull_request','refs/pull/1/merge',base,'',base),('push','refs/heads/main','',base,base),('schedule','refs/heads/main','','',base),('push','refs/tags/v0.4.0','','0'*40,base),('workflow_dispatch','refs/heads/main','','',None),('push','refs/heads/main','','0'*40,None)]:
                with self.subTest(job=job,event=event,ref=ref):
                    output=self.root/'environment';output.unlink(missing_ok=True)
                    env={**os.environ,'EVENT_NAME':event,'EVENT_REF':ref,'PR_BASE':pr,'PUSH_BASE':push,'TRUSTED_BASE':base,'GITHUB_ENV':str(output)}
                    result=subprocess.run(['bash','-e','-c',shell],cwd=self.root,env=env,capture_output=True,text=True,timeout=5)
                    if want:
                        self.assertEqual(0,result.returncode,'p05 supported event rejected: '+event+' '+ref+' '+result.stderr)
                        self.assertEqual('COCOLO_REVIEW_BASE='+want+'\n',output.read_text())
                    else:self.assertNotEqual(0,result.returncode);self.assertFalse(output.exists())
        self.assertNotEqual(base,tip)

    @requirements('TF-14','TF-15')
    def test_p06_matching_golden_requires_provenance_and_update_repairs_it(self):
        from test_golden import compare,GoldenUpdated
        args=dict(root=self.root,name='value',actual='same',requirement='TF-14',declared={'TF-14'},known={'TF-14'},environ={})
        (self.root/'value').write_text('same');sidecar=self.root/'value.requirement'
        for previous in (None,'TF-15\n'):
            with self.subTest(previous=previous):
                sidecar.unlink(missing_ok=True)
                if previous is not None:sidecar.write_text(previous)
                with self.assertRaisesRegex(AssertionError,'golden provenance'):compare(**args)
                with self.assertRaises(GoldenUpdated):compare(**{**args,'environ':{'UPDATE_GOLDEN':'1'}})
                self.assertEqual('TF-14\n',sidecar.read_text());compare(**args)

    @requirements('TF-23')
    def test_TF23_declared_fixes_require_red_green_and_positive_control(self):
        validate=getattr(fw,'validate_witness_evidence',None)
        self.assertTrue(callable(validate),'TF23 missing evidence enforcement')
        (self.root/'red.json').write_text(json.dumps({'base':'9c18c4a3','status':'red','diagnostic':'intended failure'}))
        (self.root/'green.json').write_text(json.dumps({'status':'green'}))
        good=dict(fix='fixture',requirement='TF-23',witness='root:fixture.test',positive_control='valid fixture',base='9c18c4a3',red='red.json',diagnostic='intended failure',green='green.json')
        self.assertEqual([],validate(self.root,[good]))
        for field in good:
            bad=dict(good);bad.pop(field)
            self.assertTrue(validate(self.root,[bad]),'TF23 accepted missing '+field)
        (self.root/'red.json').write_text(json.dumps({'base':'9c18c4a3','status':'green','diagnostic':'different failure'}))
        self.assertTrue(validate(self.root,[good]),'TF23 accepted wrong red outcome')
        manifests=list((ROOT/'scripts').glob('test-review-witnesses-*.json'))
        self.assertTrue(manifests,'TF23 no declared review witnesses')
        for path in manifests:self.assertEqual([],validate(ROOT,json.loads(path.read_text())),str(path))
