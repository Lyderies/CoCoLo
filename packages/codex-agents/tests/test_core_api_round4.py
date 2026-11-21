"""Core API round 4 (Codex review 040-tm0-api-r3, worker and verifier): every r3 finding that was partly or not
fixed, as a requirement test that was red first. Tests marked GUARD passed on the old code too: they pin a
requirement the code already met (scope matrix, handler table, racing confirms, replaced-test gaps). Each test
names its requirement (API-n, TERM-n) on its def line.

API-23 tests follow the owner's decision of 2026-11-21: every change and confirm is a commands.jsonl line, every
refusal is an audit.jsonl line (rotated separately), each line names caller, method and outcome, no secret appears.

The root cause the review found: earlier rounds patched each call site, so every site not patched kept the
defect. These tests check the invariant at the boundary (every reply, every event, every write, every lock)
with data planted in places no earlier test touched, and they use their own word lists, never a copy of
the code's regular expressions.
"""
import json
import os
import re
import socket
import stat
import threading
import time
from unittest import mock

from tests.core_api_support import CoreCase, jsonl, read_jsonl

# An independent list of money words (not derived from core_api.MONEY_KEY).
MONEY_WORDS = {"cost", "costs", "price", "prices", "pricing", "usd", "eur", "gbp", "dollar", "dollars", "cent",
               "cents", "spend", "spent", "money", "balance", "credit", "credits", "billing", "bill", "fee", "fees",
               "amount", "charge", "charges", "invoice", "payment", "payments", "budget", "currency"}
PLANT = 7654321
ROUTING = {"profile": "p", "providers": ["a"], "rows": []}
READS = {"schema.get", "state.get", "units.list", "units.get", "units.history", "runs.list", "usage.get",
         "usage.history", "ci.get", "versions.get", "history.list", "decisions.list", "queries.list", "queries.get",
         "health.get", "events.subscribe", "query.ask", "units.activity", "changes.list", "changes.diff",
         "messages.list", "routing.get", "commands.get", "commands.list"}
EXPECTED_SCOPES = {  # API-19 plus the owner decisions of 2026-11-21, written out by hand
    "read": READS,
    "agent": READS | {"context.push"},
    "orchestrator": READS | {"context.push", "query.reply"},
    "confirm": READS | {"context.push", "control.pause", "control.resume", "control.handoff", "unit.cancel"},
    "owner": READS | {"context.push", "query.reply", "control.pause", "control.resume", "control.handoff",
                      "unit.cancel", "control.stop", "decision.answer", "unit.verdict", "unit.message", "routing.set"},
}


def raw(path):
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(path)
    return s


def ask(s, obj):
    s.sendall((json.dumps(obj) + "\n").encode())
    return json.loads(s.makefile().readline())


def money_keys(value):
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            words = set(re.split(r"[^a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1_\2", str(k)).lower()))
            if words & MONEY_WORDS:
                found.append(k)
            found += money_keys(v)
    elif isinstance(value, list):
        for v in value:
            found += money_keys(v)
    return found


class Helpers:
    def confirmed(self, client, method, params):
        try:
            client.call(method, params)
        except self.mod.ApiError as error:
            confirm = error.data["confirm_id"]
        return client.call(method, {**params, "confirm_id": confirm})

    def confirm_id(self, client, method, params):
        try:
            client.call(method, params)
        except self.mod.ApiError as error:
            return error.data["confirm_id"]
        self.fail("no confirm was asked")

    def wait_for(self, predicate, seconds=4):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()

    def inproc(self, scope="read", token=None):
        token = token or self.mod._mint_token(str(self.records), scope)
        client = self.mod.in_process(self.service, token)
        client.call("initialize", {"protocol": self.mod.PROTOCOL_VERSION})
        return client

    def refusals(self):
        """The refusal rows (API-23): audit.jsonl and its one rotation."""
        return read_jsonl(self.records / "audit.jsonl.1") + read_jsonl(self.records / "audit.jsonl")

    def all_audit(self):
        rows = []
        for name in ("commands.jsonl", "audit.jsonl", "audit.jsonl.1"):
            rows += read_jsonl(self.records / name)
        return rows

    def assertNoMoney(self, value, label):
        text = json.dumps(value)
        self.assertNotIn(str(PLANT), text, label)
        self.assertEqual([], money_keys(value), label)


# --- no-money: one filter at every output boundary ---------------------------------------------------

