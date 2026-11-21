"""Core API round 2 (owner decisions 2026-11-21): stop records (API-15), neutral keys (API-25) and the
terminal's gaps (TERM-0.4.0-api-gaps.md items 1-8 and 10).

Record formats the core reads (the runner writes them; the core writes stop and routing records itself):
  history.jsonl  stop record {"type":"stop","id","code","reason","by":"owner|cocolo|limit","ts","next":str|null,
                 "unit_id":optional,"v":1}
  activity.jsonl {"unit_id","ts","kind":"tool|output","name"?,"text"}
  changes.json   {"worktrees":[{"name","branch","files":[{"path","status"}],"commits":[{"id","summary"}]}]}
  diffs.jsonl    {"path","diff","ts"}
  messages.jsonl {"id","ts","role","text","unit_id"?}
  routing.json   {"profile","providers","rows":[{"task","provider","model","effort","fallback"}],"set_by","ts"}
"""
import json
import os

from tests.core_api_support import CoreCase, jsonl, read_jsonl

STOP = {"type": "stop", "id": "H-2", "code": "guard_context", "reason": "The context window was nearly full.",
        "by": "cocolo", "ts": 1_000_100, "next": "Start a new session from the hand-off.", "v": 1}


class StopRecordTests(CoreCase):
    def ask(self, text):
        return self.client("read").call("query.ask", {"text": text, "mode": "records_only"})

    def test_api15_why_stopped_reads_the_stop_record_not_the_last_entry(self):
        jsonl(self.records / "history.jsonl", [
            {"id": "H-1", "summary": "an earlier note"}, STOP, {"id": "H-3", "summary": "UNRELATED LATER ENTRY"}])
        answer = self.ask("Why did the last unit stop?")
        for part in ("CoCoLo", "The context window was nearly full.", "Start a new session from the hand-off."):
            self.assertIn(part, answer["text"])
        self.assertNotIn("UNRELATED", answer["text"])
        self.assertEqual(["history.jsonl"], answer["sources"])

    def test_api15_unit_specific_why_stopped(self):
        other = dict(STOP, id="H-4", unit_id="U-0002", reason="Its tests failed twice.", by="limit", **{"next": None})
        jsonl(self.records / "history.jsonl", [dict(STOP, unit_id="U-0001"), other])
        text = self.ask("Why did U-0002 stop?")["text"]
        self.assertIn("Its tests failed twice.", text)
        self.assertNotIn("context window", text)

    def test_api15_no_stop_record_is_unknown(self):
        jsonl(self.records / "history.jsonl", [{"id": "H-1", "summary": "a note"}])
        self.assertIn("unknown", self.ask("Why did it stop?")["text"])

    def test_api15_confirmed_stop_and_cancel_write_a_stop_record(self):
        owner = self.client("owner")
        for method, params in (("control.stop", {}), ("unit.cancel", {"id": "U-0001"})):
            try:
                owner.call(method, params)
            except self.mod.ApiError as error:
                owner.call(method, {**params, "confirm_id": error.data["confirm_id"]})
        stops = [r for r in read_jsonl(self.records / "history.jsonl") if r.get("type") == "stop"]
        self.assertEqual(["owner", "owner"], [r["by"] for r in stops])
        self.assertTrue(all(r["code"] and r["reason"] and r["ts"] and r["id"] for r in stops))
        self.assertEqual([None, "U-0001"], [r.get("unit_id") for r in stops])
        self.assertIn("you", self.ask("Why did it stop?")["text"].lower())


