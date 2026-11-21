"""Core API round 6 (review rev040tm0apir6, verified): each test names its finding and was red on 8a6019d1 first.

M1 money filter: a single-digit price ("charged $5 per month") is redacted; only a shell positional parameter stays.
M2 ids.json: two services on one records folder never hand out the same id (real separate processes).
M3 a connection-audit failure reaches the caller; text that cannot be encoded is invalid_params, audited, in both adapters.
M4 transport, in-process and connection refusals obey a bounded audit budget (no unbounded lock wait).
M5 refusal rotation and append share one interprocess lock: an acknowledged refusal row is never lost to a rotation.
M6 a cut read never answers not_found for a target that may sit in the omitted prefix: it answers unavailable.
M7 a refusal raised long after the call's deadline is still audited (its own short budget, not deadline + grace).
"""
import json
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
from unittest import mock

from tests.core_api_support import CoreCase, jsonl, read_jsonl
from tests.test_core_api_round5 import Helpers, raw

LONE_SURROGATE = '"\\ud800"'  # JSON text for a lone surrogate, as a client could send it


def _code_path(mod):
    return os.path.dirname(os.path.dirname(os.path.abspath(mod.__file__)))


class MoneyDollarSingleDigit(Helpers, CoreCase):
    def test_a_single_digit_price_is_removed_but_a_shell_parameter_stays(self):  # M1, TERM-36
        amounts = {"A-1": ("charged $5 per month", "5"), "A-2": ("a $5 fee", "5"), "A-3": ("it costs $3", "3"),
                   "A-4": ("$9", "9"), "A-5": ("we paid $7.", "7")}
        kept = {"K-1": "echo $1", "K-2": "echo $1 $2", "K-3": 'printf "%s" "$1"'}
        rows = [{"id": k, "ts": 1, "role": "orchestrator", "text": t} for k, (t, _) in amounts.items()]
        rows += [{"id": k, "ts": 1, "role": "orchestrator", "text": t} for k, t in kept.items()]
        jsonl(self.records / "messages.jsonl", rows)
        got = {m["id"]: m["text"] for m in self.client("read").call("messages.list", {"limit": 200})["messages"]}
        for key, (text, number) in amounts.items():
            with self.subTest(text):
                self.assertNotIn(number, got[key])
        for key, text in kept.items():
            with self.subTest(text):
                self.assertEqual(text, got[key])


ID_CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from codex_agents import core_api
records, flag, count = sys.argv[2], sys.argv[3], int(sys.argv[4])
service = core_api.CoreService(records, os.path.join(records, "x.sock"), expected_uid=os.getuid())
while not os.path.exists(flag):
    pass
for _ in range(count):
    with service._lock:
        print(service._allocate_id("C"), flush=True)