class NoMoneyRound4(Helpers, CoreCase):
    def seed(self):
        super().seed()
        jsonl(self.records / "units.jsonl", [
            {"id": "U-0001", "kind": "child", "state": "running", "title": {"price": PLANT}},
            {"id": "U-0002", "kind": "child", "state": "cost_usd", "title": "t"}])
        state = json.loads((self.records / "state.json").read_text())
        state["phase"] = {"usd": PLANT}
        state["extra_detail"] = {"pricing": PLANT, "fee": PLANT, "amount": PLANT, "charge": PLANT,
                                 "invoice": PLANT, "payment": PLANT, "budget": PLANT, "eur": PLANT, "gbp": PLANT,
                                 "cents": PLANT, "planFee": PLANT}
        (self.records / "state.json").write_text(json.dumps(state))
        jsonl(self.records / "activity.jsonl", [{"unit_id": "U-0001", "ts": {"usd": PLANT}, "kind": "tool",
                                                  "name": "edit", "text": {"price": PLANT}}])
        jsonl(self.records / "history.jsonl", [{"id": "H-1", "summary": "spent $12.50 on the run"}])
        jsonl(self.records / "messages.jsonl", [{"id": "M-1", "ts": 1, "role": "orchestrator",
                                                  "text": "That was EUR 40 and 3.20 USD"}])

    def test_commands_queries_activity_and_answers_carry_no_money(self):  # TERM-36
        owner = self.client("owner")
        command = self.confirmed(owner, "control.pause", {})["command_id"]
        jsonl(self.records / "command_acks.jsonl", [{"type": "ack", "command_id": command, "action": "pause",
                                                      "state": "done", "reason": "ok", "ts": {"usd": PLANT}, "v": 1}])
        query = self.client("confirm").call("query.ask", {"text": "Rewrite the parser?", "mode": "forward"})["query_id"]
        jsonl(self.records / "queries.jsonl", [{"query_id": query, "state": "answered", "text": {"price": PLANT},
                                                "tokens_used": 1, "by": "x", "ts": 1}])
        client = self.client("read")
        for method, params in (("commands.get", {"id": command}), ("commands.list", {}), ("queries.get", {"id": query}),
                               ("queries.list", {}), ("units.activity", {"id": "U-0001"}), ("runs.list", {}),
                               ("state.get", {}), ("units.list", {})):
            self.assertNoMoney(client.call(method, params), method)
        for question in ("What is U-0001 doing?", "What is the status?"):
            self.assertNoMoney(client.call("query.ask", {"text": question}), question)

    def test_money_amounts_in_text_are_removed(self):  # TERM-36
        client = self.client("read")
        for method in ("history.list", "messages.list"):
            text = json.dumps(client.call(method))
            for amount in ("12.50", "EUR 40", "3.20 USD"):
                self.assertNotIn(amount, text, method)

    def test_event_envelopes_carry_no_money(self):  # TERM-36, API-14
        with open(self.records / "events.jsonl", "a") as handle:
            handle.write(json.dumps({"seq": 6, "ts": {"credits": PLANT}, "kind": "unit.updated", "v": 1,
                                     "data": {"summary": "paid $99 for it"}}) + "\n")
        self.service.stop()
        self.start()
        events = list(self.inproc().subscribe({"from_seq": 5, "limit": 1}))
        self.assertNoMoney(events, "event")
        self.assertNotIn("$99", json.dumps(events))

    def test_cursor_expired_snapshot_is_clean(self):  # API-12, TERM-36: r3 noted the old check passed when nothing was raised
        self.service.stop()
        self.start(event_buffer=2)
        with self.assertRaises(self.mod.ApiError) as raised:
            self.client("read").call("events.subscribe", {"from_seq": 0})
        self.assertEqual("cursor_expired", raised.exception.code)
        self.assertNoMoney(raised.exception.data["snapshot"], "snapshot")


# --- deadlines: one absolute deadline for the whole call ---------------------------------------------

class DeadlineRound4(Helpers, CoreCase):
    service_options = {"method_seconds": 0.3, "lock_seconds": 3.0}

    def test_a_mutation_past_its_deadline_writes_nothing(self):  # API-24
        query = self.client("confirm").call("query.ask", {"text": "Rewrite the parser?", "mode": "forward"})["query_id"]
        jsonl(self.records / "decisions.jsonl", [{"id": "D-0001", "text": "Go?", "options": ["yes", "no"]}])
        owner = self.client("owner")
        real = self.mod._append_jsonl

        def slow(path, row):
            if os.path.basename(str(path)) == "commands.jsonl" and row.get("outcome") == "intent":
                time.sleep(0.5)
            return real(path, row)
        cases = (("context.push", {"kind": "note", "text": "x"}, "inbox.jsonl", 1),
                 ("query.reply", {"query_id": query, "text": "no"}, "queries.jsonl", 0),
                 ("decision.answer", {"id": "D-0001", "choice": "yes"}, "decisions.jsonl", 1))
        for method, params, record, rows_before in cases:
            with self.subTest(method):
                with mock.patch.object(self.mod, "_append_jsonl", slow):
                    self.assertEqual("timeout", self.error_code(owner, method, params))
                self.assertEqual(rows_before, len(read_jsonl(self.records / record)), method)

    def test_a_lock_wait_is_bounded_by_the_remaining_method_time(self):  # API-24
        held, release = threading.Event(), threading.Event()

        def hold():
            with self.service._lock:
                held.set()
                release.wait(1.5)
        threading.Thread(target=hold, daemon=True).start()
        held.wait(2)
        self.addCleanup(release.set)
        agent = self.client("agent")
        started = time.monotonic()
        self.assertEqual("timeout", self.error_code(agent, "context.push", {"kind": "note", "text": "x"}))
        self.assertLess(time.monotonic() - started, 1.0)
        release.set()
        self.assertEqual([], self.inbox())

    def test_commands_reads_check_the_deadline(self):  # API-24
        def slow_acks():
            time.sleep(0.5)
            return {}
        client = self.client("read")
        with mock.patch.object(self.service, "_acks", slow_acks):
            self.assertEqual("timeout", self.error_code(client, "commands.list"))


