# RS-F S7 — permanent replay identity (RS-13)

Source: `ANALYSIS-0.4.0-rsf-migration.md` §4 S7 and `PLAN-0.4.0.md`
RS-13 (1708–1712) in `~/Code/temp/orchestrator-child-liveness`.

**RSF-S7-1 — Account bindings.** After an authorized schema 15→16 upgrade,
payload expiry, binding deletion and two reopenings, the original withdrawal
returns its original state, handle and receipt (`known=True`). A supplied
generation different from the recorded request generation, an altered digest,
or another authenticated caller is refused without changing any row. Exactly
one release and its original generation, evidence and receipt remain. The
existing digest-only legacy retry with `generation=None` remains supported;
it cannot cause a new transition.

**RSF-S7-2 — Account settlements.** A sealed armed lease and a retained sealed
claim receipt authenticate the original lost-run settlement after schema
15→16 upgrade, payload expiry and binding pruning. Repeated retries return
the same settlement receipt and byte-identical authenticated closure. Changed
identity digest, lease generation, project or caller is refused. The slot is
released once; journal data is never needed to reconstruct the answer.

**RSF-S7-3 — Project holds.** Schema 18→20 upgrade preserves permanent hold
identity and settlement reply evidence. Both bound and unbound attempts retain
their verified closure and original request identity through attempt pruning.
The original settlement outbox request returns its recorded settled answer
after two reopenings; an altered request digest/id, attempt or generation is
refused without writes. Replaying the original authenticated closure returns
the same receipt with `known=True`; its hold cannot be taken again.

**RSF-S7-4 — Launch journal expiry.** Real v1 process and v2 container entries
remain readable across a successor instance and are removed strictly after
`TOMBSTONE_MS`. After journal expiry and project payload/attempt pruning, the
original request still returns the recorded settlement answer and closure
receipt through permanent store evidence. Altered digest or generation is
refused, with no second release or launch. The journal remains transient.

**RSF-S7-5 — Historical evidence and scope.** Preserve evidence already present
in supported older layouts, including verified settlement rows pruned after
upgrade. Never invent a reply for a historical tombstone that has no replay
proof. No schema bump, socket, CLI, git operation or write outside this worktree
is authorized. Fixtures use worktree scratch directories and real SQLite.

**RSF-S7-6 — Validation.** Write acceptance first, add new store-level tests,
and run each named module red before production changes, then green. Every
unit invocation is `python3 -B -m unittest <module>` from
`packages/codex-agents`, with a 300-second bound. Existing assertions must
remain untouched; if changing one is necessary, stop and document old → new
in [assertions](rsf-s7/assertions.md). Record timings and exact unrun socket/CLI
commands in [validation](rsf-s7/validation.md).
