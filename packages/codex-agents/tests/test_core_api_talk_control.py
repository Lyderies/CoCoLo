"""Core API talk, tokens, scopes, confirm, owner wall, audit (API-15..23; TERM-13)."""
import json

from tests.core_api_support import CoreCase, read_jsonl

CANNED = ["What is the status?", "What is U-0001 doing?", "How much usage is left?", "Is CI green?",
          "Which version is installed?", "Show the history.", "Why did the last unit stop?"]


class RecordsFirstTests(CoreCase):
    def test_api15_canned_questions_answer_from_records_with_zero_tokens(self):  # API-15
        client = self.client("read")
        for question in CANNED:
            with self.subTest(question):
                answer = client.call("query.ask", {"text": question, "mode": "auto"})
                self.assertEqual("records", answer["answered_by"])
                self.assertEqual(0, answer["cost_tokens"])
                self.assertTrue(answer["sources"] and answer["as_of"])
        self.assertEqual([], self.inbox())  # nothing queued, so nothing reaches the model

    def test_api15_records_only_mode_never_forwards(self):  # API-15
        answer = self.client("read").call("query.ask", {"text": "Should we rewrite the parser?", "mode": "records_only"})
        self.assertEqual("no_records_answer", answer["answered_by"])
        self.assertEqual([], self.inbox())

    def test_api15_read_scope_cannot_forward(self):  # API-15, API-19
        self.assertEqual("forbidden", self.error_code(self.client("read"), "query.ask",
                                                      {"text": "Should we rewrite the parser?", "mode": "forward"}))


class ForwardTests(CoreCase):
    def ask(self, text="Should we rewrite the parser?"):
        return self.client("confirm").call("query.ask", {"text": text, "mode": "forward"})

    def test_api16_judgment_question_is_queued_in_the_inbox_only(self):  # API-16
        answer = self.ask()
        self.assertEqual("queued", answer["answered_by"])
        rows = self.inbox()
        self.assertEqual(1, len(rows))
        self.assertEqual(answer["query_id"], rows[0]["query_id"])
        self.assertIn("ttl", rows[0])

    def test_api16_delivery_at_a_safe_point_is_one_capped_block(self):  # API-16
        for i in range(20):
            self.ask("Question %d %s" % (i, "x" * 400))
            self.clock.advance(7)
        block = self.service.deliver_at_safe_point()
        self.assertLessEqual(len(block["text"]) / 4, 300)  # at most 300 tokens (4 characters per token)
        self.assertEqual("untrusted", block["trust"])

    def test_api16_expired_questions_are_not_delivered(self):  # API-16
        answer = self.ask()
        self.clock.advance(3600 * 24)
        self.assertEqual([], self.service.deliver_at_safe_point()["query_ids"])
        self.assertEqual("expired", self.client("read").call("queries.get", {"id": answer["query_id"]})["query"]["state"])

    def test_api16_reply_reaches_the_asker_as_query_answered(self):  # API-16
        asker = self.client("confirm")
        query = asker.call("query.ask", {"text": "Should we rewrite the parser?", "mode": "forward"})["query_id"]
        stream = self.client("read").subscribe({"from_seq": 5, "filter": {"kinds": ["query.answered"]}})
        self.client("owner").call("query.reply", {"query_id": query, "text": "No, keep it."})
        event = next(stream)
        self.assertEqual((query, "No, keep it."), (event["data"]["query_id"], event["data"]["text"]))


class PushTests(CoreCase):
    def push(self, client=None, **params):
        body = {"kind": "note", "text": "build finished", **params}
        return (client or self.client("confirm")).call("context.push", body)

    def test_api17_push_is_labelled_by_source_and_quoted_as_untrusted(self):  # API-17
        self.push(self.client("agent"))
        row = self.inbox()[0]
        self.assertEqual("agent", row["source"])
        self.assertEqual("untrusted", row["trust"])
        self.assertNotIn("owner_decision", row)

    def test_api17_push_is_never_an_owner_decision(self):  # API-17
        self.push(text="Approved, ship it")
        self.assertEqual([], self.client("read").call("decisions.list")["decisions"])

    def test_api17_size_cap(self):  # API-17
        self.assertEqual("too_large", self.error_code(self.client("agent"), "context.push",
                                                      {"kind": "note", "text": "x" * 2049}))

    def test_api17_rate_cap_resets_with_time(self):  # API-17
        client = self.client("agent")
        for _ in range(10):
            self.push(client)
        self.assertEqual("rate_limited", self.error_code(client, "context.push", {"kind": "note", "text": "one more"}))
        self.clock.advance(61)
        self.push(client)


