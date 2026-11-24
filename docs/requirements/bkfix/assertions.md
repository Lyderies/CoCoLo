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