# --- audit: change rows, refusals, durability ----------------------------------------------------------

class AuditRound4(Helpers, CoreCase):
    def test_a_failed_audit_after_the_effect_returns_the_effect(self):  # API-23
        real = self.service._audit_write

        def fail_ok(row, *args, **kwargs):
            if row.get("outcome") == "ok":
                raise self.mod.ApiError("unavailable", "the audit log cannot be written")
            return real(row, *args, **kwargs)
        agent = self.client("agent")
        with mock.patch.object(self.service, "_audit_write", fail_ok):
            result = agent.call("context.push", {"kind": "note", "text": "hello"})
        self.assertTrue(result["push_id"])
        self.assertEqual(1, len(self.inbox()))

    def test_intent_and_ok_rows_share_one_operation_id(self):  # API-23
        self.client("agent").call("context.push", {"kind": "note", "text": "hello"})
        rows = [r for r in self.audit() if r.get("method") == "context.push"]
        self.assertEqual(["intent", "ok"], [r["outcome"] for r in rows])
        self.assertTrue(rows[0].get("op"))
        self.assertEqual(rows[0]["op"], rows[1].get("op"))

    def test_a_confirm_request_is_audited_on_its_own_even_past_the_refusal_budget(self):  # API-23
        client = self.client("confirm")
        for _ in range(55):
            self.error_code(client, "control.stop")
        self.assertEqual("confirm_required", self.error_code(client, "control.pause"))
        self.assertIn(("control.pause", "confirm_required"), {(r.get("method"), r.get("outcome")) for r in self.audit()})

    def test_every_refusal_is_its_own_audit_row_with_no_hold_past_fifty(self):  # API-23 (rewritten, owner 2026-11-21)
        client = self.client("read")
        for n in range(1, 81):
            self.error_code(client, "control.pause")
            # durable before the reply: the row is there while the connection is still open
            self.assertEqual(n, len([r for r in self.refusals() if r.get("method") == "control.pause"]), n)
        rows = [r for r in self.refusals() if r.get("method") == "control.pause"]
        self.assertEqual({"forbidden"}, {r["outcome"] for r in rows})
        self.assertTrue(all(r["caller"] and r["ts"] for r in rows))
        self.assertEqual([], [r for r in self.audit() if r.get("method") == "control.pause"])  # no refusal in commands

    def test_each_refusal_row_keeps_its_own_outcome(self):  # API-23 (rewritten, owner 2026-11-21)
        client = self.client("agent")
        for _ in range(53):
            self.error_code(client, "context.push", {})
        for _ in range(2):
            self.error_code(client, "context.push", {"kind": "note", "text": "x" * 3000})
        outcomes = [r["outcome"] for r in self.refusals() if r.get("method") == "context.push"]
        self.assertEqual(["invalid_params"] * 53 + ["too_large"] * 2, outcomes)

    def test_over_cap_connections_are_audited(self):  # API-23, API-24 (rewritten, owner 2026-11-21)
        self.service.stop()
        self.start(max_connections=1)
        held = raw(self.socket)
        self.addCleanup(held.close)
        time.sleep(0.1)
        extra = raw(self.socket)
        self.addCleanup(extra.close)
        reply = ask(extra, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocol": "1.0"}})
        self.assertEqual("too_many_connections", reply["error"]["data"]["code"])
        self.assertIn(("connection", "too_many_connections"), {(r.get("method"), r.get("outcome")) for r in self.refusals()})

    def test_audit_values_are_validated_per_field(self):  # API-23 (no secret in either audit file)
        jsonl(self.records / "decisions.jsonl", [{"id": "D-0001", "text": "Go?", "options": ["yes", "no"]}])
        owner = self.client("owner")
        owner.call("context.push", {"kind": "hunter2pass", "text": "x"})
        self.error_code(owner, "unit.cancel", {"id": "pw-hunter2x"})
        self.error_code(owner, "decision.answer", {"id": "D-0001", "choice": "s3cr3tvalue"})
        text = (self.records / "commands.jsonl").read_text() + json.dumps(self.refusals())
        for secret in ("hunter2pass", "pw-hunter2x", "s3cr3tvalue"):
            self.assertNotIn(secret, text)

    def test_record_files_the_core_appends_to_are_private(self):  # API-23, API-18
        for name in ("commands.jsonl", "inbox.jsonl"):
            os.chmod(self.records / name, 0o644)
        self.client("agent").call("context.push", {"kind": "note", "text": "x"})
        for name in ("commands.jsonl", "inbox.jsonl"):
            self.assertEqual(0, stat.S_IMODE(os.stat(self.records / name).st_mode) & 0o077, name)

    def test_a_short_write_leaves_no_partial_row(self):  # API-10
        path = self.dir / "short.jsonl"
        real = os.write
        with mock.patch.object(self.mod.os, "write", lambda fd, data: real(fd, data[:10])):
            with self.assertRaises(OSError):
                self.mod._append_jsonl(path, {"a": "x" * 50})
        self.mod._append_jsonl(path, {"b": 1})
        self.assertEqual([{"b": 1}], read_jsonl(path))


class AuditEvictionRound4(Helpers, CoreCase):
    service_options = {"audit_max_bytes": 3000}

    def test_reconnecting_refusals_cannot_push_change_rows_out(self):  # API-23
        jsonl(self.records / "commands.jsonl", [{"type": "pad", "pad": "p" * 100} for _ in range(40)])
        self.client("agent").call("context.push", {"kind": "note", "text": "keep me"})
        for _ in range(6):
            anonymous = self.client(scope=None)
            for _ in range(45):
                self.error_code(anonymous, "state.get")
            anonymous.close()
        self.assertTrue(self.wait_for(lambda: (self.records / "audit.jsonl.1").exists()))
        self.assertIn(("context.push", "intent"), {(r.get("method"), r.get("outcome")) for r in self.all_audit()})


# --- events: filters, values, sequence, isolation, local subscriptions ---------------------------------

class EventsRound4(Helpers, CoreCase):
    def test_malformed_filters_are_refused(self):  # API-13
        client = self.client("read")
        for event_filter in ({"kinds": 1}, {"kinds": "unit.updated"}, {"kinds": ["no.such.kind"]}, {"unit_id": 5},
                             {"colour": "red"}):
            with self.subTest(event_filter):
                self.assertEqual("invalid_params", self.error_code(client, "events.subscribe",
                                                                   {"from_seq": 5, "filter": event_filter}))

    def test_one_failing_subscriber_does_not_break_publishing(self):  # API-13
        bad = self.inproc().subscribe({"from_seq": 5})
        good = self.inproc().subscribe({"from_seq": 5})
        self.addCleanup(bad.close)
        with mock.patch.object(self.service._subs[0], "offer", side_effect=TypeError("broken")):
            event = self.service.emit("unit.updated", {"summary": "s"}, unit_id="U-0001")
        self.assertEqual(6, event["seq"])
        self.assertEqual(6, next(good)["seq"])

    def test_a_future_cursor_is_refused(self):  # API-12
        self.assertEqual("invalid_params", self.error_code(self.client("read"), "events.subscribe", {"from_seq": 99}))

    def test_the_drop_notice_is_a_transport_notice_and_every_event_keeps_its_envelope(self):  # API-11, API-13
        self.service.stop()
        self.start(event_buffer=3)
        client = self.inproc()
        client.call("events.subscribe", {"from_seq": 5})
        sub, client.session.pending_pump = client.session.pending_pump, None
        for i in range(6):
            self.service.emit("unit.updated", {"summary": "e%d" % i}, unit_id="U-0001")
        wire = list(self.service._stream(sub))  # the messages both adapters send, in order
        events = [m["params"] for m in wire if m["method"] == "events.event"]
        self.assertEqual([6, 7, 8], [e["seq"] for e in events])  # gap-free, then the overflow
        for event in events:  # API-11 envelope on every events.event, no exception
            self.assertEqual({"seq", "ts", "kind", "data", "v"}, set(event) - {"unit_id", "session"})
            self.assertIsInstance(event["seq"], int)
        self.assertEqual([{"reason": "slow_consumer", "last_seq": 8}],
                         [m["params"] for m in wire if m["method"] == "events.dropped"])
        self.assertEqual(["events.dropped", "events.end"], [m["method"] for m in wire[-2:]])
        self.assertNotIn("subscription.dropped", self.client("read").call("schema.get")["event_kinds"])

    def test_event_values_carry_no_secrets_or_transcripts(self):  # API-14
        token = self.mod._mint_token(str(self.records), "owner")
        self.service.emit("unit.updated", {"summary": "use %s now" % token}, unit_id="U-0001")
        self.service.emit("alert.guard", {"why": "Authorization: Bearer abcdefghijklmnop1234"})
        self.service.emit("unit.updated", {"summary": "done\nUSER: secret transcript line"}, unit_id="U-0001")
        stream = json.dumps(list(self.inproc().subscribe({"from_seq": 5, "limit": 3})))
        stored = (self.records / "events.jsonl").read_text()
        for text in (stream, stored):
            for secret in (token, "abcdefghijklmnop1234", "secret transcript line"):
                self.assertNotIn(secret, text)

    def test_a_stored_row_with_an_odd_kind_does_not_end_the_stream(self):  # API-11
        with open(self.records / "events.jsonl", "a") as handle:
            handle.write(json.dumps({"seq": 6, "ts": 1, "kind": ["x"], "data": {}, "v": 1}) + "\n")
            handle.write(json.dumps({"seq": 7, "ts": 2, "kind": "unit.updated", "data": {}, "v": 1}) + "\n")
        self.service.stop()
        self.start()
        events = list(self.inproc().subscribe({"from_seq": 5, "limit": 2}))
        self.assertEqual(7, events[-1]["seq"])

    def test_closing_a_local_subscription_frees_its_slot(self):  # API-6, API-24
        self.service.stop()
        self.start(max_subscriptions_per_token=1)
        client = self.inproc()
        client.subscribe({"from_seq": 5}).close()  # closed before its first event
        client.subscribe({"from_seq": 5}).close()
        self.assertEqual([], self.service._subs)

    def test_closing_an_in_process_client_ends_its_subscriptions(self):  # API-6, API-24
        client = self.inproc()
        client.subscribe({"from_seq": 5})
        client.close()
        self.assertEqual([], self.service._subs)


# --- transport and identity ------------------------------------------------------------------------------

class TransportRound4(Helpers, CoreCase):
    def test_the_handshake_gate_comes_before_params_checks(self):  # API-5
        reply = ask(raw(self.socket), {"jsonrpc": "2.0", "id": 1, "method": "state.get", "params": [1]})
        self.assertEqual("not_initialized", reply["error"]["data"]["code"])

    def test_server_identity_uses_the_effective_uid(self):  # API-3
        with mock.patch.object(self.mod.os, "getuid", return_value=os.geteuid() + 1):
            service = self.mod.CoreService(str(self.records), str(self.dir / "x.sock"))
        self.assertEqual(os.geteuid(), service.expected_uid)

    def test_an_unauthenticated_connection_must_initialize_in_time(self):  # API-24 (connection slots)
        self.service.stop()
        with mock.patch.object(self.mod, "HANDSHAKE_SECONDS", 0.3, create=True):  # the default, read at start
            self.start()
        s = raw(self.socket)
        s.settimeout(0.2)
        started, closed = time.monotonic(), False
        while time.monotonic() - started < 2.0:
            try:
                s.sendall(b'{"jsonrpc":"2.0","id":1,"method":"state.get"}\n')
                if s.recv(65536) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
            time.sleep(0.1)
        self.assertTrue(closed)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_only_unix_sockets_are_made(self):  # API-2 GUARD: replaces the lsof check, which hid lsof errors
        families = []
        real = socket.socket

        def spy(family=-1, *args, **kwargs):
            families.append(family)
            return real(family, *args, **kwargs)
        self.service.stop()
        with mock.patch.object(self.mod.socket, "socket", spy):
            self.start()
        self.client("read").call("state.get")
        self.assertTrue(families)
        self.assertEqual({socket.AF_UNIX}, set(families))

    def test_a_socket_path_of_104_bytes_is_refused(self):  # API-2 GUARD
        long_path = str(self.dir / ("s" * (104 - len(str(self.dir)) - 1)))
        self.assertEqual(104, len(long_path.encode()))
        with self.assertRaises(ValueError):
            self.mod.CoreService(str(self.records), long_path).start()

    def test_a_probe_error_never_removes_the_socket(self):  # API-2 GUARD
        path = str(self.dir / "stale.sock")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.close()
        with mock.patch.object(socket.socket, "connect", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(RuntimeError):
                self.mod.CoreService._clear_stale(path)
        self.assertTrue(os.path.exists(path))

    def test_a_symlinked_socket_receives_nothing(self):  # API-18 GUARD: the r3 symlink test only checked the audit
        path = str(self.dir / "fake.sock")
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen(1)
        listener.settimeout(1)
        self.addCleanup(listener.close)
        link = str(self.dir / "link.sock")
        os.symlink(path, link)
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
        client = self.mod.CoreClient(link, token="SECRET-TOKEN")
        self.addCleanup(client.close)
        with self.assertRaises(self.mod.ApiError):
            client.call("initialize", {"protocol": "1.0"})
        thread.join(2)
        self.assertEqual([], got)


# --- tokens ------------------------------------------------------------------------------------------------

class TokensRound4(Helpers, CoreCase):
    def test_a_reader_never_sees_a_half_written_token_store(self):  # API-18
        token = self.mod._mint_token(str(self.records), "read")
        real = os.write
        seen = []

        def spy(fd, data):
            if b'"tokens"' in bytes(data) and not seen:
                seen.append(self.service._lookup_token(token))
            return real(fd, data)
        with mock.patch.object(self.mod.os, "write", spy):
            self.mod._mint_token(str(self.records), "read")
        self.assertEqual(1, len(seen))
        self.assertIsNotNone(seen[0])

    def test_a_damaged_token_store_is_left_alone(self):  # API-18
        path = self.records / "tokens.json"
        path.write_text('{"tokens": [{"id": "abc"')
        os.chmod(path, 0o600)
        with self.assertRaises(self.mod.ApiError) as raised:
            self.mod._mint_token(str(self.records), "read")
        self.assertEqual("unavailable", raised.exception.code)
        self.assertEqual('{"tokens": [{"id": "abc"', path.read_text())

    def test_the_token_file_is_checked_on_the_descriptor_it_reads(self):  # API-18, TERM-8
        path = self.dir / "token"
        path.write_text("file-token\n")
        os.chmod(path, 0o644)
        fake = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 1, os.geteuid(), 0, 11, 0, 0, 0))
        with mock.patch.object(self.mod.os, "lstat", return_value=fake):
            with self.assertRaises(PermissionError):
                self.mod.resolve_token(None, str(path), {})
        link = self.dir / "token-link"
        os.chmod(path, 0o600)
        os.symlink(path, link)
        with self.assertRaises(PermissionError):
            self.mod.resolve_token(None, str(link), {})


