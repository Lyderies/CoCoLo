# RS-F S7 — bounded replay evidence and permanent identity (RS-13)

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
identity and settlement reply evidence within the replay window below. Both
bound and unbound attempts retain their verified closure and original request
identity through attempt pruning within that window.
The original settlement outbox request returns its recorded settled answer
after two reopenings; an altered request digest/id, attempt or generation is
refused without writes. Replaying the original authenticated closure returns
the same receipt with `known=True`; its hold cannot be taken again.

Decision 214: the replay window is finite and inclusive, measured from the
hold's recorded `released_at_ms`. It is
`max(MAX_RESULT_RETENTION_MS, TOMBSTONE_MS) + TOMBSTONE_MS`: the longest
payload/journal retention plus the existing tombstone margin, currently eight
days. Reuse `result_contracts.py:18` (`MAX_RESULT_RETENTION_MS`, seven days)
and `launch_journal.py:118` (`TOMBSTONE_MS`, one day), both under
`packages/codex-agents/native-package/.codex/orchestration/codex_agents/`.
This window outlives payload and journal expiry; it does not promise replay
forever. Account binding/settlement identity requirements above are unchanged.

For each settled attempt, `project_holds.evidence` keeps one authenticated
canonical ASCII closure of at most 8,192 bytes inclusive, on its reservation
hold if bound or its lease hold if unbound. Retries reuse the row. After the
window, the existing `expire_results` retention/purge pass clears evidence,
keeping every other hold field and the permanent identity. Each pass examines
at most `limit * PURGE_SCAN_FACTOR` candidate row positions and clears at most
`limit` evidence rows (`limit` is 1–100). A cursor lets later rows progress.
Attempt pruning must not restore expired closure evidence. Both settlement
request replay and supplied-closure replay after the window refuse with the
fixed reason `history_result_not_reserved`, even before cleanup runs; neither
may synthesize an answer from the remaining receipt. The persisted clock floor
prevents replay revival after clock rollback. The byte ceiling, evidence
expiry, per-pass bounds and boundary test are registered in
`tests/lifetime_caps.json` under `project_history.py::record_account_settlement`.

**RSF-S7-4 — Launch journal expiry.** Real v1 process and v2 container entries
remain readable across a successor instance and are removed strictly after
`TOMBSTONE_MS`. After journal expiry and project payload/attempt pruning, the
original request within the replay window still returns the recorded settlement
answer and closure receipt through retained store evidence. Altered digest or generation is
refused, with no second release or launch. The journal remains transient.

**RSF-S7-5 — Historical evidence and scope.** Preserve evidence already present
in supported older layouts, including verified settlement rows pruned after
upgrade, while within the replay window. Never invent a reply for a historical tombstone that has no replay
proof. No schema bump, socket, CLI, git operation or write outside this worktree
is authorized. Fixtures use worktree scratch directories and real SQLite.

**RSF-S7-6 — Validation.** Write acceptance first, add new store-level tests,
and run each named module red before production changes, then green. Every
unit invocation is `python3 -B -m unittest <module>` from
`packages/codex-agents`, with a 300-second bound. Existing assertions must
remain untouched; if changing one is necessary, stop and document old → new
in [assertions](rsf-s7/assertions.md). Record timings and exact unrun socket/CLI
commands in [validation](rsf-s7/validation.md).
