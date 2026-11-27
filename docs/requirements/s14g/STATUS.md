S14g complete, uncommitted as instructed. Worktree cocolo-s14g, branch feat/s14g-0873, base 6edd6a79.
Audit: ../s14g.md; 147 compiled real-schema SQL cases and full EQP in plans.json.
8 source statements optimized: adoption kind/state, 5 missing leading-project key reads/updates/correlations,
activity and reservation-validator join order. Existing account identity reads already include their applicable kinds.
S14G-1 red: red.txt (4 failures) and red-additional.txt (3 failures); green after the code.
S14G-2 and S14G-3 audit/semantic checks green. All IDs and the new module registered.
Affected named parallel run: 4 modules, 5 workers, 15.15 seconds, all passed, metadata_errors=[];
final audit module rerun after inventory pinning: 8 tests, all passed, metadata_errors=[]. See validation.json.
No existing assertion changed; RetentionA1Tests scaffold expanded to include its production project key.
No schema/record-shape change, no load measurement, no owner decision. No tests in flight. Do not commit in this session.
S14G-4 (S14i, feat/s14i-0874): takeover finished-custody scan and next_hold_due use the live partial index; see assertions.md. Commit S14G-4.
