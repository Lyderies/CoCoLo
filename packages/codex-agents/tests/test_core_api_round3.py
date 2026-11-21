"""Core API round 3 (Codex review 040-tm0-api-r2, all MED/LOW): audit completeness, limits, why-stopped and
command acks, adapter parity, security, no-money, tokens_used. One requirement per test; each was red first.

Records read here: command_acks.jsonl {"type":"ack","command_id","action","unit_id"?,"state":"done|refused",
"reason","ts","v":1} (written by the runner), history.jsonl stop records (written by the runner when the stop happens).
"""
import json
import os
import re
import socket
import threading
import time
from types import SimpleNamespace
from unittest import mock

from tests.core_api_support import CoreCase, jsonl, read_jsonl

ROUTING = {"profile": "p", "providers": ["a"], "rows": []}
MONEY = re.compile(r"(cost|price|usd|dollar|spend|money|balance|credit|billing|bill)", re.I)


def raw(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(path)
    return s


def ask(s, obj):
    s.sendall((json.dumps(obj) + "\n").encode() if not isinstance(obj, bytes) else obj)
    return json.loads(s.makefile().readline())


def keys_of(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from keys_of(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys_of(v)


class Helpers:
    def confirmed(self, client, method, params):
        try:
            client.call(method, params)
        except self.mod.ApiError as error:
            confirm = error.data["confirm_id"]
        return client.call(method, {**params, "confirm_id": confirm})

    def wait_for(self, predicate, seconds=4):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()


class AuditRound3(Helpers, CoreCase):
    service_options = {"max_line_bytes": 4096}


    def test_malformed_envelopes_are_audited(self):
        s = raw(self.socket)
        reader = s.makefile()
        s.sendall(b"this is not json\n")
        reader.readline()
        s.sendall(b'{"jsonrpc":"1.0","id":1,"method":"x"}\n')
        reader.readline()
        s.sendall(b"x" * 9000)
        reader.readline()
        outcomes = {(r["method"], r["outcome"]) for r in self.audit()}
        self.assertTrue({("envelope", "parse_error"), ("envelope", "invalid_request"),
                         ("envelope", "too_large")} <= outcomes, outcomes)

    def test_mutation_is_audited_durably_before_the_effect(self):
        order = []
        real = self.mod._append_jsonl

        def spy(path, row):
            order.append(os.path.basename(str(path)))
            return real(path, row)
        with mock.patch.object(self.mod, "_append_jsonl", spy):
            self.client("agent").call("context.push", {"kind": "note", "text": "hello"})
        self.assertLess(order.index("commands.jsonl"), order.index("inbox.jsonl"))
        self.assertEqual("intent", self.audit()[0]["outcome"])

    def test_query_reply_is_audited_and_refused_when_the_audit_cannot_be_written(self):
        query = self.client("confirm").call("query.ask", {"text": "Rewrite the parser?", "mode": "forward"})["query_id"]
        orch = self.client("orchestrator")
        orch.call("query.reply", {"query_id": query, "text": "ok"})
        self.assertIn(("query.reply", "ok"), {(r["method"], r["outcome"]) for r in self.audit()})
        second = self.client("confirm").call("query.ask", {"text": "Another thing, please?", "mode": "forward"})["query_id"]
        os.chmod(self.records / "commands.jsonl", 0o444)
        self.addCleanup(os.chmod, self.records / "commands.jsonl", 0o644)
        self.assertEqual("unavailable", self.error_code(orch, "query.reply", {"query_id": second, "text": "ok"}))
        self.assertEqual(1, len([r for r in read_jsonl(self.records / "queries.jsonl")]))


class AuditRotation(Helpers, CoreCase):
    service_options = {"audit_max_bytes": 3000}

    def test_the_audit_log_is_bounded_by_rotation(self):
        for _ in range(4):
            anonymous = self.client(scope=None)
            for _ in range(45):
                self.error_code(anonymous, "state.get")
        sizes = {n: (self.records / n).stat().st_size for n in os.listdir(self.records)
                 if n.startswith(("commands.jsonl", "audit.jsonl"))}
        self.assertIn("audit.jsonl.1", sizes)
        self.assertLess(sum(sizes.values()), 3 * 3000 + 2000, sizes)


class LimitsRound3(Helpers, CoreCase):
    service_options = {"max_line_bytes": 4096, "idle_seconds": 0.5, "lock_seconds": 0.3, "method_seconds": 0.2}

    def test_a_trickling_line_is_cut_off_by_one_absolute_deadline(self):
        s = raw(self.socket)
        s.settimeout(0.1)
        started = time.monotonic()
        closed = False
        while time.monotonic() - started < 3.0:
            try:
                s.sendall(b" ")
            except OSError:
                closed = True
                break
            try:
                if s.recv(10) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
            time.sleep(0.1)
        self.assertTrue(closed)
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_held_lock_times_out_instead_of_waiting_forever(self):
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.service._lock:
                held.set()
                release.wait(5)
        thread = threading.Thread(target=hold, daemon=True)
        thread.start()
        held.wait(2)
        self.addCleanup(release.set)
        started = time.monotonic()
        self.assertEqual("timeout", self.error_code(self.client("agent"), "context.push",
                                                    {"kind": "note", "text": "x"}))
        self.assertLess(time.monotonic() - started, 3)

    def test_routing_set_checks_its_deadline_before_writing(self):
        real = __import__("tempfile").mkstemp

        def slow(*args, **kwargs):
            out = real(*args, **kwargs)
            time.sleep(0.5)
            return out
        owner = self.client("owner")
        with mock.patch("tempfile.mkstemp", slow):
            self.assertEqual("timeout", self.error_code(owner, "routing.set", {**ROUTING, "confirm_id": self._cid(owner)}))
        self.assertFalse((self.records / "routing.json").exists())
        self.assertEqual([], [n for n in os.listdir(self.records) if "routing" in n])

    def _cid(self, owner):
        try:
            owner.call("routing.set", ROUTING)
        except self.mod.ApiError as error:
            return error.data["confirm_id"]


class ReadBounds(Helpers, CoreCase):
    def test_every_file_read_is_size_capped(self):
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            (self.records / "state.json").write_text(json.dumps({"phase": "build", "pad": "x" * 10000}))
            jsonl(self.records / "units.jsonl", [{"id": "U-%04d" % i, "state": "running", "pad": "y" * 50}
                                                   for i in range(200)])
            client = self.client("read")
            self.assertEqual("unknown", client.call("state.get")["state"]["phase"])
            units = client.call("units.list")["units"]
            self.assertLess(len(units), 200)
            self.assertEqual("U-0199", units[-1]["id"])

    def test_the_token_lock_wait_has_a_timeout(self):
        import fcntl
        fd = os.open(self.records / "tokens.json", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with mock.patch.object(self.mod, "LOCK_SECONDS", 0.3):
            started = time.monotonic()
            with self.assertRaises(self.mod.ApiError) as raised:
                self.mod._mint_token(str(self.records), "read")
        self.assertEqual("timeout", raised.exception.code)
        self.assertLess(time.monotonic() - started, 3)


class TextAndRouting(Helpers, CoreCase):
    def test_reply_text_may_be_long_and_multiline_up_to_two_kb(self):
        query = self.client("confirm").call("query.ask", {"text": "Rewrite the parser?", "mode": "forward"})["query_id"]
        orch = self.client("orchestrator")
        text = ("line one of the answer\n" * 60).strip()
        self.assertGreater(len(text), 128)
        orch.call("query.reply", {"query_id": query, "text": text})
        self.assertEqual(text, self.client("confirm").call("queries.get", {"id": query})["query"]["reply"])
        self.assertEqual("too_large", self.error_code(orch, "query.reply", {"query_id": query, "text": "z" * 2100}))

    def test_routing_set_uses_a_unique_temp_file_per_call(self):
        owner = self.client("owner")
        sources = []
        real = os.replace

        def spy(src, dst, *a, **k):
            sources.append(os.path.basename(str(src)))
            return real(src, dst, *a, **k)
        with mock.patch.object(os, "replace", spy):
            self.confirmed(owner, "routing.set", ROUTING)
            self.confirmed(owner, "routing.set", {**ROUTING, "profile": "q"})
        sources = [s for s in sources if "routing" in s]
        self.assertEqual(2, len(set(sources)), sources)
        self.assertTrue(all(str(os.getpid()) not in s for s in sources))
        self.assertEqual("q", json.loads((self.records / "routing.json").read_text())["profile"])

    def test_concurrent_routing_sets_leave_one_valid_file_and_no_leftovers(self):
        owners = [self.client("owner") for _ in range(6)]
        threads = [threading.Thread(target=self.confirmed, args=(o, "routing.set", {**ROUTING, "profile": "p%d" % i}))
                   for i, o in enumerate(owners)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertTrue(json.loads((self.records / "routing.json").read_text())["profile"].startswith("p"))
        self.assertEqual([], [n for n in os.listdir(self.records) if n.endswith(".tmp") or n.startswith(".routing")])


class WhyStoppedAndAcks(Helpers, CoreCase):
    def ask(self, text):
        return self.client("read").call("query.ask", {"text": text, "mode": "auto"})["text"]

    def test_a_unit_with_no_stop_record_is_unknown_with_a_plain_reason(self):
        text = self.ask("Why did U-0001 stop?")
        self.assertTrue(text.startswith("unknown"))
        self.assertIn("U-0001", text)
        self.assertIn("no stop", text)

    def test_the_core_does_not_claim_a_stop_it_only_requested(self):
        owner = self.client("owner")
        reply = self.confirmed(owner, "control.stop", {})
        self.assertEqual("requested", reply["state"])
        self.assertEqual([], [r for r in read_jsonl(self.records / "history.jsonl") if r.get("type") == "stop"])
        text = self.ask("Why did it stop?")
        self.assertIn("requested", text.lower())
        self.assertIn(reply["command_id"], text)

    def test_the_runner_stop_record_is_what_why_stopped_reports(self):
        self.confirmed(self.client("owner"), "control.stop", {})
        jsonl(self.records / "history.jsonl", [{"type": "stop", "id": "H-1", "code": "owner_stop", "by": "owner",
                                                 "reason": "You asked CoCoLo to stop.", "ts": 5, "next": None, "v": 1}])
        self.assertIn("Stopped by you", self.ask("Why did it stop?"))

    def test_command_acks_are_read_and_reported_done_or_refused(self):
        owner = self.client("owner")
        first = self.confirmed(owner, "control.pause", {})["command_id"]
        second = self.confirmed(owner, "control.resume", {})["command_id"]
        self.assertEqual("requested", self.client("read").call("commands.get", {"id": first})["command"]["state"])
        jsonl(self.records / "command_acks.jsonl", [
            {"type": "ack", "command_id": first, "action": "pause", "state": "done", "reason": "paused", "ts": 9, "v": 1},
            {"type": "ack", "command_id": second, "action": "resume", "state": "refused", "reason": "not paused",
             "ts": 10, "v": 1}])
        a = self.client("read").call("commands.get", {"id": first})["command"]
        b = self.client("read").call("commands.get", {"id": second})["command"]
        self.assertEqual(("done", "paused"), (a["state"], a["reason"]))
        self.assertEqual(("refused", "not paused"), (b["state"], b["reason"]))
        listed = {c["command_id"]: c["state"] for c in self.client("read").call("commands.list")["commands"]}
        self.assertEqual({first: "done", second: "refused"}, listed)
        self.assertEqual("not_found", self.error_code(self.client("read"), "commands.get", {"id": "C-9999"}))

    def test_a_stop_acked_done_without_a_stop_record_says_so(self):
        command = self.confirmed(self.client("owner"), "control.stop", {})["command_id"]
        jsonl(self.records / "command_acks.jsonl", [
            {"type": "ack", "command_id": command, "action": "stop", "state": "refused", "reason": "nothing running",
             "ts": 9, "v": 1}])
        self.assertIn("nothing running", self.ask("Why did it stop?"))


class AdapterParity(Helpers, CoreCase):
    def inproc(self, scope="read"):
        token = self.mod._mint_token(str(self.records), scope)
        client = self.mod.in_process(self.service, token)
        client.call("initialize", {"protocol": self.mod.PROTOCOL_VERSION})
        return client

    def test_local_subscriptions_work_like_socket_ones(self):
        events = list(self.inproc().subscribe({"from_seq": 3, "limit": 2}))
        self.assertEqual([4, 5], [e["seq"] for e in events])
        stream = self.inproc().subscribe({"from_seq": 5})
        self.service.emit("unit.updated", {"summary": "x"}, unit_id="U-0001")
        self.assertEqual(6, next(stream)["seq"])

    def test_local_subscriptions_end_on_revocation(self):
        token = self.mod._mint_token(str(self.records), "read")
        client = self.mod.in_process(self.service, token)
        client.call("initialize", {"protocol": "1.0"})
        stream = client.subscribe({"from_seq": 5})
        self.mod.revoke_token(str(self.records), token)
        self.assertEqual([], list(stream))

    def test_errors_carry_seq_at_in_process_too(self):
        client = self.inproc()
        for method, params in (("nope.nothing", {}), ("units.get", {"id": "U-9999"}), ("query.ask", {"text": "x" * 9000})):
            with self.assertRaises(self.mod.ApiError) as raised:
                client.call(method, params)
            self.assertEqual(5, raised.exception.data.get("seq_at"), method)


class SecurityRound3(Helpers, CoreCase):
    service_options = {"max_subscriptions": 16, "max_subscriptions_per_token": 1}

    def test_one_token_cannot_hold_more_than_its_subscription_cap(self):
        token = self.mod._mint_token(str(self.records), "read")
        a, b = self.client(token=token), self.client(token=token)
        a.subscribe({"from_seq": 0})
        self.assertEqual("too_many_subscriptions", self.error_code(b, "events.subscribe", {"from_seq": 0}))
        other = self.client("read")
        other.subscribe({"from_seq": 0})  # another token is unaffected

    def test_event_data_passes_only_an_allow_list_of_fields(self):
        self.service.emit("unit.updated", {"summary": "ok", "detail": "LEAK-1", "body": {"x": "LEAK-2"},
                                           "result_text": "LEAK-3", "verdict": "pass"}, unit_id="U-0001")
        events = list(self.client("read").subscribe({"from_seq": 5, "limit": 1}))
        self.assertEqual({"summary": "ok", "verdict": "pass"}, events[0]["data"])
        self.assertNotIn("LEAK", (self.records / "events.jsonl").read_text())

    def test_confirm_expiry_uses_a_monotonic_clock(self):
        mono = SimpleNamespace(now=100.0)
        self.service.stop()
        self.start(monotonic=lambda: mono.now)
        owner = self.client("owner")
        cid = self._cid(owner)
        self.clock.advance(-5000)  # the wall clock going backwards must not extend the confirmation
        mono.now += 61
        self.assertEqual("confirm_expired", self.error_code(owner, "routing.set", {**ROUTING, "confirm_id": cid}))

    def _cid(self, owner):
        try:
            owner.call("routing.set", ROUTING)
        except self.mod.ApiError as error:
            return error.data["confirm_id"]

    def test_the_client_never_sends_its_token_to_a_symlinked_socket(self):
        link = self.dir / "link.sock"
        os.symlink(self.socket, link)
        client = self.mod.CoreClient(str(link), token=self.mod._mint_token(str(self.records), "read"))
        self.addCleanup(client.close)
        with self.assertRaises(Exception):
            client.call("initialize", {"protocol": "1.0"})
        self.assertEqual([], [r for r in self.audit()])

    def test_the_client_never_sends_its_token_to_a_peer_of_another_uid(self):
        path = str(self.dir / "fake.sock")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen(1)
        listener.settimeout(1)
        self.addCleanup(listener.close)
        got = []

        def accept():
            try:
                conn, _ = listener.accept()
                conn.settimeout(1)
                got.append(conn.recv(4096))
            except OSError:
                pass
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        client = self.mod.CoreClient(path, token="SECRET-TOKEN")
        self.addCleanup(client.close)
        with mock.patch.object(self.mod, "peer_uid", return_value=os.geteuid() + 1):
            with self.assertRaises(Exception):
                client.call("initialize", {"protocol": "1.0"})
        thread.join(3)
        self.assertNotIn(b"SECRET-TOKEN", b"".join(got))

    def test_the_owner_terminal_check_is_stronger_than_isatty(self):
        me = os.geteuid()
        good = SimpleNamespace(st_uid=me, st_rdev=7)
        with mock.patch.object(os, "isatty", return_value=True):
            with mock.patch.object(os, "fstat", return_value=good), mock.patch.object(os, "tcgetpgrp", return_value=1):
                self.assertTrue(self.mod._owner_terminal())
            with mock.patch.object(os, "fstat", return_value=SimpleNamespace(st_uid=me + 1, st_rdev=7)), \
                    mock.patch.object(os, "tcgetpgrp", return_value=1):
                self.assertFalse(self.mod._owner_terminal())  # a terminal someone else owns
            with mock.patch.object(os, "fstat", return_value=good), \
                    mock.patch.object(os, "tcgetpgrp", side_effect=OSError(25, "not the controlling terminal")):
                self.assertFalse(self.mod._owner_terminal())
            devices = iter([SimpleNamespace(st_uid=me, st_rdev=7), SimpleNamespace(st_uid=me, st_rdev=8)])
            with mock.patch.object(os, "fstat", side_effect=lambda fd: next(devices)), \
                    mock.patch.object(os, "tcgetpgrp", return_value=1):
                self.assertFalse(self.mod._owner_terminal())  # stdin and stdout are different devices


class NoMoneyRound3(Helpers, CoreCase):
    def seed(self):
        super().seed()
        jsonl(self.records / "usage.jsonl", [{"ts": 1, "pct": 4, "provider": {"name": "p", "credits": 7654321},
                                              "usd": 7654321}])
        jsonl(self.records / "history.jsonl", [{"id": "H-1", "summary": "ran", "detail": {"price": 7654321, "ok": 1}}])
        state = json.loads((self.records / "state.json").read_text())
        state["ci"] = {"status": "green", "detail": {"price": 7654321}}
        state["versions"] = {"installed": "0.8.40", "extra": [{"balance": 7654321}]}
        state["extra_note"] = {"provider": {"credits": 7654321, "name": "p"}}
        (self.records / "state.json").write_text(json.dumps(state))
        jsonl(self.records / "units.jsonl", [
            {"id": "U-0001", "kind": "child", "state": "running", "title": "B", "spend": 7654321,
             "result": {"cost_usd": 7654321, "ok": True}}])
        (self.records / "routing.json").write_text(json.dumps({
            "profile": "p", "providers": ["a"], "set_by": "x", "ts": 1,
            "rows": [{"task": "t", "provider": "a", "model": "m", "effort": "high", "fallback": None,
                      "cost": 7654321}]}))

    def assertClean(self, value, label):
        text = json.dumps(value)
        self.assertNotIn("7654321", text, label)
        self.assertEqual([], [k for k in keys_of(value) if MONEY.search(k)], label)

    def test_money_is_removed_recursively_from_every_reply(self):
        client = self.client("read")
        for method, params in (("usage.history", {}), ("history.list", {}), ("ci.get", {}), ("versions.get", {}),
                               ("routing.get", {}), ("state.get", {}), ("units.get", {"id": "U-0001"}),
                               ("units.list", {}), ("runs.list", {}), ("units.history", {"id": "U-0001"})):
            self.assertClean(client.call(method, params), method)
        for question in ("Is CI green?", "Which version is installed?", "How much usage is left?"):
            self.assertClean(client.call("query.ask", {"text": question}), question)

    def test_money_is_removed_from_events_and_snapshots(self):
        self.service.emit("unit.updated", {"summary": "s", "price": 7654321}, unit_id="U-0001")
        jsonl(self.records / "events.jsonl", read_jsonl(self.records / "events.jsonl")[:5] + [
            {"seq": 7, "ts": 1, "kind": "unit.updated", "unit_id": "U-0001", "v": 1,
             "data": {"summary": {"credits": 7654321}, "verdict": "pass"}}])
        self.service.stop()
        self.start(event_buffer=2)
        client = self.client("read")
        stream = list(client.subscribe({"from_seq": 5, "limit": 1}))
        self.assertClean(stream, "events")
        try:
            client.call("events.subscribe", {"from_seq": 0})
        except self.mod.ApiError as error:
            self.assertClean(error.data.get("snapshot"), "snapshot")

    def test_a_verdict_copy_drops_money_fields(self):
        self.confirmed_owner = self.client("owner")
        self.confirmed_owner.call("unit.verdict", {"id": "U-0001", "verdict": "pass"})
        latest = read_jsonl(self.records / "units.jsonl")[-1]
        self.assertClean(latest, "verdict copy")
        self.assertEqual("pass", latest["verdict"])


CANNED = ["What is the status?", "What is U-0001 doing?", "How much usage is left?", "Is CI green?",
          "Which version is installed?", "Show the history.", "Why did the last unit stop?"]


class TokensUsed(Helpers, CoreCase):
    def test_every_canned_answer_is_from_records_with_tokens_used_zero(self):  # API-15 (replaces the cost_tokens test)
        client = self.client("read")
        for question in CANNED:
            with self.subTest(question):
                answer = client.call("query.ask", {"text": question, "mode": "auto"})
                self.assertEqual("records", answer["answered_by"])
                self.assertEqual(0, answer["tokens_used"])
                self.assertTrue(answer["sources"] and answer["as_of"])
        self.assertEqual([], self.inbox())

    def test_the_query_answered_event_has_no_reply_text_but_the_asker_can_read_it(self):  # API-16
        query = self.client("confirm").call("query.ask", {"text": "Rewrite the parser?", "mode": "forward"})["query_id"]
        stream = self.client("read").subscribe({"from_seq": 6, "filter": {"kinds": ["query.answered"]}, "limit": 1})
        self.client("owner").call("query.reply", {"query_id": query, "text": "No, keep it.", "tokens_used": 12})
        event = next(stream)
        self.assertEqual(query, event["data"]["query_id"])
        self.assertNotIn("No, keep it.", json.dumps(event))
        answer = self.client("confirm").call("queries.get", {"id": query})["query"]
        self.assertEqual(("answered", "No, keep it.", 12), (answer["state"], answer["reply"], answer["tokens_used"]))

    def test_canned_answers_report_tokens_used_zero_and_no_cost_tokens(self):
        answer = self.client("read").call("query.ask", {"text": "What is the status?", "mode": "auto"})
        self.assertEqual(0, answer["tokens_used"])
        self.assertNotIn("cost_tokens", json.dumps(answer))

    def test_reply_takes_and_returns_tokens_used(self):
        query = self.client("confirm").call("query.ask", {"text": "Rewrite the parser?", "mode": "forward"})["query_id"]
        self.assertEqual(12, self.client("owner").call("query.reply", {"query_id": query, "text": "No.",
                                                                         "tokens_used": 12})["tokens_used"])
        answer = self.client("confirm").call("queries.get", {"id": query})["query"]
        self.assertEqual(12, answer["tokens_used"])
        self.assertNotIn("cost_tokens", json.dumps(answer))

    def test_reply_tokens_used_must_be_a_non_negative_integer(self):
        query = self.client("confirm").call("query.ask", {"text": "Rewrite the parser?", "mode": "forward"})["query_id"]
        self.assertEqual("invalid_params", self.error_code(self.client("owner"), "query.reply",
                                                           {"query_id": query, "text": "x", "tokens_used": -1}))


class ConnectionCapStrict(Helpers, CoreCase):
    service_options = {"max_connections": 3}

    def test_an_extra_connection_always_reads_too_many_connections_never_a_reset(self):
        held = [raw(self.socket) for _ in range(3)]
        self.addCleanup(lambda: [h.close() for h in held])
        for _ in range(10):
            extra = raw(self.socket)
            reply = ask(extra, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocol": "1.0"}})
            self.assertEqual("too_many_connections", reply["error"]["data"]["code"])
            extra.close()
