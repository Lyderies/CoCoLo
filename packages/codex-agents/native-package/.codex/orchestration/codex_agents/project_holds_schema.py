"""O0b run supervisor, slice SB: the project store's schema step 17 -> 18 (DESIGN-0.4.0-RS-BD-shared.md SH-1).

Four tables, created in one transaction through the authorized upgrade path, with their partial indexes:
- `project_holds`: RS-B's register, one row per project-side hold (RS-B draft 2 §2); a released row is a tombstone;
- `project_result_contexts`: RS-D's stored record contexts (RS-D draft 2 §1-§5, draft 3 §4);
- `project_result_publications`: RS-D's prepared publication material (RS-D draft 3 §3);
- `project_account_settlements`: RS-B's verified account settlements (RS-B draft 2 §4).

SB wrote no rows; each later slice brings its writes, its adoption class and its validator rules
(PLAN-0.4.0-supervisor-slices.md). B1 (below): the lease and reservation rows, written by `take_hold`, `arm_hold` and
`release_hold` inside the source row's own store transaction, and adopted by `adopt_project_holds` inside the 17 -> 18
step. The validators (SH-7) run at open, at restore (opening a restored copy) and inside the step before it commits.
Each is history-independent: it reads the held register rows through `project_holds_due`, the live contexts through
`project_result_contexts_live`, the live leases through `lease_expiry`, and each source row with one register lookup by
primary key; never the tombstones.
"""
from __future__ import annotations

import json
import secrets

from .history_contracts import HistoryError

HOLD_KINDS = ("driver_entry", "preparation", "lease", "reservation", "orchestration", "open_binding",
              "result_custody")
# RS-B draft 3 §14.1 and SH-3: each kind's release kinds.
RELEASE_KINDS = {
    "lease": ("withdrawn_unsent", "denied", "fenced", "bound", "lost_unbound"),
    "reservation": ("completed", "lost", "crashed", "released_unacknowledged"),
    "driver_entry": ("retired", "handed_over", "service_stopped"),
    "preparation": ("retired", "submit_refused", "never_launch_local", "service_stopped"),
    "orchestration": ("closed", "interrupted_unattended"),
    "open_binding": ("withdrawn", "claimed", "fenced", "account_final"),
    "result_custody": ("lost_with_reservation", "retention", "orchestration_closed"),
}
LEASE_CASES = ("unarmed", "armed", "closing_denied", "admitted_unbound")
CONTEXT_STATES = ("leased", "opened", "bound", "committed", "recorded", "retiring", "repair_required", "ended",
                  "expired", "legacy_no_context")
SETTLEMENT_KINDS = ("lost", "crashed", "released_unacknowledged")
# SH-3: the RS-B releases that end an unfinished attempt, and so release its custody `lost_with_reservation`.
CUSTODY_LEASE_CAUSES = ("withdrawn_unsent", "denied", "fenced", "lost_unbound")
CUSTODY_RESERVATION_CAUSES = ("lost", "crashed", "released_unacknowledged")


def _in(values) -> str:
    return "(" + ",".join(f"'{value}'" for value in values) + ")"


_RELEASE_KIND_CHECK = " OR ".join(f"(kind='{kind}' AND release_kind IN {_in(kinds)})"
                                  for kind, kinds in RELEASE_KINDS.items())