# --- reads: health, truncation, neutrality -----------------------------------------------------------------

class ReadsRound4(Helpers, CoreCase):
    def test_health_reports_damage_in_every_store(self):  # API-9
        for name in ("history.jsonl", "inbox.jsonl", "decisions.jsonl", "queries.jsonl", "command_acks.jsonl",
                     "usage.jsonl", "commands.jsonl"):
            (self.records / name).write_text('{"ok": 1}\nnot json\n')
        codes = {f["code"] for f in self.client("read").call("health.get")["findings"]}
        for label in ("history", "inbox", "decisions", "queries", "command_acks", "usage", "commands"):
            self.assertIn("%s_damaged" % label, codes)

    def test_a_cut_read_says_incomplete_never_complete(self):  # API-8
        jsonl(self.records / "units.jsonl", [{"id": "U-%04d" % i, "state": "running", "pad": "y" * 50}
                                               for i in range(200)])
        client = self.client("read")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            self.assertTrue(client.call("units.list").get("incomplete"))
            self.assertEqual("unknown", client.call("runs.list")["total"])
            self.assertEqual("unavailable", self.error_code(client, "units.get", {"id": "U-0000"}))

    def test_a_command_whose_ack_or_row_was_cut_is_not_reported_as_requested_or_missing(self):  # API-8
        owner = self.client("owner")
        command = self.confirmed(owner, "control.pause", {})["command_id"]
        jsonl(self.records / "command_acks.jsonl", [
            {"type": "ack", "command_id": command, "action": "pause", "state": "done", "reason": "ok", "ts": 1, "v": 1}]
            + [{"type": "ack", "command_id": "C-9%03d" % i, "action": "pause", "state": "done", "reason": "p" * 40,
                "ts": 1, "v": 1} for i in range(100)])
        client = self.client("read")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            self.assertEqual("unknown", client.call("commands.get", {"id": command})["command"]["state"])
        with open(self.records / "commands.jsonl", "a") as handle:
            for _ in range(60):
                handle.write(json.dumps({"type": "pad", "pad": "p" * 100}) + "\n")
        with mock.patch.object(self.mod, "READ_MAX_BYTES", 3000):
            self.assertEqual("unavailable", self.error_code(client, "commands.get", {"id": command}))

    def test_history_and_usage_rows_keep_provider_detail_under_extra(self):  # API-25
        jsonl(self.records / "history.jsonl", [{"id": "H-1", "summary": "ran", "codex_session": "abc", "model": "m"}])
        jsonl(self.records / "usage.jsonl", [{"ts": 1, "pct": 3, "claude_window": "5h"}])
        client = self.client("read")
        row = client.call("history.list")["history"][0]
        sample = client.call("usage.history")["samples"][0]
        self.assertEqual({"id", "summary", "extra"}, set(row))
        self.assertEqual({"codex_session": "abc", "model": "m"}, row["extra"])
        self.assertEqual({"ts", "pct", "extra"}, set(sample))

    def test_canned_answers_on_damaged_records_say_unknown(self):  # API-15, API-8
        (self.records / "state.json").write_text("{broken")
        (self.records / "units.jsonl").write_text("not json\n")
        text = self.client("read").call("query.ask", {"text": "What is the status?"})["text"]
        self.assertIn("Phase unknown", text)
        self.assertNotIn("0 units", text)  # a damaged units record gives no count, not a guessed zero


