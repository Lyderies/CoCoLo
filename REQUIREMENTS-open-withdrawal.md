# B3 account open withdrawal

**RS-B-5 — Authenticated withdrawal of an unclaimed open.**
The project service and the opening host class may withdraw an unclaimed open with matching enrolled project,
caller scope, run id, inputs digest, attempt custody and current binding generation. Authentication precedes
immutable receipt replay; an exact replay remains available after the binding is pruned. Different callers receive
no receipt. New transitions refuse claimed, fenced, withdrawn or stale bindings; missing generations are refused.
Withdrawal evidence is retained for the tombstone's life. The project records its withdrawal duty before registration,
marks the complete request releasing before dropping memory, retries identical requests after lost replies, and
withdraws never-claimed cancellations, pre-preparation failures, idle registrations (one hour, owner decision 90.7)
and stopped-service registrations. A registration refusal preserves the duty; a faulted writer gets one memory
withdrawal, otherwise account expiry. Armed leases belong to the fence path.
Source: DESIGN-0.4.0-RS-B.md draft 3 §4, §7 and rows W1-W5 (W4a-e), M7; PLAN-0.4.0-supervisor-slices.md B3.
