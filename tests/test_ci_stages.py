"""CI runs the test stages where the plan says: fast checks on pull requests, everything on main.

Pull requests: unit tests on Ubuntu and macOS, integration tests on Ubuntu.
main, tags and the nightly run: every stage on both systems. Each run uses every core.
"""

import re
import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def jobs(text):
    """Job name -> its text, for the top-level jobs of the workflow."""
    body = text.split("\njobs:\n", 1)[1]
    found = {}
    for match in re.finditer(r"^  ([a-z0-9-]+):\n((?:    .*\n|\n)*)", body, re.M):
        found[match.group(1)] = match.group(2)
    return found


class CiStageTests(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text()
        self.jobs = jobs(self.text)

    def test_nightly_run_is_scheduled(self):
        self.assertRegex(self.text, r"\n  schedule:\n    - cron: ")

    def test_pull_requests_run_unit_on_both_systems_and_integration_on_ubuntu(self):
        unit = self.jobs["unit"]
        self.assertIn("--stage unit", unit)
        self.assertIn("ubuntu-latest", unit)
        self.assertIn("macos-latest", unit)
        self.assertNotIn("if:", unit.split("steps:")[0])
        integration = self.jobs["integration"]
        self.assertIn("--stage integration", integration)
        self.assertIn("ubuntu-latest", integration)
        self.assertNotIn("macos-latest", integration)
        self.assertNotIn("if:", integration.split("steps:")[0])

    def test_main_tags_and_nightly_run_every_other_stage_on_both_systems(self):
        full = self.jobs["full"]
        self.assertIn("github.event_name != 'pull_request'", full)
        self.assertIn("ubuntu-latest", full)
        self.assertIn("macos-latest", full)
        self.assertIn("--stage e2e", full)
        self.assertRegex(full, r"--stage integration|matrix\.os == 'macos-latest'")

    def test_every_test_job_uses_every_core(self):
        for name in ("unit", "integration", "full"):
            with self.subTest(job=name):
                for line in self.jobs[name].splitlines():
                    if "parallel_tests.py" in line:
                        self.assertIn("--jobs", line)


    def test_integration_runs_in_three_shards_at_one_job_per_core(self):
        """PLAN T4b: three shards, each with --jobs equal to its cores."""
        integration = self.jobs["integration"]
        self.assertRegex(integration, r"shard: \[1, 2, 3\]")
        self.assertIn('--shard "${{ matrix.shard }}/3"', integration)
        self.assertIn('--jobs "$(getconf _NPROCESSORS_ONLN)"', integration)


    def test_a_failing_integration_shard_keeps_its_failed_modules_work(self):
        """PLAN T4c: the failed modules run once more with --keep, uploaded for 7 days."""
        integration = self.jobs["integration"]
        self.assertIn("if: failure()", integration)
        self.assertIn("--keep", integration)
        self.assertIn("actions/upload-artifact@v7.0.2", integration)
        self.assertIn("retention-days: 7", integration)


    def test_a_piped_test_run_still_fails_its_job(self):
        """A run piped into tee must keep the runner's exit status: GitHub's default shell has no pipefail,
        and the job passed while test_project_driver timed out (PLAN T4c, 2026-11-19)."""
        for name, job in self.jobs.items():
            for step in re.split(r"\n      - ", job):
                if "parallel_tests.py" in step and "| tee" in step:
                    with self.subTest(job=name):
                        self.assertRegex(step, r"\n        shell: bash\n")


if __name__ == "__main__":
    unittest.main()
