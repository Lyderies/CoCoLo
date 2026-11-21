"""R3 (RT-2, RT-3 escalation, RT-5): risk, failed attempts and the verifier follow the policy.

The shipped policy opts low/medium bounded coding and verifiers into O0d bands. The golden
fixture pins the route cross product; synthetic policies independently exercise all R3 mechanisms.
"""

from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import hashlib
import itertools
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from tests.support import FIXTURES_ROOT, ORCHESTRATION_ROOT

from codex_agents.contracts import ContractError
from codex_agents.learning_contracts import content_digest
from codex_agents.routing import PolicyError, load_policy, route_task

POLICY_PATH = ORCHESTRATION_ROOT / "config" / "routing-policy.json"
GOLDEN_PATH = FIXTURES_ROOT / "routing-shipped-policy-golden.json"

RISKS = ("low", "medium", "high", "critical")
FAMILIES = ("inventory", "research", "planning", "coding", "security", "verification")
SEMANTIC = {"attempt": 1, "reason": "semantic", "evidence_refs": ["verification:" + "a" * 64]}
REASONING = {"attempt": 2, "reason": "reasoning", "evidence_refs": ["verification:" + "b" * 64]}
TRANSIENT_1 = {"attempt": 1, "reason": "transient", "evidence_refs": ["host:timeout-1"]}
TRANSIENT_2 = {"attempt": 2, "reason": "transient", "evidence_refs": ["host:timeout-2"]}


def base_task(**overrides: object) -> dict:
    task = {
        "id": "task-1",
        "objective": "Complete one bounded task",
        "family": "coding",
        "complexity": "medium",
        "risk": "medium",
        "operation": "implementation",
        "ambiguity": "known",
        "context_breadth": "repository",
        "acceptance_strength": "executable",
        "routing_evidence_refs": [],
        "meaningful": True,
        "deterministic_validation": True,
        "independent_parts": 1,
        "parallel_safe": False,
        "failed_attempts": 0,
        "failure_events": [],
    }
    task.update(overrides)
    return task


def with_failures(task: dict, failed_attempts: int) -> dict:
    events = {0: [], 1: [SEMANTIC], 2: [SEMANTIC, REASONING]}[failed_attempts]
    return {**task, "failed_attempts": failed_attempts, "failure_events": deepcopy(events)}


def provider_config(*, bridge: bool = True) -> dict:
    return {
        "version": 1,
        "providers": {"codex": {"enabled": True}, "claude": {"enabled": True}},
        "codex_primary_may_bridge_to_claude": bridge,
    }


def enable_providers(policy: dict, *, bridge: bool = True) -> dict:
    config = provider_config(bridge=bridge)
    policy["provider_routing"] = config
    policy["provider_config_digest"] = content_digest(config)
    return policy


def outcome(task: dict, policy: dict, primary: str) -> object:
    """The route, or the refusal, exactly as a caller would see it."""

    try:
        return route_task(task, policy, primary_provider=primary)
    except ContractError as error:
        return {"refused": str(error)}


def load_from_disk(policy: dict) -> dict:
    """Load a policy through the file validator; runtime-only provider keys stay off disk."""

    on_disk = {k: v for k, v in policy.items() if k not in ("provider_routing", "provider_config_digest")}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "routing-policy.json"
        path.write_text(json.dumps(on_disk))
        return load_policy(path)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def golden_buckets(policy: dict) -> dict[str, str]:
    """One digest per (provider configuration, family, risk, failed attempts) bucket."""

    configurations = {
        "default_codex_primary": (policy, "codex"),
        "claude_primary": (enable_providers(deepcopy(policy)), "claude"),
        "codex_primary_bridged": (enable_providers(deepcopy(policy)), "codex"),
    }
    full_axes = {
        "operation": ("mechanical", "exploration", "implementation", "diagnosis",
                      "synthesis", "review", "end_to_end"),
        "complexity": ("low", "high", "extreme"),
        "ambiguity": ("known", "partial", "high", "unknown"),
        "context_breadth": ("narrow", "repository", "cross_system", "unknown"),
        "acceptance_strength": ("deterministic", "executable", "independent_judgment",
                                "owner_decision"),
    }
    reduced_axes = {
        "operation": full_axes["operation"],
        "complexity": ("low", "high"),
        "ambiguity": ("known", "high"),
        "context_breadth": ("narrow", "cross_system"),
        "acceptance_strength": ("executable", "independent_judgment"),
    }

    def shapes(axes: dict[str, tuple[str, ...]], family: str) -> list[dict]:
        return [
            base_task(family=family, **dict(zip(axes, values, strict=True)))
            for values in itertools.product(*axes.values())
        ]

    buckets: dict[str, str] = {}
    for name, (configured, primary) in configurations.items():
        for family in FAMILIES:
            # The full shape product at the default risk and no failed attempt.
            buckets[f"{name}/{family}/shapes"] = digest(
                [outcome(task, configured, primary) for task in shapes(full_axes, family)]
            )
            reduced = shapes(reduced_axes, family)
            for risk in RISKS:
                for failed in (0, 1, 2):
                    buckets[f"{name}/{family}/{risk}/{failed}"] = digest(
                        [
                            outcome(with_failures({**task, "risk": risk}, failed), configured, primary)
                            for task in reduced
                        ]
                    )
    return buckets


