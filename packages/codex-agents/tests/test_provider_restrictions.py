from __future__ import annotations

# ruff: noqa: I001 -- tests.support bootstraps the native package import path.

import importlib
import subprocess
import sys
import unittest
from copy import deepcopy

from tests.support import ORCHESTRATION_ROOT

from test_golden import legacy_merged_record
from codex_agents import provider_restrictions as restrictions
from codex_agents.contracts import ContractError
from codex_agents.providers import headroom_record
from codex_agents.runtime_files import RUNTIME_FILES


def window(scope="five_hour", minutes=300, used=None, reset=None):
    return {"scope": scope, "window_minutes": minutes,
            "used_percent": used, "resets_at": reset}


def record(status="rejected", windows=None, observed=1000):
    return headroom_record(
        provider="claude", account_fingerprint="a" * 64,
        windows=[] if windows is None else windows, status=status,
        run_id="run-1", phase="during", observed_at_ms=observed)


class ProviderRestrictionTests(unittest.TestCase):
    def test_rank_order_and_legacy_aliases(self):
        ledger = importlib.import_module("codex_agents.learning_store")
        for name in ("BILLING_RANK", "CLAUDE_STATUS_RANK", "_restriction_windows",
                     "_effective_window_reset", "_window_covered",
                     "_more_restrictive", "_merged_restriction"):
            self.assertIs(getattr(restrictions, name), getattr(ledger, name))
        self.assertIn("codex_agents/provider_restrictions.py", RUNTIME_FILES)
        ranked = ((None, 0), ("allowed", 0), ("allowed_warning", 1), ("rejected", 2))
        for new, new_rank in ranked:
            for old, old_rank in ranked:
                self.assertEqual(new_rank > old_rank, restrictions._more_restrictive(
                    record(new), [record(old)]))
        for new_rank, new in enumerate((False, None, True)):
            for old_rank, old in enumerate((False, None, True)):
                new_record = {"record_type": "provider_billing", "paid_usage_usable": new}
                old_record = {**new_record, "paid_usage_usable": old}
                self.assertEqual(new_rank > old_rank,
                                 restrictions._more_restrictive(new_record, [old_record]))
                self.assertTrue(restrictions._more_restrictive(new_record, []))

    def test_window_coverage_requires_scope_duration_use_and_reset(self):
        current = window(used=40, reset=2000)
        cases = (
            (window(used=40, reset=2000), True),
            (window(used=39, reset=3000), False),
            (window(used=100, reset=1999), False),
            (window(scope="seven_day", used=100, reset=3000), False),
            (window(minutes=301, used=100, reset=3000), False),
            (window(used=None, reset=3000), False),
        )
        for earlier, expected in cases:
            self.assertEqual(expected, restrictions._window_covered(
                current, earlier, observed_at_ms=1000, earlier_observed_at_ms=1000))
        self.assertTrue(restrictions._more_restrictive(
            record(windows=[current]), [record("allowed", [window(used=100, reset=3000)])]))

    def test_merge_preserves_resetless_horizon_across_rollback_and_validates(self):
        horizon = restrictions.FIVE_HOURS_MS
        self.assertEqual(18_000_000, horizon)
        for status in ("rejected", "allowed_warning"):
            for aggregate in (True, False):
                original = [record(status, [] if aggregate else [window()], 2000),
                            record("allowed", [window(used=40, reset=2500)], 1000)]
                before = deepcopy(original)
                result = restrictions._merged_restriction(original, observed_at_ms=500)
                expected = [window(used=40, reset=2500 if aggregate else 2000 + horizon + 1)]
                if aggregate:
                    expected.append(window(scope=None, minutes=None, reset=2000 + horizon + 1))
                expected.sort(key=lambda item: repr((item["scope"], item["window_minutes"])))
                self.assertEqual(record(status, expected, 500), legacy_merged_record(result))
                self.assertEqual(before, original)
                self.assertEqual(result, restrictions._merged_restriction([result], observed_at_ms=500))
        with self.assertRaises(ValueError):
            restrictions._merged_restriction([], observed_at_ms=1000)
        invalid = record()
        invalid["windows"] = [window(used=101, reset=2000)]
        with self.assertRaises(ContractError):
            restrictions._merged_restriction([invalid], observed_at_ms=1000)

    def test_fresh_worker_import_and_merge_do_not_load_persistence_or_provider_io(self):
        source = (
            "import importlib.abc, pathlib, sys\n"
            f"sys.path.insert(0, {str(ORCHESTRATION_ROOT)!r})\n"
            "class NoOwners(importlib.abc.MetaPathFinder):\n"
            " def find_spec(self, fullname, path=None, target=None):\n"
            "  if fullname == 'sqlite3' or fullname.startswith(('codex_agents.account_', 'codex_agents.learning_store', 'codex_agents.orchestration_store', 'codex_agents.collector_', 'codex_agents.providers')):\n"
            "   raise AssertionError(fullname)\n"
            "sys.meta_path.insert(0, NoOwners())\n"
            "from unittest.mock import patch\n"
            "with patch('pathlib.Path.read_text', side_effect=AssertionError('file I/O')), patch('builtins.open', side_effect=AssertionError('file I/O')):\n"
            " from codex_agents.provider_restrictions import merged_restriction\n"
            " item = dict(provider='claude', account_fingerprint='a' * 64, windows=[], status='rejected', run_id='run-1', phase='during', observed_at_ms=2000)\n"
            " result = merged_restriction([item], observed_at_ms=1000)\n"
            " assert result['windows'][0]['resets_at'] == 2000 + 18000000 + 1\n"
        )
        completed = subprocess.run([sys.executable, "-I", "-c", source], capture_output=True, text=True, check=False)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
