# RECOV: recovery status reflects service progress

**RECOV-1 — A healthy serving service does not report unresponsive while evidence is pending.**
After the initial validated history open, and after a periodic maintenance pass on the project history writer
whose maintenance steps all succeeded, publish an exact next check no earlier
than that pass's wall time when a hold deadline remains overdue. Repeat this for every successful pass, even if no
hold changed. Keep future deadlines and null (no outstanding deadline) unchanged. In particular, an expired
releasing reservation may legitimately wait for verified account closure while an account binding remains held
until a future `next_eligible`; neither that wait nor evidence-only holds imply an unresponsive project service.
Acceptance: the real writer pass, publisher, ledger start record and recovery formatter keep returning null across
more than START_RECHECK_SECONDS of successful passes with an unchanged overdue releasing reservation; a future
deadline is preserved. Subsequent hold transactions retain the last completed check as a floor for live exact
publications, without renewing it, including a real hold write committed through the history store between passes.
No hold is released or rescheduled by this status correction.

**RECOV-2 — The published orchestration check respects B6 backoff.**
The wake query uses max(due_at_ms, next_check_ms) for each held orchestration, then takes the earliest check across
all outstanding holds and withdrawal retries. A B6 refusal must invalidate the queued publication before changing
its next check, and publish the updated exact check after commit. Released tombstones do not contribute.
Acceptance: the real unattended close backs off a blocked orchestration and publishes its future retry; another
earlier due hold still wins; releasing its execution obligation resets the orchestration check through the existing
B6 release path. Before resetting a held orchestration's retry to its earlier due time, publish `pending` under
E2a-7, so a crash before the next exact publication cannot leave the collector holding the later retry.

**RECOV-3 — Preserve E2c-9's stuck-service signal and stopped-service recovery.**
After the initial validated history open, only a periodic history-writer maintenance pass whose maintenance steps
all succeeded supplies progress. A suppressed failure in any maintenance step prevents renewal for that pass,
while the remaining cleanup steps still run. Status reads, a serving process,
the publisher's sender thread, unrelated writes and failed maintenance do not renew this check. If progress stops
and a serving project's published due time is at least START_RECHECK_SECONDS past, keep the exact line
`recovery pending: service unresponsive`, including at the threshold. A clean stop publishes the actual outstanding
deadline, including an overdue evidence obligation, so the collector still wakes a stopped project conservatively.
Acceptance: no completed pass and a failed pass leave the overdue warning; after successful passes stop, the warning
returns at the threshold, including when one sub-step fails persistently inside otherwise completed passes;
clean_stop retains the raw overdue deadline. Existing E2c-9 assertions stay unchanged.
