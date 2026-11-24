# bkfix: changed assertions (decisions 36, 192)

Requirement: a backup command's reply names the backup by file id and a relative name (`backup_name`), never an
absolute path or the home folder. Restore and verify find the backup by file id (registered relative path), so
nothing the owner needs is lost.

| Test | Old | New | Why |
|---|---|---|---|
| tests/test_owner_backup_streamed.py F3-1 | `assertEqual(str(path), report["path"])` | `assertEqual(path.name, report["backup_name"])` | reply no longer carries the absolute path |
| tests/test_owner_backup_streamed.py F3-5 | report keys include `"path"` | report keys include `"backup_name"` instead | same |
| tests/test_installer.py (backup script) | `Path(report["path"]).name == "preupdate_test.sqlite3"`, opened `report['path']` | `report["backup_name"] == "preupdate_test.sqlite3"`, `"path"` absent, file located under the state root by that name | same |

New: tests/test_backup_reply_private.py BK-1..BK-3 (cli backup, cli verify, owner script).

## Round 2 (review: error replies, file_id, hard links)
Requirement: every error, cleanup and rollback reply names no absolute path, temp path, child stderr or raw
exception text; the CLI backup reply returns `file_id` with `backup_name`; streamed verification refuses a backup
file with st_nlink > 1.

| Test | Old | New | Why |
|---|---|---|---|
| test_owner_backup_streamed F3-14 (stuck cleanup, ~:422) | message contains the `.stuck.partial` absolute path | message contains neither the backups folder nor the state root, and says "1 partial file(s)" | decisions 36/192 |
| F3-12 left-stopped (temp file stuck) | message contains raw "temporary file stuck" | raw text absent; message says "a temporary copy remains" | fixed reason |
| F3-9 child error output | `assertRaisesRegex "boom in the child"` (child stderr) | regex `could not be written or checked \(exit 3\)` | child stderr no longer relayed |

New: BK-4 (cli file_id + verify round trip), BK-5 (hard link refused), BK-6 (fixed reasons, folder-check text).

## Round 3 (decision 199: allowlist, missing file, CLI exception text)
Requirement: only an allowlist of fixed reason strings passes through the owner script; a child's `error` text or an
unlisted RuntimeError becomes "the backup could not be completed (<Type>)"; verify of a missing registered file says
"project backup is unavailable"; the CLI's caught exception prints `reason_code` (e.g. ledger_refused), never its text.

| Test (test_owner_backup_streamed) | Old | New | Why |
|---|---|---|---|
| quiesce failed (F3-3e) | `quiesce_error == "authorization unreadable"` | `== "the backup could not be completed (RuntimeError)"` | mock text not allowlisted |
| F3-12 quiesce undone | `"restore failed" in quiesce_error` | `== "...completed (QuiesceLeftStopped)"` | unlisted text |
| F3-13 / F3-16 restart | `"collector would not start" in restart_error` | contains "the installed collector did not restart", raw text absent | unlisted text |
| F3-16 close | `"ledger would not close" in close_error` | `== "...completed (RuntimeError)"` | unlisted text |
| test_backup_reply_private BK-6 (mine, round 2) | `reason(RuntimeError("fixed text")) == "fixed text"` | generic reason | allowlist, not RuntimeError type |
New: BK-7..BK-10. Existing CLI tests that pinned printed exception text are listed below if any changed.
| test_owner_backup_streamed F3-16 (conclude) | `final["error"] == "preflight refused"` | `== "the backup could not be completed (RuntimeError)"` | unlisted text |
| test_project_driver while-a-child-runs (both classes) | `'a child run is active; nothing was changed' in refused['error']` | `refused['reason_code'] == 'ledger_refused'` and that text absent | CLI prints a fixed code, never exception text (decision 199) |
