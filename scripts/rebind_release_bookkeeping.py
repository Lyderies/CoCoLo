#!/usr/bin/env python3
"""TF-25: append-only release bookkeeping; never a general baseline refresh."""
from __future__ import annotations
import argparse
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import hashlib
import test_framework as fw

PACKAGE='packages/claude-agents'
FILES=(PACKAGE+'/previous-releases.json',PACKAGE+'/src/claude_agents/packaging.py',PACKAGE+'/tests/test_previous_release_catalogue.py')
PREFIX='claude:tests.test_previous_release_catalogue.'
BINDINGS='scripts/test-release-bindings'


def git(root,*args):
    return subprocess.check_output(['git',*args],cwd=root,stderr=subprocess.DEVNULL)


def revision(root,name):
    return git(root,'rev-parse','--verify',name+'^{commit}').decode().strip()


def texts(root,commit):
    return {name:git(root,'show',commit+':'+name).decode() for name in FILES}


def transition_errors(before,after,record):
    """Generate the sole permitted three-file transition, then compare every byte."""
    try:
        old=fw.decode_json(before[FILES[0]])
        if set(old)!={'releases','schema_version'} or old['schema_version']!=1:
            raise ValueError('catalogue schema')
        entries=old['releases'];count=len(entries)
        if not entries or any(row['release']==record['release'] for row in entries):
            raise ValueError('duplicate previous release')
        if tuple(map(int,record['release'].split('.'))) <= tuple(map(int,entries[-1]['release'].split('.'))):
            raise ValueError('release order')
        expected={FILES[0]:json.dumps({**old,'releases':entries+[record]},indent=1,sort_keys=True)+'\n'}
        code=before[FILES[1]]
        marker='    "'+record['bundle_sha256']+'",  # '
        lines=code.splitlines(keepends=True);matches=[i for i,line in enumerate(lines) if line.startswith(marker)]
        if len(matches)!=1:raise ValueError('new pin needs ordinary proof review')
        index=matches[0];annotation=lines[index]
        suffix=' compile to the same bundle\n'
        last=entries[-1]['release']+' (source '+entries[-1]['source_commit']+')'
        if not annotation.endswith(' and '+last+suffix):
            raise ValueError('pin annotation needs ordinary proof review')
        lines[index]=annotation[:-len(' and '+last+suffix)]+', '+last+' and '+record['release']+' (source '+record['source_commit']+')'+suffix
        expected[FILES[1]]=''.join(lines)
        code=before[FILES[2]]
        assertion='self.assertEqual('+str(count)+', len(released_bundles(self.source)))'
        if code.count(assertion)!=2:raise ValueError('paired count assertions missing')
        code=code.replace(assertion,'self.assertEqual('+str(count+1)+', len(released_bundles(self.source)))')
        words=('zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty').split()
        same=0
        for row in reversed(entries+[record]):
            if row['bundle_sha256']!=record['bundle_sha256']:break
            same+=1
        number=words[same] if same<len(words) else str(same)
        comment='  # '+', '.join(row['release'] for row in entries+[record])+' (last '+number+': same bundle)'
        lines=code.splitlines(keepends=True)
        indices=[i for i,line in enumerate(lines) if 'self.assertEqual('+str(count+1)+', len(released_bundles(self.source)))  #' in line]
        if len(indices)!=1:raise ValueError('count annotation missing')
        i=indices[0];lines[i]=lines[i].split('  #',1)[0]+comment+'\n';expected[FILES[2]]=''.join(lines)
        return [name+': change outside permitted release bookkeeping' for name in FILES if after[name]!=expected[name]]
    except (ValueError,KeyError,TypeError,IndexError) as error:
        return ['invalid release bookkeeping: '+str(error)]


def unpack(root,commit,target):
    """Only regular files/directories from our trusted package; never extract links."""
    data=git(root,'archive',commit,PACKAGE)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for item in archive:
            path=target/item.name
            if not path.resolve().is_relative_to(target.resolve()) or not (item.isdir() or item.isfile()):
                raise ValueError('unsafe source snapshot')
            if item.isdir():path.mkdir(parents=True,exist_ok=True)
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(archive.extractfile(item).read())