"""


class IdsAcrossProcesses(Helpers, CoreCase):
    start_service = False

    def test_ids_are_unique_across_services_on_one_records_folder(self):  # M2
        flag = str(self.dir / "go")
        kids = [subprocess.Popen([sys.executable, "-c", ID_CHILD, _code_path(self.mod), str(self.records), flag, "40"],
                                 stdout=subprocess.PIPE, text=True) for _ in range(3)]
        self.addCleanup(lambda: [k.kill() for k in kids if k.poll() is None])
        open(flag, "w").close()
        ids = []
        for kid in kids:
            out, _ = kid.communicate(timeout=60)
            self.assertEqual(0, kid.returncode)
            ids += out.split()
        self.assertEqual(120, len(ids))
        self.assertEqual(len(ids), len(set(ids)), "duplicate ids were handed out")


class ConnectionAuditFailure(Helpers, CoreCase):
    service_options = {"max_connections": 1}

    def test_a_failed_connection_audit_is_not_swallowed(self):  # M3, API-23
        self.client("read")  # holds the only connection slot
        (self.records / "audit.jsonl").mkdir()  # the refusal row cannot be written
        s = raw(self.socket)
        reply = json.loads(s.makefile().readline())
        self.assertEqual("unavailable", reply["error"]["data"]["code"])


class FailureBoundary(Helpers, CoreCase):
    def test_text_that_cannot_be_encoded_is_an_audited_invalid_params_on_the_wire(self):  # M3, API-23
        token = self.mod._mint_token(str(self.records), "agent")
        for method, text_key in (("context.push", "text"), ("query.ask", "text")):
            with self.subTest(method):
                s = raw(self.socket)
                reader = s.makefile()
                s.sendall(('{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocol":"1.0","token":"%s"}}\n'
                           % token).encode())
                reader.readline()
                s.sendall(('{"jsonrpc":"2.0","id":2,"method":"%s","params":{"kind":"note","%s":%s}}\n'
                           % (method, text_key, LONE_SURROGATE)).encode())
                reply = json.loads(reader.readline())
                self.assertEqual("invalid_params", reply["error"]["data"]["code"])
                self.assertIn((method, "invalid_params"),
                              {(r["method"], r["outcome"]) for r in self.refusals()})

    def test_text_that_cannot_be_encoded_is_an_api_error_in_process(self):  # M3 (adapter parity)
        client = self.inproc("agent")
        with self.assertRaises(self.mod.ApiError) as raised:
            client.call("context.push", {"kind": "note", "text": "a\ud800b"})
        self.assertEqual("invalid_params", raised.exception.code)
        self.assertIn(("context.push", "invalid_params"), {(r["method"], r["outcome"]) for r in self.refusals()})

    def test_an_unexpected_handler_failure_is_an_audited_internal_error(self):  # M3, API-23
        reader = self.client("read")
        with mock.patch.object(self.service, "_latest_units", side_effect=RuntimeError("boom")):
            self.assertEqual("internal_error", self.error_code(reader, "units.list"))
        self.assertIn(("units.list", "internal_error"), {(r["method"], r["outcome"]) for r in self.refusals()})


class RefusalBudget(Helpers, CoreCase):
    service_options = {"method_seconds": 0.3, "lock_seconds": 3.0}

    def hold(self, lock, seconds):
        held, release = threading.Event(), threading.Event()

        def run():
            with lock:
                held.set()
                release.wait(seconds)
        threading.Thread(target=run, daemon=True).start()
        held.wait(2)
        self.addCleanup(release.set)
        return release

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(self.mod, "AUDIT_GRACE_SECONDS", 0.2)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_transport_refusal_waits_a_bounded_time_for_the_audit(self):  # M4, API-24
        s = raw(self.socket)
        self.hold(self.service._audit_lock, 3.0)
        started = time.monotonic()
        s.sendall(b"this is not json\n")
        reply = json.loads(s.makefile().readline())
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn(reply["error"]["data"]["code"], ("timeout", "unavailable"))

    def test_an_in_process_refusal_waits_a_bounded_time_for_the_audit(self):  # M4, API-24
        client = self.inproc("agent")
        self.hold(self.service._audit_lock, 3.0)
        started = time.monotonic()
        with self.assertRaises(self.mod.ApiError) as raised:
            client.call("context.push", {"kind": "note", "text": "x" * (2 << 20)})
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn(raised.exception.code, ("timeout", "unavailable"))

    def test_a_connection_audit_waits_a_bounded_time(self):  # M4, API-24
        self.hold(self.service._audit_lock, 3.0)
        started = time.monotonic()
        with self.assertRaises(self.mod.ApiError):
            self.service._audit_connection("too_many_connections")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_refusal_raised_long_after_the_deadline_is_still_audited(self):  # M7, API-23, API-24
        real = self.service._latest_units

        def slow():
            time.sleep(0.8)  # longer than the deadline (0.3 s) plus the grace (0.2 s)
            return real()
        reader = self.client("read")
        with mock.patch.object(self.service, "_latest_units", slow):
            self.assertEqual("timeout", self.error_code(reader, "units.list"))
        self.assertIn(("units.list", "timeout"), {(r["method"], r["outcome"]) for r in self.refusals()})


ROTATE_CHILD = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
from codex_agents import core_api
records = sys.argv[2]
service = core_api.CoreService(records, os.path.join(records, "y.sock"), expected_uid=os.getuid(), audit_max_bytes=1)
service._audit_write({"ts": 1, "caller": "b", "method": "other", "outcome": "one"}, change=False)
print("R1", flush=True)
sys.stdin.readline()
service._audit_write({"ts": 2, "caller": "b", "method": "other", "outcome": "two"}, change=False)
print("R2", flush=True)
"""


