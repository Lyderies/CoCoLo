"""T7 S3 (scripts/e2e_checks_learning_guard.py): strictness of the learning, backup and guard checks.

Each test builds the scene a check reads (a SimpleNamespace of what the scene collected) and runs the check on it:
a check must fail on missing or null data, on a value read from the command that made it, on a refusal for the wrong
reason, and must pass on the one honest scene. Written before the module (red first)."""

import ast
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import e2e_core  # noqa: E402
import e2e_support  # noqa: E402
import e2e_checks_learning_guard as lg  # noqa: E402

ORCHESTRATION = ROOT / "packages" / "codex-agents" / "native-package" / ".codex" / "orchestration" / "codex_agents"
CODES = {"turn_started": 1, "tool_started": 1, "tool_output_observed": 1, "session_started": 1, "turn_stop_observed": 1}
ZERO = {"turn_started": 0, "tool_started": 0, "tool_output_observed": 0, "session_started": 0, "turn_stop_observed": 0}


def run_check(function) -> bool:
    phase = e2e_core.Phase(function.__name__, Path("/nonexistent"))
    try:
        function(phase)
    except e2e_core.CheckFailed:
        return False
    return phase.expectations > 0


class SceneTest(unittest.TestCase):
    def tearDown(self):
        e2e_core._SCENES.clear()

    def put(self, name, scene):
        e2e_core._SCENES[name] = scene


def reads(codes, accepted=None, **extra):
    """What the three independent reads of the hook counts say: the collector's status, the ledger's event rows and
    its ingestion table."""
    return {"status": dict(codes), "accepted": sum(codes.values()) if accepted is None else accepted,
            "ledger": dict(codes), "ingestion": dict(codes), "freshness": "fresh", "sessions": 1, "secrets": [], **extra}


def learn_scene(**changes):
    refused = (2, {"ready": False, "error": "input validation failed"}, "")
    scene = SimpleNamespace(
        world=None, project_id="prj_x",
        hook_results=[("UserPromptSubmit", 0, ""), ("PreToolUse", 0, ""), ("PostToolUse", 0, ""),
                      ("SessionStart", 0, ""), ("Stop", 0, "{}\n")],
        before=reads(ZERO, 0, freshness="awaiting_hook_review_or_first_event", sessions=0),
        on=reads(CODES),
        outside={"code": 0, "stdout": "", "after": reads(ZERO, 0, freshness="awaiting_hook_review_or_first_event", sessions=0)},
        unregistered={"code": 0, "stdout": "", "after": reads(ZERO, 0, freshness="awaiting_hook_review_or_first_event", sessions=0)},
        migrate=SimpleNamespace(
            status=(0, {"ready": True, "state": "complete", "can_start_now": True}, ""),
            learning_status_state="complete", failure=None, again=(0, {"ready": True, "state": "complete",
                                                                      "ledger_retired": True, "imported_records": 0}, ""),
            history_run_id="run:r1",
            history_before={"store": {"dispatch_result:a": "h1", "out:b": "h2"}, "ledger": {"admission_decision:c": "h3"}},
            history_after={"store": {"dispatch_result:a": "h1", "out:b": "h2"}, "ledger": {"admission_decision:c": "h3"}},
            run=(0, {"ready": True, "analysis": {"as_of_ms": 1500, "schema_version": 1, "slices": {
                "s": {"configurations": {"c": {"observations": {"trials": 1}}}}}},
                     "mined_case_ids": [], "proposal_status": "complete", "proposed_candidate_digests": [],
                     "expired_records": 0}, ""),
            window=(1000, 2000), store_files=["history.sqlite3"], store_identity=1, ledger_project_ids=["prj_x"],
            store_project_ids=["prj_x"], ledger_before={"records": 9, "projects": 1, "stored_files": 1},
            ledger_after={"records": 9, "projects": 1, "stored_files": 1},
            store_identity_ids=["prj_x"], checkout_ids=["prj_x"], cases_before=["case:1"], cases_after=["case:1"], candidates_before=[],
            candidates_after=[], store_integrity_after="ok", store_identity_ids_after=["prj_x"]),
        retention=SimpleNamespace(
            now=(0, {"ready": True, "expired_records": 0}, ""), past=(0, {"ready": True, "expired_records": 0}, ""),
            future=refused, events_before=5, events_after_now=5, events_after_past=5, events_after_future=5,
            summaries_before=0, summaries_after=0, expiries=[1_000_000 + 604_800_000] * 5, ingested=[1_000_000] * 5,
            real_now=1_000_100),
        backup=SimpleNamespace(
            create=(0, {"ready": True, "verified": True, "project_id": "prj_x", "path": "/b/e2eb1.sqlite3",
                        "backup_sha256": "a" * 64, "manifest_sha256": "b" * 64,
                        "counts": {"records": 20}, "isolated_restore_integrity": "ok"}, ""),
            file_sha="a" * 64, file_manifest_sha="b" * 64, from_registry=True, file_exists=True, file_integrity="ok", file_records=20, ledger_records=20,
            stored=[{"file_id": "e2eb1", "kind": "backup", "content_digest": "a" * 64, "expires_at_ms": 9_999_999_999_999}],
            verify=(0, {"ready": True, "verified": True, "backup_sha256": "a" * 64, "manifest_sha256": "b" * 64,
                        "counts": {"records": 20}, "isolated_restore_integrity": "ok"}, ""),
            unknown=(2, {"ready": False, "error": "project backup is unavailable"}, ""), real_now=1_000_100),
        short=SimpleNamespace(
            expires_at_ms=1_000_000, now_ms=1_000_100,
            expire=(0, {"ready": True, "expired_records": 0}, ""), row_after=[], file_after=False,
            verify_after=(2, {"ready": False, "error": "project backup is unavailable"}, ""),
            long_made=True, long_rows_after=1, events_after=5, events_before=5),
    )
    for name, value in changes.items():
        setattr(scene, name, value)
    return scene


