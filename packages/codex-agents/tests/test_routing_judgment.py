"""RT-21 Phase 0: real policy cells, truthful records and bounded quality retries."""
from __future__ import annotations

# ruff: noqa: I001 -- support bootstraps the native package.
import json
import unittest
from copy import deepcopy

from tests.support import FIXTURES_ROOT, ORCHESTRATION_ROOT
from tests.test_routing_risk import (
    GOLDEN_PATH, POLICY_PATH, SEMANTIC, base_task, enable_providers,
    golden_buckets, load_from_disk, short, with_failures,
)
from codex_agents.contracts import ContractError, validate_task
from codex_agents.learning_contracts import content_digest
from codex_agents.routing import PolicyError, load_policy, route_task

LEGACY_PATH = FIXTURES_ROOT / "routing-policy-v5.json"
LEGACY_GOLDEN = FIXTURES_ROOT / "routing-policy-v5-golden.json"


def judgment(**overrides):
    return base_task(operation="review", acceptance_strength="independent_judgment",
                     deterministic_validation=False, **overrides)


class JudgmentRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shipped = load_policy(POLICY_PATH)

    def setUp(self):
        self.policy = enable_providers(deepcopy(self.shipped))

    def route(self, task=None, policy=None):
        return route_task(task or judgment(), policy or self.policy, primary_provider="claude")

    def test_low_medium_judgment_routes_codex_first_per_family(self):
        for family, keys in (("coding", ("exploratory", "judgment")),
                             ("research", ("exploratory", "judgment")),
                             ("planning", ("synthesis",))):
            for key in keys:
                for risk in ("low", "medium"):
                    with self.subTest(family=family, key=key, risk=risk):
                        effort = "medium" if family == "coding" and risk == "low" else "high"
                        expected = [{"provider": "codex", "model": "gpt-6.1-sol", "effort": effort},
                                    {"provider": "claude", "model": "claude-sonnet-5-5", "effort": effort}]
                        self.assertEqual(expected, self.policy.get("risk_candidates", {}).get(
                            family, {}).get(key, {}).get(risk))
                        task = judgment(family=family, risk=risk,
                                        complexity="medium" if key == "exploratory" else "extreme")
                        route = self.route(task)
                        self.assertEqual(tuple(expected[0].values()), short(route))
                        self.assertEqual(risk, route["routing_basis"]["risk_band"])
                        fallback = deepcopy(self.policy)
                        fallback["provider_routing"]["providers"]["codex"]["models"] = ["gpt-6-luna"]
                        fallback["provider_config_digest"] = content_digest(fallback["provider_routing"])
                        self.assertEqual(tuple(expected[1].values()), short(self.route({**task, "meaningful": False}, fallback)))

    def test_medium_complexity_review_reaches_the_banded_cell(self):
        for family in ("coding", "research", "planning"):
            for risk in ("low", "medium"):
                with self.subTest(family=family, risk=risk):
                    medium = self.route(judgment(family=family, risk=risk, complexity="medium"))
                    extreme = self.route(judgment(family=family, risk=risk, complexity="extreme"))
                    self.assertEqual(short(medium), short(extreme))
                    self.assertEqual(risk, medium["routing_basis"]["risk_band"])

    def test_high_critical_routes_are_unchanged(self):
        old = json.loads(LEGACY_GOLDEN.read_text())
        actual = golden_buckets(self.shipped)
        high = {key: value for key, value in old.items()
                if "/high/" in key or "/critical/" in key}
        self.assertEqual(108, len(high))
        self.assertEqual(high, {key: actual[key] for key in high})
        shipped_golden = json.loads(GOLDEN_PATH.read_text())
        self.assertEqual(high, {key: shipped_golden[key] for key in high})

    def test_missing_cell_falls_back_and_records_it(self):
        policy = deepcopy(self.policy)
        policy.get("risk_candidates", {}).get("coding", {}).get("judgment", {}).pop("low", None)
        task = judgment(risk="low", complexity="extreme")
        route = self.route(task, policy)
        self.assertEqual(tuple(policy["provider_candidates"]["coding"]["judgment"][0].values()), short(route))
        self.assertNotIn("risk_band", route.get("routing_basis", {}))
        self.assertEqual("provider_candidates", route["routing_basis"]["risk_cell_fallback"])

    def test_worker_advertises_sonnet_verifier_for_codex_worker_low_medium(self):
        for risk in ("low", "medium"):
            with self.subTest(risk=risk):
                task = judgment(risk=risk)
                worker = self.route(task)
                self.assertEqual("codex", worker["provider"])
                self.assertEqual(("claude", "claude-sonnet-5-5", "medium"), short(worker["verifier"]))
                verify = base_task(family="verification", operation="review", risk=risk,
                                   verification_for_family="coding", verification_for_provider="codex",
                                   verification_for_outcome="a" * 64)
                self.assertEqual(short(worker["verifier"]), short(self.route(verify)))
                for admission in ({"enabled": False}, {"enabled": True, "models": []}):
                    blocked = deepcopy(self.policy)
                    blocked["provider_routing"]["providers"]["claude"] = admission
                    blocked["provider_config_digest"] = content_digest(blocked["provider_routing"])
                    # Same-provider fallback (owner decision 17), recorded as not independent.
                    fallback = self.route(task, blocked)
                    self.assertEqual("codex", fallback["verifier"]["provider"])
                    self.assertIs(False, fallback["verifier"]["independent"])
                    self.assertEqual("codex", self.route(verify, blocked)["provider"])

    def test_high_security_verifier_stays_opus(self):
        for risk in ("high", "critical"):
            route = self.route(judgment(family="security", risk=risk))
            self.assertEqual(("codex", "gpt-6-astra", "high"), short(route))
            self.assertEqual(("claude", "claude-opus-5-5", "high"), short(route["verifier"]))

    def test_confirmed_quality_failure_escalates_once_to_next_rung(self):
        for family in ("coding", "research", "planning"):
            for risk in ("low", "medium"):
                with self.subTest(family=family, risk=risk):
                    task = judgment(family=family, risk=risk, complexity="extreme")
                    initial = self.route(task)
                    retry = self.route(with_failures(task, 1))
                    record = retry["routing_basis"]["escalation"]
                    self.assertEqual("next", record["step"])
                    self.assertEqual(2, record["attempt"])
                    ladder = [short(rung) for rung in self.policy["escalation"]["ladders"][family]]
                    self.assertEqual(ladder[ladder.index(short(initial)) + 1], short(retry))
                    expected_effort = "medium" if family == "coding" and risk == "low" else "high"
                    self.assertEqual(("claude", "claude-sonnet-5-5", expected_effort), short(retry))
                    required = [
                        ("codex", "gpt-6.1-sol", "medium"),
                        ("claude", "claude-sonnet-5-5", "medium"),
                        ("codex", "gpt-6.1-sol", "high"),
                        ("claude", "claude-sonnet-5-5", "high"),
                        ("claude", "claude-opus-5-5", "high"),
                        ("codex", "gpt-6-astra", "high"),
                    ]
                    self.assertEqual(required, [rung for rung in ladder if rung in required])
                    self.assertEqual(short(initial), short(record["from"]))
                    self.assertEqual(short(retry), short(record["to"]))

    def test_code_not_ready_without_event_does_not_escalate(self):
        self.assertNotIn("escalation", self.route().get("routing_basis", {}))

    def test_unlisted_or_unadjudicated_failure_is_held(self):
        for reason in ("transient", "result_contract"):
            with self.subTest(reason=reason):
                event = {"attempt": 1, "reason": reason, "evidence_refs": ["host:not-ready"]}
                retry = self.route(judgment(failed_attempts=1, failure_events=[event]))
                self.assertEqual(short(self.route()), short(retry))
                self.assertEqual("held_judgment", retry["routing_basis"]["escalation"]["step"])
        for reason in ("semantic", "reasoning"):
            retry = self.route(judgment(failed_attempts=1, failure_events=[
                {**SEMANTIC, "reason": reason, "evidence_refs": ["host:unconfirmed"]}]))
            self.assertEqual(short(self.route()), short(retry))
            self.assertEqual("held_judgment", retry["routing_basis"]["escalation"]["step"])

    def test_second_failure_goes_to_strongest_and_third_is_refused(self):
        retry = self.route(with_failures(judgment(complexity="extreme"), 2))
        self.assertEqual(("codex", "gpt-6-astra", "high"), short(retry))
        self.assertEqual("strongest", retry["routing_basis"]["escalation"]["step"])
        with self.assertRaisesRegex(ContractError, "0 to 2"):
            self.route(judgment(failed_attempts=3))

    def test_no_ladder_family_never_escalates(self):
        for family in ("security", "inventory"):
            task = judgment(family=family)
            first = self.route(task)
            retry = self.route(with_failures(task, 1))
            self.assertEqual(first, retry)
            self.assertNotIn("escalation", retry.get("routing_basis", {}))

    def test_policy_v6_loads_and_old_policy_still_routes(self):
        self.assertEqual(6, self.shipped["version"])
        legacy = load_policy(LEGACY_PATH)
        self.assertEqual(5, legacy["version"])
        self.assertEqual(json.loads(LEGACY_GOLDEN.read_text()), golden_buckets(legacy))
        # Already shipped bounded coding remains byte-for-byte identical, including retries.
        legacy = enable_providers(legacy)
        for risk in ("low", "medium"):
            for failed in (0, 1, 2):
                task = with_failures(base_task(risk=risk), failed)
                self.assertEqual(self.route(task, legacy), self.route(task))

    def test_ladder_must_contain_every_candidate(self):
        for family in ("coding", "research", "planning"):
            with self.subTest(family=family):
                policy = deepcopy(self.policy)
                ladder = policy.get("escalation", {}).get("ladders", {}).get(family)
                self.assertIsNotNone(ladder)
                policy["escalation"]["ladders"][family] = [rung for rung in ladder
                    if rung["model"] != "claude-sonnet-5-5"]
                with self.assertRaisesRegex(PolicyError, "ladder"):
                    load_from_disk(policy)

    def test_high_band_below_first_choice_still_refused(self):
        for family in ("coding", "research"):
            for risk in ("high", "critical"):
                with self.subTest(family=family, risk=risk):
                    policy = deepcopy(self.policy)
                    policy.setdefault("risk_candidates", {}).setdefault(family, {}).setdefault("judgment", {})[risk] = [
                        {"provider": "codex", "model": "gpt-6.1-sol", "effort": "medium"}]
                    with self.assertRaisesRegex(PolicyError, "first choice"):
                        load_from_disk(policy)

    def test_route_record_carries_phase_parent_and_band(self):
        fields = dict(review_phase="confirmation", parent_review_id="review-1", expanded_scope=False)
        task = judgment(**fields)
        route = self.route(task)
        basis = route["routing_basis"]
        self.assertEqual("medium", basis["risk_band"])
        for name, value in fields.items():
            self.assertEqual(value, basis[name])
        for name in fields:
            self.assertNotIn(name, self.route()["routing_basis"])
        self.assertEqual({key: route[key] for key in ("provider", "model", "effort")}, basis["requested_setting"])
        self.assertIsNone(basis["observed_setting"])
        self.assertIsNone(basis["usage"])
        self.assertEqual(dict(admission_state=None, execution_state=None, semantic_verdict=None), basis["verdict_axes"])
        self.assertEqual(short(self.route()), short(route))
        schema = json.loads((ORCHESTRATION_ROOT / "schemas/task.schema.json").read_text())
        self.assertEqual(["first", "delta", "confirmation"], schema["properties"]["review_phase"]["enum"])
        self.assertEqual("boolean", schema["properties"]["expanded_scope"]["type"])
        for name, value in (("review_phase", "final"), ("review_phase", []),
                            ("parent_review_id", ""), ("parent_review_id", 3),
                            ("expanded_scope", 1), ("expanded_scope", None)):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ContractError):
                    validate_task(judgment(**{name: value}))
        for phase in ("first", "delta", "confirmation"):
            self.assertEqual(phase, validate_task(judgment(review_phase=phase))["review_phase"])

    def test_retry_keeps_parent_lineage_and_failure_evidence(self):
        task = with_failures(judgment(id="review-retry", parent_review_id="review-root",
                                     review_phase="delta", expanded_scope=True), 1)
        route = self.route(task)
        self.assertEqual("review-retry", route["task_id"])
        basis = route["routing_basis"]
        self.assertEqual("review-root", basis["parent_review_id"])
        self.assertEqual(1, basis["failed_attempts"])
        self.assertEqual(task["failure_events"], basis["failure_events"])
        self.assertEqual(2, basis["escalation"]["attempt"])
        self.assertTrue(basis["expanded_scope"])

    def test_scope_validation_and_legacy_unadjudicated_failure(self):
        for version, scope in ((5, "risk_banded_judgment"), (6, "all"), (6, [])):
            with self.subTest(version=version, scope=scope):
                policy = deepcopy(self.policy)
                policy["version"] = version
                policy["escalation"]["scope"] = scope
                with self.assertRaisesRegex(PolicyError, "scope"):
                    load_from_disk(policy)
        legacy = enable_providers(load_policy(LEGACY_PATH))
        with self.assertRaisesRegex(ContractError, "verification record digests"):
            self.route(judgment(failed_attempts=1, failure_events=[
                {**SEMANTIC, "evidence_refs": ["host:unconfirmed"]}]), legacy)

    def test_older_confirmed_failure_does_not_confirm_the_latest_attempt(self):
        task = with_failures(judgment(), 2)
        task["failure_events"][1] = {"attempt": 2, "reason": "transient",
                                     "evidence_refs": ["host:capacity"]}
        route = self.route(task)
        self.assertEqual(short(self.route()), short(route))
        self.assertEqual("held_judgment", route["routing_basis"]["escalation"]["step"])

    def test_review_labels_are_not_rewritten_by_routing(self):
        task = judgment(risk="low")
        original = deepcopy(task)
        route = self.route(task)
        self.assertEqual(original, task)
        self.assertEqual("coding", route["routed_family"])
        self.assertNotEqual("bounded_executable_work", route["reason"])
        for key in ("family", "operation", "acceptance_strength", "context_breadth", "routing_evidence_refs"):
            self.assertEqual(task[key], route["routing_basis"][key])