class NeutralTests(CoreCase):
    def test_api25_state_get_moves_provider_keys_under_extra_and_keeps_neutral_ones(self):
        state = {"phase": "build", "session": "S-7", "now": "Reviewing", "ctx": 1, "window": 2,
                 "claude_session_id": "abc", "codex_thread": "t1", "usage": {"input": 1}}
        (self.records / "state.json").write_text(json.dumps(state))
        got = self.client("read").call("state.get")["state"]
        self.assertEqual(("build", "S-7", "Reviewing"), (got["phase"], got["session"], got["now"]))
        self.assertNotIn("claude_session_id", got)
        self.assertNotIn("codex_thread", got)
        self.assertEqual({"claude_session_id": "abc", "codex_thread": "t1"}, got["extra"])

    def test_api25_usage_get_has_windows_and_no_money(self):
        usage = {"input": 10, "output": 5, "cost_usd": 3.2, "price": 1,
                 "windows": [{"label": "5h", "pct": 40, "resets": "x", "spend": 9}], "claude_plan": "pro"}
        (self.records / "state.json").write_text(json.dumps({"phase": "x", "usage": usage}))
        got = self.client("read").call("usage.get")["usage"]
        self.assertEqual([{"label": "5h", "pct": 40}], got["windows"])
        self.assertEqual("pro", got["extra"]["claude_plan"])
        for banned in ("cost_usd", "price", "spend", "3.2"):
            self.assertNotIn(banned, json.dumps(got))

    def test_api25_units_have_core_row_fields_neutral_keys_and_no_money(self):
        jsonl(self.records / "units.jsonl", [{"id": "U-1", "kind": "child", "state": "running", "title": "T",
                                              "role": "builder", "started": 5, "last": 9, "result": "r.txt",
                                              "model": "sonnet", "cost": 2.5, "claude_agent_id": "z"}])
        unit = self.client("read").call("units.get", {"id": "U-1"})["unit"]
        self.assertEqual(("builder", 5, 9, "r.txt"), (unit["role"], unit["started"], unit["last"], unit["result"]))
        self.assertNotIn("claude_agent_id", unit)
        self.assertEqual("z", unit["extra"]["claude_agent_id"])
        self.assertEqual("sonnet", unit["extra"]["model"])
        self.assertNotIn("cost", json.dumps(unit))

    def test_api25_handoff_replies_in_state(self):
        (self.records / "state.json").write_text(json.dumps({"phase": "x", "handoff_replies": [
            {"id": "R-1", "ts": 3, "text": "Done.", "by": "worker", "junk": 1}]}))
        got = self.client("read").call("state.get")["state"]
        self.assertEqual([{"id": "R-1", "ts": 3, "text": "Done.", "by": "worker"}], got["handoff_replies"])

    def test_api25_missing_handoff_replies_is_unknown(self):
        self.assertEqual("unknown", self.client("read").call("state.get")["state"]["handoff_replies"])

    def test_term_decision_rows_carry_why_reason_recommended_text(self):
        row = {"id": "D-1", "text": "Merge now?", "why": "CI is green", "reason": "owner choice", "recommended": "yes",
               "options": ["yes", "no"], "internal_claude_note": "x"}
        jsonl(self.records / "decisions.jsonl", [row])
        got = self.client("agent").call("decisions.list")["decisions"][0]
        for key in ("text", "why", "reason", "recommended", "options"):
            self.assertEqual(row[key], got[key])
        self.assertNotIn("internal_claude_note", got)


class ActivityTests(CoreCase):
    def test_term1_units_activity_returns_tool_calls_and_output_tail(self):
        rows = [{"unit_id": "U-0001", "ts": i, "kind": "tool", "name": "edit", "text": "file%d" % i} for i in range(60)]
        rows += [{"unit_id": "U-0001", "ts": 100 + i, "kind": "output", "text": "line %d" % i} for i in range(80)]
        rows += [{"unit_id": "U-0002", "ts": 5, "kind": "output", "text": "OTHER"},
                 {"unit_id": "U-0001", "ts": 7, "kind": "output", "text": "x" * 900, "token": "tok-SECRET"}]
        jsonl(self.records / "activity.jsonl", rows)
        got = self.client("agent").call("units.activity", {"id": "U-0001"})
        self.assertEqual(50, len(got["calls"]))
        self.assertEqual("file59", got["calls"][-1]["text"])
        self.assertLessEqual(len(got["tail"]), 50)
        self.assertNotIn("OTHER", json.dumps(got))
        self.assertNotIn("tok-SECRET", json.dumps(got))
        self.assertTrue(all(len(t["text"]) <= 500 for t in got["tail"]))

    def test_term1_missing_activity_is_unknown_and_unknown_unit_is_not_found(self):
        client = self.client("read")
        self.assertEqual(("unknown", "unknown"), tuple(client.call("units.activity", {"id": "U-0001"})[k]
                                                       for k in ("calls", "tail")))
        self.assertEqual("not_found", self.error_code(client, "units.activity", {"id": "U-9"}))


