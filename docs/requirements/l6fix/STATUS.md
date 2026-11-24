# l6fix status
- L6.1: codex_worker._safe_report and dispatch_api response validation carry permission_denials {count, tools} (fixed names only). Test: tests.test_project_driver.ClaudeChildDriverTests.test_l6_1_*. Terminal state stays completed (T7 check expects terminal_state None); stored dispatch_result record unchanged (closed schema).
- L6.2: cli.py open_route runs committed_export_manifest before any admission. Test: ...test_l6_2_*.
- Digest 0.8.55 refreshed in place (not installed anywhere). Done and committed.
