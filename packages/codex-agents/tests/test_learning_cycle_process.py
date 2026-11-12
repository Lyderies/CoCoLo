from __future__ import annotations

import contextlib
from copy import deepcopy
import hashlib
import io
import json
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import ORCHESTRATION_ROOT, TEMP_ROOT
from tests.learning_support import verification_outcome
from tests import test_learning_cycle_core as parity

from codex_agents.learning_contracts import candidate_base_digest, canonical_json, content_digest, keyed_digest
from codex_agents.learning_engine import mine_evaluation_case
from codex_agents.learning_cycle_core import plan_learning_cycle
from codex_agents import learning_cycle_process as helper

BOOTSTRAP = '''import sys, resource
sys.path.insert(0,sys.argv[1])
allowed={"codex_agents.contracts","codex_agents.learning_contracts","codex_agents.learning_analysis",
         "codex_agents.learning_cycle_core","codex_agents.learning_cycle_process"}
class FixedImports:
    def find_spec(self,name,path=None,target=None):
        if (name.partition(".")[0] in {"sqlite3","_sqlite3","anthropic","openai"}
                or (name.startswith("codex_agents.") and name not in allowed)):
            raise RuntimeError("calculation dependency refused")
sys.meta_path.insert(0,FixedImports())
from codex_agents.learning_cycle_process import main
code=main(sys.argv[2:])
if code==0:
    for kind, maximum in ((resource.RLIMIT_CPU,30),(resource.RLIMIT_FSIZE,0),
                          (resource.RLIMIT_CORE,0),(resource.RLIMIT_NOFILE,64)):
        soft,hard=resource.getrlimit(kind)
        assert 0 <= soft <= hard <= maximum
    assert not any(name.partition(".")[0] in {"sqlite3","_sqlite3","anthropic","openai"}
                   or name.startswith("codex_agents.") and name not in allowed for name in sys.modules)
raise SystemExit(code)
'''
PROFILE = '(version 1) (allow default) (deny network*) (deny file-write*) (deny process-fork)'
BINDING = "d" * 64


def request(operation, outcomes, cases=(), **clock):
    return canonical_json({"schema_version": 1, "operation": operation, "binding_digest": BINDING,
                           "outcomes": outcomes, "cases": list(cases), "owner_invalidated_run_ids": [], **clock})