PROJECT_HOLDS_SCHEMA = f"""
CREATE TABLE project_holds(
 project_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN {_in(HOLD_KINDS)}),
 identity TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('held','releasing','released')),
 generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>=1),
 instance TEXT,lease_case TEXT CHECK(lease_case IN {_in(LEASE_CASES)}),
 taken_at_ms INTEGER NOT NULL,due_at_ms INTEGER,next_check_ms INTEGER NOT NULL DEFAULT 0,
 released_at_ms INTEGER,release_kind TEXT,request_id TEXT,request_digest TEXT,receipt TEXT,evidence TEXT,
 CHECK((kind='lease')=(lease_case IS NOT NULL)),
 CHECK((kind IN ('driver_entry','preparation'))=(instance IS NOT NULL)),
 CHECK(state='released' OR released_at_ms IS NULL),
 CHECK(state<>'released' OR release_kind IS NOT NULL),
 CHECK(release_kind IS NULL OR {_RELEASE_KIND_CHECK}),
 PRIMARY KEY(project_id,kind,identity),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
CREATE INDEX project_holds_due ON project_holds(state,next_check_ms) WHERE state<>'released';
CREATE INDEX project_holds_kind_due ON project_holds(kind,state,due_at_ms) WHERE state<>'released';
CREATE TABLE project_result_contexts(
 project_id TEXT NOT NULL,run_handle TEXT NOT NULL,attempt INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN {_in(CONTEXT_STATES)}),
 context_digest TEXT,context_size INTEGER NOT NULL CHECK(context_size>=0),host_class TEXT,
 claim_id TEXT,claim_digest TEXT,outcome_digest TEXT,
 lease_deadline_ms INTEGER NOT NULL,retained_until_ms INTEGER,repair_reason TEXT,
 CHECK((claim_id IS NULL)=(claim_digest IS NULL)),
 CHECK((state='repair_required')=(repair_reason IS NOT NULL)),
 PRIMARY KEY(project_id,run_handle,attempt),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
CREATE INDEX project_result_contexts_live ON project_result_contexts(state) WHERE state NOT IN ('ended','expired');
CREATE UNIQUE INDEX project_result_contexts_outcome ON project_result_contexts(project_id,outcome_digest)
 WHERE outcome_digest IS NOT NULL;
CREATE TABLE project_result_publications(
 project_id TEXT NOT NULL,claim_id TEXT NOT NULL,material BLOB NOT NULL,
 PRIMARY KEY(project_id,claim_id),
 FOREIGN KEY(project_id,claim_id) REFERENCES result_reservations(project_id,claim_id));
CREATE TABLE project_account_settlements(
 project_id TEXT NOT NULL,run_handle TEXT NOT NULL,attempt INTEGER NOT NULL,claim_id TEXT,
 release_kind TEXT NOT NULL CHECK(release_kind IN {_in(SETTLEMENT_KINDS)}),
 closure BLOB NOT NULL,settled_at_ms INTEGER NOT NULL,
 PRIMARY KEY(project_id,run_handle,attempt),UNIQUE(project_id,claim_id),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
"""
PROJECT_HOLDS_COLUMNS = {
    "project_holds": ("project_id", "kind", "identity", "state", "generation", "instance", "lease_case",
                      "taken_at_ms", "due_at_ms", "next_check_ms", "released_at_ms", "release_kind", "request_id",
                      "request_digest", "receipt", "evidence"),
    "project_result_contexts": ("project_id", "run_handle", "attempt", "state", "context_digest", "context_size",
                                "host_class", "claim_id", "claim_digest", "outcome_digest", "lease_deadline_ms",
                                "retained_until_ms", "repair_reason"),
    "project_result_publications": ("project_id", "claim_id", "material"),
    "project_account_settlements": ("project_id", "run_handle", "attempt", "claim_id", "release_kind", "closure",
                                    "settled_at_ms"),
}


def attempt_identity(run_handle: str, attempt: int) -> str:
    """The register identity of an attempt's holds (lease, driver entry, preparation, open binding, custody)."""
    return f"{run_handle}:{attempt}"


# One attempt's source row for a register row `r` holding an attempt identity: found by the source's primary key,
# and the identity must be exactly that row's (no other spelling of the same attempt).
def _attempt_of(alias: str) -> str:
    return (f"{alias}.project_id=r.project_id AND {alias}.run_handle=substr(r.identity,1,instr(r.identity,':')-1) "
            f"AND {alias}.attempt=CAST(substr(r.identity,instr(r.identity,':')+1) AS INTEGER) "
            f"AND {alias}.run_handle||':'||{alias}.attempt=r.identity")


# B1: the lease cases a held row may carry for each live lease state (SH-2). `closing_denied` and `admitted_unbound`
# are later slices' cases of an armed lease.
_LIVE_LEASE_CASES = {"reserved": ("unarmed",), "claim_pending": ("armed", "closing_denied", "admitted_unbound")}
_LEASE_CASE_RULE = " OR ".join(f"(l.state='{state}' AND r.lease_case NOT IN {_in(cases)})"
                               for state, cases in _LIVE_LEASE_CASES.items())

