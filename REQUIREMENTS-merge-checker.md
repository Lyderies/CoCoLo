# Merge checker implementation contract

Authority: TF-19; ANALYSIS-0.4.0-merge-library-states.md §§1,2,3,5; final ML-1..30.
The immutable inputs remain in the control worktree. This document adds no alternate outcomes.

- One check_end_state rereads history, then checks operation-start S and target T by durable direction.
- Q1=needs_owner(io) for unrecorded created folders; Q2=put-back/cleanup retry; Q3=durable terminal plus retirement pending; Q4=needs_owner(owner); Q5=all emptied ancestors; Q6=put-back on applying cancel.
- Landed forward targets T; reverted restores S; undone uses the original batch's base with its folder rules.
- Applying/record pending and needs_owner allow only complete S/T versions except explicitly named owner/moved/unknown paths. Pin only this operation and its dependencies.
- CU provenance is sealed folder events or undo-created parent candidates, observed at the injected boundary and absent from durable created identities. Never change Fixture.before to authorize it. CU must remain empty and named.
- Listed emptied folders may remain empty with matching identity or be removed after landed; unrelated empty folders cannot be removed. Reverted cleanup cannot delete restored paths.
- Terminal cleanup/retirement never changes the durable state or the tree direction. Ordinary swallowed EIO cannot yield landed; commit uncertainty is settled by durable reread.
- Read-history injection is allowed only for checker self-tests supplying independent immutable rows; production-path tests always reread the actual database.
- Full fault/kill enumeration remains available under explicit soak. Normal samples cover each observed persistence operation/phase/side; additional state-machine cases select persistence boundaries.
- The durable/volatile model consumes actual IOCall events. Missing flush must leave volatile state that disappears at model power loss; a production mutation must be caught, not merely a hand-written sequence.

Assertion migration ledger and exact replacement locations will accompany the implementation. No existing assertion is removed merely because it fails.
