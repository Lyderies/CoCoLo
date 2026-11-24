# D2 deadlines and retention of result custody

**RS-D-D2 — Deadlines and retention of result custody.**
No deadline before the commit retires a lease-born reservation: a reserved row past its lease deadline stays charged and
publishable while its RS-B obligation is held, and is retired only after RS-B releases that obligation (SH-4, RS-4);
a row that lacks its hold is kept and reported, never retired by a deadline. Committed custody is released `retention`
exactly at `retained_until_ms`, judged at max(wall, persisted floor), so a moved-back clock revives nothing and blocks no
pass (RS-D-7). A closed orchestration releases committed custody `orchestration_closed`, but an unattended close does not
end custody (owner decision 90.8). A verifier opens only when its worker outlasts the verifier's lease by one hour (owner
decision 90.4); a worker expiring while the verifier runs leaves the verifier's custody live, and a record on an expired
attempt is refused. A retiring row refuses body, context and admission reads. Only the lease path (`prepare_lease`) is
reachable from product code: the non-lease `prepare` is refused at runtime on any lane built without the local-reference allowance (the service runtime's lane).
Source: DESIGN-0.4.0-RS-D.md draft 2 s1, s8, s9, draft 3 s8, rows D1-D4, C1-C3, V1-V4; DESIGN-0.4.0-RS-BD-shared.md SH-3, SH-4; docs/requirements/d2.md.
