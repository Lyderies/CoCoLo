# RS-F S6: classify historical project obligations

Base: `230c2fc0` (0.8.62 candidate). Basis: migration analysis section 4/S6,
section 3's project classifications, B4s settlement, owner decisions 129.2/129.3.
Old records upgrade; old executables gain no compatibility. B4t performs live
handover using S2's fence. This slice never signals a predecessor or invents an
account closure. Keep project schema 22, VERSION and the digest catalogue.

**RSF-S6-1 — Orchestration.** Adopt open and paused sealed orchestrations as
held, with their original identity and activity-based due time. Preserve closed
orchestrations. Run classification inside S3's bounded, cursor-based migration
under S2's lifetime lock and epoch, including interruption and two reopens.

**RSF-S6-2 — Open-binding duty.** Backfill missing pre-B3 duties from retained
host opens. Verify the original journal and lease before using their facts.
A bound lease has a claimed duty tombstone; an unresolved open stays held.
Retain the original run and attempt. If withdrawal inputs are absent, keep a
named evidence wait with a plain owner next step; never guess inputs or release
on a timer. Existing duties and authenticated replies remain unchanged. An
already recorded result proves its open duty fulfilled; preserve its register
without adding a duty.

Fix round MED-2: a verified historical open whose expired lease already has a
released `denied` or `fenced` tombstone uses that retained authenticated receipt
to release its backfilled duty as `fenced`, as the existing denial path does.
Prove this from a real schema-21 store, without inferring closure from expiry,
missing inputs or predecessor exit alone; preserve existing duties and receipts.
For a pre-B1 denial adopted without a receipt, the verified sealed expired lease
plus its verified host open and the existing B1 `denied` adoption predicate are
the retained no-launch evidence; preserve the absent receipt without inventing one.

**RSF-S6-3 — Settlement.** Classify an unfinished historical reservation as
held awaiting account settlement, with a named reason and plain next step.
Run B4s publication arbitration before the account outbox; only its verified
closure releases the reservation as lost. In the report, lost maps to the
existing `abandoned` count; the reservation hold records release kind `lost`.
Preserve a completed result during arbitration. Replay remains stable.

**RSF-S6-4 — Result custody.** Preserve finished result, terminal, sealed
context and historical record bytes. A verifiable committed result with its
stored inputs and open sealed orchestration remains recordable by the current
fenced service after upgrade, including after restart. Bind that permission to
this migrated attempt and the current owner runtime; an arbitrary runtime or an
ordinary fresh-store mismatch still refuses. Missing inputs remain
legacy_no_context with custody and an owner next step, without fabricated inputs.

**RSF-S6-5 — Memory-only preparation.** Do not reconstruct driver or preparation
memory. Report those categories as not_tracked. Classify any surviving unarmed
lease as held with a named legacy preparation reason and plain owner next step;
no missing-memory inference releases an uncertain obligation. Pure memory with
no durable source creates no invented hold or launch authority.

Fix round MED-1/LOW-3: every new owner step names an existing CLI command or a
documented automatic release. Ordinary fresh-store runtime mismatch points to
the existing setup update command. Unarmed leases still release automatically
on the service's expiry sweep or when the successor releases unused leases;
pending claims require the account's authenticated no-launch evidence.

**RSF-S6-6 — Late publication refusal.** The B4s releasing fence and verified
lost settlement each refuse a late publication with zero changes to terminal,
record, context, body or reservation state. Resume classification without
replacing existing duty/settlement evidence. Verify bounded batches and atomic
cursor rollback under real process death and a revoked S2 epoch.

Fix round LOW-1: both `publish_result` and `commit_result` must refuse with the
specific reason `history_result_expired` before and after lost settlement and
after reopen, with zero database or artifact changes.

## Evidence and scope

Use one mixed project store created by an archived pre-B3 schema-18 runtime
through its real store operations. Include a finished result with stored context,
an unfinished bound attempt, an unbound host open and memory-only preparation.
Fixture identity/enrollment controls are explicit; no source database rows are
hand-edited. Add enough real leases to cross S3's 64-row boundary. Supplemental
pre-context layouts must be explicitly labelled if used. Requirements precede
red tests, which precede production edits. Register tests and requirement IDs;
record named runs, limitations and assertion changes in [STATUS.md](rsf-s6/STATUS.md)
and [assertions.md](rsf-s6/assertions.md). No wire reply fields are added.