class HookTests(SceneTest):
    def test_the_honest_scene_passes(self):
        self.put("learn", learn_scene())
        self.assertTrue(run_check(lg.l10_1_hook_events_are_ingested_for_the_registered_checkout))
        self.assertTrue(run_check(lg.l10_1_events_outside_the_checkout_or_for_an_unregistered_project_are_not_recorded))

    def test_a_missing_or_null_count_fails(self):
        for key in ("status", "ledger", "ingestion"):
            for value in (None, {}):
                with self.subTest(key=key, value=value):
                    on = reads(CODES)
                    on[key] = value
                    self.put("learn", learn_scene(on=on))
                    self.assertFalse(run_check(lg.l10_1_hook_events_are_ingested_for_the_registered_checkout))

    def test_the_three_reads_must_agree_exactly(self):
        for key in ("status", "ledger", "ingestion"):
            with self.subTest(key=key):
                on = reads(CODES)
                on[key] = {**CODES, "turn_started": 2}
                self.put("learn", learn_scene(on=on))
                self.assertFalse(run_check(lg.l10_1_hook_events_are_ingested_for_the_registered_checkout))

    def test_a_hook_that_exits_non_zero_or_a_stop_without_its_quiet_answer_fails(self):
        for results in ([("UserPromptSubmit", 1, "")] + learn_scene().hook_results[1:],
                        learn_scene().hook_results[:-1] + [("Stop", 0, "")]):
            with self.subTest(results=results):
                self.put("learn", learn_scene(hook_results=results))
                self.assertFalse(run_check(lg.l10_1_hook_events_are_ingested_for_the_registered_checkout))

    def test_nothing_was_recorded_before_the_events(self):
        for before in (reads(CODES), {**reads(ZERO, 0), "ledger": None}):
            with self.subTest(before=before):
                self.put("learn", learn_scene(before=before))
                self.assertFalse(run_check(lg.l10_1_hook_events_are_ingested_for_the_registered_checkout))

    def test_event_content_never_reaches_the_state(self):
        self.put("learn", learn_scene(on=reads(CODES, secrets=["learning.sqlite3"])))
        self.assertFalse(run_check(lg.l10_1_hook_events_are_ingested_for_the_registered_checkout))

    def test_a_stale_or_missing_freshness_fails(self):
        for freshness in ("awaiting_hook_review_or_first_event", None):
            self.put("learn", learn_scene(on=reads(CODES, freshness=freshness)))
            self.assertFalse(run_check(lg.l10_1_hook_events_are_ingested_for_the_registered_checkout))

    def test_an_event_outside_the_checkout_that_was_recorded_fails(self):
        self.put("learn", learn_scene(outside={"code": 0, "stdout": "", "after": reads({**ZERO, "turn_started": 1})}))
        self.assertFalse(run_check(lg.l10_1_events_outside_the_checkout_or_for_an_unregistered_project_are_not_recorded))

    def test_an_unregistered_project_that_was_recorded_fails_in_any_of_the_reads(self):
        for key in ("status", "ledger", "ingestion"):
            with self.subTest(key=key):
                after = reads(ZERO, 0, freshness="awaiting_hook_review_or_first_event", sessions=0)
                after[key] = {**ZERO, "tool_started": 1}
                self.put("learn", learn_scene(unregistered={"code": 0, "stdout": "", "after": after}))
                self.assertFalse(run_check(lg.l10_1_events_outside_the_checkout_or_for_an_unregistered_project_are_not_recorded))

    def test_a_missing_read_after_the_unrecorded_event_fails(self):
        self.put("learn", learn_scene(outside={"code": 0, "stdout": "", "after": {**reads(ZERO, 0, sessions=0), "ledger": None}}))
        self.assertFalse(run_check(lg.l10_1_events_outside_the_checkout_or_for_an_unregistered_project_are_not_recorded))

    def test_a_noisy_unrecorded_event_fails(self):
        self.put("learn", learn_scene(outside={"code": 0, "stdout": "oops", "after": reads(ZERO, 0, sessions=0)}))
        self.assertFalse(run_check(lg.l10_1_events_outside_the_checkout_or_for_an_unregistered_project_are_not_recorded))


class CycleTests(SceneTest):
    def judged(self, **changes):
        scene = learn_scene()
        for name, value in changes.items():
            setattr(scene.migrate, name, value)
        self.put("learn", scene)
        return run_check(lg.l10_2_history_lives_in_the_project_store_and_the_cycle_runs)

    def test_the_honest_scene_passes(self):
        self.assertTrue(self.judged())

    def test_a_project_not_moved_fails(self):
        self.assertFalse(self.judged(learning_status_state=None))
        self.assertFalse(self.judged(learning_status_state="pending"))
        self.assertFalse(self.judged(status=(0, {"ready": True, "state": "pending"}, "")))
        self.assertFalse(self.judged(status=(0, None, "")))

    def test_the_store_must_exist_and_be_the_ledger_project(self):
        self.assertFalse(self.judged(store_files=[]))
        self.assertFalse(self.judged(store_project_ids=[]))
        self.assertFalse(self.judged(store_project_ids=["prj_other"]))
        self.assertFalse(self.judged(ledger_project_ids=[]))

    def test_the_cycle_must_run_in_its_window_with_the_closed_shape(self):
        run = learn_scene().migrate.run
        self.assertFalse(self.judged(run=(1, run[1], "")))
        self.assertFalse(self.judged(run=(0, {**run[1], "ready": False}, "")))
        self.assertFalse(self.judged(run=(0, {**run[1], "analysis": {}}, "")))
        self.assertFalse(self.judged(run=(0, {**run[1], "analysis": {"as_of_ms": 3000, "schema_version": 1}}, "")))
        self.assertFalse(self.judged(run=(0, {**run[1], "analysis": {"as_of_ms": None, "schema_version": 1}}, "")))
        self.assertFalse(self.judged(run=(0, {**run[1], "proposal_status": None}, "")))
        self.assertFalse(self.judged(run=(0, None, "")))

    def test_the_cycle_must_not_touch_the_shared_ledger(self):
        self.assertFalse(self.judged(ledger_after={"records": 10, "projects": 1, "stored_files": 1}))
        self.assertFalse(self.judged(ledger_after=None))

    def test_a_second_move_must_import_nothing_and_say_the_ledger_is_retired(self):
        again = learn_scene().migrate.again
        self.assertFalse(self.judged(again=(0, {**again[1], "imported_records": 3}, "")))
        self.assertFalse(self.judged(again=(0, {**again[1], "ledger_retired": False}, "")))
        self.assertFalse(self.judged(again=(0, {k: v for k, v in again[1].items() if k != "ledger_retired"}, "")))

    def test_a_recorded_migration_failure_fails(self):
        self.assertFalse(self.judged(failure={"reason_code": "x"}))


