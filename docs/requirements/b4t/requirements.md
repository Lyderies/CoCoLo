# B4t accepted requirements

The source is accepted design Draft 2 (decision 326), its fix round and final fix, and plan S5/SW-3. The fix round additionally implements accepted Amendment A1 (decision 337), including A1.15; its proof supersedes the predecessor process census.

| ID | Observable criterion |
|---|---|
| RS-B-B4t-0 | Update with a child running proceeds; lost settlement, retained committed custody and phase recovery; all other preflight refusals write nothing. |
| RS-B-B4t-1 | No idle wait or manual stop for a verified predecessor. |
| RS-B-B4t-2 | Committed results and open/paused orchestrations stay recordable under current authority. |
| RS-B-B4t-3 | Proven predecessor exit before adoption; lifetime lock and epoch fence; schema and version floor prevent reopen. |
| RS-B-B4t-4 | Updated project's running children end lost; other work follows D2.5 and other projects survive. |
| RS-B-B4t-5 | One recording or settlement effect per binding across versions and retries. |
| RS-B-B4t-6 | Primary recheck remains idempotent across takeover. |
| RS-B-B4t-7 | Existing B4m commands and documented installed-old-client refusals remain accurate. |
| RS-B-B4t-8 | Status names each takeover outcome using D2.8 wording without changing existing reply shapes. |
| RS-B-B4t-9 | Unknown lineage or corrupt custody is refused and held, never guessed or freed. |
| RS-B-B4t-10 | Every named phase resumes with one effect, including internal migration and atomic-file crash cuts. |
