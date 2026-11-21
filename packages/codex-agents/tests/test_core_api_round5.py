"""Core API round 5 (Codex review 040-tm0-api-r5): each test names its requirement and was red first, unless it
is marked GUARD (it passed on the old code too and pins a requirement already met).

API-23 (owner 2026-11-21): every change and confirm is a commands.jsonl line; every refusal is an audit.jsonl line
(rotated separately, so refusals cannot push changes out); each line names caller, method and outcome; no secret.
TERM-36 (owner 2026-11-21): no money on screen while usage is subscription-only. The required money words below are
taken from the words of TERM-36 itself (total_cost_usd, dollar, --max-budget-usd, credits, billed, paid, money), not
from the production list; the test checks that production catches every one.

This module also holds the stricter replacements for the owner-approved retirements (2026-11-21): the premature
"cancelled" outcome (API-21), the 30 ms coalescing sleep (API-13), the misnamed writer test (API-10/TERM-17), the
lsof check (API-2) and the dead CANNED list (API-15).
"""
import fcntl
import json
import os
import socket
import threading
import time
from unittest import mock

from tests.core_api_support import CoreCase, jsonl, read_jsonl

REQUIRED_MONEY_WORDS = ["cost", "usd", "dollar", "budget", "credits", "billed", "paid", "money"]  # TERM-36 text
PLANT = 7654321
_REAL_WRITE = os.write  # the other writer below must not see the test's patched os.write
ROUTING = {"profile": "p", "providers": ["a"], "rows": []}