class ChangesTests(CoreCase):
    LIST = {"worktrees": [{"name": "main", "branch": "m", "files": [{"path": "a.py", "status": "M", "x": 1}],
                           "commits": [{"id": "abc", "summary": "Fix", "extra": 1}]}]}

    def test_term2_changes_list_and_diff(self):
        (self.records / "changes.json").write_text(json.dumps(self.LIST))
        jsonl(self.records / "diffs.jsonl", [{"path": "a.py", "diff": "old", "ts": 1}, {"path": "a.py", "diff": "+new", "ts": 2}])
        client = self.client("agent")
        listed = client.call("changes.list")["worktrees"]
        self.assertEqual([{"path": "a.py", "status": "M"}], listed[0]["files"])
        self.assertEqual([{"id": "abc", "summary": "Fix"}], listed[0]["commits"])
        self.assertEqual("+new", client.call("changes.diff", {"path": "a.py"})["diff"])

    def test_term2_missing_is_unknown_not_found_and_big_diffs_are_cut(self):
        client = self.client("read")
        self.assertEqual("unknown", client.call("changes.list")["worktrees"])
        self.assertEqual("not_found", self.error_code(client, "changes.diff", {"path": "zzz"}))
        jsonl(self.records / "diffs.jsonl", [{"path": "big", "diff": "x" * 200000, "ts": 1}])
        got = client.call("changes.diff", {"path": "big"})
        self.assertTrue(got["truncated"])
        self.assertLessEqual(len(got["diff"].encode()), 65536)
        self.assertEqual("invalid_params", self.error_code(client, "changes.diff", {"path": "a\nb"}))


class MessagesTests(CoreCase):
    def test_term3_messages_list_returns_the_latest_in_order(self):
        jsonl(self.records / "messages.jsonl", [{"id": "M-%d" % i, "ts": i, "role": "orchestrator", "text": "t%d" % i,
                                                 "junk": 1} for i in range(1, 8)])
        client = self.client("agent")
        got = client.call("messages.list", {"limit": 3})["messages"]
        self.assertEqual(["M-5", "M-6", "M-7"], [m["id"] for m in got])
        self.assertNotIn("junk", got[0])
        self.assertEqual(["M-7"], [m["id"] for m in client.call("messages.list", {"since": 6})["messages"]])
        self.assertEqual("invalid_params", self.error_code(client, "messages.list", {"limit": 0}))

    def test_term3_missing_messages_is_unknown(self):
        self.assertEqual("unknown", self.client("read").call("messages.list")["messages"])


class UnitMessageTests(CoreCase):
    def test_term4_unit_message_is_owner_only(self):
        for scope in ("read", "agent", "orchestrator", "confirm"):
            self.assertEqual("forbidden", self.error_code(self.client(scope), "unit.message",
                                                          {"id": "U-0001", "text": "hi"}), scope)

    def test_term4_owner_message_writes_a_runner_command_and_audits_no_text(self):
        owner = self.client("owner")
        result = owner.call("unit.message", {"id": "U-0001", "text": "Please also run the linter."})
        row = [r for r in self.audit() if r.get("type") == "command"][-1]
        self.assertEqual(("message", "U-0001", "Please also run the linter."), (row["action"], row["unit_id"], row["text"]))
        self.assertEqual(row["command_id"], result["command_id"])
        audit = [r for r in self.audit() if r.get("method") == "unit.message"]
        self.assertTrue(audit)
        self.assertNotIn("linter", json.dumps(audit))

    def test_term4_validation(self):
        owner = self.client("owner")
        self.assertEqual("not_found", self.error_code(owner, "unit.message", {"id": "U-9", "text": "hi"}))
        self.assertEqual("too_large", self.error_code(owner, "unit.message", {"id": "U-0001", "text": "é" * 1100}))
        self.assertEqual("invalid_params", self.error_code(owner, "unit.message", {"id": "U-0001", "text": " "}))


