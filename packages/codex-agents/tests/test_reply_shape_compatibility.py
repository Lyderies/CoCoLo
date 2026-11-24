"""Existing account/history reply fields are frozen; new data needs a new operation."""
import importlib
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest import mock

import tests.support  # noqa: F401
from tests.support import REPOSITORY_ROOT
from codex_agents import account_api, history_api

sys.path.insert(0, str(REPOSITORY_ROOT.parents[1] / 'scripts'))
from test_framework import requirements  # noqa: E402


@requirements('RS-B-B4m-2', why_not_scenario='Exhaustive field-set contract, with add/remove mutations and both real API validator entry points; no sockets needed.')
class ReplyShapeCompatibilityTests(unittest.TestCase):
    def catalogue(self):
        return json.loads((Path(__file__).parent / 'fixtures/reply-fields-8d4a7ce3.json').read_text())

    def test_every_existing_reply_matches_previous_release_catalogue(self):
        shapes = importlib.import_module('codex_agents.reply_shapes')
        previous = self.catalogue()
        operations = account_api.ACCOUNT_OPERATIONS | history_api.HISTORY_OPERATIONS
        self.assertLessEqual(previous['operations'].keys(), operations)
        self.assertEqual(operations, shapes.REPLY_FIELDS.keys())
        for op, expected in previous['operations'].items():
            with self.subTest(operation=op):
                self.assertEqual(expected, shapes.REPLY_FIELDS[op], 'New reply data requires a NEW operation.')
        self.assertEqual(set(previous['envelope']), account_api._RESPONSE_FIELDS)
        self.assertEqual(set(previous['envelope']), history_api._RESPONSE_FIELDS)

    def test_added_and_removed_fields_are_refused_for_every_operation(self):
        shapes = importlib.import_module('codex_agents.reply_shapes')
        for op, row in self.catalogue()['operations'].items():
            required, optional = set(row['required']), row['optional']
            # Preserve all already-supported optional variants, including the capacity report's run_slots.
            for size in range(len(optional) + 1):
                for variant in itertools.combinations(optional, size):
                    result = dict.fromkeys(required | set(variant))
                    with self.subTest(operation=op, variant=variant):
                        self.assertTrue(shapes.reply_fields_match(op, result))
                        self.assertFalse(shapes.reply_fields_match(op, {**result, 'NEW_FIELD': None}))
                        for name in required:
                            self.assertFalse(shapes.reply_fields_match(op, {k: v for k, v in result.items() if k != name}))

    def test_both_apis_apply_the_shape_guard_before_value_checks(self):
        for api, operations, checker, error in (
            (account_api, account_api.ACCOUNT_OPERATIONS, account_api._checked_result, account_api.AccountError),
            (history_api, history_api.HISTORY_OPERATIONS, history_api._validate_result, history_api.HistoryError),
        ):
            for op in operations:
                request = SimpleNamespace(operation=op)
                result = {'NEW_FIELD': None}
                with self.subTest(operation=op), mock.patch.object(api, 'reply_fields_match', return_value=False) as guard:
                    with self.assertRaises(error):
                        checker(result, request) if api is account_api else checker(request, result)
                    guard.assert_called_once_with(op, result)


if __name__ == '__main__':
    unittest.main()
