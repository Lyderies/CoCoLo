"""Core API event stream: order, resume, filtering, slow clients, no secrets (API-11..14)."""
import json

from tests.core_api_support import CoreCase, read_jsonl


class OrderTests(CoreCase):
    def test_api11_replay_is_ordered_gap_free_with_the_envelope(self):  # API-11
        events = list(self.client().subscribe({"from_seq": 0, "limit": 5}))
        self.assertEqual([1, 2, 3, 4, 5], [e["seq"] for e in events])
        for e in events:
            self.assertEqual({"seq", "ts", "kind", "data", "v"}, set(e) - {"unit_id", "session"})

    def test_api11_a_new_event_gets_the_next_seq_and_reaches_the_subscriber(self):  # API-11
        stream = self.client().subscribe({"from_seq": 5})
        self.service.emit("unit.updated", {"n": 6}, unit_id="U-0001")
        self.assertEqual(6, next(stream)["seq"])
        self.assertEqual(6, read_jsonl(self.records / "events.jsonl")[-1]["seq"])


class ResumeTests(CoreCase):
    def test_api12_resume_replays_exactly_the_missed_events(self):  # API-12
        events = list(self.client().subscribe({"from_seq": 3, "limit": 2}))
        self.assertEqual([4, 5], [e["seq"] for e in events])

    def test_api12_expired_cursor_returns_cursor_expired_with_a_snapshot(self):  # API-12
        self.service.stop()
        self.start(event_buffer=2)
        client = self.client()
        try:
            client.call("events.subscribe", {"from_seq": 1})
        except self.mod.ApiError as error:
            self.assertEqual("cursor_expired", error.code)
            self.assertIn("units", error.data["snapshot"])
            self.assertEqual(5, error.data["snapshot"]["seq_at"])
        else:
            self.fail("an expired cursor was accepted")


class FilterAndBoundsTests(CoreCase):
    def test_api13_kind_filter(self):  # API-13
        stream = self.client().subscribe({"from_seq": 5, "filter": {"kinds": ["alert.guard"]}})
        self.service.emit("unit.updated", {}, unit_id="U-0001")
        self.service.emit("alert.guard", {"why": "ctx"})
        self.assertEqual("alert.guard", next(stream)["kind"])


class NoSecretsTests(CoreCase):
    def test_api14_events_carry_ids_and_summaries_never_transcripts_or_secrets(self):  # API-14
        self.service.emit("unit.updated", {"summary": "edited 3 files", "transcript": "SECRET-TRANSCRIPT",
                                           "token": "tok-SECRET", "passphrase": "pw-SECRET"}, unit_id="U-0001")
        text = json.dumps(list(self.client().subscribe({"from_seq": 5, "limit": 1})))
        for banned in ("SECRET-TRANSCRIPT", "tok-SECRET", "pw-SECRET"):
            self.assertNotIn(banned, text)
        self.assertIn("edited 3 files", text)