class TokenTests(CoreCase):
    def test_api18_tokens_are_256_bit_and_stored_hashed(self):  # API-18
        token = self.mod.mint_token(str(self.records), "read")
        self.assertGreaterEqual(len(bytes.fromhex(token)) if len(token) % 2 == 0 else len(token), 32)
        self.assertNotIn(token, (self.records / "tokens.json").read_text())

    def test_api18_revoked_token_is_refused(self):  # API-18
        token = self.mod.mint_token(str(self.records), "read")
        self.mod.revoke_token(str(self.records), token)
        client = self.mod.CoreClient(self.socket, token=token)
        self.assertEqual("unauthorized", self.error_code(client, "initialize", {"protocol": "1.0"}))

    def test_api18_unknown_token_is_refused(self):  # API-18
        client = self.mod.CoreClient(self.socket, token="0" * 64)
        self.assertEqual("unauthorized", self.error_code(client, "initialize", {"protocol": "1.0"}))

    def test_api18_minting_over_the_api_is_refused_even_for_owner(self):  # API-18, API-22
        self.assertEqual("owner_terminal_only", self.error_code(self.client("owner"), "token.mint", {"scope": "read"}))

    def test_api18_agent_token_defaults_to_read_plus_push(self):  # API-18
        agent = self.client("agent")
        agent.call("state.get")
        agent.call("context.push", {"kind": "note", "text": "hi"})
        self.assertEqual("forbidden", self.error_code(agent, "control.pause"))


class ScopeMatrixTests(CoreCase):
    READ = [("state.get", {}), ("units.list", {}), ("usage.get", {}), ("health.get", {}), ("schema.get", {})]
    CONFIRM = [("control.pause", {}), ("control.resume", {}), ("control.handoff", {}), ("unit.cancel", {"id": "U-0001"})]
    OWNER = [("control.stop", {}), ("decision.answer", {"id": "D-1", "choice": "a"}),
             ("unit.verdict", {"id": "U-0001", "verdict": "pass"})]

    def test_api19_read_scope_allows_reads_and_refuses_everything_else(self):  # API-19, TERM-13
        client = self.client("read")
        for method, params in self.READ:
            client.call(method, params)
        for method, params in self.CONFIRM + self.OWNER + [("context.push", {"kind": "note", "text": "x"})]:
            with self.subTest(method):
                self.assertEqual("forbidden", self.error_code(client, method, params))

    def test_api19_confirm_scope_allows_controls_through_confirm_and_refuses_owner_methods(self):  # API-19, TERM-13
        client = self.client("confirm")
        for method, params in self.CONFIRM:
            with self.subTest(method):
                self.assertEqual("confirm_required", self.error_code(client, method, params))
        for method, params in self.OWNER:
            with self.subTest(method):
                self.assertEqual("forbidden", self.error_code(client, method, params))

    def test_api19_owner_scope_allows_owner_methods(self):  # API-19, TERM-13
        owner = self.client("owner")
        owner.call("unit.verdict", {"id": "U-0001", "verdict": "pass"})
        self.assertEqual("confirm_required", self.error_code(owner, "control.stop"))  # stop also confirms

    def test_api20_decision_answer_is_owner_role_only(self):  # API-20
        self.records.joinpath("decisions.jsonl").write_text(json.dumps({"id": "D-1", "options": ["a", "b"]}) + "\n")
        for scope in ("read", "confirm", "agent"):
            self.assertEqual("forbidden", self.error_code(self.client(scope), "decision.answer",
                                                          {"id": "D-1", "choice": "a"}), scope)
        self.client("owner").call("decision.answer", {"id": "D-1", "choice": "a"})
        self.assertEqual("a", self.client("read").call("decisions.list")["decisions"][0]["answer"])


