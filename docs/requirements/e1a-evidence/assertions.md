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