# RS-B's register validator (SH-7): every held row of a kind with a store source has that source (a released row may
# outlive its pruned source as a tombstone), and the reservation mapping of RS-B draft 3 §6. B1: every held lease
# (reserved or armed) has its held row with its lease case, every held lease row a held lease, and every reservation
# its row (its obligation ends only by its own release, whatever its state). B2 adds the memory kinds' instance rule;
# B3 the open bindings.
_REGISTER_INCONSISTENT = (
    "SELECT 1 FROM project_holds r INDEXED BY project_holds_due WHERE r.state<>'released' AND ("
    f"(r.kind='lease' AND NOT EXISTS(SELECT 1 FROM result_leases l WHERE {_attempt_of('l')} "
    "AND l.state IN ('reserved','claim_pending'))) "
    "OR (r.kind='reservation' AND NOT EXISTS(SELECT 1 FROM result_reservations s "
    "WHERE s.project_id=r.project_id AND s.claim_id=r.identity)) "
    "OR (r.kind='orchestration' AND NOT EXISTS(SELECT 1 FROM project_orchestrations o "
    "WHERE o.project_id=r.project_id AND o.orchestration_handle=r.identity))) LIMIT 1",
    # RS-B draft 3 §6: `completed` only for a reservation past `reserved` whose release ack exists; a settlement
    # release only with its settlement row, of the same kind and claim.
    "SELECT 1 FROM result_reservations s JOIN project_holds r ON r.project_id=s.project_id "
    "AND r.kind='reservation' AND r.identity=s.claim_id WHERE r.state='released' AND ("
    "(r.release_kind='completed' AND (s.state='reserved' OR NOT EXISTS(SELECT 1 FROM project_cleanup_acknowledgements a "
    "WHERE a.project_id=s.project_id AND a.run_handle=s.run_handle AND a.attempt=s.attempt))) "
    f"OR (r.release_kind IN {_in(SETTLEMENT_KINDS)} AND NOT EXISTS(SELECT 1 FROM project_account_settlements t "
    "WHERE t.project_id=s.project_id AND t.run_handle=s.run_handle AND t.attempt=s.attempt "
    "AND t.release_kind=r.release_kind AND t.claim_id IS s.claim_id))) LIMIT 1",
    # B1 coverage: a held lease with no held row, or one whose case disagrees (one lookup per live lease).
    "SELECT 1 FROM result_leases l INDEXED BY lease_expiry LEFT JOIN project_holds r ON r.project_id=l.project_id "
    "AND r.kind='lease' AND r.identity=l.run_handle||':'||l.attempt "
    "WHERE l.project_id=(SELECT project_id FROM history_identity) "
    f"AND l.state IN ('reserved','claim_pending') AND (r.kind IS NULL OR r.state='released' OR {_LEASE_CASE_RULE}) "
    "LIMIT 1",
    # B1 coverage: a reservation with no row (held, or released by its own release).
    "SELECT 1 FROM result_reservations s LEFT JOIN project_holds r ON r.project_id=s.project_id "
    "AND r.kind='reservation' AND r.identity=s.claim_id WHERE r.kind IS NULL LIMIT 1",
)


def register_consistent(connection) -> bool:
    """RS-B's register validator (SH-7), as far as SB and B1 can check it."""
    return all(connection.execute(sql).fetchone() is None for sql in _REGISTER_INCONSISTENT)


# B1: the writes. Each runs inside the caller's store transaction, with the source row it records, so the two change
# together or not at all (RS-B R1). A missing or already released row is never written over: the register then
# disagrees with its source, which only a defect can cause, and the whole transaction is refused.

def evidence_json(**facts) -> str:
    return json.dumps(facts, sort_keys=True, separators=(",", ":"))


def take_hold(connection, project_id: str, kind: str, identity: str, *, now_ms: int, lease_case: str | None = None,
              due_at_ms: int | None = None, evidence: str | None = None) -> None:
    """Take one hold. An identity is taken once: its tombstone (a released row, kept for good) or a row still held
    refuses the take (RS-B R2, S4), so a purged attempt can never be presented again."""
    if connection.execute("SELECT 1 FROM project_holds WHERE project_id=? AND kind=? AND identity=?",
                          (project_id, kind, identity)).fetchone() is not None:
        raise HistoryError("history_conflict")
    connection.execute(
        "INSERT INTO project_holds(project_id,kind,identity,state,lease_case,taken_at_ms,due_at_ms,evidence) "
        "VALUES(?,?,?,'held',?,?,?,?)", (project_id, kind, identity, lease_case, now_ms, due_at_ms, evidence))


def arm_hold(connection, project_id: str, identity: str) -> None:
    """SH-2: an unarmed lease is armed (its claim may be sent from now on)."""
    if connection.execute(
            "UPDATE project_holds SET lease_case='armed' WHERE project_id=? AND kind='lease' AND identity=? "
            "AND state='held' AND lease_case='unarmed'", (project_id, identity)).rowcount != 1:
        raise HistoryError("history_incompatible")


