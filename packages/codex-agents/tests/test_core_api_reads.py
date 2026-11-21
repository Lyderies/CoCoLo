"""Core API reads equal the files; unknown stays unknown; files stay canonical (API-7..10; TERM-6, 7, 17)."""
import json
import os
import shutil

from tests.core_api_support import CoreCase, jsonl, UNITS, STATE


class ReadTests(CoreCase):
    def test_api7_units_list_equals_file(self):  # API-7
        listed = self.client().call("units.list")["units"]
        self.assertEqual([u["id"] for u in UNITS], [u["id"] for u in listed])
        self.assertEqual(["running", "done"], [u["state"] for u in listed])

    def test_api7_units_get_and_not_found(self):  # API-7
        client = self.client()
        self.assertEqual("Build B1", client.call("units.get", {"id": "U-0001"})["unit"]["title"])
        self.assertEqual("not_found", self.error_code(client, "units.get", {"id": "U-9999"}))

    def test_api7_units_history_returns_every_record_of_one_unit(self):  # API-7
        rows = UNITS + [dict(UNITS[0], state="done")]
        jsonl(self.records / "units.jsonl", rows)
        history = self.client().call("units.history", {"id": "U-0001"})["records"]
        self.assertEqual(["running", "done"], [r["state"] for r in history])

    def test_api7_state_get_equals_state_file(self):  # API-7
        state = self.client().call("state.get")["state"]
        for key in ("phase", "session", "ctx", "cost"):
            self.assertEqual(STATE[key], state[key])

    def test_term6_runs_list_counts_match_the_units_file(self):  # API-7, TERM-6
        result = self.client().call("runs.list")
        self.assertEqual(2, result["total"])
        self.assertEqual({"running": 1, "done": 1}, result["counts"])

    def test_term17_a_read_sees_a_file_change_without_restart(self):  # API-7, TERM-17
        client = self.client()
        jsonl(self.records / "units.jsonl", UNITS + [{"id": "U-0003", "kind": "child", "state": "running"}])
        self.assertEqual(3, len(client.call("units.list")["units"]))


class UsageTests(CoreCase):
    def test_api8_usage_ci_and_versions_equal_records(self):  # API-8, TERM-7
        client = self.client()
        self.assertEqual({"input": 10, "output": 5}, client.call("usage.get")["usage"])
        self.assertEqual("green", client.call("ci.get")["ci"]["status"])
        self.assertEqual("0.8.40", client.call("versions.get")["versions"]["installed"])

    def test_api8_unreadable_or_missing_is_unknown_never_a_number(self):  # API-8, TERM-7
        (self.records / "state.json").write_text("{broken")
        client = self.client()
        self.assertEqual("unknown", client.call("usage.get")["usage"])
        self.assertEqual("unknown", client.call("ci.get")["ci"])
        (self.records / "state.json").write_text(json.dumps({"phase": "build"}))
        self.assertEqual("unknown", client.call("usage.get")["usage"])
        self.assertEqual("unknown", client.call("state.get")["state"]["window"])

    def test_api8_history_list(self):  # API-8
        jsonl(self.records / "history.jsonl", [{"id": "H-1", "summary": "stopped: guard"}])
        self.assertEqual(["H-1"], [h["id"] for h in self.client().call("history.list")["history"]])


class HealthTests(CoreCase):
    def test_api9_health_findings_have_code_and_plain_text(self):  # API-9
        (self.records / "state.json").write_text("{broken")
        findings = self.client().call("health.get")["findings"]
        self.assertTrue(findings)
        for f in findings:
            self.assertTrue(f["code"] and f["text"])
            self.assertNotIn("Traceback", f["text"])
            self.assertNotIn(str(self.records), f["text"])  # no private paths


class CanonicalFilesTests(CoreCase):
    def test_api10_cache_deleted_and_restart_give_identical_answers(self):  # API-10
        methods = ("state.get", "units.list", "usage.get", "runs.list")
        before = {m: self.client().call(m) for m in methods}
        self.service.stop()
        for cache in self.records.glob("*.cache"):
            os.remove(cache)
        shutil.rmtree(self.records / "cache", ignore_errors=True)
        self.start()
        self.assertEqual(before, {m: self.client().call(m) for m in methods})

    def test_api10_files_written_by_the_core_are_unchanged_by_restart(self):  # API-10
        client = self.client("confirm")
        client.call("context.push", {"kind": "note", "text": "hello"})
        before = (self.records / "inbox.jsonl").read_bytes()
        self.service.stop(); self.start()
        self.assertEqual(before, (self.records / "inbox.jsonl").read_bytes())

    def test_term17_the_terminal_reads_only_through_the_api(self):  # TERM-17
        # Every read method exists in the contract, so a client never needs the record files.
        methods = set(self.client().call("schema.get")["methods"])
        self.assertTrue({"state.get", "units.list", "units.get", "units.history", "runs.list", "usage.get",
                         "usage.history", "ci.get", "versions.get", "history.list", "decisions.list",
                         "queries.list", "health.get"} <= methods)