def child(root,snapshot,code):
    from parallel_tests import worker_environment
    return json.loads(subprocess.check_output([sys.executable,'-B','-c',code],cwd=snapshot,
        env={**worker_environment(),'PYTHONDONTWRITEBYTECODE':'1'},text=True))


def source_record(root,source,previous):
    source=revision(root,source)
    if git(root,'show',source+':packages/codex-agents/native-package/VERSION').decode().strip()!=previous:
        raise ValueError('source version does not match previous installed release')
    with tempfile.TemporaryDirectory(prefix='release-source-') as folder:
        snapshot=Path(folder).resolve();unpack(root,source,snapshot)
        code=f"""import sys,json
from pathlib import Path
sys.path.insert(0,{str(snapshot/PACKAGE/'src')!r})
from claude_agents.packaging import compile_bundle
b=compile_bundle(Path({str(snapshot/PACKAGE)!r}));m=json.loads(b.manifest)
print(json.dumps(dict(release={previous!r},source_commit={source[:8]!r},package_version=m['package_version'],bundle_sha256=m['bundle_sha256'],files={{p:v.decode() for p,v in b.files}})))
"""
        return child(root,snapshot,code)


def base_bindings(root):
    result={}
    for name in ('test-legacy-release-0850.json','test-legacy-release-0850-bindings.json','test-legacy-release-0850-runtime.json'):
        path=root/'scripts'/name
        if path.exists():result.update(fw.decode_json(path.read_text())['tests'])
    return result


def inventory(root,snapshot,seed,*,test_owned=False):
    scripts=snapshot/'scripts';scripts.mkdir(exist_ok=True)
    shutil.copyfile(root/'scripts/test_framework.py',scripts/'test_framework.py')
    code=f"""import sys,json,unittest
from pathlib import Path
root=Path({str(snapshot)!r})
sys.path[:0]=[str(root/'scripts'),str(root/{PACKAGE!r}/'src'),str(root/{PACKAGE!r})]
import test_framework as fw
import tests.test_previous_release_catalogue as module
seed={seed!r};out={{}}
def walk(suite):
 for test in suite:
  if isinstance(test,unittest.TestSuite):walk(test)
  else:
   key='claude:'+test.id();deps=seed.get(key,{{}}).get('source_dependencies',[])
   out[key]={{**fw.describe_test(test,tuple(root/p for p in deps),test_owned={test_owned!r}),'source_dependencies':deps}}
walk(unittest.defaultTestLoader.loadTestsFromModule(module))
print(json.dumps(out))
"""
    return child(root,snapshot,code)


def build(root,before_commit,source,previous,seed,after_commit=None,expected_sources=None,*,include_policy=False):
    before_commit=revision(root,before_commit);source=revision(root,source)
    before=texts(root,before_commit)
    expected_sources = expected_sources or source_hashes(texts(root,fw.RELEASE_BASE))
    if source_hashes(before)!=expected_sources:
        raise ValueError("pre-bump bookkeeping sources do not match admitted state")
    after=texts(root,after_commit) if after_commit else {name:(root/name).read_text() for name in FILES}
    record=source_record(root,source,previous)
    errors=transition_errors(before,after,record)
    if errors:raise ValueError('; '.join(errors))
    eligible={name:rec for name,rec in seed.items() if name.startswith(PREFIX)}
    with tempfile.TemporaryDirectory(prefix='release-proof-') as folder:
        snapshot=Path(folder).resolve();unpack(root,before_commit,snapshot)
        old=inventory(root,snapshot,eligible)
        if old!=eligible:raise ValueError('pre-bump proof does not match existing bindings')
        for name,text in after.items():(snapshot/name).write_text(text)
        new=inventory(root,snapshot,eligible)
        translated=inventory(root,snapshot,eligible,test_owned=True) if include_policy else None
        if set(new)!=set(old) or any(rec['requirements'] or not rec['dependency_values'] for rec in new.values()):
            raise ValueError('bookkeeping cannot admit, retire or tag tests')
    result=dict(schema=1,previous_release=previous,before_commit=before_commit,source_commit=source,
                before_sources=source_hashes(before),after_sources=source_hashes(after),tests=new)
    if translated is not None:result['test_owned_tests']=translated
    return result