class EndpointTests(CoreCase):
    def test_term8_default_socket_path_and_precedence(self):
        m = self.mod
        self.assertEqual("/p/.codex/orchestration/core.sock", m.default_socket_path("/p"))
        self.assertEqual("/a.sock", m.resolve_socket("/a.sock", {"COCOLO_CORE_SOCKET": "/b.sock"}, "/p"))
        self.assertEqual("/b.sock", m.resolve_socket(None, {"COCOLO_CORE_SOCKET": "/b.sock"}, "/p"))
        self.assertEqual("/p/.codex/orchestration/core.sock", m.resolve_socket(None, {}, "/p"))

    def test_term8_token_sources_and_private_token_file(self):
        m = self.mod
        path = self.dir / "token"
        path.write_text("file-token\n")
        os.chmod(path, 0o600)
        self.assertEqual("arg", m.resolve_token("arg", None, {"COCOLO_TOKEN": "env"}))
        self.assertEqual("file-token", m.resolve_token(None, str(path), {"COCOLO_TOKEN": "env"}))
        self.assertEqual("env", m.resolve_token(None, None, {"COCOLO_TOKEN": "env"}))
        self.assertEqual("file-token", m.resolve_token(None, str(path), {}))
        os.chmod(path, 0o644)
        with self.assertRaises(PermissionError):
            m.resolve_token(None, str(path), {})
        self.assertIsNone(m.resolve_token(None, None, {}))

    def test_term8_client_from_environment_reaches_the_service(self):
        token = self.mod._mint_token(str(self.records), "read")
        client = self.mod.CoreClient.from_environment(env={"COCOLO_CORE_SOCKET": self.socket, "COCOLO_TOKEN": token})
        self.addCleanup(client.close)
        client.call("initialize", {"protocol": "1.0"})
        self.assertIn("state", client.call("state.get"))


class RoutingTests(CoreCase):
    ROUTING = {"profile": "balanced", "providers": ["alpha", "beta"],
               "rows": [{"task": "build", "provider": "alpha", "model": "m1", "effort": "high", "fallback": "beta"},
                        {"task": "review", "provider": "beta", "model": "m2", "effort": "medium", "fallback": None}]}

    def set(self, client, routing=None):
        params = routing or self.ROUTING
        try:
            return client.call("routing.set", params)
        except self.mod.ApiError as error:
            self.assertEqual("confirm_required", error.code)
            return client.call("routing.set", {**params, "confirm_id": error.data["confirm_id"]})

    def test_term10_routing_set_is_owner_only_and_needs_confirm(self):
        for scope in ("read", "agent", "orchestrator", "confirm"):
            self.assertEqual("forbidden", self.error_code(self.client(scope), "routing.set", self.ROUTING), scope)
        self.assertEqual("confirm_required", self.error_code(self.client("owner"), "routing.set", self.ROUTING))
        self.assertFalse((self.records / "routing.json").exists())

    def test_term10_set_then_get_round_trips_and_is_private_and_audited(self):
        self.set(self.client("owner"))
        got = self.client("agent").call("routing.get")["routing"]
        self.assertEqual(self.ROUTING["rows"], got["rows"])
        self.assertEqual(("balanced", ["alpha", "beta"]), (got["profile"], got["providers"]))
        self.assertEqual(0o600, os.stat(self.records / "routing.json").st_mode & 0o777)
        self.assertIn(("routing.set", "ok"), {(r["method"], r["outcome"]) for r in self.audit() if "method" in r})

    def test_term10_unset_is_unknown(self):
        self.assertEqual("unknown", self.client("read").call("routing.get")["routing"])

    def test_term10_validation_happens_before_the_confirm(self):
        owner = self.client("owner")
        bad = [{**self.ROUTING, "profile": ""}, {**self.ROUTING, "providers": "x"},
               {**self.ROUTING, "rows": [{"task": "build"}]},
               {**self.ROUTING, "rows": [{**self.ROUTING["rows"][0], "model": "m\n1"}]},
               {**self.ROUTING, "rows": [self.ROUTING["rows"][0]] * 65}]
        for params in bad:
            self.assertEqual("invalid_params", self.error_code(owner, "routing.set", params))

    def test_term10_schema_lists_the_new_methods_with_scopes(self):
        methods = self.client("read").call("schema.get")["methods"]
        self.assertEqual(("owner", True), (methods["routing.set"]["scope"], methods["routing.set"]["confirm"]))
        self.assertEqual("owner", methods["unit.message"]["scope"])
        for name in ("units.activity", "changes.list", "changes.diff", "messages.list", "routing.get"):
            self.assertEqual("read", methods[name]["scope"])