class RetentionTests(SceneTest):
    def judged(self, check=None, **changes):
        scene = learn_scene()
        for name, value in changes.items():
            setattr(scene.retention, name, value)
        self.put("learn", scene)
        return run_check(check or lg.l10_3_unexpired_events_are_kept_and_a_future_clock_is_refused)

    def test_the_honest_scene_passes(self):
        self.assertTrue(self.judged())

    def test_events_that_went_or_a_missing_count_fail(self):
        self.assertFalse(self.judged(events_after_now=4))
        self.assertFalse(self.judged(events_after_past=None))
        self.assertFalse(self.judged(events_after_future=4))
        self.assertFalse(self.judged(events_before=0, events_after_now=0, events_after_past=0, events_after_future=0))

    def test_the_future_clock_must_be_refused_for_the_reason(self):
        self.assertFalse(self.judged(future=(0, {"ready": True, "expired_records": 5}, "")))
        self.assertFalse(self.judged(future=(2, {"ready": False, "error": "something else"}, "")))
        self.assertFalse(self.judged(future=(2, None, "")))

    def test_events_must_carry_at_least_the_seven_day_retention(self):
        self.assertFalse(self.judged(expiries=[1_000_000 + 60_000] * 5))
        self.assertFalse(self.judged(expiries=[None] * 5))
        self.assertFalse(self.judged(expiries=[]))

    def test_expiry_at_the_real_clock_removes_nothing_and_makes_no_summary(self):
        self.assertFalse(self.judged(now=(0, {"ready": True, "expired_records": 2}, "")))
        self.assertFalse(self.judged(now=(0, {"ready": True}, "")))
        self.assertFalse(self.judged(summaries_after=1))


def retain_scene(**changes):
    codes = {"turn_started": 1, "tool_started": 1, "tool_output_observed": 1, "session_started": 1, "turn_stop_observed": 1}
    scene = SimpleNamespace(
        world=None, project_id="prj_x", expiries=[1_008_000] * 5, ingested=[1_000_000] * 5, real_now=1_009_000,
        events_before=5, codes_before=dict(codes), expire=(0, {"ready": True, "expired_records": 5}, ""),
        events_after=0, summaries_before=0, summaries_after=1, summary_events=5, summary_codes=dict(codes),
        summary_runs_ok=True)
    for name, value in changes.items():
        setattr(scene, name, value)
    return scene


class RawRetentionTests(SceneTest):
    """Decision 193: the raw-event window is scaled by the test-only BOOT hook; removal is judged through learning-expire."""

    def judged(self, **changes):
        self.put("retain", retain_scene(**changes))
        return run_check(lg.l10_3_expired_raw_events_are_removed_and_their_summary_kept)

    def test_the_honest_scene_passes(self):
        self.assertTrue(self.judged())

    def test_an_event_that_is_not_expired_at_the_clock_fails(self):
        self.assertFalse(self.judged(expiries=[1_008_000, 1_008_000, 1_008_000, 1_008_000, 1_010_000]))
        self.assertFalse(self.judged(expiries=[]))
        self.assertFalse(self.judged(expiries=[None] * 5))

    def test_the_window_must_be_the_scaled_one_not_the_seven_days(self):
        self.assertFalse(self.judged(expiries=[1_000_000 + lg.SEVEN_DAYS_MS] * 5, real_now=1_000_000 + lg.SEVEN_DAYS_MS))

    def test_events_must_exist_before_the_command_and_be_gone_after_it(self):
        self.assertFalse(self.judged(events_before=0, events_after=0, codes_before={}))
        self.assertFalse(self.judged(events_after=1))
        self.assertFalse(self.judged(events_after=None))

    def test_the_command_must_succeed_and_report_the_removal(self):
        self.assertFalse(self.judged(expire=(2, {"ready": False}, "")))
        self.assertFalse(self.judged(expire=(0, None, "")))
        self.assertFalse(self.judged(expire=(0, {"ready": True, "expired_records": 0}, "")))

    def test_the_summary_must_be_kept_and_account_for_every_removed_event(self):
        self.assertFalse(self.judged(summaries_after=0))
        self.assertFalse(self.judged(summaries_after=None))
        self.assertFalse(self.judged(summary_events=4))
        self.assertFalse(self.judged(summary_codes={"turn_started": 5}))