def build_policy(shipped: dict) -> dict:
    """The shipped policy plus synthetic R3 candidates, escalation and verifier settings."""

    policy = enable_providers(deepcopy(shipped))
    codex = lambda model, effort: {"provider": "codex", "model": model, "effort": effort}  # noqa: E731
    claude = lambda model, effort: {"provider": "claude", "model": model, "effort": effort}  # noqa: E731
    policy["escalation"] = {
        "failure_reasons": ["semantic", "reasoning"],
        # Ascending strength. Every candidate of a laddered family must be on its ladder.
        "ladders": {
            "coding": [
                claude("claude-haiku-5-5", "high"),
                codex("gpt-6.1-sol", "medium"),
                claude("claude-sonnet-5-5", "medium"),
                claude("claude-opus-5-5", "medium"),
                codex("gpt-6.1-sol", "high"),
                claude("claude-opus-5-5", "high"),
            ]
        },
    }
    policy["risk_candidates"] = {
        "coding": {
            "bounded": {
                "low": [claude("claude-haiku-5-5", "high"), codex("gpt-6.1-sol", "medium")],
                "high": [claude("claude-opus-5-5", "medium"), codex("gpt-6.1-sol", "medium")],
                "critical": [claude("claude-opus-5-5", "high"), codex("gpt-6.1-sol", "high")],
            }
        }
    }
    policy["providers"]["claude"]["verifier"]["by_risk"] = {
        "low": {"model": "claude-haiku-5-5", "effort": "medium"},
        "high": {"model": "claude-opus-5-5", "effort": "high"},
        "critical": {"model": "claude-opus-5-5", "effort": "max"},
    }
    policy["verification"]["by_risk"] = {
        "low": {"model": "gpt-6.1-sol", "effort": "low"},
        "critical": {"model": "gpt-6-astra", "effort": "high"},
    }
    return policy


def short(route: dict) -> tuple[str, str, str]:
    return (route["provider"], route["model"], route["effort"])


