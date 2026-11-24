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