class BootHookTests(unittest.TestCase):
    """Decision 193 (and 128): the stand-in Python's rewrites change only the silence limits and the raw-event window."""

    def test_the_only_scene_rewrites_are_the_silence_limits_the_retention_window_and_a_fault(self):
        allowed = {"liveness_patch", "retention_patch", "fault_patches"}
        found = set()
        for path in sorted((ROOT / "scripts").glob("e2e_checks_*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "write_patches":
                    for inner in ast.walk(node.args[1]):
                        if isinstance(inner, ast.Call):
                            found.add(getattr(inner.func, "id", ""))
        self.assertTrue(found and found <= allowed, found)
        self.assertIn("retention_patch", found)

    def test_the_retention_rewrite_changes_only_the_raw_event_window(self):
        patch = lg.retention_patch()
        self.assertEqual("codex_agents.learning_store", patch["module"])
        source = (ORCHESTRATION / "learning_store.py").read_text()
        self.assertEqual(1, source.count(patch["old"]))
        changed = source.replace(patch["old"], patch["new"])
        import difflib
        diff = [line for line in difflib.unified_diff(source.splitlines(), changed.splitlines(), lineterm="", n=0)
                if line[:1] in "+-" and line[:3] not in ("+++", "---")]
        self.assertEqual(2, len(diff), diff)
        self.assertIn("keep[code]", diff[0])
        self.assertNotIn("summary", diff[1])

    def test_the_boot_hook_itself_rewrites_nothing_unless_a_patch_file_lists_it(self):
        boot = e2e_support.BOOT
        self.assertEqual(1, boot.count('"/.e2e-bin/patches.json"'))
        for word in ("time", "LIVENESS", "keep_", "RETENTION", "PROBE_"):
            self.assertNotIn(word, boot)


class ShortBackupTests(SceneTest):
    def judged(self, **changes):
        scene = learn_scene()
        for name, value in changes.items():
            setattr(scene.short, name, value)
        self.put("learn", scene)
        return run_check(lg.l10_3_an_expired_backup_is_removed_by_retention)

    def test_the_honest_scene_passes(self):
        self.assertTrue(self.judged())

    def test_a_backup_that_stays_in_any_read_fails(self):
        self.assertFalse(self.judged(row_after=[{"file_id": "x"}]))
        self.assertFalse(self.judged(row_after=None))
        self.assertFalse(self.judged(file_after=True))
        self.assertFalse(self.judged(file_after=None))
        self.assertFalse(self.judged(verify_after=(0, {"ready": True, "verified": True}, "")))

    def test_the_longer_backup_and_the_events_must_stay(self):
        self.assertFalse(self.judged(long_made=False))
        self.assertFalse(self.judged(long_rows_after=0))
        self.assertFalse(self.judged(long_rows_after=None))
        self.assertFalse(self.judged(events_after=4))

    def test_expiry_before_its_time_does_not_count(self):
        self.assertFalse(self.judged(now_ms=999_000))
        self.assertFalse(self.judged(expires_at_ms=None))

    def test_the_expire_command_must_succeed(self):
        # expired_records counts expired RECORD ids (a stored file alone makes it 0): the removal is judged from the
        # registry, the disk and the verify refusal, not from that number.
        self.assertFalse(self.judged(expire=(0, {"ready": False}, "")))
        self.assertFalse(self.judged(expire=(2, {"ready": False}, "")))
        self.assertFalse(self.judged(expire=(0, None, "")))
        self.assertTrue(self.judged(expire=(0, {"ready": True, "expired_records": 0}, "")))

    def test_the_removed_backup_must_refuse_verification_for_its_reason(self):
        self.assertFalse(self.judged(verify_after=(2, {"ready": False, "error": "input validation failed"}, "")))


class BackupTests(SceneTest):
    def judged(self, **changes):
        scene = learn_scene()
        for name, value in changes.items():
            setattr(scene.backup, name, value)
        self.put("learn", scene)
        return run_check(lg.l11_1_backup_verifies_and_matches_the_ledger)

    def test_the_honest_scene_passes(self):
        self.assertTrue(self.judged())

    def test_a_digest_the_file_does_not_have_fails(self):
        self.assertFalse(self.judged(file_sha="c" * 64))
        self.assertFalse(self.judged(file_sha=None))

    def test_the_file_must_exist_be_sound_and_hold_the_ledger_s_rows(self):
        self.assertFalse(self.judged(file_exists=False))
        self.assertFalse(self.judged(file_integrity="malformed"))
        self.assertFalse(self.judged(file_integrity=None))
        self.assertFalse(self.judged(file_records=19))
        self.assertFalse(self.judged(ledger_records=21))
        self.assertFalse(self.judged(file_records=None, ledger_records=None))

    def test_a_reply_missing_a_field_or_not_verified_fails(self):
        create = learn_scene().backup.create
        for field in ("verified", "backup_sha256", "counts", "manifest_sha256", "isolated_restore_integrity"):
            reply = {k: v for k, v in create[1].items() if k != field}
            with self.subTest(field=field):
                self.assertFalse(self.judged(create=(0, reply, "")))
        self.assertFalse(self.judged(create=(0, {**create[1], "verified": False}, "")))
        self.assertFalse(self.judged(create=(1, create[1], "")))

    def test_the_registry_row_must_name_the_file_and_expire_later(self):
        self.assertFalse(self.judged(stored=[]))
        self.assertFalse(self.judged(stored=None))
        self.assertFalse(self.judged(stored=[{"file_id": "e2eb1", "kind": "backup", "content_digest": "d" * 64,
                                              "expires_at_ms": 9_999_999_999_999}]))
        self.assertFalse(self.judged(stored=[{"file_id": "e2eb1", "kind": "backup", "content_digest": "a" * 64,
                                              "expires_at_ms": 5}]))

    def test_the_verify_command_must_agree_with_the_file(self):
        verify = learn_scene().backup.verify
        self.assertFalse(self.judged(verify=(0, {**verify[1], "backup_sha256": "e" * 64}, "")))
        self.assertFalse(self.judged(verify=(1, verify[1], "")))
        self.assertFalse(self.judged(verify=(0, {**verify[1], "counts": {"records": 3}}, "")))
        self.assertFalse(self.judged(verify=(0, None, "")))

    def test_an_unknown_backup_must_be_refused_for_its_reason_only(self):
        self.assertFalse(self.judged(unknown=(0, {"ready": True, "verified": True}, "")))
        self.assertFalse(self.judged(unknown=(2, {"ready": False, "error": "input validation failed"}, "")))
        self.assertFalse(self.judged(unknown=(2, None, "")))


def guard_scene(**changes):
    def refusal(text):
        return (2, {"ready": False, "error": text}, "")

    scene = SimpleNamespace(
        world=None, copies={"a": "/c/a/cli.py", "b": "/c/b/cli.py"},
        cases={
            "control": (0, {"ready": True}, ""),
            "danger": refusal("This Codex session runs without its sandbox (danger-full-access); open Codex with its normal sandbox"),
            "unknown_sandbox": refusal("This Codex session's record is not in the format the orchestrator knows (Codex 0.164), "
                                       "so it cannot check which folders the session can write"),
            "no_thread": refusal("This command did not come from a Codex session (CODEX_THREAD_ID is not set), so the "
                                 "orchestrator cannot check which folders the session can write"),
            "no_record": refusal("Run Codex without --ephemeral so the orchestrator can check this session's folders "
                                 "(no record of this session was found)"),
            "home": refusal("This Codex session can write to your home folder; open Codex on the project folder instead"),
            "above_home": refusal("This Codex session can write to a folder that holds your home folder; open Codex on "
                                  "the project folder instead"),
            "project_parent": refusal("This Codex session was opened on a folder that contains this project, where it "
                                      "could change the project's Codex settings and hooks"),
        },
        ledger_before={"projects": 2, "records": 30}, ledger_after={"projects": 2, "records": 30},
        other={"project": refusal("--project-root is refused: an installed CLI serves only its own project"),
               "state": refusal("--state-root is refused: an installed CLI uses only the default learning state"),
               "b_for_a": refusal("--project-root is refused: an installed CLI serves only its own project"),
               "own": (0, {"ready": True}, ""), "b_own": (0, {"ready": True}, "")},
        bound={"a": "A", "b": "B"})
    for name, value in changes.items():
        setattr(scene, name, value)
    return scene


class GuardTests(SceneTest):
    def judged(self, check, **changes):
        scene = guard_scene()
        for key, value in changes.get("cases", {}).items():
            scene.cases[key] = value
        for key, value in changes.get("other", {}).items():
            scene.other[key] = value
        for key, value in changes.items():
            if key not in ("cases", "other"):
                setattr(scene, key, value)
        self.put("guard", scene)
        return run_check(check)

    def test_the_honest_scene_passes(self):
        self.assertTrue(self.judged(lg.l13_1_session_guard_refuses_each_unsafe_session_with_its_reason))
        self.assertTrue(self.judged(lg.l13_2_protected_copy_refuses_a_project_it_is_not_bound_to))

    def test_a_safe_session_that_is_refused_fails(self):
        self.assertFalse(self.judged(lg.l13_1_session_guard_refuses_each_unsafe_session_with_its_reason,
                                     cases={"control": (2, {"ready": False, "error": "x"}, "")}))
        self.assertFalse(self.judged(lg.l13_1_session_guard_refuses_each_unsafe_session_with_its_reason,
                                     cases={"control": (0, None, "")}))

    def test_an_unsafe_session_that_is_admitted_fails(self):
        for name in ("danger", "unknown_sandbox", "no_thread", "no_record", "home", "above_home", "project_parent"):
            with self.subTest(name=name):
                self.assertFalse(self.judged(lg.l13_1_session_guard_refuses_each_unsafe_session_with_its_reason,
                                             cases={name: (0, {"ready": True}, "")}))

    def test_a_refusal_for_another_reason_fails(self):
        other = (2, {"ready": False, "error": "a protected runtime copy runs only the primary-host commands"}, "")
        for name in ("danger", "unknown_sandbox", "no_thread", "no_record", "home", "above_home", "project_parent"):
            with self.subTest(name=name):
                self.assertFalse(self.judged(lg.l13_1_session_guard_refuses_each_unsafe_session_with_its_reason,
                                             cases={name: other}))

    def test_a_refusal_with_no_reply_or_a_crash_fails(self):
        for case in ((2, None, "Traceback"), (1, {"ready": False, "error": "This Codex session runs without its sandbox "
                                                                       "(danger-full-access)"}, "")):
            self.assertFalse(self.judged(lg.l13_1_session_guard_refuses_each_unsafe_session_with_its_reason,
                                         cases={"danger": case}))

    def test_a_refusal_must_not_name_a_path_of_the_machine(self):
        leaky = (2, {"ready": False, "error": "This Codex session runs without its sandbox (danger-full-access) at /Users/x/p"}, "")
        self.assertFalse(self.judged(lg.l13_1_session_guard_refuses_each_unsafe_session_with_its_reason,
                                     cases={"danger": leaky}))

    def test_the_guard_must_leave_the_ledger_alone(self):
        self.assertFalse(self.judged(lg.l13_1_session_guard_refuses_each_unsafe_session_with_its_reason,
                                     ledger_after={"projects": 2, "records": 31}))
        self.assertFalse(self.judged(lg.l13_1_session_guard_refuses_each_unsafe_session_with_its_reason, ledger_after=None))

    def test_another_project_that_is_served_fails(self):
        for name in ("project", "state", "b_for_a"):
            with self.subTest(name=name):
                self.assertFalse(self.judged(lg.l13_2_protected_copy_refuses_a_project_it_is_not_bound_to,
                                             other={name: (0, {"ready": True}, "")}))

    def test_another_project_refused_for_another_reason_fails(self):
        for name in ("project", "state", "b_for_a"):
            with self.subTest(name=name):
                self.assertFalse(self.judged(lg.l13_2_protected_copy_refuses_a_project_it_is_not_bound_to,
                                             other={name: (2, {"ready": False, "error": "input validation failed"}, "")}))

    def test_each_copy_must_serve_its_own_project(self):
        self.assertFalse(self.judged(lg.l13_2_protected_copy_refuses_a_project_it_is_not_bound_to,
                                     other={"own": (2, {"ready": False, "error": "x"}, "")}))
        self.assertFalse(self.judged(lg.l13_2_protected_copy_refuses_a_project_it_is_not_bound_to,
                                     other={"b_own": (0, None, "")}))

    def test_the_two_copies_must_be_bound_to_the_two_projects(self):
        self.assertFalse(self.judged(lg.l13_2_protected_copy_refuses_a_project_it_is_not_bound_to,
                                     bound={"a": "A", "b": "A"}))
        self.assertFalse(self.judged(lg.l13_2_protected_copy_refuses_a_project_it_is_not_bound_to, bound={"a": None, "b": "B"}))

    def test_a_refusal_must_leave_the_ledger_alone(self):
        self.assertFalse(self.judged(lg.l13_2_protected_copy_refuses_a_project_it_is_not_bound_to,
                                     ledger_after={"projects": 3, "records": 30}))


class FaultTests(SceneTest):
    def test_every_rewrite_matches_the_product_source_exactly_once(self):
        for name, patches in lg.FAULTS.items():
            for patch in patches:
                with self.subTest(fault=name, module=patch["module"], old=patch["old"][:50]):
                    source = (ORCHESTRATION / (patch["module"].split(".")[-1] + ".py")).read_text()
                    self.assertEqual(1, source.count(patch["old"]))
                    self.assertNotEqual(patch["old"], patch["new"])

    def test_a_fault_is_selected_only_by_the_argument(self):
        with mock.patch.dict(os.environ, {"E2E_T7S3_FAULT": "learning_guard_broken"}), mock.patch.object(lg, "FAULT", None):
            self.assertEqual([], lg.fault_patches())
        with mock.patch.object(lg, "FAULT", "learning_guard_broken"):
            self.assertEqual(lg.FAULTS["learning_guard_broken"], lg.fault_patches())

    def test_the_runner_hands_each_fault_only_to_the_module_that_owns_it(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("e2e_lifecycle_s3", ROOT / "scripts" / "e2e_lifecycle.py")
        lifecycle = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lifecycle)
        import e2e_checks_run_control as control
        seen = []
        with mock.patch.object(lifecycle, "_execute", side_effect=lambda names, root: seen.append((control.FAULT, lg.FAULT)) or []):
            for fault in ("learning_guard_broken", "slot_not_released", None):
                import tempfile
                with tempfile.TemporaryDirectory() as folder:
                    lifecycle.main(["--execute", "x", "--results", str(Path(folder) / "r.json"),
                                    *(["--fault", fault] if fault else [])])
        self.assertEqual([(None, "learning_guard_broken"), ("slot_not_released", None), (None, None)], seen)

    def test_a_fault_run_must_turn_exactly_the_s3_ids_red(self):
        ids = lg.S3_IDS
        normal = {"requirements": {**{i: {"passed": i != "L10.3"} for i in ids}, "G1": {"passed": True}, "G3": {"passed": True}}}
        fault = {"requirements": {**{i: {"passed": False} for i in ids}, "G1": {"passed": True}, "G3": {"passed": True}}}
        everything = set(normal["requirements"])
        self.assertTrue(lg.fault_isolated(normal, fault, expected_ids=everything)["isolated"])
        outside = {"requirements": {**fault["requirements"], "G3": {"passed": False}}}
        self.assertFalse(lg.fault_isolated(normal, outside, expected_ids=everything)["isolated"])
        g1 = {"requirements": {**fault["requirements"], "G1": {"passed": False}}}
        self.assertFalse(lg.fault_isolated(normal, g1, expected_ids=everything)["isolated"])
        one_green = {"requirements": {**fault["requirements"], "L13.2": {"passed": True}}}
        self.assertFalse(lg.fault_isolated(normal, one_green, expected_ids=everything)["isolated"])
        missing = {"requirements": {i: v for i, v in fault["requirements"].items() if i != "L11.1"}}
        self.assertFalse(lg.fault_isolated(normal, missing, expected_ids=everything)["isolated"])
        self.assertEqual({"L10.1", "L10.2", "L10.3", "L11.1", "L13.1", "L13.2"}, set(lg.S3_IDS))

    def test_no_scene_rewrites_a_clock_or_a_billing_constant(self):
        source = (ROOT / "scripts" / "e2e_checks_learning_guard.py").read_text()
        for word in ("time.time", "_clock_ms", "PROBE_REUSE_MS", "PROBE_COOLDOWN_MS", "LIVENESS"):
            for patches in lg.FAULTS.values():
                self.assertFalse(any(word in patch["old"] or word in patch["new"] for patch in patches), word)
        self.assertNotIn("account_billing_admission", source)


class DisciplineTests(unittest.TestCase):
    """Calls the guards of the harness rest on: environments, expected refusals, no writes to the product's state."""

    def tree(self):
        return ast.parse((ROOT / "scripts" / "e2e_checks_learning_guard.py").read_text())

    def test_every_subprocess_call_names_its_environment(self):
        for path in ("e2e_checks_learning_guard.py", "e2e_support.py"):
            tree = ast.parse((ROOT / "scripts" / path).read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                        and node.func.attr in ("run", "Popen", "check_output")):
                    with self.subTest(path=path, line=node.lineno):
                        self.assertTrue(any(k.arg == "env" for k in node.keywords) or any(k.arg is None for k in node.keywords),
                                        "a subprocess call without env=")

    def test_refuses_is_passed_only_by_the_listed_expected_refusals(self):
        """Every `refuses=True` is one expected refusal listed with its expected reason; any other call is judged by G1."""
        sites = [node for node in ast.walk(self.tree()) if isinstance(node, ast.Call)
                 for keyword in node.keywords if keyword.arg == "refuses"
                 and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)]
        constants = [keyword for node in sites for keyword in node.keywords
                     if keyword.arg == "refuses" and isinstance(keyword.value, ast.Constant)]
        passed_on = [keyword for node in sites for keyword in node.keywords
                     if keyword.arg == "refuses" and not isinstance(keyword.value, ast.Constant)]
        self.assertEqual(len(lg.EXPECTED_REFUSALS), len(constants), "each refuses=True is listed in EXPECTED_REFUSALS")
        for label, reason in lg.EXPECTED_REFUSALS.items():
            self.assertTrue(label and reason, label)
        # a flag that is computed is only the helper's own parameter being handed on
        self.assertTrue(all(isinstance(k.value, ast.Name) and k.value.id == "refuses" for k in passed_on))

    def test_the_scenes_write_nothing_into_the_products_state(self):
        source = (ROOT / "scripts" / "e2e_checks_learning_guard.py").read_text()
        for word in ("mode=rw", "UPDATE ", "DELETE FROM", "INSERT INTO", "executescript", ".commit("):
            self.assertNotIn(word, source, word)
        self.assertEqual(source.count("sqlite3.connect("), source.count("?mode=ro"))

    def test_the_timed_out_step_is_captured_for_g1_and_g8(self):
        import subprocess
        import tempfile
        captured = []
        with tempfile.TemporaryDirectory() as folder:
            world = SimpleNamespace(env=dict(os.environ), project=Path(folder), outputs=[], steps=[],
                                    _capture=lambda text, **kw: captured.append((text, kw)))
            code, out, text = e2e_support.World._run_captured(world, ["/bin/sh", "-c", "echo partial; sleep 5"], "slow", timeout=0.5)
        self.assertEqual(-1, code)
        self.assertIn("partial", text)
        self.assertTrue(captured and captured[0][1]["timed_out"])
        self.assertTrue(world.steps[0]["timed_out"])
        self.assertTrue(subprocess.TimeoutExpired)


class SessionHelperTests(unittest.TestCase):
    def test_the_codex_session_record_is_what_the_guard_reads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "h"
            (home / ".codex").mkdir(parents=True)
            world = SimpleNamespace(home=home, SESSION_ROLLOUT=e2e_support.World.SESSION_ROLLOUT)
            thread = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5b"
            path = e2e_support.World.codex_session(world, Path("/w/project"), thread=thread)
            self.assertRegex(path.name, r"^rollout-\d{4}-\d{2}-\d{2}T10-00-00-" + thread + r"\.jsonl$")
            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(["session_meta", "turn_context"], [r["type"] for r in records])
            self.assertEqual(thread, records[0]["payload"]["id"])
            self.assertEqual("workspace-write", records[1]["payload"]["sandbox_policy"]["type"])
            writable = [e["path"].get("path") for e in records[1]["payload"]["file_system_sandbox_policy"]["entries"]
                        if e["access"] == "write" and e["path"]["type"] == "path"]
            self.assertEqual(["/w/project"], writable)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)

    def test_a_copy_command_refuses_a_project_with_no_protected_copy(self):
        world = SimpleNamespace(protected_copies=lambda: {})
        with self.assertRaises(e2e_support.HarnessError):
            e2e_support.World.copy_cli(world, Path("/nowhere"), "learning-status")