def raw(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(path)
    return s


def ask(s, obj):
    s.sendall((json.dumps(obj) + "\n").encode())
    return json.loads(s.makefile().readline())


def closed_within(s, seconds, chunk=b'{"jsonrpc":"2.0","id":1,"method":"state.get"}\n', every=0.1):
    """Keep sending `chunk` until the core closes the connection; True when it did within `seconds`."""
    s.settimeout(0.1)
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            s.sendall(chunk)
            while True:
                data = s.recv(65536)
                if data == b"":
                    return True
        except socket.timeout:
            pass
        except OSError:
            return True
        time.sleep(every)
    return False


class Helpers:
    def confirmed(self, client, method, params):
        try:
            client.call(method, params)
        except self.mod.ApiError as error:
            confirm = error.data["confirm_id"]
        return client.call(method, {**params, "confirm_id": confirm})

    def refusals(self):
        return read_jsonl(self.records / "audit.jsonl.1") + read_jsonl(self.records / "audit.jsonl")

    def inproc(self, scope="read", token=None):
        token = token or self.mod._mint_token(str(self.records), scope)
        client = self.mod.in_process(self.service, token)
        client.call("initialize", {"protocol": self.mod.PROTOCOL_VERSION})
        return client

    def wait_for(self, predicate, seconds=4):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()


# --- API-23: changes and confirms in commands.jsonl, every refusal in audit.jsonl ------------------------

class AuditContract(Helpers, CoreCase):
    service_options = {"max_line_bytes": 4096}

    def test_changes_and_confirms_are_command_lines_and_every_refusal_is_an_audit_line(self):  # API-23
        s = raw(self.socket)
        reader = s.makefile()
        for line in (b"this is not json\n", b'{"jsonrpc":"1.0","id":1,"method":"x"}\n', b"x" * 5000 + b"\n"):
            s.sendall(line)
            reader.readline()
        anonymous = self.client(scope=None)
        self.error_code(anonymous, "state.get")
        client = self.client("confirm")
        client.call("context.push", {"kind": "note", "text": "hello"})
        self.error_code(client, "nope.nothing")
        self.error_code(client, "control.stop")
        self.error_code(client, "context.push", {})
        self.assertEqual("confirm_required", self.error_code(client, "unit.cancel", {"id": "U-0001"}))
        refused = {(r["method"], r["outcome"]) for r in self.refusals()}
        self.assertTrue({("envelope", "parse_error"), ("envelope", "invalid_request"), ("envelope", "too_large"),
                         ("state.get", "unauthorized"), ("unknown", "method_not_found"), ("control.stop", "forbidden"),
                         ("context.push", "invalid_params")} <= refused, refused)
        changes = {(r.get("method"), r.get("outcome")) for r in self.audit() if "outcome" in r}
        self.assertEqual({("context.push", "intent"), ("context.push", "ok"), ("unit.cancel", "confirm_required")},
                         changes)
        for row in self.refusals() + [r for r in self.audit() if "outcome" in r]:
            self.assertTrue(row["caller"] and row["method"] and row["outcome"] and row["ts"])

    def test_an_in_process_oversize_request_is_an_audited_refusal(self):  # API-23, API-6 (adapter parity)
        client = self.inproc("agent")
        with self.assertRaises(self.mod.ApiError) as raised:
            client.call("context.push", {"kind": "note", "text": "x" * 5000})
        self.assertEqual("too_large", raised.exception.code)
        self.assertIn(("envelope", "too_large"), {(r["method"], r["outcome"]) for r in self.refusals()})

    def test_a_connection_dropped_in_a_burst_is_an_audited_refusal(self):  # API-23, API-24
        self.service.stop()
        self.start(max_connections=1)
        held = raw(self.socket)
        self.addCleanup(held.close)
        time.sleep(0.1)
        with mock.patch.object(self.mod, "MAX_REFUSAL_THREADS", 0):  # no refusal worker: dropped at accept
            extra = raw(self.socket)
            self.addCleanup(extra.close)
            self.assertTrue(self.wait_for(lambda: any(r.get("method") == "connection" for r in self.refusals())))


class AuditRotationContract(Helpers, CoreCase):
    service_options = {"audit_max_bytes": 3000}

    def test_refusals_rotate_on_their_own_from_the_first_one(self):  # API-23
        anonymous = self.client(scope=None)
        self.error_code(anonymous, "state.get")
        self.assertEqual([], [r for r in self.audit() if r.get("outcome") == "unauthorized"])
        for _ in range(80):
            self.error_code(anonymous, "state.get")
        self.assertTrue((self.records / "audit.jsonl.1").exists())
        for name in ("audit.jsonl", "audit.jsonl.1"):
            self.assertLess((self.records / name).stat().st_size, 3000 + 400, name)


# --- API-24: one deadline, obeyed by the append lock, the audit write and every read ----------------------

class DeadlineBoundary(Helpers, CoreCase):
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

    def test_the_append_lock_wait_obeys_the_deadline(self):  # API-24
        agent = self.client("agent")
        release = self.hold(self.mod._APPEND_LOCK, 3.0)
        started = time.monotonic()
        code = self.error_code(agent, "context.push", {"kind": "note", "text": "x"})
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertIn(code, ("timeout", "unavailable"))
        release.set()
        self.assertEqual([], self.inbox())

    def test_the_audit_write_obeys_the_deadline(self):  # API-24, API-23
        reader = self.client("read")
        self.hold(self.service._audit_lock, 3.0)
        started = time.monotonic()
        self.assertIn(self.error_code(reader, "control.pause"), ("timeout", "unavailable"))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_read_that_runs_past_its_deadline_is_a_timeout(self):  # API-24
        real = self.service._latest_units

        def slow():
            time.sleep(0.5)
            return real()
        reader = self.client("read")
        with mock.patch.object(self.service, "_latest_units", slow):
            self.assertEqual("timeout", self.error_code(reader, "units.list"))


# --- API-8: a cut or capped read says so, through every answer built on it --------------------------------

class CompletenessBoundary(Helpers, CoreCase):
    def test_a_tail_that_starts_at_a_newline_is_still_incomplete(self):  # API-8
        limit = 3000
        for pad in range(0, 200):  # the last row's length moves the cut until it falls just after a newline
            rows = [{"id": "U-%04d" % i, "state": "running", "pad": "y" * 50} for i in range(199)] + [
                {"id": "U-0199", "state": "running", "pad": "q" * pad}]
            data = "".join(json.dumps(r) + "\n" for r in rows)
            if data.encode()[len(data.encode()) - limit] == ord("\n"):
                break
        else:
            self.fail("no padding puts a newline at the cut")
        (self.records / "units.jsonl").write_text(data)
        with mock.patch.object(self.mod, "READ_MAX_BYTES", limit):
            result = self.client("read").call("runs.list")
        self.assertEqual("unknown", result["total"])
        self.assertTrue(result.get("incomplete"))

    def test_a_capped_collection_says_so(self):  # API-8
        jsonl(self.records / "units.jsonl", [{"id": "U-%04d" % i, "state": "done"} for i in range(1001)])
        result = self.client("read").call("units.list")
        self.assertTrue(len(result["units"]) == 1001 or result.get("incomplete") is True, len(result["units"]))

    def test_every_answer_built_on_a_cut_read_says_incomplete(self):  # API-8
        big = [{"id": "H-%d" % i, "summary": "s" * 60} for i in range(100)]
        jsonl(self.records / "history.jsonl", big)
        jsonl(self.records / "usage.jsonl", [{"ts": i, "pct": 1, "pad": "u" * 60} for i in range(100)])
        jsonl(self.records / "units.jsonl", [{"id": "U-0001", "state": "running", "pad": "y" * 60}] * 100)
        jsonl(self.records / "decisions.jsonl", [{"id": "D-%04d" % i, "text": "t" * 60} for i in range(100)])
        client = self.client("read")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            for method, params in (("history.list", {}), ("usage.history", {}), ("units.history", {"id": "U-0001"}),
                                   ("decisions.list", {})):
                with self.subTest(method):
                    self.assertTrue(client.call(method, params).get("incomplete"), method)
            text = client.call("query.ask", {"text": "Show the history."})["text"]
        self.assertNotRegex(text, r"^\d+ history entries")  # a count from part of the record is a guess

    def test_why_stopped_with_a_cut_ack_record_is_unknown_not_requested(self):  # API-8, API-15
        command = self.confirmed(self.client("owner"), "control.stop", {})["command_id"]
        jsonl(self.records / "command_acks.jsonl", [
            {"type": "ack", "command_id": command, "action": "stop", "state": "done", "reason": "ok", "ts": 1, "v": 1}]
            + [{"type": "ack", "command_id": "C-9%03d" % i, "action": "pause", "state": "done", "reason": "p" * 40,
                "ts": 1, "v": 1} for i in range(100)])
        client = self.client("read")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            text = client.call("query.ask", {"text": "Why did it stop?"})["text"]
        self.assertNotIn("has not been confirmed", text)
        self.assertIn("unknown", text)


# --- TERM-36: no money, at one boundary, with no exempt key -----------------------------------------------

class MoneyBoundary(Helpers, CoreCase):
    def seed(self):
        super().seed()
        state = json.loads((self.records / "state.json").read_text())
        state["extra_detail"] = {}
        for word in REQUIRED_MONEY_WORDS:
            for key in (word, "total_" + word, word.capitalize() + "Total", word.upper() + "Total"):
                state["extra_detail"][key] = PLANT
        state["extra_detail"]["apricot"] = 3  # a benign key that only contains a money-like fragment
        (self.records / "state.json").write_text(json.dumps(state))

    def test_every_money_word_of_the_requirement_is_caught_in_keys(self):  # TERM-36
        extra = self.client("read").call("state.get")["state"]["extra"]["extra_detail"]
        self.assertEqual({"apricot": 3}, extra)

    AMOUNTS = {"M-1": ("cost 12.50", "12.50"), "M-2": ("price: 7654321", "7654321"), "M-3": ("the bill was 40", "40"),
               "M-4": ("refund $-12.50", "12.50"), "M-5": ("a $  12 fee", "12"), "M-6": ("it was 40€", "40"),
               "M-7": ("about 12.50 $", "12.50"), "M-8": ("CHF 40 a month", "40"), "M-9": ("₹500", "500"),
               "M-10": ("30 pounds", "30"), "M-11": ("500 yen", "500"), "M-12": ("we paid 99", "99")}
    BENIGN = {"B-1": "echo $1", "B-2": "3 files changed", "B-3": "ran 12 tests in 4.5 s"}

    def test_amounts_are_caught_with_or_without_a_currency_sign(self):  # TERM-36
        rows = [{"id": k, "ts": 1, "role": "orchestrator", "text": t} for k, (t, _) in self.AMOUNTS.items()]
        rows += [{"id": k, "ts": 1, "role": "orchestrator", "text": t} for k, t in self.BENIGN.items()]
        jsonl(self.records / "messages.jsonl", rows)
        got = {m["id"]: m["text"] for m in self.client("read").call("messages.list", {"limit": 200})["messages"]}
        for key, (text, number) in self.AMOUNTS.items():
            with self.subTest(text):
                self.assertNotIn(number, got[key])
        for key, text in self.BENIGN.items():
            self.assertEqual(text, got[key])  # ordinary numbers and shell parameters are kept

    def test_a_diff_carries_no_money_either(self):  # TERM-36 (no key exempts a string)
        jsonl(self.records / "diffs.jsonl", [{"path": "a.py", "diff": '+price = "$12.50"\n+echo $1\n', "ts": 1}])
        diff = self.client("read").call("changes.diff", {"path": "a.py"})["diff"]
        self.assertNotIn("12.50", diff)
        self.assertIn("echo $1", diff)

    def test_a_money_amount_used_as_a_key_is_removed(self):  # TERM-36
        jsonl(self.records / "history.jsonl", [{"id": "H-1", "summary": "ran", "$12.50": "x", "40€": "y"}])
        row = self.client("read").call("history.list")["history"][0]
        self.assertEqual({"id", "summary"}, set(row))


# --- API-23, API-21: no unbounded intent rows from refused calls; ids never repeat --------------------------

class IntentsAndIds(Helpers, CoreCase):
    def test_refused_pushes_write_no_intent_rows(self):  # API-23, API-17
        agent = self.client("agent")
        for _ in range(30):
            self.error_code(agent, "context.push", {})
        codes = []
        for _ in range(12):
            try:
                agent.call("context.push", {"kind": "note", "text": "x"})
                codes.append("ok")
            except self.mod.ApiError as error:
                codes.append(error.code)
        self.assertEqual(["ok"] * 10 + ["rate_limited"] * 2, codes)
        intents = [r for r in self.audit() if r.get("method") == "context.push" and r.get("outcome") == "intent"]
        self.assertEqual(10, len(intents))

    def test_command_ids_never_repeat_when_the_record_is_cut(self):  # API-21
        owner = self.client("owner")
        first = self.confirmed(owner, "control.pause", {})["command_id"]
        with open(self.records / "commands.jsonl", "a") as handle:
            for _ in range(60):
                handle.write(json.dumps({"type": "pad", "pad": "p" * 100}) + "\n")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            second = self.confirmed(owner, "control.resume", {})["command_id"]
        self.assertNotEqual(first, second)
        ids = [r["command_id"] for r in read_jsonl(self.records / "commands.jsonl") if r.get("type") == "command"]
        self.assertEqual(len(ids), len(set(ids)))

    def test_push_and_query_ids_never_repeat_when_the_inbox_is_cut(self):  # API-16, API-17
        agent = self.client("agent")
        first = agent.call("context.push", {"kind": "note", "text": "x"})["push_id"]
        with open(self.records / "inbox.jsonl", "a") as handle:
            for _ in range(60):
                handle.write(json.dumps({"kind": "pad", "pad": "p" * 100}) + "\n")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            second = agent.call("context.push", {"kind": "note", "text": "y"})["push_id"]
        self.assertNotEqual(first, second)


# --- API-10: appends are whole rows even with another writer --------------------------------------------------

class CrossProcessAppend(Helpers, CoreCase):
    def test_a_short_write_never_removes_another_writers_row(self):  # API-10
        path = self.dir / "shared.jsonl"
        path.write_text("")
        other = threading.Thread(target=self._other_writer, args=(path,))

        def short(fd, data):
            written = _REAL_WRITE(fd, data[:10])
            if not other.is_alive() and other.ident is None:
                other.start()  # another writer that follows the lock protocol (flock on the file)
                time.sleep(0.3)
            return written
        with mock.patch.object(self.mod.os, "write", short):
            with self.assertRaises(OSError):
                self.mod._append_jsonl(path, {"a": "x" * 50})
        other.join(5)
        self.assertEqual([{"other": 1}], read_jsonl(path))

    @staticmethod
    def _other_writer(path):
        fd = os.open(path, os.O_WRONLY | os.O_APPEND)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _REAL_WRITE(fd, b'{"other": 1}\n')
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# --- API-16/17: a wide label cannot block delivery -------------------------------------------------------------

class LabelBoundary(Helpers, CoreCase):
    def test_a_64_character_non_ascii_label_cannot_block_later_rows(self):  # API-16, API-17
        wide = self.mod._mint_token(str(self.records), "agent", label="日" * 64)
        first = self.mod.CoreClient(self.socket, token=wide)
        self.addCleanup(first.close)
        first.call("initialize", {"protocol": "1.0"})
        a = first.call("context.push", {"kind": "note", "text": "hello"})["push_id"]
        b = self.client("agent").call("context.push", {"kind": "note", "text": "after"})["push_id"]
        delivered = []
        for _ in range(6):
            block = self.service.deliver_at_safe_point()
            self.assertLessEqual(len(block["text"].encode()), 300)
            delivered += block["push_ids"]
        self.assertIn(b, delivered)
        self.assertIn(a, delivered)


# --- API-24: the handshake deadline holds for draining and for a tokenless initialize ---------------------------

class HandshakeBoundary(Helpers, CoreCase):
    start_service = False

    def setUp(self):
        super().setUp()
        with mock.patch.object(self.mod, "HANDSHAKE_SECONDS", 0.5, create=True):
            self.start(max_line_bytes=4096)

    def test_draining_an_oversize_line_obeys_the_handshake_deadline(self):  # API-24
        s = raw(self.socket)
        s.sendall(b"x" * 9000)
        started = time.monotonic()
        self.assertTrue(closed_within(s, 3.0, chunk=b"x" * 50, every=0.05))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_tokenless_initialize_does_not_lift_the_handshake_deadline(self):  # API-24
        s = raw(self.socket)
        ask(s, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocol": "1.0"}})
        started = time.monotonic()
        self.assertTrue(closed_within(s, 3.0))
        self.assertLess(time.monotonic() - started, 1.5)


# --- API-18, API-10: no record file is followed through a symlink ---------------------------------------------

class NoFollowBoundary(Helpers, CoreCase):
    def test_symlinked_record_files_are_never_followed(self):  # API-18, API-10
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "state.json").write_text(json.dumps({"phase": "evil"}))
        (elsewhere / "units.jsonl").write_text(json.dumps({"id": "U-0666", "state": "evil"}) + "\n")
        (elsewhere / "inbox.jsonl").write_text("")
        client = self.client("agent")
        for name in ("state.json", "units.jsonl", "inbox.jsonl"):
            os.remove(self.records / name)
            os.symlink(elsewhere / name, self.records / name)
        self.assertEqual("unknown", client.call("state.get")["state"]["phase"])
        self.assertEqual("unknown", client.call("units.list")["units"])
        self.assertEqual("unavailable", self.error_code(client, "context.push", {"kind": "note", "text": "x"}))
        self.assertEqual("", (elsewhere / "inbox.jsonl").read_text())


# --- replacements for the owner-approved retirements (2026-11-21) -----------------------------------------------

class RetirementReplacements(Helpers, CoreCase):
    def test_a_confirmed_control_runs_once_and_says_requested_never_done(self):  # API-21 (replaces talk :155)
        owner = self.client("owner")
        done_words = {"paused", "resumed", "stopped", "cancelled", "handed_off"}
        for method, params in (("control.pause", {}), ("control.resume", {}), ("control.handoff", {}),
                               ("control.stop", {}), ("unit.cancel", {"id": "U-0001"})):
            with self.subTest(method):
                try:
                    owner.call(method, params)
                except self.mod.ApiError as error:
                    cid = error.data["confirm_id"]
                before = len([r for r in self.audit() if r.get("type") == "command"])
                result = owner.call(method, {**params, "confirm_id": cid})
                self.assertEqual("requested", result["state"])
                self.assertTrue(result["outcome"].endswith("_requested"), result["outcome"])
                self.assertNotIn(result["outcome"], done_words)
                self.assertEqual("confirm_invalid", self.error_code(owner, method, {**params, "confirm_id": cid}))
                self.assertEqual(before + 1, len([r for r in self.audit() if r.get("type") == "command"]))

    def test_a_healthy_reader_keeps_up_with_a_burst_while_a_stalled_one_is_dropped(self):  # API-13 (replaces events :46)
        self.service.stop()
        self.start(event_buffer=50, max_subscriptions=4, write_seconds=0.5)
        stalled = raw(self.socket)
        stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 2048)
        token = self.mod._mint_token(str(self.records), "read")
        ask(stalled, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocol": "1.0", "token": token}})
        stalled.sendall(b'{"jsonrpc":"2.0","id":2,"method":"events.subscribe","params":{"from_seq":5}}\n')
        healthy = self.client("read").subscribe({"from_seq": 5, "limit": 300})
        got = []
        reader = threading.Thread(target=lambda: got.extend(e["seq"] for e in healthy))
        reader.start()
        started = time.monotonic()
        for i in range(300):
            self.service.emit("unit.updated", {"summary": "e%d %s" % (i, "z" * 200)}, unit_id="U-0001")
        self.assertLess(time.monotonic() - started, 10)  # the writer never waits on a subscriber
        reader.join(15)
        self.assertEqual(list(range(6, 306)), got)  # every event, in order, no gap, no drop
        self.assertTrue(self.wait_for(lambda: len(self.service._subs) == 0, 10))  # the stalled one is gone

    def test_no_read_method_writes_any_record_file(self):  # API-10, TERM-17 (replaces transport :37)
        jsonl(self.records / "diffs.jsonl", [{"path": "a.py", "diff": "+x", "ts": 1}])
        client = self.client("read")
        before = {p.name: p.read_bytes() for p in self.records.iterdir() if p.name != "audit.jsonl"}
        for method, params in (("schema.get", {}), ("state.get", {}), ("units.list", {}), ("units.get", {"id": "U-0001"}),
                               ("units.history", {"id": "U-0001"}), ("runs.list", {}), ("usage.get", {}),
                               ("usage.history", {}), ("ci.get", {}), ("versions.get", {}), ("history.list", {}),
                               ("decisions.list", {}), ("queries.list", {}), ("health.get", {}),
                               ("units.activity", {"id": "U-0001"}), ("changes.list", {}),
                               ("changes.diff", {"path": "a.py"}), ("messages.list", {}), ("routing.get", {}),
                               ("commands.list", {}), ("query.ask", {"text": "What is the status?"}),
                               ("events.subscribe", {"from_seq": 5, "limit": 1})):
            client.call(method, params)
        after = {p.name: p.read_bytes() for p in self.records.iterdir() if p.name != "audit.jsonl"}
        self.assertEqual(before, after)

    def test_no_tcp_listener_is_ever_opened(self):  # API-2 GUARD (replaces the lsof check at transport :32)
        listened = []
        real_listen = socket.socket.listen

        def spy(sock, *args):
            listened.append(sock.family)
            return real_listen(sock, *args)
        self.service.stop()
        with mock.patch.object(socket.socket, "listen", spy):
            self.start()
            self.client("read").call("state.get")
        self.assertEqual([socket.AF_UNIX], listened)

    def test_each_canned_answer_states_the_record_values(self):  # API-15 (replaces the dead CANNED list)
        jsonl(self.records / "history.jsonl", [{"id": "H-1", "summary": "built B1"},
                                                {"type": "stop", "id": "H-2", "code": "guard", "by": "cocolo",
                                                 "reason": "The window was nearly full.", "ts": 5, "next": None, "v": 1}])
        client = self.client("read")
        expected = {"What is the status?": ("build", "S-7", "2 units", "1 running"),
                    "What is U-0001 doing?": ("U-0001", "running", "Build B1"),
                    "How much usage is left?": ("10", "5"),
                    "Is CI green?": ("green",), "Which version is installed?": ("0.8.40",),
                    "Show the history.": ("2 history entries",),
                    "Why did the last unit stop?": ("The window was nearly full.", "CoCoLo")}
        for question, parts in expected.items():
            with self.subTest(question):
                answer = client.call("query.ask", {"text": question, "mode": "records_only"})
                self.assertEqual(("records", 0), (answer["answered_by"], answer["tokens_used"]))
                self.assertTrue(answer["sources"] and answer["as_of"])
                for part in parts:
                    self.assertIn(part, answer["text"])
        self.assertEqual([], self.inbox())

    def test_walled_actions_change_no_record_and_each_is_one_refusal_row(self):  # API-22, API-23 (replaces talk :187)
        walled = ["install.run", "update.run", "setup.run", "install.reviewed", "records.write", "billing.attest",
                  "client_range.widen", "token.mint"]
        owner = self.client("owner")
        before = {p.name: p.read_bytes() for p in self.records.iterdir() if p.name not in ("audit.jsonl", "tokens.json")}
        for method in walled:
            self.assertEqual("owner_terminal_only", self.error_code(owner, method, {"confirm_id": "x"}))
        after = {p.name: p.read_bytes() for p in self.records.iterdir() if p.name not in ("audit.jsonl", "tokens.json")}
        self.assertEqual(before, after)  # commands.jsonl included: a refusal is not a change
        self.assertEqual(walled, [r["method"] for r in self.refusals() if r["outcome"] == "owner_terminal_only"])