class RotationLock(Helpers, CoreCase):
    def test_a_refusal_row_in_flight_is_not_lost_to_another_services_rotation(self):  # M5, API-23
        self.service._audit_write({"ts": 0, "caller": "a", "method": "seed", "outcome": "seed"}, change=False)
        audit = self.records / "audit.jsonl"
        marker = {"ts": 5, "caller": "a", "method": "marker", "outcome": "forbidden"}
        state = {"proc": None, "sent": False}
        real_flock = self.mod.fcntl.flock

        def line(timeout):
            ready, _, _ = select.select([state["proc"].stdout], [], [], timeout)
            return state["proc"].stdout.readline().strip() if ready else None

        def hooked(fd, operation):
            real_flock(fd, operation)
            if state["proc"] is None and os.fstat(fd).st_ino == os.stat(audit).st_ino:
                state["proc"] = subprocess.Popen(
                    [sys.executable, "-c", ROTATE_CHILD, _code_path(self.mod), str(self.records)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
                self.addCleanup(lambda: state["proc"].kill())
                # The other service tries to rotate twice while this row is between open and write. With a shared
                # lock it waits (no line within the wait); without one it finishes both rotations.
                if line(2.0) == "R1":
                    state["proc"].stdin.write("go\n")
                    state["proc"].stdin.flush()
                    state["sent"] = True
                    line(2.0)

        with mock.patch.object(self.mod.fcntl, "flock", hooked):
            self.service._audit_write(marker, change=False)
        kept = read_jsonl(self.records / "audit.jsonl.1") + read_jsonl(audit)
        self.assertIn("marker", {r["method"] for r in kept}, "the acknowledged row is in neither retained file")
        if not state["sent"]:
            state["proc"].stdin.write("go\n")
            state["proc"].stdin.flush()
        state["proc"].communicate(timeout=30)


class CutReadsAreUnknown(Helpers, CoreCase):
    def pad(self, name, rows):
        jsonl(self.records / name, rows)

    def test_units_history_of_a_unit_before_the_retained_tail_is_unavailable(self):  # M6, API-8
        self.pad("units.jsonl", [{"id": "U-0001", "state": "done"}]
                 + [{"id": "U-9%03d" % i, "state": "running", "pad": "y" * 60} for i in range(100)])
        client = self.client("read")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            self.assertEqual("unavailable", self.error_code(client, "units.history", {"id": "U-0001"}))
        self.assertEqual("not_found", self.error_code(client, "units.history", {"id": "U-0777"}))  # whole record

    def test_a_query_before_the_retained_tail_is_unavailable(self):  # M6, API-8
        self.pad("inbox.jsonl", [{"query_id": "Q-0001", "kind": "query", "text": "q", "ts": 1}]
                 + [{"push_id": "P-%04d" % i, "kind": "note", "text": "p" * 60, "ts": 1} for i in range(100)])
        client = self.client("read")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            self.assertEqual("unavailable", self.error_code(client, "queries.get", {"id": "Q-0001"}))
        self.assertEqual("not_found", self.error_code(client, "queries.get", {"id": "Q-0777"}))

    def test_a_diff_before_the_retained_tail_is_unavailable(self):  # M6, API-8
        self.pad("diffs.jsonl", [{"path": "a.py", "diff": "+x\n", "ts": 1}]
                 + [{"path": "b%d.py" % i, "diff": "+" + "d" * 60, "ts": 1} for i in range(100)])
        client = self.client("read")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            self.assertEqual("unavailable", self.error_code(client, "changes.diff", {"path": "a.py"}))
        self.assertEqual("not_found", self.error_code(client, "changes.diff", {"path": "zz.py"}))