if __name__ == "__main__":
    unittest.main()


class SectionTests(SceneTest):
    """A section is built only when the scene does not hold it, by the check that needs it (so a step that breaks G1 or
    G8 is charged to that check, not to whichever check happened to run first)."""

    def test_a_scene_that_holds_a_section_is_not_built_again(self):
        self.put("learn", learn_scene())
        with mock.patch.dict(lg.SECTIONS, {name: mock.Mock(side_effect=AssertionError(name)) for name in lg.SECTIONS}):
            for name in lg.SECTIONS:
                lg.section(name)

    def test_a_missing_section_is_built_after_the_ones_it_needs(self):
        scene = SimpleNamespace(world=None)
        order = []
        self.put("learn", scene)
        built = {name: (lambda s, n=name: (order.append(n), setattr(s, lg.SECTION_FIELDS[n], True))) for name in lg.SECTIONS}
        with mock.patch.dict(lg.SECTIONS, built):
            lg.section("short")
        self.assertEqual(["hooks", "short"], order)
        self.assertEqual({"hooks", "migrate", "retention", "backup", "short"}, set(lg.SECTION_FIELDS))


class FixRoundCycleTests(CycleTests):
    """S3 fix round, L10.2: the store's identity and the cycle's results are read from what is persisted."""

    def test_the_stores_own_identity_rows_must_be_the_checkouts_project(self):
        self.assertFalse(self.judged(store_identity_ids=[]))
        self.assertFalse(self.judged(store_identity_ids=["prj_other"]))
        self.assertFalse(self.judged(store_identity_ids=["prj_x", "prj_y"]))
        self.assertFalse(self.judged(checkout_ids=["prj_other"]))
        self.assertFalse(self.judged(checkout_ids=[]))
        self.assertFalse(self.judged(store_identity_ids=None))

    def test_the_cycle_s_mined_cases_must_be_what_the_stores_persisted(self):
        run = learn_scene().migrate.run
        self.assertFalse(self.judged(cases_after=["case:1", "c1"]))  # persisted but never reported
        self.assertFalse(self.judged(run=(0, {**run[1], "mined_case_ids": ["c1"]}, "")))  # reported but never persisted
        self.assertTrue(self.judged(run=(0, {**run[1], "mined_case_ids": ["c1"]}, ""), cases_after=["case:1", "c1"]))
        self.assertFalse(self.judged(run=(0, {**run[1], "mined_case_ids": ["c2"]}, ""), cases_after=["case:1", "c1"]))  # other id
        self.assertFalse(self.judged(cases_after=None))
        self.assertFalse(self.judged(cases_before=None))

    def test_the_cycle_s_proposals_must_be_what_the_stores_persisted(self):
        run = learn_scene().migrate.run
        self.assertFalse(self.judged(candidates_after=["k1"]))
        self.assertFalse(self.judged(run=(0, {**run[1], "proposed_candidate_digests": ["d1"]}, "")))
        self.assertTrue(self.judged(run=(0, {**run[1], "proposed_candidate_digests": ["d1"]}, ""), candidates_after=["d1"]))
        self.assertFalse(self.judged(run=(0, {**run[1], "proposed_candidate_digests": ["d1"]}, ""), candidates_after=["k1"]))  # same count, other id
        self.assertFalse(self.judged(candidates_after=None))

    def test_the_store_must_be_sound_and_keep_its_identity_after_the_cycle(self):
        self.assertFalse(self.judged(store_integrity_after="malformed"))
        self.assertFalse(self.judged(store_integrity_after=None))
        self.assertFalse(self.judged(store_identity_ids_after=[]))
        self.assertFalse(self.judged(store_identity_ids_after=["prj_other"]))


