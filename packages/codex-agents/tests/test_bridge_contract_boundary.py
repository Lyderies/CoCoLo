from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest

from tests.support import ORCHESTRATION_ROOT


class BridgeContractBoundaryTests(unittest.TestCase):
    def test_client_observation_callbacks_work_without_loading_persistence(self) -> None:
        program = textwrap.dedent('''
            import importlib.abc
            import sys

            class RefusePersistence(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname in {"sqlite3", "codex_agents.learning_store"}:
                        raise AssertionError("bridge client loaded persistence: " + fullname)

            sys.meta_path.insert(0, RefusePersistence())
            from codex_agents import bridge
            from codex_agents.contracts import ContractError

            class Client:
                fail = False
                calls = []

                def record_provider_usage(self, project_id, **arguments):
                    self.calls.append(("usage", project_id, arguments))
                    if self.fail:
                        raise ContractError("controlled client failure")

                def record_observation_unavailable(self, project_id, **arguments):
                    self.calls.append(("unavailable", project_id, arguments))
                    if self.fail:
                        raise ContractError("controlled client failure")

            client = Client()
            for fail in (False, True):
                client.fail = fail
                report = {"usage": {"input_tokens": 3, "output_tokens": 2}}
                bridge._record_usage(client, "project", "run", "codex", "gpt-6.1-sol", report)
                assert report["usage_recorded"] is (not fail)
                report = {}
                bridge._record_usage(client, "project", "run", "codex", "gpt-6.1-sol", report)
                assert report["usage_recorded"] is False
                assert report.get("observation_record_failed", False) is fail
                if not fail:
                    assert report["observations_unavailable"] == [
                        {"phase": "after", "reason_code": "usage_unavailable"}
                    ]
            assert len(client.calls) == 4
            assert "sqlite3" not in sys.modules
            assert "codex_agents.learning_store" not in sys.modules
        ''')
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); exec(sys.argv[2])",
             str(ORCHESTRATION_ROOT), program],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)

    def test_owner_exception_reexport_preserves_existing_client_catches(self) -> None:
        from codex_agents import bridge
        from codex_agents.contracts import LedgerError
        from codex_agents.learning_store import LedgerError as OwnerLedgerError
        from codex_agents.learning_store import ProjectNotRegisteredError

        self.assertIs(LedgerError, OwnerLedgerError)
        self.assertTrue(issubclass(ProjectNotRegisteredError, LedgerError))

        class RefusingClient:
            def record_provider_usage(self, *_args, **_kwargs):
                raise OwnerLedgerError("controlled owner failure")

            def record_observation_unavailable(self, *_args, **_kwargs):
                raise ProjectNotRegisteredError("controlled registration failure")

        report = {"usage": {"input_tokens": 1}}
        bridge._record_usage(RefusingClient(), "project", "run", "codex", "gpt-6.1-sol", report)
        self.assertFalse(report["usage_recorded"])
        report = {}
        bridge._record_unavailable(
            RefusingClient(), "project", "run", "codex", "after", "usage_unavailable", report,
        )
        self.assertTrue(report["observation_record_failed"])
