# S14f assertions changed (fix round after reviews/040-s14f-r1)

1. tests/test_s14f_owner_bounds.py `test_S14F_1_an_edge_set_change_or_a_fallen_rowid_never_releases_a_stale_answer`, second half
   - old: after deleting the highest `active_run_capacity` rowid mid-cycle, `assertIsNone(scan.step(db, NOW, 100_000))` (a fallen max rowid discards the cycle)
   - new: `assertEqual(whole_keys(db, NOW), answer)` (the cycle continues and equals the whole answer)
   - reason: r1 LOW and the brief replace the max-rowid comparison with a change watch. A delete only removes custody, so the
     continued answer is exact; the hazard the old assertion guarded (a row reappearing behind the cursor) is now asserted
     directly and more strongly by `test_S14F_6_a_delete_and_reinsert_behind_the_cursor_restoring_the_max_rowid_is_still_found`
     (which the old max-rowid check fails).
2. tests/lifetime_caps.json: entry `runtime_lineage.py::step` renamed to `runtime_lineage.py::_scan` (the counted lines moved
   there; same test) and `runtime_lineage.py::advance` added (S14F-5). Registry text, not a test assertion.
3. (r2c round) tests/test_s14f_owner_bounds.py helper `must_keep` (added in the r1 round, used by every pruning check): the
   reference was rewritten from a per-path walk (quadratic on 2,000-key chains, ~20 s) to an independent reachability search
   (frontier + visited set). Same set by definition (every edge key reachable from custody before the current runtime); no
   assertion changed. `checked_advance` gained an optional `race` hook; `interleave` now runs on a real file with a second
   connection (ops 10/11). tests/lifetime_caps.json: `runtime_lineage.py::_drain` added, `advance` text updated.