class RealHistoryTests(CycleTests):
    """Decision 207, L10.2: the project holds KNOWN recorded history; the move and the cycle must carry it."""

    def test_a_project_with_no_recorded_history_fails(self):
        self.assertFalse(self.judged(history_before={"store": {}, "ledger": {}}, history_after={"store": {}, "ledger": {}}))
        self.assertFalse(self.judged(history_before=None))
        self.assertFalse(self.judged(history_run_id=None))
        self.assertFalse(self.judged(history_before={"store": {"out:b": "h2"}, "ledger": {}},
                                     history_after={"store": {"out:b": "h2"}, "ledger": {}}))  # no dispatch result

    def test_a_skipped_move_fails(self):
        self.assertFalse(self.judged(history_after={"store": {}, "ledger": {}}))
        self.assertFalse(self.judged(history_after=None))
        self.assertFalse(self.judged(history_after={"store": {"dispatch_result:a": "h1"}, "ledger": {}}))  # one record lost

    def test_a_record_that_changed_content_fails(self):
        self.assertFalse(self.judged(history_after={"store": {"dispatch_result:a": "XX", "out:b": "h2"}, "ledger": {}}))

    def test_a_no_op_cycle_fails(self):
        run = learn_scene().migrate.run
        self.assertFalse(self.judged(run=(0, {**run[1], "analysis": {**run[1]["analysis"], "slices": {}}}, "")))
        self.assertFalse(self.judged(run=(0, {**run[1], "analysis": {**run[1]["analysis"], "slices": {
            "s": {"configurations": {"c": {"observations": {"trials": 0}}}}}}}, "")))

    def test_no_mined_case_for_the_history_fails(self):
        self.assertFalse(self.judged(cases_before=[], cases_after=[]))
        self.assertFalse(self.judged(cases_after=[]))  # a case the history had is gone after the cycle


