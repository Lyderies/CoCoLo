"""O10 S1: the orchestration skills show run control with the arguments the command requires."""
import re
import unittest

from tests.support import BUNDLE_ROOT

SKILLS = (BUNDLE_ROOT / ".claude/skills/agent-orchestration/SKILL.md",
          BUNDLE_ROOT / ".agents/skills/orchestrate-agents/SKILL.md")


class SkillCommandTests(unittest.TestCase):
    def test_run_status_and_cancel_examples_name_the_project_root(self):
        for skill in SKILLS:
            text = " ".join(skill.read_text().split())
            examples = re.findall(r"`(?:cli\.py )?learning-run-status [^`]*`", text)
            with self.subTest(skill=skill.parent.name):
                self.assertTrue(examples, "no learning-run-status example")
                for example in examples:
                    self.assertIn("--project-root", example)
                self.assertRegex(text, r"learning-cancel-run` with the same arguments")


class SkillReviewCommandTests(unittest.TestCase):
    def test_H1d_5_both_skills_offer_the_review_command(self):
        for skill in SKILLS:
            with self.subTest(skill=skill.parent.name):
                self.assertIn("learning-review", " ".join(skill.read_text().split()))


if __name__ == "__main__":
    unittest.main()
