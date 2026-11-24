# B2 service-owned outcome recording

**B2-OUTCOME — The project service records a finished child's outcome itself.**
A committed terminal whose orchestration is still open (active or paused), whose reservation is live, whose context is
`committed` and whose run belongs to the writer's own runtime and host class is recorded by the service on its own
writer, right after the result is safely stored and again by a bounded, fair sweep after a restart. The outcome is a
pure function of the store: the service's and the primary's records are byte-identical. One final predicate
(`ServiceRecordAuthority`, a required typed check) runs as the first statement of the admission transaction at the
service entry and at the primary's, so admission, close, interruption, the 24 h idle end and expiry meet at one
boundary; after any of those nothing new is recorded and "unrecorded" is final (owner decision 92.1 A). The primary's
later call on a recorded run is a read-only retrieval that writes nothing, works after a normal close, and never
changes who recorded it; its refusals are named and write nothing. Who recorded (`service` or `primary`) lives in a
small separate table keyed by the receipt id with no foreign key (schema step 19 -> 20, owner decision 92.3 A);
receipts, records and the enrolled operation set are unchanged; the service entry has no socket entry; the service
writes no QA and never sets `accepted`.
Source: DESIGN-0.4.0-B2-outcome.md draft 2 with the s2 corrections, rows B2-1 to B2-18; owner decision 92; docs/requirements/b2.md.