class FixRoundBackupTests(BackupTests):
    """S3 fix round, L11.1: the file comes from the registry, the manifest digest from the stored manifest."""

    def test_the_file_must_be_the_registered_one(self):
        self.assertFalse(self.judged(from_registry=False))

    def test_the_manifest_digest_must_be_present_and_the_stored_manifests(self):
        create = learn_scene().backup.create
        verify = learn_scene().backup.verify
        self.assertFalse(self.judged(file_manifest_sha=None))
        self.assertFalse(self.judged(create=(0, {**create[1], "manifest_sha256": None}, ""),
                                     verify=(0, {**verify[1], "manifest_sha256": None}, "")))
        self.assertFalse(self.judged(create=(0, {k: v for k, v in create[1].items() if k != "manifest_sha256"}, ""),
                                     verify=(0, {k: v for k, v in verify[1].items() if k != "manifest_sha256"}, "")))
        self.assertFalse(self.judged(create=(0, {**create[1], "manifest_sha256": "c" * 64}, ""),
                                     verify=(0, {**verify[1], "manifest_sha256": "c" * 64}, "")))  # both replies agree, file does not
        self.assertFalse(self.judged(verify=(0, {**verify[1], "manifest_sha256": "c" * 64}, "")))

    def test_the_reply_need_not_name_a_path(self):
        create = learn_scene().backup.create
        self.assertTrue(self.judged(create=(0, {k: v for k, v in create[1].items() if k != "path"}, "")))