class ShippedVerifierBandTests(unittest.TestCase):
    """O0d: ship cheaper low/medium bands without changing high/critical routes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.policy = load_policy(POLICY_PATH)

    def test_shipped_claude_verifier_low_medium_is_sonnet_medium(self) -> None:
        policy = enable_providers(deepcopy(self.policy))
        for risk in ("low", "medium"):
            with self.subTest(risk=risk):
                self.assertEqual(
                    {"model": "claude-sonnet-5-5", "effort": "medium"},
                    policy["providers"]["claude"]["verifier"].get("by_risk", {}).get(risk),
                )
                worker = route_task(base_task(risk=risk), policy, primary_provider="claude")
                self.assertEqual(("claude", "claude-sonnet-5-5", "medium"), short(worker["verifier"]))
                review = route_task(base_task(
                    family="verification", operation="review", risk=risk,
                    verification_for_family="coding", verification_for_provider="codex",
                    verification_for_outcome="a" * 64,
                ), policy, primary_provider="claude")
                self.assertEqual(short(worker["verifier"]), short(review))

    def test_shipped_codex_verifier_low_medium_is_sol_medium(self) -> None:
        policy = enable_providers(deepcopy(self.policy))
        for risk in ("low", "medium"):
            with self.subTest(risk=risk):
                self.assertEqual(
                    {"model": "gpt-6.1-sol", "effort": "medium"},
                    policy["verification"].get("by_risk", {}).get(risk),
                )
                worker = route_task(base_task(
                    risk=risk, operation="review", ambiguity="partial",
                    acceptance_strength="independent_judgment", deterministic_validation=False,
                ), policy, primary_provider="claude")
                self.assertEqual("codex", worker["provider"])
                self.assertEqual(("claude", "claude-sonnet-5-5", "medium"), short(worker["verifier"]))
                review = route_task(base_task(
                    family="verification", operation="review", risk=risk,
                    verification_for_family="coding", verification_for_provider="claude",
                    verification_for_outcome="a" * 64,
                ), policy, primary_provider="claude")
                self.assertEqual(("codex", "gpt-6.1-sol", "medium"), short(review))

    def test_shipped_bounded_coding_low_medium_candidates_and_fallback(self) -> None:
        expected = [
            {"provider": "codex", "model": "gpt-6-sol", "effort": "medium"},
            {"provider": "claude", "model": "claude-sonnet-5-5", "effort": "medium"},
        ]
        for risk in ("low", "medium"):
            with self.subTest(risk=risk):
                self.assertEqual(expected, self.policy.get("risk_candidates", {}).get(
                    "coding", {}).get("bounded", {}).get(risk))
                policy = enable_providers(deepcopy(self.policy))
                self.assertEqual(tuple(expected[0].values()), short(route_task(
                    base_task(risk=risk), policy, primary_provider="claude")))
                policy["provider_routing"]["providers"]["codex"]["enabled"] = False
                policy["provider_config_digest"] = content_digest(policy["provider_routing"])
                self.assertEqual(tuple(expected[1].values()), short(route_task(
                    base_task(risk=risk), policy, primary_provider="claude")))

    def test_shipped_high_critical_security_reviews_keep_opus(self) -> None:
        policy = enable_providers(deepcopy(self.policy))
        for risk in ("high", "critical"):
            with self.subTest(risk=risk):
                worker = route_task(base_task(family="security", risk=risk, operation="review"),
                                    policy, primary_provider="claude")
                self.assertEqual(("codex", "gpt-6-astra", "high"), short(worker))
                self.assertEqual(("claude", "claude-opus-5-5", "high"), short(worker["verifier"]))
                review = route_task(base_task(
                    family="verification", operation="review", risk=risk,
                    verification_for_family="security", verification_for_provider="codex",
                    verification_for_outcome="a" * 64,
                ), policy, primary_provider="claude")
                self.assertEqual(short(worker["verifier"]), short(review))


class GoldenTests(unittest.TestCase):
    """The shipped policy routes exactly as its approved golden fixture records."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.shipped = load_policy(POLICY_PATH)

    def test_shipped_policy_carries_only_approved_risk_entries(self) -> None:
        self.assertEqual({"coding", "research", "planning"}, set(self.shipped["risk_candidates"]))
        expected_keys = {"coding": {"bounded", "exploratory", "judgment"},
                         "research": {"exploratory", "judgment"}, "planning": {"synthesis"}}
        for family, keys in expected_keys.items():
            self.assertEqual(keys, set(self.shipped["risk_candidates"][family]))
            for bands in self.shipped["risk_candidates"][family].values():
                self.assertEqual({"low", "medium"}, set(bands))
        self.assertEqual("risk_banded_judgment", self.shipped["escalation"]["scope"])
        self.assertEqual(["semantic", "reasoning"], self.shipped["escalation"]["failure_reasons"])
        self.assertEqual(set(expected_keys), set(self.shipped["escalation"]["ladders"]))
        for bands in (self.shipped["verification"]["by_risk"],
                      self.shipped["providers"]["claude"]["verifier"]["by_risk"]):
            self.assertEqual({"low", "medium"}, set(bands))

    def test_shipped_policy_routes_identically_over_the_cross_product(self) -> None:
        expected = json.loads(GOLDEN_PATH.read_text())
        actual = golden_buckets(self.shipped)
        self.assertEqual(sorted(expected), sorted(actual))
        changed = sorted(name for name in expected if expected[name] != actual[name])
        self.assertEqual([], changed, "routes differ from the approved shipped policy")


class RiskRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shipped = load_policy(POLICY_PATH)
        cls.policy = build_policy(cls.shipped)

    def route(self, **overrides: object) -> dict:
        return route_task(base_task(**overrides), self.policy, primary_provider="claude")

    def test_each_risk_band_selects_its_own_candidate_list(self) -> None:
        self.assertEqual(
            ("claude", "claude-haiku-5-5", "high"), short(self.route(risk="low"))
        )
        self.assertEqual(
            ("codex", "gpt-6.1-sol", "medium"), short(self.route(risk="medium"))
        )
        self.assertEqual(
            ("claude", "claude-opus-5-5", "medium"), short(self.route(risk="high"))
        )
        self.assertEqual(
            ("claude", "claude-opus-5-5", "high"), short(self.route(risk="critical"))
        )

    def test_a_band_without_an_entry_uses_todays_list(self) -> None:
        medium = self.route(risk="medium")
        plain = enable_providers(deepcopy(self.shipped))
        plain.pop("risk_candidates", None)  # Exercise absence, independent of shipped opt-ins.
        today = route_task(
            base_task(risk="medium"), plain,
            primary_provider="claude",
        )
        self.assertEqual(short(today), short(medium))
        self.assertNotIn("routing_basis", medium)
        # A family with no risk entries at all is untouched, whatever the risk.
        for risk in RISKS:
            with self.subTest(risk=risk):
                research = route_task(
                    base_task(family="research", risk=risk, operation="exploration",
                              ambiguity="partial", acceptance_strength="independent_judgment",
                              deterministic_validation=False),
                    self.policy, primary_provider="claude",
                )
                self.assertNotIn("routing_basis", research)

    def test_the_route_record_states_the_risk_band_used(self) -> None:
        route = self.route(risk="high")
        self.assertEqual("high", route["routing_basis"]["risk"])
        self.assertEqual("high", route["routing_basis"]["risk_band"])
        self.assertNotIn("escalation", route["routing_basis"])
        self.assertEqual("bounded_executable_work", route["reason"])

    def test_the_role_and_agent_follow_the_banded_provider(self) -> None:
        route = self.route(risk="critical")
        self.assertEqual("claude", route["provider"])
        self.assertEqual("claude_agents_implementation_worker", route["role"])
        self.assertEqual("claude_agents_implementation_worker", route["agent_type"])

    def test_high_and_critical_never_route_below_the_first_choice(self) -> None:
        floor = ("codex", "gpt-6.1-sol", "medium")
        ladder = [tuple(item.values()) for item in self.policy["escalation"]["ladders"]["coding"]]
        for risk in ("high", "critical"):
            with self.subTest(risk=risk):
                route = self.route(risk=risk)
                self.assertGreaterEqual(ladder.index(short(route)), ladder.index(floor))

    def test_a_high_band_below_the_first_choice_is_refused(self) -> None:
        for band in ("high", "critical"):
            with self.subTest(band=band):
                policy = deepcopy(self.policy)
                policy["risk_candidates"]["coding"]["bounded"][band] = [
                    {"provider": "claude", "model": "claude-haiku-5-5", "effort": "high"}
                ]
                self.assert_refused(policy, "first choice")
        # Lower bands may go cheaper: that is the point of the mechanism.
        policy = deepcopy(self.policy)
        policy["risk_candidates"]["coding"]["bounded"]["low"] = [
            {"provider": "claude", "model": "claude-haiku-5-5", "effort": "high"}
        ]
        self.write_and_load(policy)

    def test_a_high_band_needs_a_ladder_to_prove_it_is_not_below(self) -> None:
        policy = deepcopy(self.policy)
        del policy["escalation"]
        self.assert_refused(policy, "ladder")

    def test_malformed_risk_entries_are_refused(self) -> None:
        cases = {
            "unknown band": lambda p: p["risk_candidates"]["coding"]["bounded"].update(
                urgent=[{"provider": "codex", "model": "gpt-6.1-sol", "effort": "medium"}]),
            "unknown family": lambda p: p["risk_candidates"].update(nothing={}),
            "unknown route key": lambda p: p["risk_candidates"]["coding"].update(
                other={"low": [{"provider": "codex", "model": "gpt-6.1-sol", "effort": "medium"}]}),
            "empty list": lambda p: p["risk_candidates"]["coding"]["bounded"].update(low=[]),
            "unknown model": lambda p: p["risk_candidates"]["coding"]["bounded"].update(
                low=[{"provider": "codex", "model": "gpt-9", "effort": "medium"}]),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                policy = deepcopy(self.policy)
                mutate(policy)
                self.assert_refused(policy, "")

    def assert_refused(self, policy: dict, text: str) -> None:
        with self.assertRaisesRegex(PolicyError, text):
            load_from_disk(policy)

    def write_and_load(self, policy: dict) -> dict:
        return load_from_disk(policy)


class EscalationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shipped = load_policy(POLICY_PATH)
        cls.policy = build_policy(cls.shipped)

    def route(self, failed: int, events: list[dict] | None = None, **overrides: object) -> dict:
        task = base_task(**overrides)
        if events is not None:
            task = {**task, "failed_attempts": failed, "failure_events": events}
        else:
            task = with_failures(task, failed)
        return route_task(task, self.policy, primary_provider="claude")

    def judgment(self, failed: int, **overrides: object) -> dict:
        return self.route(
            failed, operation="exploration", ambiguity="partial",
            acceptance_strength="independent_judgment", deterministic_validation=False,
            **overrides,
        )

    def test_attempt_one_does_not_escalate(self) -> None:
        route = self.route(0)
        self.assertEqual(("codex", "gpt-6.1-sol", "medium"), short(route))
        self.assertNotIn("routing_basis", route)

    def test_attempt_two_after_a_failed_executable_check_moves_one_up(self) -> None:
        route = self.route(1)
        self.assertEqual(("claude", "claude-sonnet-5-5", "medium"), short(route))
        basis = route["routing_basis"]
        self.assertEqual(
            {"attempt": 2, "step": "next",
             "from": {"provider": "codex", "model": "gpt-6.1-sol", "effort": "medium"},
             "to": {"provider": "claude", "model": "claude-sonnet-5-5", "effort": "medium"}},
            basis["escalation"],
        )
        self.assertEqual("claude_agents_implementation_worker", route["role"])
        self.assertEqual(route["model"], route["selection"]["selected"]["model"])

    def test_escalation_starts_from_the_risk_banded_pick(self) -> None:
        route = self.route(1, risk="high")
        # Band pick opus medium; the next ladder rung up is sol high.
        self.assertEqual(("codex", "gpt-6.1-sol", "high"), short(route))
        self.assertEqual("high", route["routing_basis"]["risk_band"])
        self.assertEqual(2, route["routing_basis"]["escalation"]["attempt"])

    def test_attempt_three_goes_to_the_strongest_candidate_and_is_reported(self) -> None:
        route = self.route(2)
        self.assertEqual(("claude", "claude-opus-5-5", "high"), short(route))
        self.assertEqual("strongest", route["routing_basis"]["escalation"]["step"])
        self.assertEqual(3, route["routing_basis"]["escalation"]["attempt"])

    def test_the_strongest_is_the_strongest_one_the_project_admits(self) -> None:
        policy = deepcopy(self.policy)
        config = {
            "version": 1,
            "providers": {
                "codex": {"enabled": True},
                "claude": {"enabled": True, "models": ["claude-sonnet-5-5"]},
            },
            "codex_primary_may_bridge_to_claude": True,
        }
        policy["provider_routing"] = config
        policy["provider_config_digest"] = content_digest(config)
        route = route_task(with_failures(base_task(), 2), policy, primary_provider="claude")
        self.assertEqual(("codex", "gpt-6.1-sol", "high"), short(route))

    def test_judgment_work_does_not_cascade_at_attempt_two(self) -> None:
        first = self.judgment(0)
        retry = self.judgment(1)
        self.assertEqual(short(first), short(retry))
        self.assertEqual("held_judgment", retry["routing_basis"]["escalation"]["step"])

    def test_judgment_work_reaches_the_strongest_at_attempt_three(self) -> None:
        route = self.judgment(2)
        self.assertEqual(("claude", "claude-opus-5-5", "high"), short(route))
        self.assertEqual("strongest", route["routing_basis"]["escalation"]["step"])

    def test_only_listed_failure_reasons_escalate(self) -> None:
        for events in ([TRANSIENT_1], [{**TRANSIENT_1, "reason": "result_contract"}]):
            with self.subTest(events=events):
                route = self.route(1, events)
                self.assertEqual(("codex", "gpt-6.1-sol", "medium"), short(route))
                self.assertNotIn("routing_basis", route)
        # Attempt 3 with nothing but transient failures stays put as well.
        route = self.route(2, [TRANSIENT_1, TRANSIENT_2])
        self.assertEqual(("codex", "gpt-6.1-sol", "medium"), short(route))
        # One qualifying failure among others is enough.
        route = self.route(2, [TRANSIENT_1, REASONING])
        self.assertEqual("strongest", route["routing_basis"]["escalation"]["step"])

    def test_a_family_without_a_ladder_never_escalates(self) -> None:
        for failed in (1, 2):
            with self.subTest(failed=failed):
                route = route_task(
                    with_failures(
                        base_task(family="research", operation="exploration", ambiguity="partial",
                                  acceptance_strength="independent_judgment",
                                  deterministic_validation=False),
                        failed),
                    self.policy, primary_provider="claude",
                )
                self.assertNotIn("routing_basis", route)

    def test_the_top_of_the_ladder_is_reported_as_unchanged(self) -> None:
        policy = deepcopy(self.policy)
        policy["risk_candidates"]["coding"]["bounded"]["critical"] = [
            {"provider": "claude", "model": "claude-opus-5-5", "effort": "high"}
        ]
        route = route_task(
            with_failures(base_task(risk="critical"), 1), policy, primary_provider="claude"
        )
        self.assertEqual(("claude", "claude-opus-5-5", "high"), short(route))
        self.assertEqual("none_higher", route["routing_basis"]["escalation"]["step"])

    def test_every_candidate_of_a_laddered_family_must_be_on_the_ladder(self) -> None:
        policy = deepcopy(self.policy)
        policy["escalation"]["ladders"]["coding"].pop(2)  # sonnet medium
        with self.assertRaisesRegex(PolicyError, "ladder"):
            load_from_disk(policy)

    def test_malformed_escalation_is_refused(self) -> None:
        cases = {
            "unknown reason": lambda p: p["escalation"].update(failure_reasons=["vibes"]),
            "empty reasons": lambda p: p["escalation"].update(failure_reasons=[]),
            "unknown key": lambda p: p["escalation"].update(extra=1),
            "unknown family": lambda p: p["escalation"]["ladders"].update(nothing=[]),
            "duplicate rung": lambda p: p["escalation"]["ladders"]["coding"].append(
                deepcopy(p["escalation"]["ladders"]["coding"][0])),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                policy = deepcopy(self.policy)
                mutate(policy)
                with self.assertRaises(PolicyError):
                    load_from_disk(policy)


class VerifierRiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.shipped = load_policy(POLICY_PATH)
        cls.policy = build_policy(cls.shipped)

    def judgment(self, risk: str) -> dict:
        # Opus (Claude) does the work, so the verifier is Codex.
        return base_task(
            risk=risk, operation="exploration", ambiguity="partial",
            acceptance_strength="independent_judgment", deterministic_validation=False,
        )

    def test_a_claude_verifier_follows_the_workers_risk(self) -> None:
        # Codex does the bounded coding at medium and the verifier is Claude.
        expected = {
            "low": ("claude-haiku-5-5", "medium"),
            "medium": ("claude-opus-5-5", "high"),  # no entry: today's verifier
            "high": ("claude-opus-5-5", "high"),
            "critical": ("claude-opus-5-5", "max"),
        }
        for risk, model_effort in expected.items():
            with self.subTest(risk=risk):
                policy = deepcopy(self.policy)
                # Keep the worker on Codex at every risk so the verifier is Claude's.
                policy["risk_candidates"] = {}
                route = route_task(base_task(risk=risk), policy, primary_provider="claude")
                self.assertEqual("codex", route["provider"])
                verifier = route["verifier"]
                self.assertEqual("claude", verifier["provider"])
                self.assertEqual(model_effort, (verifier["model"], verifier["effort"]))

    def test_a_codex_verifier_follows_the_workers_risk(self) -> None:
        expected = {
            "low": ("gpt-6.1-sol", "low"),
            "medium": ("gpt-6.1-sol", "high"),  # no entry: today's verifier
            "high": ("gpt-6.1-sol", "high"),
            "critical": ("gpt-6-astra", "high"),
        }
        for risk, model_effort in expected.items():
            with self.subTest(risk=risk):
                route = route_task(self.judgment(risk), self.policy, primary_provider="claude")
                self.assertEqual("claude", route["provider"])
                verifier = route["verifier"]
                self.assertEqual("codex", verifier["provider"])
                self.assertEqual(model_effort, (verifier["model"], verifier["effort"]))

    def test_the_verifier_record_states_the_risk_it_followed(self) -> None:
        route = route_task(self.judgment("critical"), self.policy, primary_provider="claude")
        self.assertEqual("critical", route["verifier"]["risk_band"])
        medium = route_task(self.judgment("medium"), self.policy, primary_provider="claude")
        self.assertNotIn("risk_band", medium["verifier"])

    def test_the_verifier_is_always_the_other_provider(self) -> None:
        for risk in RISKS:
            for task in (base_task(risk=risk), self.judgment(risk)):
                with self.subTest(risk=risk, operation=task["operation"]):
                    route = route_task(task, self.policy, primary_provider="claude")
                    self.assertNotEqual(route["provider"], route["verifier"]["provider"])

    def test_a_verification_task_follows_its_own_risk(self) -> None:
        # A verification task for known work follows the same rule as the worker route's verifier.
        worker = route_task(base_task(risk="low"), self.policy, primary_provider="claude")
        verification = route_task(
            base_task(
                family="verification", risk="low",
                verification_for_family="coding",
                verification_for_provider=worker["provider"],
                verification_for_outcome="a" * 64,
            ),
            self.policy, primary_provider="claude",
        )
        self.assertEqual(
            (worker["verifier"]["provider"], worker["verifier"]["model"], worker["verifier"]["effort"]),
            short(verification),
        )

    def test_absent_per_risk_entries_keep_todays_verifier(self) -> None:
        plain = enable_providers(deepcopy(self.shipped))
        plain["providers"]["claude"]["verifier"].pop("by_risk", None)
        plain["verification"].pop("by_risk", None)
        for risk in RISKS:
            with self.subTest(risk=risk):
                route = route_task(base_task(risk=risk), plain, primary_provider="claude")
                self.assertEqual(
                    ("claude", "claude-opus-5-5", "high"),
                    short(route["verifier"]),
                )

    def test_malformed_per_risk_verifiers_are_refused(self) -> None:
        cases = {
            "claude unknown band": lambda p: p["providers"]["claude"]["verifier"]["by_risk"].update(
                urgent={"model": "claude-opus-5-5", "effort": "high"}),
            "claude wrong vendor model": lambda p: p["providers"]["claude"]["verifier"]["by_risk"].update(
                low={"model": "gpt-6.1-sol", "effort": "low"}),
            "claude bad effort": lambda p: p["providers"]["claude"]["verifier"]["by_risk"].update(
                low={"model": "claude-opus-5-5", "effort": "extreme"}),
            "claude extra field": lambda p: p["providers"]["claude"]["verifier"]["by_risk"].update(
                low={"model": "claude-opus-5-5", "effort": "high", "x": 1}),
            "codex unknown model": lambda p: p["verification"]["by_risk"].update(
                low={"model": "gpt-9", "effort": "low"}),
            "codex bad effort": lambda p: p["verification"]["by_risk"].update(
                low={"model": "gpt-6.1-sol", "effort": "extreme"}),
            "codex unknown band": lambda p: p["verification"]["by_risk"].update(
                urgent={"model": "gpt-6.1-sol", "effort": "low"}),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                policy = deepcopy(self.policy)
                mutate(policy)
                with self.assertRaises(PolicyError):
                    load_from_disk(policy)


if __name__ == "__main__":
    unittest.main()