# --- delivery at a safe point: a strict token bound and no lost text ---------------------------------------

class DeliveryRound4(Helpers, CoreCase):
    def test_the_block_is_bounded_by_bytes_a_strict_token_upper_bound(self):  # API-16
        client = self.client("agent")
        for i in range(8):
            client.call("context.push", {"kind": "note", "text": "%d %s" % (i, "word " * 40)})
        block = self.service.deliver_at_safe_point()["text"]
        self.assertTrue(block)
        self.assertLessEqual(len(block.encode("utf-8")), 300)  # each token is at least one byte

    def test_a_long_message_is_delivered_in_parts_until_complete(self):  # API-16
        text = "A" * 700 + "B" * 300
        push_id = self.client("agent").call("context.push", {"kind": "note", "text": text})["push_id"]
        parts, done = [], False
        for _ in range(20):
            block = self.service.deliver_at_safe_point()
            for line in block["text"].splitlines()[1:]:
                entry = json.loads(line)
                if entry["id"] == push_id:
                    parts.append(entry["text"])
            if push_id in block["push_ids"]:
                done = True
                break
        self.assertTrue(done)
        self.assertEqual(text, "".join(parts))
        self.assertEqual("", self.service.deliver_at_safe_point()["text"])


# --- GUARD: scope matrix, handler table, racing confirms, replaced-test gaps -------------------------------

