# Review LOWs (CoCoLo review rev040testtimer1), listed, not fixed

- test_install.py:249,254: shared mutable `built`.
- test_install.py:292,347; test_install_claude_upgrade.py:421: target assumed at base/"target".
- test_install.py:250: rmtree ignore_errors on restore.
- test_install.py:246: snapshot cleaned only at exit.
- learning_support.py:38: launcher folder taken from $0.
- test_result_lifetime.py:99-121: 0.5 s expiry waits and swallowed sqlite3.Error.
- test_install_claude_upgrade.py:373-388: possibly unused imports.