def source_hashes(values):
    return {name:hashlib.sha256(text.encode()).hexdigest() for name,text in values.items()}


def checked_bindings(root,check_current=True,*,test_owned=False):
    """Re-derive each immutable admission from Git; current edits still face normal hashing."""
    state=base_bindings(root);admitted={}
    anchor=source_hashes(texts(root,fw.RELEASE_BASE))
    paths=sorted((root/BINDINGS).glob('*.json'),key=lambda p:tuple(map(int,p.stem.split('.'))))
    for path in paths:
        name=path.relative_to(root).as_posix();data=fw.decode_json(path.read_text())
        commits=git(root,'log','--reverse','--format=%H','HEAD','--',name).decode().split()
        if not commits or fw.decode_json(git(root,'show',commits[0]+':'+name).decode())!=data:
            raise ValueError('immutable bookkeeping binding changed or uncommitted')
        rebuilt=build(root,data['before_commit'],data['source_commit'],data['previous_release'],state,commits[0],anchor,include_policy='test_owned_tests' in data)
        if rebuilt!=data:raise ValueError('bookkeeping binding does not match permitted transition')
        state.update(data['tests'])
        records = data['tests']
        if test_owned and 'test_owned_tests' in data:
            records = {name: {**row, 'dependency_policy': 'test-owned'} for name, row in data['test_owned_tests'].items()}
        admitted.update(records);anchor=data['after_sources']
    if paths and check_current and source_hashes({name:(root/name).read_text() for name in FILES})!=anchor:
        raise ValueError('current bookkeeping sources differ from admitted state; use the checked rebinding command for a permitted bump')
    return admitted


def apply_fingerprints(versions,records):
    for version in versions:
        for name,record in records.items():
            if name in version['tests']:version['tests'][name]=record['fingerprint']


def write_new(path,data):
    if path.exists():raise ValueError('immutable binding already exists')
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as stream:stream.write(json.dumps(data,indent=1,sort_keys=True)+'\n')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--from',dest='before',required=True,help='trusted pre-bookkeeping commit matching existing bindings')
    parser.add_argument('--source',required=True,help='trusted source commit of the previous installed release')
    parser.add_argument('--previous-release',required=True)
    args=parser.parse_args();root=Path(__file__).resolve().parents[1]
    if not re.fullmatch(r'\d+\.\d+\.\d+',args.previous_release):parser.error('invalid release')
    path=root/BINDINGS/(args.previous_release+'.json')
    try:
        if path.exists():raise ValueError('immutable binding already exists')
        state=base_bindings(root);state.update(checked_bindings(root,check_current=False))
        active=fw.decode_json((root/'scripts/test-legacy-0850.json').read_text())['tests']
        state={name:rec for name,rec in state.items() if name in active}
        paths=sorted((root/BINDINGS).glob('*.json'),key=lambda p:tuple(map(int,p.stem.split('.'))))
        anchor=fw.decode_json(paths[-1].read_text())['after_sources'] if paths else None
        data=build(root,args.before,args.source,args.previous_release,state,expected_sources=anchor,include_policy=(root/'scripts/test-legacy-test-owned-v1.json').exists())
        write_new(path,data)
    except (ValueError,KeyError,subprocess.CalledProcessError) as error:
        parser.exit(1,str(error)+'\n')
    print(path.relative_to(root).as_posix()+': '+str(len(data['tests']))+' existing proofs rebound; commit this new immutable file')

if __name__=='__main__':main()
