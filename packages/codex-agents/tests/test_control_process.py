from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import asdict
from pathlib import Path

import tests.support  # noqa: F401
from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.test_project_monitor import binding, scope

from codex_agents.control_api import ControlClient, ControlError, ControlScope
from codex_agents.service_protocol import ServiceTarget


# Both private listeners inject fixture-owned context. These process tests prove
# isolation/known cleanup behavior, not native caller enrollment or admission.
CONTROL_OWNER = r'''
import json,os,queue,select,signal,socket,subprocess,sys,threading,time
from dataclasses import asdict
from pathlib import Path
from codex_agents.control_api import ControlScope
from codex_agents.control_service import ControlService,ControlAuthorization
from codex_agents.history_contracts import AttemptBinding
from codex_agents.liveness_clock import LivenessLimits
from codex_agents.process_tree import ProcessTree,process_record,process_table
from codex_agents.project_monitor import ProjectMonitor,SupervisorScope
from codex_agents.service_protocol import service_request_digest
endpoint,binding_json,deadline_seconds=sys.argv[1:]
binding=AttemptBinding(**json.loads(binding_json))
names=('project_id','primary_session_id','runtime_digest','supervisor_instance_id','supervisor_generation')
scope=SupervisorScope(**{name:getattr(binding,name) for name in names})
offset=[0]
def now():return time.monotonic_ns()+offset[0]
monitor=ProjectMonitor(scope,LivenessLimits(50_000_000_000,5_000_000_000,10_000_000_000,30_000_000_000),
 max_tracked_attempts=4,clock_ns=now)
monitor.register(binding,hard_deadline_ns=now()+int(deadline_seconds)*1_000_000_000)
context=object();control_scope=ControlScope(**asdict(scope))
def authorize(request,token):
 if token is not context:return None
 return ControlAuthorization(control_scope,request.target,service_request_digest(request),frozenset({'project.status','project.cancel'}))
service=ControlService(monitor,authorizer=authorize)
program="import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-I','-B','-S','-c','import time;time.sleep(60)']);print('tree-ready',flush=True);time.sleep(60)"
child=subprocess.Popen([sys.executable,'-I','-B','-S','-c',program],stdin=subprocess.DEVNULL,
 stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True,close_fds=True)
tree=ProcessTree(child.pid);initial=process_record(child.pid);birth=initial[2] if initial else None
cleanup=queue.Queue();worker=None;cleanup_confirmed=False;known_tracked=0
listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
def terminate_tree():
 try:
  tree.terminate();child.wait(timeout=2)
  gone=not any((record:=tree._record(pid)) is not None and not record[3] for pid in tuple(tree.tracked))
  cleanup.put(gone)
 except Exception:cleanup.put(False)
try:
 if not select.select([child.stdout],[],[],2)[0] or child.stdout.readline().strip()!='tree-ready':
  raise RuntimeError('owned child startup unavailable')
 if birth is None or not tree.identify(process_table(),birth) or len(tree.tracked)<2:
  raise RuntimeError('owned tree not identified')
 known_tracked=len(tree.tracked);monitor.startup(binding)
 listener.bind(endpoint);listener.listen(8);listener.setblocking(False)
 print('ready',flush=True)
 while True:
  ready,_,_=select.select([sys.stdin,listener],[],[],0.01)
  if sys.stdin in ready:
   command=sys.stdin.readline().strip()
   if command=='stop':break
   if command=='advance':
    offset[0]+=31_000_000_000
    print('advanced',flush=True)
  for status in monitor.tick():
   if (status.stop_requested or status.terminal_observed) and not status.cleanup_confirmed and worker is None:
    worker=threading.Thread(target=terminate_tree);worker.start()
  try:confirmed=cleanup.get_nowait()
  except queue.Empty:confirmed=None
  if confirmed is not None:
   if confirmed:
    monitor.observe_terminal(binding);monitor.observe_cleanup(binding);cleanup_confirmed=True
    print('cleaned',flush=True)
   else:raise RuntimeError('owned cleanup unconfirmed')
  if listener not in ready:continue
  connection,_=listener.accept()
  with connection:
   deadline=time.monotonic()+0.2;raw=bytearray()
   while b'\n' not in raw and len(raw)<16384:
    connection.settimeout(max(0.001,deadline-time.monotonic()))
    chunk=connection.recv(min(8192,16384-len(raw)))
    if not chunk:break
    raw.extend(chunk)
    if time.monotonic()>=deadline:break
   reply=service.handle(bytes(raw).split(b'\n',1)[0],context)
   connection.sendall(reply+b'\n')
finally:
 service.close();listener.close();Path(endpoint).unlink(missing_ok=True)
 if worker is not None:worker.join(timeout=3)
 if child.poll() is None:
  if tree.identified:tree.terminate()
  else:
   current=process_record(child.pid)
   if current is not None and current[2]==birth:os.killpg(child.pid,signal.SIGKILL)
  child.wait(timeout=2)
 child.communicate(timeout=2)
 print(json.dumps({'root_reaped':child.returncode is not None,'known_tracked':known_tracked,
                   'cleanup_confirmed':cleanup_confirmed}),flush=True)
'''

