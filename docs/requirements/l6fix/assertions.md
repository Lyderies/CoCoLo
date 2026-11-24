# Existing assertions changed (L6.1: a client-denied tool now stops the run)
1. tests/test_bridge.py result_frame() fixture: `"permission_denials": [{"tool_name": "Read", ...}]` -> `[]` (the shared default frame carried a denial, which now stops the run; a clean child has none).
2. tests/test_bridge.py test_happy_path_returns_the_result_and_usage: `{"count": 1, "tools": ["Read"]}` -> `{"count": 0, "tools": []}`; and `assertNotIn("/x", ...)` removed (no longer meaningful; the denial-input privacy is asserted in the new test_l6_1 test with a canary).
