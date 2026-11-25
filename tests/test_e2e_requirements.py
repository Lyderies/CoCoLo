"""T7: the lifecycle end-to-end requirements and their checks stay in step.

Every requirement in tests/e2e/requirements.json names its flow and its source and may link to checks in scripts/e2e_lifecycle.py; every check there names only known requirements, and
every check a requirement lists exists. The script and its scripts/e2e_checks_<group>.py modules declare
their checks with `@check(...)`, read here without running anything.
"""
import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = ROOT / "tests" / "e2e" / "requirements.json"
SCRIPT = ROOT / "scripts" / "e2e_lifecycle.py"
CHECK_SOURCES = [SCRIPT, *sorted((ROOT / "scripts").glob("e2e_checks_*.py"))]


def declared_checks():
    """{check name: requirement IDs} from the @check("ID", ...) decorators of the script and its checks modules."""
    found = {}
    for source in CHECK_SOURCES:
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.FunctionDef):
                for decorator in node.decorator_list:
                    if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
                            and decorator.func.id == "check"):
                        found[node.name] = [arg.value for arg in decorator.args if isinstance(arg, ast.Constant)]
    return found


class E2ERequirementsTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(REQUIREMENTS.read_text())
        self.requirements = {item["id"]: item for item in self.doc["requirements"]}

    def test_every_requirement_is_complete_and_unique(self):
        self.assertEqual(len(self.doc["requirements"]), len(self.requirements))
        for item in self.doc["requirements"]:
            with self.subTest(item["id"]):
                base = {"id", "flow", "source", "statement", "checks"}
                if "deferred" in item:  # unchecked ON PURPOSE, with its reason (never silently): no check, a reason
                    self.assertEqual(base | {"deferred", "reason"}, set(item))
                    self.assertIs(True, item["deferred"])
                    self.assertEqual([], item["checks"], "a deferred requirement has no check (it would be green on a stand-in)")
                    self.assertTrue(isinstance(item["reason"], str) and len(item["reason"]) > 20)
                else:
                    self.assertEqual(base, set(item))
                self.assertIn(item["flow"], {*self.doc["flows"], "all"})
                self.assertTrue(item["source"] and item["statement"])

    def test_every_flow_has_requirements(self):
        flows = {item["flow"] for item in self.doc["requirements"]}
        self.assertEqual(set(self.doc["flows"]), flows - {"all"})

    def test_every_listed_requirement_check_exists_and_links_back(self):
        self.assertTrue(SCRIPT.is_file(), "scripts/e2e_lifecycle.py is missing")
        checks = declared_checks()
        for item in self.doc["requirements"]:
            with self.subTest(item["id"]):
                self.assertIsInstance(item["checks"], list)
                self.assertEqual(len(item["checks"]), len(set(item["checks"])))
                for name in item["checks"]:
                    self.assertIn(name, checks, "the listed check does not exist")
                    self.assertIn(item["id"], checks[name], "the check does not name this requirement")

    def test_every_check_names_only_known_requirements(self):
        self.assertTrue(SCRIPT.is_file(), "scripts/e2e_lifecycle.py is missing")
        for name, ids in declared_checks().items():
            with self.subTest(name):
                self.assertTrue(ids, "a check must name a requirement")
                self.assertTrue(set(ids) <= set(self.requirements), set(ids) - set(self.requirements))
                for requirement in ids:
                    self.assertIn(name, self.requirements[requirement]["checks"],
                                  "the requirement does not list this check")


if __name__ == "__main__":
    unittest.main()
