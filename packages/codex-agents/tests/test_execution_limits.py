from __future__ import annotations

# These pure contract checks must import without probing writable temp storage.
# ruff: noqa: I001 -- bootstrap only the native source import path.
import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "native-package/.codex/orchestration"))
from codex_agents.contracts import (
    ContractError, validate_assignment, validate_assignment_execution_limits,
)


class ExecutionLimitTests(unittest.TestCase):
    def task(self):
        return {
            "id": "task-1", "objective": "Inspect fixture", "family": "research",
            "complexity": "medium", "risk": "low", "operation": "exploration",
            "ambiguity": "known", "context_breadth": "repository",
            "acceptance_strength": "independent_judgment", "routing_evidence_refs": [],
            "meaningful": True, "deterministic_validation": False, "independent_parts": 1,
            "parallel_safe": False, "failed_attempts": 0, "failure_events": [],
        }

    def parent_grant(self):
        return {"tools": ["shell"], "filesystem": [], "network": False,
                "mcps": [], "external_effects": False}

    def assignment(self):
        return {
            "assignment_id": "limits-1", "task_id": "task-1", "objective": "Inspect fixture",
            "role": "codex_agents_researcher", "model": "gpt-6.1-sol", "effort": "high",
            "attempt": 1, "meaningful": True, "owned_paths": [], "context": [],
            "permissions": self.parent_grant(), "expected_output": "Evidence",
            "required_evidence": [{"id": "limits", "description": "Bound units"}],
            "verification_required": True, "bounds": {
                "max_attempts": 2, "max_steers_per_attempt": 1, "max_no_progress_events": 2,
                "wait_seconds": 60, "timeout_seconds": 600, "max_tool_calls": 30,
            }, "stopping_conditions": [], "prohibited_actions": [],
        }

    def route(self, **changes):
        # A contract fixture, not an admission or policy-selection claim.
        assigned = self.assignment()
        route = {key: deepcopy(assigned[key]) for key in (
            "task_id", "role", "model", "effort", "verification_required", "bounds",
        )}
        return {**route, "provider": "claude", "dispatch": "bridge", **changes}

    def validate_assignment(self, assigned):
        return validate_assignment(assigned, task=self.task(), route=self.route(),
                                   parent_grant=self.parent_grant())

    def test_explicit_execution_limits_have_closed_integer_units(self) -> None:
        for limits in (None, {}, [], "turns", {"unknown": 1}, {"max_model_turns": True},
                       {"max_tool_calls": False}, {"max_model_turns": 0},
                       {"max_tool_calls": -1}, {"max_model_turns": 2**31},
                       {"max_tool_calls": 2**31},
                       {"max_model_turns": 1.0}):
            assigned = self.assignment()
            assigned["execution_limits"] = limits
            with self.subTest(limits=limits), self.assertRaises(ContractError) as caught:
                self.validate_assignment(assigned)
            self.assertTrue(caught.exception.field.startswith("assignment.execution_limits"))

    def test_exact_tool_requests_refuse_including_zero_on_both_bridges(self) -> None:
        task = self.task()
        for provider in ("codex", "claude"):
            route = self.route(provider=provider)
            for requested in ({"max_tool_calls": 0}, {"max_tool_calls": 1},
                              {"max_tool_calls": 30},
                              {"max_tool_calls": 0, "max_model_turns": 1}):
                assigned = self.assignment()
                assigned["execution_limits"] = requested
                with self.subTest(provider=provider, requested=requested), \
                        self.assertRaisesRegex(ContractError, "exact tool-call limits") as caught:
                    validate_assignment(assigned, task=task, route=route,
                                        parent_grant=self.parent_grant())
                self.assertEqual("assignment.execution_limits.max_tool_calls", caught.exception.field)

    def test_explicit_turns_only_narrow_the_claude_bridge_legacy_ceiling(self) -> None:
        task = self.task()
        route = self.route()
        assigned = self.assignment()
        assigned["bounds"]["max_tool_calls"] = 3
        for turns in (1, 3):
            assigned["execution_limits"] = {"max_model_turns": turns}
            self.assertIs(assigned, validate_assignment(
                assigned, task=task, route=route, parent_grant=self.parent_grant(),
            ))
        assigned["execution_limits"] = {"max_model_turns": 4}
        with self.assertRaisesRegex(ContractError, "exceeds the legacy dispatch ceiling"):
            validate_assignment(assigned, task=task, route=route, parent_grant=self.parent_grant())
        assigned["execution_limits"] = {"max_model_turns": 1}
        for changed in ({**route, "provider": "codex"}, {**route, "dispatch": "native"}):
            with self.subTest(route=changed), \
                    self.assertRaisesRegex(ContractError, "model-turn limits are unavailable"):
                validate_assignment(assigned, task=task, route=changed,
                                    parent_grant=self.parent_grant())

    def test_explicit_turn_request_requires_typed_legacy_ceilings(self):
        for label in ("route", "assignment"):
            for bounds in (None, {}, {"max_tool_calls": False}, {"max_tool_calls": "3"},
                           {"max_tool_calls": -1}):
                route = self.route()
                assigned = self.assignment()
                assigned["execution_limits"] = {"max_model_turns": 1}
                (route if label == "route" else assigned)["bounds"] = bounds
                with self.subTest(label=label, bounds=bounds), self.assertRaises(ContractError) as caught:
                    validate_assignment_execution_limits(assigned, route)
                self.assertEqual(f"{label}.bounds.max_tool_calls", caught.exception.field)

    def test_ceiling_uses_route_and_retains_the_legacy_zero_projection(self):
        route = self.route()
        assigned = self.assignment()
        route["bounds"]["max_tool_calls"] = 2
        assigned["execution_limits"] = {"max_model_turns": 3}
        with self.assertRaisesRegex(ContractError, "exceeds the legacy dispatch ceiling"):
            validate_assignment_execution_limits(assigned, route)
        assigned["execution_limits"] = {"max_model_turns": 2}
        self.assertEqual({"max_model_turns": 2}, validate_assignment_execution_limits(assigned, route))
        assigned["bounds"]["max_tool_calls"] = 0
        assigned["execution_limits"] = {"max_model_turns": 1}
        self.assertIs(assigned, validate_assignment(
            assigned, task=self.task(), route=route, parent_grant=self.parent_grant(),
        ))

    def test_full_validation_refuses_malformed_legacy_bounds(self):
        fields = ("max_attempts", "max_steers_per_attempt", "max_no_progress_events",
                  "wait_seconds", "timeout_seconds", "max_tool_calls")
        for label in ("route", "assignment"):
            for field in fields:
                for value in (None, "3", True, -1):
                    route = self.route()
                    assigned = self.assignment()
                    assigned["execution_limits"] = {"max_model_turns": 1}
                    (route if label == "route" else assigned)["bounds"][field] = value
                    with self.subTest(label=label, field=field, value=value):
                        with self.assertRaises(ContractError) as caught:
                            validate_assignment(assigned, task=self.task(), route=route,
                                                parent_grant=self.parent_grant())
                        self.assertEqual(f"{label}.bounds.{field}", caught.exception.field)


if __name__ == "__main__":
    unittest.main()
