"""A1 native archive acceptance. Run explicitly outside the builder sandbox.

COCOLO_TEST_B4T_SKIP_SOCKETS=1 is the builder's explicit no-socket gate, never a CI
fallback. Every skipped name is listed in the durable fix-round handoff.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import time
import unittest
from unittest import mock

import tests.support
from tests.test_project_start import StarterBase, wait_for
from tests.test_host_commands import install_package
from test_framework import requirements


@unittest.skipIf(os.environ.get('COCOLO_TEST_B4T_SKIP_SOCKETS') == '1',
                 'Primary must rerun: this builder is forbidden to run socket tests')
class NativeArchiveTests(StarterBase):
    def setUp(self):
        StarterBase.setUp(self)
        from e2e_checks_update_safety import previous_clone
        from types import SimpleNamespace
        self.archive=previous_clone(SimpleNamespace(base=self.base))
        package=self.archive/'packages/codex-agents/native-package'
        with mock.patch('tests.test_host_commands.BUNDLE_ROOT',package), \
                mock.patch('tests.test_host_commands.PACKAGE_VERSION','0.8.66'):
            self.project=install_package(self.base/'P').resolve()
        (self.project/'.git').mkdir()
        self.older=self.project/'.codex/orchestration'
        self.state.collector_authorization.unlink()
        code='''import sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from codex_agents.learning_store import LearningState,LearningLedger,authorize_collector_runtime
state=LearningState(Path(sys.argv[2]),create=False)
owner=LearningLedger(state)
try: print(json.dumps(owner.register_project(Path(sys.argv[3]))))
finally:owner.close()
authorize_collector_runtime(state,Path(sys.argv[1])/'cli.py',project_root=Path(sys.argv[3]))
'''
        made=subprocess.run([sys.executable,'-I','-B','-c',code,str(self.older),str(self.state.root),str(self.project)],
            env={'PATH':os.defpath,'TMPDIR':str(self.base)},capture_output=True,text=True,timeout=30)
        self.assertEqual(0,made.returncode,made.stderr)
        self.project_id=json.loads(made.stdout)
        self.launch(orchestration=self.older)
        from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
        self.host=subprocess.Popen([sys.executable,*PYTHON_ISOLATION_FLAGS,str(self.older/'project_host.py'),
            '--state-root',str(self.state.root),'--project-root',str(self.project)],
            env={'PATH':os.defpath,'TMPDIR':str(self.base)},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.held.append(self.host)
        from codex_agents.project_host_client import _hinted_host
        wait_for(self,lambda:(_hinted_host(self.paths,self.project,any_runtime=True) or (None,None,False))[2],30,
                 lambda:f'archived host did not serve: {self.host.poll()}')

    def relaunch(self):
        # The account start after the hand-over: the stopped predecessor collector is kept for
        # reaping (stop_all), then this build's collector starts.
        if self.collector is not None:
            self.held.append(self.collector)
        self.launch()

    @requirements('RS-B-B4t-0','RS-B-B4t-3',why_not_scenario='Real archived collector/service, native peer credentials and lifetime locks.')
    def test_B4t_A1_idle_predecessor_updates_without_process_proof(self):
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.takeover_owner import OwnerOperations,run_owner_takeover
        from tests.support import ORCHESTRATION_ROOT
        from codex_agents.takeover_process import Kernel
        # Native (a), then injected EPERM (b). 146955fc refuses at read_plan
        # before intent because its census cannot inspect real Mac processes.
        native=read_plan(self.state,self.project)
        self.assertEqual('signal',native['report']['service_stop'])
        with mock.patch.object(Kernel,'identity',side_effect=PermissionError('EPERM')):
            plan=read_plan(self.state,self.project)
        self.assertEqual('hint',plan['report']['service_stop'])
        ops=OwnerOperations(self.state,ORCHESTRATION_ROOT/'cli.py',self.project,lambda:plan['intent'])
        # Only the existing test-home launcher is substituted for account start;
        # preflight, stop, flocks, both owner opens, migrations and authorization
        # are product code. No signal may be used for an idle predecessor.
        def start(document):
            self.relaunch()
        ops.start_collector=start
        with mock.patch('os.kill',side_effect=AssertionError('idle service signalled')), \
                mock.patch('os.killpg',side_effect=AssertionError('idle group signalled')), \
                mock.patch.object(signal,'pidfd_send_signal',create=True,side_effect=AssertionError('idle pidfd signal')):
            done=run_owner_takeover(ops)
        self.assertEqual('account_done',done['phase'])
        self.host.wait(timeout=5)
        self.assertEqual(0,self.host.returncode)
        from codex_agents.collector_client import CollectorClient,COLLECTOR_RUNTIME_DIGEST
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST,CollectorClient(self.state.socket).health()['runtime_digest'])

    @requirements('RS-B-B4t-0','RS-B-B4t-3',why_not_scenario='Real archived collector/service, native peer credentials and lifetime locks.')
    def test_B4t_A1_idle_predecessor_with_native_identity_updates_without_a_signal(self):
        from codex_agents.takeover_preflight import read_plan
        from codex_agents.takeover_owner import OwnerOperations,run_owner_takeover
        from tests.support import ORCHESTRATION_ROOT
        from codex_agents.takeover_process import Kernel
        # Native (a), then injected EPERM (b). 146955fc refuses at read_plan
        # before intent because its census cannot inspect real Mac processes.
        native=read_plan(self.state,self.project)
        self.assertEqual('signal',native['report']['service_stop'])
        plan=native
        self.assertEqual('signal',plan['report']['service_stop'])
        ops=OwnerOperations(self.state,ORCHESTRATION_ROOT/'cli.py',self.project,lambda:plan['intent'])
        # Only the existing test-home launcher is substituted for account start;
        # preflight, stop, flocks, both owner opens, migrations and authorization
        # are product code. No signal may be used for an idle predecessor.
        def start(document):
            self.relaunch()
        ops.start_collector=start
        with mock.patch('os.kill',side_effect=AssertionError('idle service signalled')), \
                mock.patch('os.killpg',side_effect=AssertionError('idle group signalled')), \
                mock.patch.object(signal,'pidfd_send_signal',create=True,side_effect=AssertionError('idle pidfd signal')):
            done=run_owner_takeover(ops)
        self.assertEqual('account_done',done['phase'])
        self.host.wait(timeout=5)
        self.assertEqual(0,self.host.returncode)
        from codex_agents.collector_client import CollectorClient,COLLECTOR_RUNTIME_DIGEST
        self.assertEqual(COLLECTOR_RUNTIME_DIGEST,CollectorClient(self.state.socket).health()['runtime_digest'])

    @requirements('RS-B-B4t-3','RS-B-B4t-9',why_not_scenario='Real 0.8.66 service on the native kernel, with system processes present.')
    def test_B4t_A1_no_other_process_is_inspected(self):
        from codex_agents.takeover_preflight import read_plan
        from codex_agents import takeover_process
        # Absence of the census is also statically guarded in T-36's component
        # half; this is the real peer/argv pin that the earlier census blocked.
        plan=read_plan(self.state,self.project)
        self.assertEqual(self.host.pid,plan['intent']['predecessor_service']['pid'])
        self.assertEqual('signal',plan['report']['service_stop'])

class ArchivedServiceAudit:
    """T-43 (b), run by tests.test_b4t_native_audit_p66 and tests.test_b4t_native_audit_v34: one archived
    service per module keeps each within the 60 s scenario budget (S5 final fix)."""

    def audit_revision(self,revision):
        # A1.7 item 7 / TL: this audit belongs to the service's own pid. Worker
        # processes may exec; an import-closure prohibition would be false.
        import e2e_support
        import e2e_checks_update_safety as update
        import e2e_checks_shared as shared
        from codex_agents.learning_store import LearningState
        from codex_agents.project_host_client import _hinted_host
        from codex_agents.takeover_preflight import _peer
        from codex_agents.takeover_process import Kernel
        from codex_agents.process_tree import process_gone
        from codex_agents.collector_paths import CollectorPaths
        from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
        with e2e_support.World() as world:
            update.trusted_interpreter_checks(world)
            update.write_patches(world,[])
            if revision=='afb6f610':
                clone=update.previous_clone(world)
                update.set_up_previous(world,clone)
            else:
                # S5 final fix: a 0.3.x release is set up by its own installer, registration and
                # attestation (the L12.1 path); the 0.8.6x scripted wizard answers do not apply to it.
                import e2e_checks_update_history as history
                try:
                    clone=history.archive_tag(world,revision)
                except e2e_support.HarnessError:
                    self.skipTest('0.8.32 archive unavailable offline; primary must supply v0.3.4 for this native acceptance witness')
                history.set_up_release(world,revision)
            state=LearningState(shared.state_dir(world),create=False)
            paths=CollectorPaths(state.root)  # hint readers take the client paths type only
            if revision=='afb6f610':
                # 0.8.66's collector starter has started the project service after setup.
                hint=wait_for(self,lambda:(lambda found:found if found is not None and found[2] is True else None)(
                    _hinted_host(paths,world.project,any_runtime=True)),30,'the archived service never served')
                pid=_peer(hint[1][1])
                # The World runs every product process through its stand-in interpreter (boot.py), so
                # this fixture service does not present A1 I3's launch line and pin_predecessor rightly
                # refuses it. It is stopped by its exact peer pid, after its arguments name the archived
                # entry with this World's own roots (never a pattern kill).
                argv=Kernel().identity(pid)['argv']
                boot=str(world.home/'.e2e-bin'/'boot.py')
                self.assertIn(boot,argv)
                entry=Path(argv[argv.index(boot)+1])
                self.assertEqual('project_host.py',entry.name)
                self.assertTrue(entry.resolve().is_relative_to(world.base.resolve()),argv)
                self.assertFalse(entry.resolve().is_relative_to(Path(__file__).resolve().parents[3]),argv)
                self.assertEqual(['--state-root',str(state.root),'--project-root',str(world.project)],
                                 argv[argv.index(boot)+2:])
                os.kill(pid,signal.SIGTERM)
                wait_for(self,lambda:process_gone(pid),35,'the fixture service did not stop')
            else:
                # 0.3.4 has no collector starter: its service starts on the first command that needs
                # it (V34 cli.py `_start_project_host`), here the child run's orchestration open, which
                # finds the audited service below already serving. Nothing runs yet, so nothing is stopped.
                hinted=_hinted_host(paths,world.project,any_runtime=True)
                self.assertFalse(hinted is not None and hinted[2] is True)
                entry=world.project/'.codex/orchestration/project_host.py'
                self.assertTrue(entry.is_file())
            log=world.base/'service-audit.jsonl'
            code='''import os,sys,runpy,json
from pathlib import Path
entry,log,*args=sys.argv[1:]
service_pid=os.getpid()
def audit(event,arguments):
 if os.getpid()==service_pid and event in ('os.exec','os.posix_spawn','subprocess.Popen'):
  with open(log,'a') as output:output.write(json.dumps({'pid':service_pid,'event':event})+'\\n')
sys.addaudithook(audit)
sys.path.insert(0,str(Path(entry).parent))
sys.argv=[entry,*args]
runpy.run_path(entry,run_name='__main__')
'''
            # The audited service runs through the same stand-in interpreter as the World's own
            # services, so its account home is the World's (never the real one).
            host=subprocess.Popen([str(world.shim),*PYTHON_ISOLATION_FLAGS,'-c',code,str(entry),str(log),
                '--state-root',str(state.root),'--project-root',str(world.project)],
                env=e2e_support.child_environment(world.env),stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                wait_for(self,lambda:(_hinted_host(paths,world.project,any_runtime=True) or (None,None,False))[2],30,
                         lambda:f'the audited service never served: {host.poll()}')
                self.assertEqual(host.pid,_peer(_hinted_host(paths,world.project,any_runtime=True)[1][1]))
                tools=contextlib.nullcontext()
                if revision!='afb6f610':
                    # The fake Claude client announces the tools its bridge allows: V34's are
                    # Read/Glob/Grep (V34 bridge.py CLAUDE_TOOLS), without the later StructuredOutput.
                    tools=mock.patch.dict(shared.INIT,{'tools':['Read','Glob','Grep']})
                with tools:
                    work=update.run_a_child(world,'exec-audit')
                self.assertEqual(0,work.run.code)
                self.assertEqual('completed',work.run.reply['result']['execution_state'],json.dumps(work.run.reply)[-3000:])
                host.terminate();host.wait(timeout=35)
                events=[json.loads(line) for line in log.read_text().splitlines()]
                self.assertTrue(events,'a real claim/child must emit process-start audit events')
                self.assertTrue(all(row['pid']==host.pid for row in events))
                self.assertNotIn('os.exec',[row['event'] for row in events])
                self.assertTrue({'os.posix_spawn','subprocess.Popen'} & {row['event'] for row in events})
            finally:
                if host.poll() is None:host.kill()
                host.wait(timeout=5)


class ResidualStub:
    """A1.7 item 6 accepted-residual cases; the TERM-ignoring case is its own module
    (tests.test_b4t_native_kill) because its real 30 s TERM bound alone fills half the budget."""

    def run_stub(self,*,ignore_term=False,held_history=False):
        # A1.7 item 6 / TL: this pins the accepted outcome. The hash attests the
        # unchanged entry file, not this same-user program's executable image.
        import shutil
        import fcntl
        from codex_agents.takeover_process import Kernel,pin_predecessor
        from codex_agents.takeover_preflight import _peer,read_plan,trusted_package
        from codex_agents.project_host_client import _hinted_host
        from codex_agents.client_runtime import runtime_hint_path,runtime_directory
        from codex_agents.takeover_owner import OwnerOperations,run_owner_takeover
        from codex_agents.update_takeover import TakeoverError
        from codex_agents.host_paths import PYTHON_ISOLATION_FLAGS
        from tests.support import ORCHESTRATION_ROOT
        from codex_agents import takeover_owner
        self.assertEqual(30.0,takeover_owner.SERVICE_SELF_STOP_SECONDS)
        compiler=shutil.which('cc')
        if compiler is None:self.skipTest('Native accepted-residual fixture needs a C compiler; no substitute process proof is claimed')
        hint=runtime_hint_path(self.paths,self.project);hint_bytes=hint.read_bytes()
        entry=self.older/'project_host.py';entry_bytes=entry.read_bytes()
        pinned=pin_predecessor(self.host.pid,entry=entry,state_root=self.state.root,project_root=self.project)
        pinned.stop();self.host.wait(timeout=5)
        directory=runtime_directory(self.state,self.project)
        source=self.base/'same-user-stub.c';binary=self.base/'same-user-stub';ready=self.base/'stub-ready'
        source.write_text(r'''#include <sys/socket.h>
#include <sys/un.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/select.h>
#include <fcntl.h>
#include <signal.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
int main(int argc,char **argv){
 char path[4096];const char *dir=getenv("B4T_STUB_DIRECTORY");
 snprintf(path,sizeof(path),"%s/host.lock",dir);int lock=open(path,O_RDWR|O_CREAT,0600);
 if(lock<0||flock(lock,LOCK_EX))return 10;
 int sockets[2];const char *names[]={"c.sock","p.sock"};
 for(int i=0;i<2;i++){
  sockets[i]=socket(AF_UNIX,SOCK_STREAM,0);struct sockaddr_un address;memset(&address,0,sizeof(address));
  address.sun_family=AF_UNIX;snprintf(address.sun_path,sizeof(address.sun_path),"%s/%s",dir,names[i]);
  unlink(address.sun_path);if(bind(sockets[i],(struct sockaddr*)&address,sizeof(address)))return 11;
  chmod(address.sun_path,0600);if(listen(sockets[i],16))return 12;
 }
 if(getenv("B4T_STUB_IGNORE_TERM"))signal(SIGTERM,SIG_IGN);
 int fd=open(getenv("B4T_STUB_READY"),O_WRONLY|O_CREAT,0600);if(fd<0)return 13;close(fd);
 /* Like a real service, hold each accepted connection until its client closes it: the
    caller reads its peer's credentials after connect, and macOS reports none once the
    listener's side is closed (S5 final fix: closing at once made that read racy). */
 int clients[64];int count=0;
 for(;;){fd_set fds;FD_ZERO(&fds);FD_SET(sockets[0],&fds);FD_SET(sockets[1],&fds);
  int high=sockets[0]>sockets[1]?sockets[0]:sockets[1];
  for(int i=0;i<count;i++){FD_SET(clients[i],&fds);if(clients[i]>high)high=clients[i];}
  if(select(high+1,&fds,0,0,0)<0)continue;
  for(int i=0;i<count;){char byte[256];
   if(FD_ISSET(clients[i],&fds)&&read(clients[i],byte,sizeof(byte))<=0){close(clients[i]);clients[i]=clients[--count];}
   else i++;}
  for(int i=0;i<2;i++)if(FD_ISSET(sockets[i],&fds)){int client=accept(sockets[i],0,0);
   if(client>=0){if(count<64)clients[count++]=client;else close(client);}}
 }
}
''')
        built=subprocess.run([compiler,str(source),'-o',str(binary)],capture_output=True,text=True,timeout=60)
        self.assertEqual(0,built.returncode,built.stderr)
        expected=[sys.executable,*PYTHON_ISOLATION_FLAGS,str(entry),'--state-root',str(self.state.root),
                  '--project-root',str(self.project)]
        env={'PATH':os.defpath,'B4T_STUB_DIRECTORY':str(directory),'B4T_STUB_READY':str(ready)}
        if ignore_term:env['B4T_STUB_IGNORE_TERM']='1'
        stub=subprocess.Popen([sys.executable,'-I','-B','-c','import os,sys;os.execv(sys.argv[1],sys.argv[2:])',
                               str(binary),*expected],env=env,stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.held.append(stub)
        wait_for(self,ready.exists,20,lambda:f'stub did not bind: {stub.poll()}')
        hint.write_bytes(hint_bytes);hint.chmod(0o600)
        self.assertEqual(entry_bytes,entry.read_bytes())
        self.assertEqual('0.8.66',trusted_package(self.project)['version'])
        holder=None
        if held_history:
            holder=os.open(self.state.root/'projects'/self.project_id/'history.lock',os.O_RDWR)
            fcntl.flock(holder,fcntl.LOCK_EX)
        try:
            plan=read_plan(self.state,self.project)
            self.assertEqual('signal',plan['report']['service_stop'])
            self.assertEqual(stub.pid,plan['intent']['predecessor_service']['pid'])
            ops=OwnerOperations(self.state,ORCHESTRATION_ROOT/'cli.py',self.project,lambda:plan['intent'])
            ops.start_collector=lambda document:self.relaunch()
            sent=[];times=[];real_send=Kernel.send
            def send(kernel,pin,pid,number):
                sent.append((pid,number));times.append(time.monotonic());return real_send(kernel,pin,pid,number)
            # S5 final fix: the stub never stops by itself, so the T3 wait for a withdrawn idle
            # service to stop on its own (A1.5 row 3) always runs out before the proven signal. That
            # wait is pinned at its product value above and shortened here; the TERM/KILL bounds
            # that these cases witness are not changed.
            with mock.patch.object(Kernel,'send',send), \
                    mock.patch.object(takeover_owner,'SERVICE_SELF_STOP_SECONDS',1.0):
                if holder is not None:
                    # macOS cannot name this unrelated holder before J1. Linux
                    # I5 refuses at T0 instead (covered by its product witness).
                    if sys.platform.startswith('linux'):
                        self.fail('Linux must exercise its write-free I5 path before run_owner_takeover')
                    with self.assertRaises(TakeoverError) as caught:run_owner_takeover(ops)
                    self.assertEqual('predecessor_lock_inherited',caught.exception.code)
                else:
                    self.assertEqual('account_done',run_owner_takeover(ops)['phase'])
            self.assertEqual([stub.pid],sorted({pid for pid,number in sent}))
            self.assertEqual([signal.SIGTERM,signal.SIGKILL] if ignore_term else [signal.SIGTERM],
                             [number for pid,number in sent])
            if ignore_term:  # KILL only after the whole real TERM bound (PinnedPredecessor.stop: 30 s)
                self.assertGreaterEqual(times[1]-times[0],29.9)
            self.assertEqual(entry_bytes,entry.read_bytes())
            stub.wait(timeout=5)
            if holder is not None:
                os.close(holder);holder=None
                resumed=OwnerOperations(self.state,ORCHESTRATION_ROOT/'cli.py',self.project,lambda:plan['intent'])
                resumed.start_collector=lambda document:self.relaunch()
                self.assertEqual('account_done',run_owner_takeover(resumed)['phase'])
        finally:
            if holder is not None:os.close(holder)

@unittest.skipIf(os.environ.get('COCOLO_TEST_B4T_SKIP_SOCKETS') == '1',
                 'Primary must rerun: accepted same-user residual uses real Unix endpoints')
class AcceptedResidualStubTests(ResidualStub,StarterBase):
    setUp=NativeArchiveTests.setUp
    relaunch=NativeArchiveTests.relaunch

    @requirements('RS-B-B4t-3','RS-B-B4t-9',why_not_scenario='A same-user C stub presents the exact archived arguments over the real project endpoint; only lifetime flocks permit adoption.')
    def test_same_user_impostor_with_expected_arguments_is_the_only_signal_target(self):
        self.run_stub()

    @requirements('RS-B-B4t-3','RS-B-B4t-9',why_not_scenario='Unchanged on-disk entry hash cannot attest running code; a separately held flock still forbids adoption.')
    @unittest.skipUnless(sys.platform=='darwin','macOS unnamed-holder residual; Linux has the separate I5 write-free witness')
    def test_same_user_impostor_with_unchanged_entry_hash_and_held_lock_refuses_adoption(self):
        self.run_stub(held_history=True)