class FixRoundReadTests(unittest.TestCase):
    def world(self, folder):
        home = Path(folder) / "h"
        (lg.state_root(SimpleNamespace(home=home))).mkdir(parents=True)
        return SimpleNamespace(home=home)

    def test_the_privacy_scan_fails_a_file_it_cannot_read(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            world = self.world(folder)
            good = lg.state_root(world) / "good.bin"
            good.write_bytes(b"nothing here")
            self.assertEqual([], lg.state_files_holding(world, ("SECRET",)))
            hidden = lg.state_root(world) / "hidden.bin"
            hidden.write_bytes(b"SECRET")
            hidden.chmod(0)
            try:
                self.assertEqual(["unreadable:hidden.bin"], lg.state_files_holding(world, ("SECRET",)))
            finally:
                hidden.chmod(0o600)

    def test_the_privacy_scan_streams_and_still_finds_a_token_across_a_chunk_boundary(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            world = self.world(folder)
            (lg.state_root(world) / "x.bin").write_bytes(b"aaaaaaSEC" + b"RETbbbbbb")
            with mock.patch.object(lg, "READ_CHUNK", 8):
                self.assertEqual(["x.bin"], lg.state_files_holding(world, ("SECRET",)))
            (lg.state_root(world) / "x.bin").write_bytes(b"a" * 100)
            with mock.patch.object(lg, "READ_CHUNK", 8):
                self.assertEqual([], lg.state_files_holding(world, ("SECRET",)))

    def test_the_file_hash_streams_and_an_unreadable_file_has_no_hash(self):
        import hashlib
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "f"
            data = bytes(range(256)) * 100
            path.write_bytes(data)
            with mock.patch.object(lg, "READ_CHUNK", 1000):
                self.assertEqual(hashlib.sha256(data).hexdigest(), lg.sha256_file(path))
            path.chmod(0)
            try:
                self.assertIsNone(lg.sha256_file(path))
            finally:
                path.chmod(0o600)
            self.assertIsNone(lg.sha256_file(Path(folder) / "missing"))

    def test_the_manifest_digest_is_the_digest_of_the_stored_manifest(self):
        import hashlib
        import sqlite3
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "b.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("create table cocolo_backup_manifest(manifest blob)")
            connection.execute("insert into cocolo_backup_manifest values (?)", (b'{"x":1}',))
            connection.commit()
            connection.close()
            self.assertEqual(hashlib.sha256(b'{"x":1}').hexdigest(), lg.backup_manifest_sha256(path))
            self.assertIsNone(lg.backup_manifest_sha256(Path(folder) / "missing"))


class FixRoundCliTimeoutTests(unittest.TestCase):
    def test_world_cli_itself_captures_a_timeout_with_its_partial_output(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "cli.py"
            script.write_text("import sys, time\nprint('partial output', flush=True)\ntime.sleep(5)\n")
            captured = []
            world = SimpleNamespace(shim=sys.executable, installed_cli=script, env=dict(os.environ), project=Path(folder),
                                    base=Path(folder), outputs=[], steps=[],
                                    _capture=lambda text, **kw: captured.append((text, kw)))
            code, reply, text = e2e_support.World.cli(world, "provider-observe", timeout=0.5)
            self.assertEqual((-1, None), (code, reply))
            self.assertIn("partial output", text)
            self.assertTrue(captured and captured[0][1]["timed_out"] and captured[0][1]["code"] == -1)
            self.assertTrue(world.steps[0]["timed_out"] and world.steps[0]["step"] == "provider-observe")
            self.assertNotIn(folder, text)


class FixRoundFaultTests(SceneTest):
    def test_an_id_missing_from_both_reports_is_not_isolation(self):
        registry = json.loads((ROOT / "tests" / "e2e" / "requirements.json").read_text())["requirements"]
        every = {item["id"] for item in registry}
        both = {i: {"passed": True} for i in every}
        fault = {i: {"passed": i not in lg.S3_IDS} for i in every}
        self.assertTrue(lg.fault_isolated({"requirements": both}, {"requirements": fault})["isolated"])
        gone = "G2"
        self.assertIn(gone, every)
        for side in ("normal", "fault"):
            with self.subTest(side=side):
                n = {i: v for i, v in both.items() if i != gone}
                f = {i: v for i, v in fault.items() if i != gone}
                self.assertFalse(lg.fault_isolated({"requirements": n}, {"requirements": f})["isolated"])
        self.assertFalse(lg.fault_isolated({"requirements": {}}, {"requirements": {}})["isolated"])
        # a partial run judges the IDs of the flows it ran (and the every-flow ones), not the whole registry
        part = {i["id"] for i in registry if (i["flow"] in ("L10", "L11", "L13") or (i["flow"] == "all" and i["checks"]))}
        n = {"flows": ["L10", "L11", "L13"], "requirements": {i: {"passed": True} for i in part}}
        f = {"flows": ["L10", "L11", "L13"], "requirements": {i: {"passed": i not in lg.S3_IDS} for i in part}}
        self.assertTrue(lg.fault_isolated(n, f)["isolated"])

    def test_the_two_guard_protection_faults_turn_only_l13_1_red(self):
        for name in ("guard_disabled_profile_refusal_removed", "guard_project_parent_refusal_removed"):
            with self.subTest(fault=name):
                self.assertIn(name, lg.FAULTS)
                self.assertEqual(("L13.1",), lg.FAULT_TARGETS[name])
                ids = ["L13.1", "L13.2", "G1"]
                normal = {"requirements": {i: {"passed": True} for i in ids}}
                red = {"requirements": {**normal["requirements"], "L13.1": {"passed": False}}}
                self.assertTrue(lg.fault_isolated(normal, red, name, expected_ids=ids)["isolated"])


class FixRoundSessionTests(unittest.TestCase):
    def test_a_disabled_profile_session_is_written_as_codex_writes_it(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "h"
            (home / ".codex").mkdir(parents=True)
            world = SimpleNamespace(home=home, SESSION_ROLLOUT=e2e_support.World.SESSION_ROLLOUT)
            thread = "019a2b3c-4d5e-7f60-8a9b-0c1d2e3f4a5c"
            path = e2e_support.World.codex_session(world, Path("/w/project"), thread=thread,
                                                   sandbox="danger-full-access", disabled_profile=True)
            context = json.loads(path.read_text().splitlines()[-1])["payload"]
            self.assertEqual("danger-full-access", context["sandbox_policy"]["type"])
            self.assertEqual({"type": "disabled"}, context["permission_profile"])
            self.assertNotIn("file_system_sandbox_policy", context)

    def test_the_guard_scene_uses_the_disabled_profile_and_a_checkout_nested_under_a_parent_that_excludes_home(self):
        source = (ROOT / "scripts" / "e2e_checks_learning_guard.py").read_text()
        self.assertIn("disabled_profile=True", source)
        self.assertNotIn("entries=[]", source)
        self.assertIn('"project_parent"', source)
        self.assertIn('"nest"', source)