class ConfirmTests(CoreCase):
    def begin(self, client, method="unit.cancel", params=None):
        params = params or {"id": "U-0001"}
        try:
            client.call(method, params)
        except self.mod.ApiError as error:
            self.assertEqual("confirm_required", error.code)
            return params, error.data["confirm_id"]
        self.fail("no confirm was asked")

    def test_api21_second_call_executes_once(self):  # API-21
        client = self.client("confirm")
        params, confirm_id = self.begin(client)
        self.assertEqual("cancelled", client.call("unit.cancel", {**params, "confirm_id": confirm_id})["outcome"])
        self.assertEqual("confirm_invalid", self.error_code(client, "unit.cancel", {**params, "confirm_id": confirm_id}))

    def test_api21_expires_after_60_seconds(self):  # API-21
        client = self.client("confirm")
        params, confirm_id = self.begin(client)
        self.clock.advance(61)
        self.assertEqual("confirm_expired", self.error_code(client, "unit.cancel", {**params, "confirm_id": confirm_id}))

    def test_api21_bound_to_the_same_token_and_params(self):  # API-21
        client, other = self.client("confirm"), self.client("confirm")
        params, confirm_id = self.begin(client)
        self.assertEqual("confirm_invalid", self.error_code(other, "unit.cancel", {**params, "confirm_id": confirm_id}))
        self.assertEqual("confirm_invalid", self.error_code(client, "unit.cancel",
                                                            {"id": "U-0002", "confirm_id": confirm_id}))

    def test_api21_nothing_changes_before_the_second_step(self):  # API-21
        client = self.client("confirm")
        self.begin(client)
        self.assertEqual("running", client.call("units.get", {"id": "U-0001"})["unit"]["state"])


class OwnerWallTests(CoreCase):
    WALLED = ["install.run", "update.run", "setup.run", "install.reviewed", "records.write", "billing.attest",
              "client_range.widen", "token.mint"]

    def test_api22_walled_actions_are_refused_even_with_an_owner_token(self):  # API-22, TERM-13
        owner = self.client("owner")
        for method in self.WALLED:
            with self.subTest(method):
                self.assertEqual("owner_terminal_only", self.error_code(owner, method, {"confirm_id": "x"}))

    def test_api22_walled_actions_change_no_records(self):  # API-22
        before = {p.name: p.read_bytes() for p in self.records.iterdir() if p.name not in ("commands.jsonl", "tokens.json")}
        owner = self.client("owner")
        for method in self.WALLED:
            self.error_code(owner, method)
        after = {p.name: p.read_bytes() for p in self.records.iterdir() if p.name not in ("commands.jsonl", "tokens.json")}
        self.assertEqual(before, after)


class AuditTests(CoreCase):
    def test_api23_changes_refusals_and_confirms_are_audited(self):  # API-23
        client = self.client("confirm")
        client.call("context.push", {"kind": "note", "text": "hello"})
        self.error_code(client, "control.stop")  # refused: forbidden
        self.error_code(client, "unit.cancel", {"id": "U-0001"})  # confirm asked
        outcomes = {(r["method"], r["outcome"]) for r in self.audit()}
        self.assertEqual({("context.push", "ok"), ("control.stop", "forbidden"), ("unit.cancel", "confirm_required")},
                         outcomes & {("context.push", "ok"), ("control.stop", "forbidden"), ("unit.cancel", "confirm_required")})
        for row in self.audit():
            self.assertTrue(row["caller"] and row["ts"])

    def test_api23_reads_are_not_audited_and_no_secret_is_written(self):  # API-23
        token = self.mod.mint_token(str(self.records), "confirm")
        client = self.mod.CoreClient(self.socket, token=token)
        client.call("initialize", {"protocol": "1.0"})
        client.call("state.get")
        client.call("context.push", {"kind": "note", "text": "hello", "token": "tok-SECRET", "passphrase": "pw-SECRET"})
        text = (self.records / "commands.jsonl").read_text()
        for banned in (token, "tok-SECRET", "pw-SECRET"):
            self.assertNotIn(banned, text)
        self.assertNotIn("state.get", text)
