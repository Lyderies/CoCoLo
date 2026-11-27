# Assertion changes

No assertions in the base tree changed or were removed. All evidence tests are additive.

Corrections during development of the new evidence tests:

| Draft assertion | Replacement | Requirement reason |
| --- | --- | --- |
| Deferred publication's stored row has state `opened` | Stored row has state `committed`; public reply remains `opened` | E1a-T3 requires proving the commit preceded the refused publication. The journal's state vocabulary differs from the API's. |
| Claim/recapture `assert_no_claim()` while holder remains locked | Same complete assertion after releasing the holder, before retry | The assertion's repository read also needs the real lock. Busy reply and lane checks still run while it is held; no other writer runs before the unchanged no-claim assertion. |
| Compare the count snapshot inside health thread with a raising assertion | Record any mismatched dictionary and assert the error list is empty on the owner | A negative control must finish barrier coordination and fail an assertion, instead of causing a secondary broken-barrier exception. Same exact dictionary at every wave. |
| Collector frame checks two per-wait caps | Check two waits per frame, `.75` then `.3`, on each of two frames | Strengthens E1a-T3: omitting the collector request wrapper would otherwise preserve the per-wait cap and escape detection. |
| Final count dictionary has `read_call: 3` | Final dictionary has `read_call: 9`; other labels remain exactly 3 | Each of the three waves now exercises all three real SQL scope entry APIs, each counted under their shared `read_call` label. |

## Fix round

No product-result assertion was removed or relaxed. The fixed-scope review adds
one exceptional-frame test and changes only test coordination bounds:

| Before | After | Reason |
| --- | --- | --- |
| T4 holder-acquired and health-read/count event assertions wait 5 s; barriers and joins use 5 s; holder safety release uses 30 s | T4 event assertions, barriers and joins use 20 s; holder safety release uses 120 s | MED-2: tolerate host scheduling load without changing event coordination or any product wait budget. Other tests retain the helper's 5 s/30 s defaults. |
| T4 `labels` includes `read_call`, then `expected()` overwrites it with `3 * wave` | `labels` contains only the eight one-per-wave labels; `expected()` still adds exactly `read_call: 3 * wave` | LOW-1: clarify that the idle probe adds no `read_call` count. Every expected dictionary is unchanged. |
| No assertion for collector dispatch raising after a real ledger read | `test_raising_collector_frame_restores_deadline_and_next_frame_budget` checks the identical raised exception, restored prior deadline, successful next frame, and two actual acquisitions each with a fresh 0.75 s cap | LOW-2 / E1a-T3: additive exceptional-exit coverage; the broken-wiring control restores only on success and fails the deadline-restoration assertion. |

MED-1 changes only the negative-control mutation; the existing exact busy-reason,
wire shape, guidance, lane and retry assertions remain unchanged.
