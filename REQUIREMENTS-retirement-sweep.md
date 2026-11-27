# D3 crash-safe retirement and the context sweep

**RS-D-D3 — Crash-safe retirement and the context sweep.**
Retirement records `retiring` (custody released, reads refused), unlinks the fixed names without opening or following them
(a missing, corrupt, wrong-mode, extra-hard-link, symlink, FIFO or link-pair entry is unlinked as an entry), fsyncs the
directory, and only then records `expired`; a stop at any step is finished by the next pass, and the charge is kept until
`expired`. Only a directory under a fixed name needs repair: the row stays `retiring` and charged, its context is
`repair_required` with the reason, it is shown by `result_retirement_repairs`, retried every pass, and other custody keeps
retiring in the same pass; an unlink or fsync error keeps the row `retiring`, charged and retried. The context sweep finishes
context-only custody (custody released, no reservation row, or its own `retiring` / `repair_required` retry) with no
reservation row read or written, and never selects a context that has a reservation. Start-up reconciliation removes orphan
and partial context and body files and one name of a link pair using directory listing and `lstat` only: no body or context
bytes are read, and its work does not grow with the number of tombstones (RS-D-9). A 1 MiB body is returned through every
chunk (K2).
Source: DESIGN-0.4.0-RS-D.md draft 3 s4-s6, draft 4 s1, rows U1-U10, K1, K2, S1; DESIGN-0.4.0-RS-BD-shared.md SH-3 (retirement); docs/requirements/d3.md.
