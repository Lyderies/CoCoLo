# l6fix status
- L6.1: any client permission denial ends the run failed / claude_tool_not_allowed (bridge.run_claude). Denials are accumulated per result frame (sticky: a later clean frame or a concurrent stop never erases or hides them). The reply and stored result carry it via the existing outcome path plus report permission_denials {count, tools}; tool names are limited to learning_contracts.CLIENT_TOOL_NAMES, else "other". The client's result frame does not tell a refused read/write/network call from a harmless prompt, so every denial fails closed. Stored dispatch_result record schema unchanged.
- L6.2: learning-open-host-run runs the committed-export check right after validate_assignment, before the project host starts, any backend call, admission or hold (cli.py). Tests spy on host start and backend check.
- runtime-releases.json untouched (0.8.55 installed; digest bookkeeping happens in the batch release).
- Assertion changes: see assertions.md.
- Done: committed.
