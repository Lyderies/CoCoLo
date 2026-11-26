# Batch 0.8.74 re-cut c (branch merge/batch-0874c)

Base 80814875 (0.8.74 re-cut, never installed). VERSION stays 0.8.74; the 0.8.74 runtime digest is refreshed in place
(as b3fc4914 did). The 0.8.73 record, floor and VERSION are unchanged.

## Merged
- 80814875: 0.8.74 re-cut.
- fix/0874-ci2: CI round 2 (decision 411 accepted).
- Round 3a (05a60a6f): setup_update shared bytecode cache. Round 3b (2ea5a6b1): b2 template copies, staged chunk parse once,
  E2c-1 per decision 412 (serving within one production interval; bytecode isolation stays), observation TESTSPEED-3.
- Decision 413a: CI round 3 merged as batch20 (owner question 413 on verified bytecode remains open).

## Digest refresh
`runtime-releases.json` 0.8.74: 3c14149b... -> 6429442b4160b05e19bf848777188aecb36c15ed6e17e630da219899fbd86d05
(round 3 runtime edits changed it; `test_runtime_release` proves it). `scripts/generate_test_metadata.py --catalogue` run twice:
byte-identical, no change to scripts/test-requirements.json.

## Full run (`--base 80814875`, real sockets; docs/batch-0874c-full-run.txt, redacted)
574 units, 603.5 s, 16 jobs, metadata_errors []. Two failures:
- `test_learning_source_image.test_rows_and_bytes_use_keyset_pages_...`: test-side. 0f1cc213 made `_page` return
  `(rows, encoded)`; the observer wrapper in the test assumed a list. Fixed the wrapper only (unpacks result[0], returns the
  original result); every assertion unchanged (TF requirement: keyset pages <=32 rows, <=PAGE_BYTES). Module green (33 tests).
- `test_synchronized_races#1 test_the_same_result_committed_twice_...`: not reproduced; the module passed 3 of 3 reruns alone
  (22 s each) and failed only under full-run load ("store owner was replaced or lost its writer lock"). Flake under load,
  unchanged, flagged for the primary.
Budget debt: 18 modules over the 60 s layer budget listed in the run (existing debt, 3 retired); no new debt.