class CoverageRound4(Helpers, CoreCase):
    service_options = {"max_subscriptions": 16}

    def seed(self):
        super().seed()
        jsonl(self.records / "decisions.jsonl", [{"id": "D-0001", "text": "Go?", "options": ["yes", "no"]}])
        jsonl(self.records / "inbox.jsonl", [{"query_id": "Q-0001", "kind": "query", "text": "Rewrite?", "source": "x",
                                              "trust": "untrusted", "ts": 1, "ttl": 3600, "expires": 10 ** 12}])
        jsonl(self.records / "commands.jsonl", [{"type": "command", "command_id": "C-0001", "action": "pause",
                                                 "unit_id": None, "caller": "x", "ts": 1, "v": 1, "state": "requested"}])
        jsonl(self.records / "diffs.jsonl", [{"path": "a.py", "diff": "+x", "ts": 1}])

    # Valid params per method and what an allowed call must give: a result key, or the confirm step (API-21).
    CALLS = {
        "schema.get": ({}, "methods"), "state.get": ({}, "state"), "units.list": ({}, "units"),
        "units.get": ({"id": "U-0001"}, "unit"), "units.history": ({"id": "U-0001"}, "records"),
        "runs.list": ({}, "total"), "usage.get": ({}, "usage"), "usage.history": ({}, "samples"),
        "ci.get": ({}, "ci"), "versions.get": ({}, "versions"), "history.list": ({}, "history"),
        "decisions.list": ({}, "decisions"), "queries.list": ({}, "queries"), "queries.get": ({"id": "Q-0001"}, "query"),
        "health.get": ({}, "findings"), "events.subscribe": ({"from_seq": 5, "limit": 1}, "subscribed"),
        "query.ask": ({"text": "What is the status?"}, "answered_by"), "units.activity": ({"id": "U-0001"}, "calls"),
        "changes.list": ({}, "worktrees"), "changes.diff": ({"path": "a.py"}, "diff"), "messages.list": ({}, "messages"),
        "routing.get": ({}, "routing"), "commands.get": ({"id": "C-0001"}, "command"), "commands.list": ({}, "commands"),
        "context.push": ({"kind": "note", "text": "x"}, "push_id"),
        "query.reply": ({"query_id": "Q-0001", "text": "ok"}, "outcome"),
        "control.pause": ({}, "confirm_required"), "control.resume": ({}, "confirm_required"),
        "control.handoff": ({}, "confirm_required"), "control.stop": ({}, "confirm_required"),
        "unit.cancel": ({"id": "U-0001"}, "confirm_required"), "routing.set": (ROUTING, "confirm_required"),
        "decision.answer": ({"id": "D-0001", "choice": "yes"}, "outcome"),
        "unit.verdict": ({"id": "U-0001", "verdict": "pass"}, "outcome"),
        "unit.message": ({"id": "U-0001", "text": "hi"}, "command_id"),
    }

    EFFECT_FILES = {"context.push": "inbox.jsonl", "query.reply": "queries.jsonl", "decision.answer": "decisions.jsonl",
                    "unit.verdict": "units.jsonl", "unit.message": "commands.jsonl"}  # where the change is recorded

    def test_every_method_against_every_scope(self):  # API-19 (valid params; an allowed call records its change, a refused one none)
        self.assertEqual(EXPECTED_SCOPES["owner"], set(self.CALLS))

        def size(method):
            name = self.EFFECT_FILES.get(method)
            path = self.records / name if name else None
            return path.stat().st_size if path is not None and path.exists() else 0
        for scope, allowed in EXPECTED_SCOPES.items():
            client = self.client(scope)
            for method, (params, expect) in sorted(self.CALLS.items()):
                with self.subTest(scope=scope, method=method):
                    before = size(method)
                    try:
                        result, code = client.call(method, params), None
                    except self.mod.ApiError as error:
                        result, code = None, error.code
                    if method not in allowed:
                        self.assertEqual("forbidden", code)
                        self.assertEqual(before, size(method), "a refused call changed a record")
                    elif expect == "confirm_required":
                        self.assertEqual("confirm_required", code)
                        self.assertEqual(before, size(method), "an unconfirmed call changed a record")
                    else:
                        self.assertIsNone(code)
                        self.assertIn(expect, result)
                        if method in self.EFFECT_FILES:
                            self.assertGreater(size(method), before, "an allowed call recorded nothing")

    def test_the_handler_table_equals_the_declared_methods(self):  # API-6 GUARD
        declared = set(self.client("read").call("schema.get")["methods"])
        self.assertEqual(declared, set(self.service._handlers))
        self.assertEqual(EXPECTED_SCOPES["owner"], declared)

    def test_two_racing_confirms_execute_once(self):  # API-21 GUARD
        token = self.mod._mint_token(str(self.records), "confirm")
        a, b = self.client(token=token), self.client(token=token)
        cid = self.confirm_id(a, "control.pause", {})
        before = len([r for r in self.audit() if r.get("type") == "command"])
        results, barrier = [], threading.Barrier(2)

        def go(client):
            barrier.wait()
            try:
                results.append(client.call("control.pause", {"confirm_id": cid})["state"])
            except self.mod.ApiError as error:
                results.append(error.code)
        threads = [threading.Thread(target=go, args=(c,)) for c in (a, b)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        self.assertEqual(["confirm_invalid", "requested"], sorted(results))
        self.assertEqual(before + 1, len([r for r in self.audit() if r.get("type") == "command"]))

    def test_a_cancel_writes_no_stop_record_and_why_stopped_says_requested(self):  # API-15 GUARD (retired test had cancel)
        command = self.confirmed(self.client("owner"), "unit.cancel", {"id": "U-0001"})
        self.assertEqual("requested", command["state"])
        self.assertEqual([], [r for r in read_jsonl(self.records / "history.jsonl") if r.get("type") == "stop"])
        text = self.client("read").call("query.ask", {"text": "Why did U-0001 stop?"})["text"]
        self.assertIn(command["command_id"], text)
        self.assertIn("requested", text)

    def test_a_stop_acked_done_without_a_stop_record_says_done(self):  # API-15 GUARD (r3: the old test seeded refused)
        command = self.confirmed(self.client("owner"), "control.stop", {})["command_id"]
        jsonl(self.records / "command_acks.jsonl", [{"type": "ack", "command_id": command, "action": "stop",
                                                      "state": "done", "reason": "stopped", "ts": 9, "v": 1}])
        text = self.client("read").call("query.ask", {"text": "Why did it stop?"})["text"]
        self.assertIn("done", text)
        self.assertIn(command, text)

    def test_concurrent_routing_sets_all_succeed(self):  # TERM-35 GUARD (r3: the old test dropped thread errors)
        owners = [self.client("owner") for _ in range(4)]
        results = []

        def go(i, owner):
            try:
                results.append(self.confirmed(owner, "routing.set", {**ROUTING, "profile": "p%d" % i})["outcome"])
            except Exception as error:  # noqa: BLE001 - every failure is collected and reported
                results.append(repr(error))
        threads = [threading.Thread(target=go, args=(i, o)) for i, o in enumerate(owners)]
        [t.start() for t in threads]
        [t.join(10) for t in threads]
        self.assertEqual(["saved"] * 4, results)
