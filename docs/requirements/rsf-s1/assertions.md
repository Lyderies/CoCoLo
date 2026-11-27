# Existing assertion conflict: work stopped

No existing assertion or existing test file was edited. After the new tests went
green, source inspection found the following exact-frame assertion necessarily
conflicts with RSF-S1-1. Per the task's explicit stop rule, implementation and
test execution stopped at this finding. The socket modules have not been run.

## Required assertion change (not applied)

File: `packages/codex-agents/tests/test_service_transport.py:242`
Test: `ServiceTransportTests.test_collector_adapter_keeps_negative_runtime_and_binding_paths`.

Old:

```python
self.assertEqual([
    {"protocol": learning_store.COLLECTOR_PROTOCOL_VERSION, "op": "bind",
     "project_root": str(root.resolve()), "session_id": "session-fixture",
     "producer": "hook", "client_version": "codex-hooks-v1"},
    {"protocol": learning_store.COLLECTOR_PROTOCOL_VERSION, "op": "ingest",
     "binding_token": "bnd_fixture", "record": record},
], [json.loads(call.args[1]) for call in exchange.call_args_list])
```

New:

```python
self.assertEqual([
    {"protocol": learning_store.COLLECTOR_PROTOCOL_VERSION, "op": "bind",
     "project_root": str(root.resolve()), "session_id": "session-fixture",
     "producer": "hook", "client_version": "codex-hooks-v1",
     "runtime_digest": learning_store.COLLECTOR_RUNTIME_DIGEST},
    {"protocol": learning_store.COLLECTOR_PROTOCOL_VERSION, "op": "ingest",
     "binding_token": "bnd_fixture", "record": record,
     "runtime_digest": learning_store.COLLECTOR_RUNTIME_DIGEST},
], [json.loads(call.args[1]) for call in exchange.call_args_list])
```

Reason: both requests mutate collector state, so their exact expected frames must
include the caller runtime identity. Omitting that field would violate RSF-S1-1.

## Additional fixture conflict (not applied)

`packages/codex-agents/tests/test_legacy_capacity_upgrade.py:120` builds an
old-format `shutdown_if_drained` frame, then `:130` expects success and `:131`
expects the stop event. That request now correctly refuses. To preserve this
test's existing assertions and its earlier-run drain purpose, its input frame
would need `"runtime_digest": learning_store.COLLECTOR_RUNTIME_DIGEST` added.
No assertion change is proposed for this fixture. It has not been edited or run.

Approved: primary decision 145 (follows RSF-S1-1 / owner 129.3)
