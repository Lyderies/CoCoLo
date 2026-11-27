# S14f STATUS
- dbe36103: S14F-1/-2/-3, S9-WAIT built; Codex safety review r1 NOT-READY (HIGH clock rollback, MED total pass work, LOW rowid invalidation).
- Fix round (this commit): S14F-4 (cycle-start cutoff + restart on rollback), S14F-5 (whole cycle incl. walk/deletes bounded per pass;
  linear walk also in the whole prune), S14F-6 (TEMP change watch + data_version + schema_version; VACUUM reset), S14F-7 property test.
  All new tests red on dbe36103 (separate worktree at that commit), green here. Measured 259/515 MiB (s14c/RESULTS.md "After S14f fix round").
- Next: primary re-review (r2) and the affected/full run before merge.
- r2c NOT-READY (HIGH external-commit race before the write lock; MED list(found) copy and synchronous reset): fixed as
  S14F-8 (recheck under the write lock, abandon + restart) and S14F-9 (walk pops the custody set; O(1) reset + budgeted drain);
  the property test gained other-connection commits and the race. New tests red on 2baa6af8, green here. Next: primary r3 check.