PERSISTENCE_OWNER = r'''
import json,select,sys
from pathlib import Path
from codex_agents.history_contracts import AttemptBinding
from codex_agents.learning_contracts import canonical_json
from codex_agents.project_history import ProjectHistory
from codex_agents.result_contracts import MAX_RESULT_BODY_BYTES,ResultCapacity,ResultObservation
directory,data_json=sys.argv[1:];data=json.loads(data_json);binding=AttemptBinding(**data['binding'])
directory=Path(directory);directory.mkdir(mode=0o700)
store=ProjectHistory(directory,binding.project_id,result_key=b'r'*32,
 result_capacity=ResultCapacity(4,MAX_RESULT_BODY_BYTES,60000,120000),result_clock_ms=lambda:1000)
try:
 store.reserve_result(binding,data['assignment'],hard_deadline_at_ms=2000)
 original=store._write_result_file
 def stall(*args):
  # Deliberate fault injection after durable preparation: hold an actual SQLite
  # writer and block the file publication callback together. Production writes
  # do not intentionally keep this transaction over artifact I/O.
  store._connection.execute('BEGIN IMMEDIATE')
  print('publication-stalled',flush=True)
  try:
   if not select.select([sys.stdin],[],[],6)[0] or sys.stdin.readline().strip()!='release':
    raise RuntimeError('bounded fixture release unavailable')
  finally:store._connection.execute('ROLLBACK')
  return original(*args)
 store._write_result_file=stall
 result=store.publish_result(binding,data['assignment'],canonical_json(data['result']),
                            ResultObservation(**data['observation']))
 print(json.dumps({'published':True,'delivery_sequence':result.delivery_sequence}),flush=True)
finally:store.close()
'''


class ControlProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.line_buffers = {}

    def start(self, program, args):
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-S", "-c",
             f"import sys;sys.path.insert(0,{str(ORCHESTRATION_ROOT)!r});" + program, *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, close_fds=True,
        )
        def stop():
            if process.poll() is None:
                try:
                    return process.communicate("stop\n", timeout=8)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
            return process.communicate(timeout=3)
        self.addCleanup(stop)
        return process, stop

    def line(self, process, expected, *, seconds=2):
        buffer = self.line_buffers.setdefault(process.pid, bytearray())
        deadline = time.monotonic() + seconds
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            self.assertGreater(remaining, 0, "owned fixture control timeout")
            self.assertTrue(select.select([process.stdout], [], [], remaining)[0], "owned fixture control timeout")
            chunk = os.read(process.stdout.fileno(), 4096)
            self.assertTrue(chunk, "owned fixture closed before expected metadata")
            buffer.extend(chunk)
            self.assertLessEqual(len(buffer), 8192)
        value, _, rest = buffer.partition(b"\n")
        buffer[:] = rest
        self.assertEqual(expected, value.decode("ascii"))

    def control(self, project, *, deadline=60):
        endpoint = self.base / f"{project}.sock"
        target = binding(project=project)
        process, stop = self.start(CONTROL_OWNER, [str(endpoint), json.dumps(target.as_data()), str(deadline)])
        self.line(process, "ready")
        client = ControlClient(endpoint, ControlScope(**asdict(scope(project=project))), timeout=1)
        return client, ServiceTarget(target.orchestration_handle, target.run_handle, target.attempt), process, stop

    def exercise_stall(self, *, b_deadline, expected_reason):
        # A's persistence work has a distinct attempt from the two live trees.
        # It is synthetic validated output, never a fabricated native terminal
        # assertion for either running test child.
        from tests.learning_support import assignment, result
        from tests.test_result_publication import observation

        a, target_a, process_a, stop_a = self.control("a")
        b, target_b, process_b, stop_b = self.control("b", deadline=b_deadline)
        data = {"binding": binding(99).as_data(), "assignment": assignment(),
                "result": result(), "observation": observation().as_data()}
        writer, stop_writer = self.start(PERSISTENCE_OWNER, [str(self.base / "history-a"), json.dumps(data)])
        self.line(writer, "publication-stalled")
        self.assertIsNone(writer.poll())
        started = time.monotonic()
        self.assertFalse(a.status(target_a).result["cleanup_confirmed"])
        self.assertFalse(b.status(target_b).result["cleanup_confirmed"])
        wrong = ControlClient(self.base / "a.sock", ControlScope(**asdict(scope(project="b"))), timeout=1)
        with self.assertRaisesRegex(ControlError, "control_not_authorized"):
            wrong.cancel(target_a)
        cancelled = a.cancel(target_a).result
        self.assertEqual("cancel_requested", cancelled["stop_reason"])
        self.assertEqual("stop_requested", cancelled["cancellation_state"])
        self.assertFalse(cancelled["cleanup_confirmed"])
        self.assertFalse(cancelled["durable"])
        self.assertFalse(b.status(target_b).result["stop_requested"])
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(writer.poll())
        self.line(process_a, "cleaned")
        self.assertTrue(a.status(target_a).result["cleanup_confirmed"])
        self.assertFalse(b.status(target_b).result["cleanup_confirmed"])

        # Advance only the trusted fixture clock, then make NO status request
        # until the independent timer/executor has actually cleaned B's tree.
        process_b.stdin.write("advance\n")
        process_b.stdin.flush()
        self.line(process_b, "advanced")
        self.line(process_b, "cleaned")
        timed = b.status(target_b).result
        self.assertEqual(expected_reason, timed["stop_reason"])
        self.assertTrue(timed["cleanup_confirmed"])
        self.assertEqual("cleaned", timed["health"])
        self.assertIsNone(writer.poll())

        writer.stdin.write("release\n")
        writer.stdin.flush()
        output, error = writer.communicate(timeout=3)
        self.assertEqual(0, writer.returncode, error)
        self.assertEqual({"published": True, "delivery_sequence": 1}, json.loads(output))
        for process, stop in ((process_a, stop_a), (process_b, stop_b)):
            output, error = stop()
            self.assertEqual(0, process.returncode, error)
            record = json.loads(output)
            self.assertTrue(record["root_reaped"])
            self.assertTrue(record["cleanup_confirmed"])
            self.assertGreaterEqual(record["known_tracked"], 2)
        # Already terminated, so this cleanup cannot restart or republish work.
        stop_writer()

    def test_cancel_and_silence_cleanup_continue_during_sql_and_publication_stall(self):
        self.exercise_stall(b_deadline=60, expected_reason="work_silence")

    def test_hard_deadline_cleanup_continues_during_sql_and_publication_stall(self):
        self.exercise_stall(b_deadline=20, expected_reason="absolute_deadline")


if __name__ == "__main__":
    unittest.main()