def release_hold(connection, project_id: str, kind: str, identity: str, release_kind: str, *, now_ms: int,
                 receipt: str | None = None, evidence: str | None = None) -> None:
    """Release one hold with its kind's evidence; the row stays as its tombstone. Released exactly once."""
    if connection.execute(
            "UPDATE project_holds SET state='released',released_at_ms=?,release_kind=?,receipt=?,"
            "evidence=COALESCE(?,evidence) WHERE project_id=? AND kind=? AND identity=? AND state<>'released'",
            (now_ms, release_kind, receipt, evidence, project_id, kind, identity)).rowcount != 1:
        raise HistoryError("history_incompatible")


# B1 adoption (SH-1, RS-9), inside the 17 -> 18 step before both validators. Each class follows from the source row's
# own state, never from a deadline: a reserved lease is held unarmed, an armed one held armed; a bound lease is
# released `bound` (its reservation holds the obligation); an expired lease with no host open is released
# `withdrawn_unsent` (no claim is ever sent without a recorded host open), one with a host open `denied` (only a
# verified denial closure expires it then). A reservation is held, unless the account's release of its attempt is
# acknowledged and it is past
# `reserved` (`completed`, RS-B draft 3 §6). Each insert skips an identity already in the register: adoption is
# idempotent, and an ordinary open never adopts (a missing row is refused there, R3).
ADOPTED = evidence_json(adopted=18)
_ADOPT = (
    "INSERT INTO project_holds(project_id,kind,identity,state,lease_case,taken_at_ms,due_at_ms,released_at_ms,"
    "release_kind,evidence) SELECT l.project_id,'lease',l.run_handle||':'||l.attempt,"
    "CASE WHEN l.state IN ('reserved','claim_pending') THEN 'held' ELSE 'released' END,"
    "CASE WHEN l.state='reserved' OR (l.state='expired' AND NOT EXISTS(SELECT 1 FROM project_host_opens o "
    "WHERE o.project_id=l.project_id AND o.run_handle=l.run_handle AND o.attempt=l.attempt)) THEN 'unarmed' "
    "ELSE 'armed' END,l.created_at_ms,l.reserved_until_ms,"
    "CASE WHEN l.state IN ('reserved','claim_pending') THEN NULL ELSE :now END,"
    "CASE WHEN l.state='bound' THEN 'bound' WHEN l.state<>'expired' THEN NULL WHEN EXISTS(SELECT 1 FROM "
    "project_host_opens o WHERE o.project_id=l.project_id AND o.run_handle=l.run_handle AND o.attempt=l.attempt) "
    "THEN 'denied' ELSE 'withdrawn_unsent' END,:evidence FROM result_leases l WHERE NOT EXISTS(SELECT 1 FROM "
    "project_holds h WHERE h.project_id=l.project_id AND h.kind='lease' AND h.identity=l.run_handle||':'||l.attempt)",
    "INSERT INTO project_holds(project_id,kind,identity,state,taken_at_ms,due_at_ms,released_at_ms,release_kind,"
    "evidence) SELECT s.project_id,'reservation',s.claim_id,"
    "CASE WHEN s.state<>'reserved' AND EXISTS(SELECT 1 FROM project_cleanup_acknowledgements a WHERE "
    "a.project_id=s.project_id AND a.run_handle=s.run_handle AND a.attempt=s.attempt) THEN 'released' ELSE 'held' "
    "END,s.created_at_ms,s.reserved_until_ms,"
    "CASE WHEN s.state<>'reserved' AND EXISTS(SELECT 1 FROM project_cleanup_acknowledgements a WHERE "
    "a.project_id=s.project_id AND a.run_handle=s.run_handle AND a.attempt=s.attempt) THEN :now END,"
    "CASE WHEN s.state<>'reserved' AND EXISTS(SELECT 1 FROM project_cleanup_acknowledgements a WHERE "
    "a.project_id=s.project_id AND a.run_handle=s.run_handle AND a.attempt=s.attempt) THEN 'completed' END,"
    ":evidence FROM result_reservations s WHERE NOT EXISTS(SELECT 1 FROM project_holds h WHERE "
    "h.project_id=s.project_id AND h.kind='reservation' AND h.identity=s.claim_id)",
)