def private_helper(raw, operation, *, restricted=True, extra_env=None, pass_fds=(), bootstrap=BOOTSTRAP):
    command = [sys.executable, "-I", "-B", "-S", "-c", bootstrap,
               str(ORCHESTRATION_ROOT), operation, "30"]
    if restricted:
        command = ["/usr/bin/sandbox-exec", "-p", PROFILE, *command]
    process = subprocess.Popen(
        command, env={**helper._ENVIRONMENT, **(extra_env or {})},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, pass_fds=pass_fds, start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(raw, timeout=10)
    except BaseException:
        process.kill()
        process.communicate(timeout=5)
        raise
    return process, stdout, stderr


def response(raw, source, operation):
    return helper.decode_calculation_reply(
        raw, input_digest=hashlib.sha256(source).hexdigest(), operation=operation, binding_digest=BINDING,
    )


class LearningCycleWireTests(unittest.TestCase):
    def test_selection_returns_only_exact_source_references_and_bound_reply(self):
        outcome = verification_outcome()
        raw = request("select_mining", [outcome])
        reply = helper.calculate_learning_request(raw, operation="select_mining")
        self.assertEqual({"mining_references": [[outcome["record_id"], content_digest(outcome)]]},
                         response(reply, raw, "select_mining"))
        changed = json.loads(reply)
        for field, value in (("input_digest", "f" * 64), ("binding_digest", "f" * 64),
                             ("operation", "plan"), ("schema_version", True), ("extra", "CANARY")):
            with self.subTest(field=field), self.assertRaises(helper.LearningProcessError):
                response(canonical_json({**changed, field: value}), raw, "select_mining")

    def test_plan_matches_complete_core_object_without_receiving_key_material(self):
        outcome = verification_outcome()
        criteria = {name: outcome[name] for name in (
            "deterministic_pass", "security_pass", "required_criteria_total", "required_criteria_passed",
        )}
        case = mine_evaluation_case(
            digest_key=b"K" * 32, outcome=outcome, fixture_digest=outcome["fixture_digest"],
            criteria_digest=content_digest(criteria), lineage_digest=keyed_digest(b"K" * 32, outcome["run_id"]),
        )
        raw = request("plan", [outcome], [case], as_of_ms=10, max_age_ms=100)
        expected = plan_learning_cycle([outcome], all_cases=[case], as_of_ms=10, max_age_ms=100)
        self.assertEqual(expected, response(helper.calculate_learning_request(raw, operation="plan"), raw, "plan"))
        self.assertNotIn(b'digest_key', raw)

    def test_nested_analysis_reply_refuses_unknown_fields_types_and_inconsistent_counts(self):
        outcome = verification_outcome()
        case = mine_evaluation_case(
            digest_key=b"K" * 32, outcome=outcome, fixture_digest=outcome["fixture_digest"],
            criteria_digest="a" * 64, lineage_digest="b" * 64,
        )
        raw = request("plan", [outcome], [case], as_of_ms=10, max_age_ms=100)
        valid = json.loads(helper.calculate_learning_request(raw, operation="plan"))
        task_slice, digest = outcome["task_slice"], outcome["configuration_digest"]
        paths = [
            (("analysis",), {"CANARY": "PRIVATE"}),
            (("analysis", "schema_version"), True),
            (("analysis", "as_of_ms"), -1),
            (("analysis", "owner_feedback_excluded"), 10_000),
            (("analysis", "slices", task_slice, "extra"), "CANARY"),
        ]
        prefix = ("analysis", "slices", task_slice, "configurations", digest)
        for field, value in (("trials", 0), ("accepted", 2), ("terminal_failures", True),
                             ("mean_quality_micros", 1_000_001), ("repairs", -1),
                             ("workflow_token_volume", None), ("CANARY", "PRIVATE")):
            paths.append(((*prefix, "observations", field), value))
        for field, value in (("stale", 1), ("insufficient_sample", False),
                             ("version_confounding", True), ("CANARY", "PRIVATE")):
            paths.append(((*prefix, "evidence_flags", field), value))
        paths.extend([((*prefix, "diagnoses"), ["CANARY"]),
                      ((*prefix, "alternative_explanations"), ["PRIVATE"])])
        for path, value in paths:
            changed = deepcopy(valid)
            target = changed["result"]
            for name in path[:-1]:
                target = target[name]
            target[path[-1]] = value
            with self.subTest(path=path):
                with self.assertRaises(helper.LearningProcessError) as caught:
                    response(canonical_json(changed), raw, "plan")
                self.assertEqual("learning_process_unavailable", str(caught.exception))

    def test_nested_candidate_reply_is_closed_without_creating_owner_records(self):
        raw = request("plan", [], as_of_ms=10, max_age_ms=100)
        valid = json.loads(helper.calculate_learning_request(raw, operation="plan"))
        champion = {name: verification_outcome()[name] for name in (
            "model", "effort", "role", "topology", "max_parallel", "verification_required",
            "verifier_digest", "bounds_digest", "package_digest", "policy_digest", "agent_digest",
            "skill_digests", "prompt_digest", "client_digest",
        )}
        before = {name: champion[name] for name in ("model", "effort")}
        after = {**before, "effort": "medium"}
        evidence = [f"{index:064x}" for index in range(40)]
        # Inert shape fixture only: it supplies no actual source or durable QA.
        spec = {
            "scope": "project", "task_slice": "verification-learning", "target": "model_effort",
            "effect": "optimize", "base_digest": candidate_base_digest(champion),
            "champion_configuration": champion,
            "change_manifest": [{"path_code": "routing.model_effort", "before_value": before,
                                 "after_value": after, "before_digest": content_digest(before),
                                 "after_digest": content_digest(after)}],
            "evidence_digests": evidence,
            "eligibility_receipt": {"task_slice": "verification-learning",
                                    "evidence_set_digest": content_digest(evidence), "independent_case_count": 20,
                                    "usage_complete": True, "version_unconfounded": True, "task_mix_matched": True,
                                    "retry_deduplicated": True, "verifier_evidence_complete": True},
        }
        valid["result"]["candidate_specs"] = [spec]
        self.assertEqual([spec], response(canonical_json(valid), raw, "plan")["candidate_specs"])
        changes = [
            (("CANARY",), "PRIVATE"), (("scope",), "general"), (("target",), "bounds"),
            (("task_slice",), "CANARY/private"), (("base_digest",), "f" * 64),
            (("champion_configuration", "extra"), "CANARY"),
            (("change_manifest",), [None]),
            (("change_manifest", 0, "after_value"), None),
            (("change_manifest", 0, "before_digest"), "f" * 64),
            (("change_manifest", 0, "after_value", "extra"), "CANARY"),
            (("evidence_digests",), evidence[::-1]), (("evidence_digests",), evidence[:-1]),
            (("eligibility_receipt", "independent_case_count"), True),
            (("eligibility_receipt", "task_slice"), "other"),
            (("eligibility_receipt", "usage_complete"), False),
            (("eligibility_receipt", "evidence_set_digest"), "f" * 64),
            (("eligibility_receipt", "extra"), "CANARY"),
        ]
        for path, value in changes:
            changed = deepcopy(valid)
            target = changed["result"]["candidate_specs"][0]
            for name in path[:-1]:
                target = target[name]
            target[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(helper.LearningProcessError):
                response(canonical_json(changed), raw, "plan")
        valid["result"]["proposal_status"] = "configuration_limit"
        with self.assertRaises(helper.LearningProcessError):
            response(canonical_json(valid), raw, "plan")

    def test_changed_core_cannot_emit_open_result_to_wire(self):
        raw = request("plan", [], as_of_ms=10, max_age_ms=100)
        with mock.patch.object(helper, "plan_learning_cycle", return_value={
            "analysis": {"CANARY": "PRIVATE"}, "proposal_status": "complete", "candidate_specs": [],
        }), self.assertRaises(helper.LearningProcessError):
            helper.calculate_learning_request(raw, operation="plan")

    def test_root_keys_canonical_bytes_mode_and_numeric_contract_are_closed(self):
        raw = request("select_mining", [])
        value = json.loads(raw)
        bad = [b"CANARY", b"[]", raw + b"\n", b'{"schema_version":1,"schema_version":1}',
               canonical_json({**value, "digest_key": "CANARY"}),
               canonical_json({**value, "schema_version": True}),
               canonical_json({**value, "outcomes": {}})]
        for data in bad:
            with self.subTest(size=len(data)), self.assertRaises(helper.LearningProcessError) as caught:
                helper.calculate_learning_request(data, operation="select_mining")
            self.assertNotIn("CANARY", str(caught.exception))
        with self.assertRaises(helper.LearningProcessError):
            helper.calculate_learning_request(raw, operation="plan")

    def test_wire_and_output_bounds_refuse_instead_of_truncating(self):
        raw = request("select_mining", [])
        with mock.patch.object(helper, "MAX_INPUT_BYTES", 1), self.assertRaises(helper.LearningProcessError):
            helper.calculate_learning_request(raw, operation="select_mining")
        with mock.patch.object(helper, "MAX_OUTPUT_BYTES", 1), self.assertRaises(helper.LearningProcessError):
            helper.calculate_learning_request(raw, operation="select_mining")
        with mock.patch.object(helper, "MAX_STRUCTURE_WEIGHT", 1), self.assertRaises(helper.LearningProcessError):
            helper.calculate_learning_request(raw, operation="select_mining")

    def test_closed_arguments_and_fault_fence_do_not_echo_content(self):
        self.assertEqual(("plan", 30), helper.process_arguments(["plan", "30"]))
        for args in (None, (), [], ["CANARY", "30"], ["plan", "0"], ["plan", "01"],
                     ["plan", "9999"], ["plan", "\u0661"], ["plan", "30", "CANARY"]):
            with self.assertRaises(helper.LearningProcessError):
                helper.process_arguments(args)
        for error in (RuntimeError, MemoryError, AssertionError, KeyboardInterrupt, SystemExit):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                    mock.patch.object(helper, "_require_environment"), mock.patch.object(helper, "_require_descriptors"), \
                    mock.patch.object(helper, "_apply_limits"), mock.patch.object(helper, "_require_fork_denial"), \
                    mock.patch.object(helper, "_run", side_effect=error("CANARY")):
                self.assertEqual(3, helper.main(["plan", "30"]))
            self.assertEqual(("", ""), (stdout.getvalue(), stderr.getvalue()))


MACOS_HELPER = sys.platform == "darwin" and Path("/usr/bin/sandbox-exec").is_file()


@unittest.skipUnless(MACOS_HELPER, "requires disposable macOS no-fork policy; no installed policy is selected")
class LearningCycleProcessTests(unittest.TestCase):
    def ready_body(self, process, raw, operation):
        mode = b"S" if operation == "select_mining" else b"P"
        self.assertEqual(helper.LEARNING_PROCESS_READY + mode + struct.pack(">Q", process.pid), raw[:helper.READY_BYTES])
        return raw[helper.READY_BYTES:]

    def test_real_restricted_process_returns_same_pid_and_exact_nonempty_result(self):
        raw = request("select_mining", [verification_outcome()])
        process, stdout, stderr = private_helper(raw, "select_mining")
        self.assertEqual((0, b""), (process.returncode, stderr))
        body = self.ready_body(process, stdout, "select_mining")
        self.assertEqual(response(helper.calculate_learning_request(raw, operation="select_mining"), raw, "select_mining"),
                         response(body, raw, "select_mining"))

    def test_unrestricted_process_or_extra_environment_refuses_before_ready(self):
        raw = request("select_mining", [])
        for options in ({"restricted": False}, {"extra_env": {"OPENAI_API_KEY": "SYNTHETIC-CANARY"}}):
            with self.subTest(options=tuple(options)):
                process, stdout, stderr = private_helper(raw, "select_mining", **options)
                self.assertEqual((2, b"", b""), (process.returncode, stdout, stderr))

    def test_inherited_descriptor_is_refused_without_reading_or_echoing_it(self):
        with tempfile.TemporaryFile(dir=TEMP_ROOT) as file:
            file.write(b"SYNTHETIC-PRIVATE-CANARY")
            file.flush()
            process, stdout, stderr = private_helper(request("select_mining", []), "select_mining", pass_fds=(file.fileno(),))
        self.assertEqual((2, b"", b""), (process.returncode, stdout, stderr))

    def test_invalid_input_leaves_only_ready_marker_and_no_traceback(self):
        process, stdout, stderr = private_helper(b"CANARY", "plan")
        self.assertEqual((2, b""), (process.returncode, stderr))
        self.assertEqual(b"", self.ready_body(process, stdout, "plan"))

    def test_calculation_io_is_refused_in_actual_process(self):
        bootstrap = ("import sys;sys.path.insert(0,sys.argv[1]);"
                     "from codex_agents import learning_cycle_process as p;"
                     "p.calculate_learning_request=lambda *a,**k: open('SYNTHETIC-CANARY').read();"
                     "raise SystemExit(p.main(sys.argv[2:]))")
        process, stdout, stderr = private_helper(request("plan", [], as_of_ms=1, max_age_ms=1), "plan", bootstrap=bootstrap)
        self.assertEqual((2, b""), (process.returncode, stderr))
        self.assertEqual(b"", self.ready_body(process, stdout, "plan"))


@unittest.skipUnless(MACOS_HELPER, "requires disposable macOS no-fork policy; no installed policy is selected")
class LearningProcessLegacyParityTests(parity.LearningCycleLegacyParityTests):
    """Actual keyless helper roundtrips inside the existing unchanged-legacy oracle."""

    def setUp(self):
        super().setUp()

        def isolated(outcomes, *, all_cases, as_of_ms, max_age_ms, owner_invalidated_run_ids=()):
            value = json.loads(request("plan", outcomes, all_cases, as_of_ms=as_of_ms, max_age_ms=max_age_ms))
            value["owner_invalidated_run_ids"] = sorted(owner_invalidated_run_ids)
            raw = canonical_json(value)
            process, stdout, stderr = private_helper(raw, "plan")
            self.assertEqual((0, b""), (process.returncode, stderr))
            self.assertEqual(helper.LEARNING_PROCESS_READY + b"P" + struct.pack(">Q", process.pid), stdout[:helper.READY_BYTES])
            return response(stdout[helper.READY_BYTES:], raw, "plan")

        patch = mock.patch.object(parity.core, "plan_learning_cycle", side_effect=isolated)
        patch.start()
        self.addCleanup(patch.stop)