def adopt_project_holds(connection, now_ms: int) -> None:
    """Give every lease and reservation without a register row its row, by class (idempotent; in the step)."""
    if not connection.in_transaction:
        raise HistoryError("history_unavailable")
    for sql in _ADOPT:
        connection.execute(sql, {"now": now_ms, "evidence": ADOPTED})


_HELD_LEASE = ("EXISTS(SELECT 1 FROM project_holds h WHERE h.project_id=c.project_id AND h.kind='lease' "
               "AND h.identity=c.run_handle||':'||c.attempt AND h.state<>'released')")
_HELD_RESERVATION = ("EXISTS(SELECT 1 FROM result_reservations s JOIN project_holds h ON h.project_id=s.project_id "
                     "AND h.kind='reservation' AND h.identity=s.claim_id AND h.state<>'released' "
                     "WHERE s.project_id=c.project_id AND s.run_handle=c.run_handle AND s.attempt=c.attempt)")
_CUSTODY_INCONSISTENT = (
    # Every live context has its custody row; while that row is held, the context's state agrees with its lease and
    # reservation (RS-D draft 4 §6). A released custody row's context is the context sweep's to finish (RS-D draft 3
    # §5), so a context not yet `retiring` there is not refused.
    "SELECT 1 FROM project_result_contexts c INDEXED BY project_result_contexts_live "
    "LEFT JOIN project_holds r ON r.project_id=c.project_id AND r.kind='result_custody' "
    "AND r.identity=c.run_handle||':'||c.attempt WHERE c.state NOT IN ('ended','expired') AND ("
    "r.kind IS NULL "
    f"OR (r.state<>'released' AND c.state IN ('leased','opened','bound') AND NOT {_HELD_LEASE} "
    f"AND NOT {_HELD_RESERVATION}) "
    "OR (r.state<>'released' AND c.state IN ('committed','recorded') AND NOT EXISTS(SELECT 1 FROM result_reservations s "
    "WHERE s.project_id=c.project_id AND s.run_handle=c.run_handle AND s.attempt=c.attempt "
    "AND s.state IN ('committed','retiring'))) "
    # The joint check (SH-7): custody released `lost_with_reservation` keeps its evidence, and the RS-B release that
    # caused it is there: its lease released as an unfinished attempt, or its reservation released by settlement.
    "OR (r.state='released' AND r.release_kind='lost_with_reservation' AND (r.evidence IS NULL OR ("
    "NOT EXISTS(SELECT 1 FROM project_holds h WHERE h.project_id=c.project_id AND h.kind='lease' "
    f"AND h.identity=r.identity AND h.state='released' AND h.release_kind IN {_in(CUSTODY_LEASE_CAUSES)}) "
    "AND NOT EXISTS(SELECT 1 FROM result_reservations s JOIN project_holds h ON h.project_id=s.project_id "
    "AND h.kind='reservation' AND h.identity=s.claim_id WHERE s.project_id=c.project_id AND s.run_handle=c.run_handle "
    f"AND s.attempt=c.attempt AND h.state='released' AND h.release_kind IN {_in(CUSTODY_RESERVATION_CAUSES)}))))"
    ") LIMIT 1",
    # Every held custody row has its live context (so an `ended` or `expired` context has only released custody).
    "SELECT 1 FROM project_holds r INDEXED BY project_holds_due WHERE r.state<>'released' AND r.kind='result_custody' "
    "AND NOT EXISTS(SELECT 1 FROM project_result_contexts c WHERE " + _attempt_of("c")
    + " AND c.state NOT IN ('ended','expired')) LIMIT 1",
)


def custody_consistent(connection, result_key: bytes | None) -> bool:
    """RS-D's custody validator (SH-7), as far as SB can check it: contexts and custody rows, their states, the
    joint check, and each publication: only for a `reserved` reservation with a prepared digest, and (when the
    store holds its result key) its material hashing to that digest. Context bytes are checked with D1's budget."""
    if not all(connection.execute(sql).fetchone() is None for sql in _CUSTODY_INCONSISTENT):
        return False
    from .learning_contracts import keyed_digest

    for material, state, prepared in connection.execute(
            "SELECT p.material,s.state,s.prepared_digest FROM project_result_publications p "
            "LEFT JOIN result_reservations s ON s.project_id=p.project_id AND s.claim_id=p.claim_id"):
        if state != "reserved" or type(prepared) is not str or type(material) is not bytes:
            return False
        if result_key is not None and not secrets.compare_digest(keyed_digest(result_key, material), prepared):
            return False
    return True
