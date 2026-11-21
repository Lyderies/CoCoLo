"""O0b run supervisor, stage RS-A: the hold register for the account's own holds (DESIGN-0.4.0-RS-A.md).

One row per account hold: an account run slot (active_run_capacity), a pending binding (host_run_bindings) or a
billing-probe request (account_billing_probe_requests). Triggers on those tables write the row in the same
transaction as every take and every ordinary release, on every code path, at the ledger's own time (the ledger
registers `ledger_now_ms()` on its connection; a connection without it cannot release a hold, and only the ledger
writes holds); the supervisor's own releases
(crashed, lost, expired, unlaunched) set the row's release kind first, and the triggers keep it. A released row
stays as a tombstone. The register is derived from that evidence, so install (and a restore) rebuild it by
adoption.
"""
from __future__ import annotations

import contextlib
import hashlib
import json

RUN_HOLD_KINDS = ("run_slot", "binding", "probe")
RELEASE_KINDS = ("completed", "consumed", "lost", "crashed", "expired", "withdrawn", "unlaunched",
                 "service_lost", "claim_denied", "upgrade_superseded", "abandoned", "removed")

RUN_HOLDS_SCHEMA = '''CREATE TABLE run_holds(
kind TEXT NOT NULL CHECK(kind IN ('run_slot','binding','probe')),
project_id TEXT NOT NULL,identity TEXT NOT NULL,
state TEXT NOT NULL CHECK(state IN ('held','releasing','released')),
generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>=1),
taken_at_ms INTEGER NOT NULL,released_at_ms INTEGER,release_kind TEXT,receipt TEXT,evidence TEXT,
next_check_ms INTEGER NOT NULL DEFAULT 0,
CHECK(state='released' OR released_at_ms IS NULL),
CHECK(state<>'released' OR release_kind IS NOT NULL),
PRIMARY KEY(project_id,kind,identity),
FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE)'''

# A release time is the time of the decision: the slot's own release time, a binding's consumed time, else the
# ledger's clock at the release. A slot's receipt is its release digest unless a written decision set it first.
_RELEASE = ("UPDATE run_holds SET state='released',generation=generation+1,"
            "released_at_ms=COALESCE(released_at_ms,{at}),release_kind=COALESCE(release_kind,{kind}),"
            "receipt=COALESCE(receipt,{receipt}) "
            "WHERE kind='{hold}' AND project_id=NEW.project_id AND identity={identity} AND state<>'released';")

# A source row deleted (record retention, project deletion) ends its hold: its row is released `removed`, unless
# already released, and stays as a tombstone.
_REMOVED = ("CREATE TRIGGER run_holds_{name}_removed AFTER DELETE ON {table} BEGIN "
            "UPDATE run_holds SET state='released',generation=generation+1,released_at_ms=ledger_now_ms(),"
            "release_kind=COALESCE(release_kind,'removed') WHERE kind='{hold}' AND project_id=OLD.project_id "
            "AND identity=OLD.{identity} AND state<>'released'; END")

RUN_HOLDS_OBJECTS = (
    ("table", "run_holds", RUN_HOLDS_SCHEMA),
    ("index", "run_holds_due",
     "CREATE INDEX run_holds_due ON run_holds(state,next_check_ms) WHERE state<>'released'"),
    ("trigger", "run_holds_slot_taken",
     # A row inserted already final (a restore, a migration) is recorded as it is, in adoption's terms.
     "CREATE TRIGGER run_holds_slot_taken AFTER INSERT ON active_run_capacity BEGIN "
     "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms,released_at_ms,release_kind,receipt) "
     "VALUES('run_slot',NEW.project_id,NEW.handle_digest,"
     "CASE WHEN NEW.released_at_ms IS NULL THEN 'held' ELSE 'released' END,NEW.claimed_at_ms,NEW.released_at_ms,"
     "CASE WHEN NEW.released_at_ms IS NOT NULL THEN 'completed' END,NEW.release_digest); END"),
    ("trigger", "run_holds_slot_released",
     "CREATE TRIGGER run_holds_slot_released AFTER UPDATE OF released_at_ms ON active_run_capacity "
     "WHEN OLD.released_at_ms IS NULL AND NEW.released_at_ms IS NOT NULL BEGIN "
     + _RELEASE.format(at="NEW.released_at_ms", kind="'completed'", hold="run_slot", identity="NEW.handle_digest",
                       receipt="NEW.release_digest")
     + " END"),
    ("trigger", "run_holds_binding_taken",
     "CREATE TRIGGER run_holds_binding_taken AFTER INSERT ON host_run_bindings BEGIN "
     "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms,released_at_ms,release_kind) "
     "VALUES('binding',NEW.project_id,NEW.handle_digest,"
     "CASE WHEN NEW.binding_state IS 'pending' THEN 'held' ELSE 'released' END,NEW.created_at_ms,"
     # O0b RS-A round 2 G2: consumed_at_ms is a release time only for a terminal binding (a
     # recovery_unavailable one gains it later, by fallback); an abandoned row's time stays unknown.
     "CASE WHEN NEW.binding_state='terminal' THEN NEW.consumed_at_ms END,"
     "CASE WHEN NEW.binding_state IS 'pending' THEN NULL WHEN NEW.binding_state='terminal' THEN 'consumed' "
     "ELSE COALESCE(NEW.abandoned_reason,'abandoned') END); END"),
    ("trigger", "run_holds_binding_released",
     "CREATE TRIGGER run_holds_binding_released AFTER UPDATE OF binding_state ON host_run_bindings "
     "WHEN OLD.binding_state IS 'pending' AND NEW.binding_state IS NOT 'pending' BEGIN "
     + _RELEASE.format(at="COALESCE(NEW.consumed_at_ms,ledger_now_ms())",
                       kind="CASE WHEN NEW.binding_state='terminal' THEN 'consumed' "
                            "ELSE COALESCE(NEW.abandoned_reason,'abandoned') END",
                       hold="binding", identity="NEW.handle_digest", receipt="NULL")
     + " END"),
    ("trigger", "run_holds_probe_taken",
     "CREATE TRIGGER run_holds_probe_taken AFTER INSERT ON account_billing_probe_requests BEGIN "
     "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms,release_kind) "
     "VALUES('probe',NEW.project_id,NEW.request_key,"
     "CASE WHEN NEW.state='committed' THEN 'released' ELSE 'held' END,NEW.created_at_ms,"
     "CASE WHEN NEW.state='committed' THEN 'completed' END); END"),
    ("trigger", "run_holds_probe_released",
     "CREATE TRIGGER run_holds_probe_released AFTER UPDATE OF state ON account_billing_probe_requests "
     "WHEN OLD.state<>'committed' AND NEW.state='committed' BEGIN "
     + _RELEASE.format(at="ledger_now_ms()", kind="'completed'", hold="probe", identity="NEW.request_key",
                       receipt="NULL")
     + " END"),
    ("trigger", "run_holds_slot_removed", _REMOVED.format(
        name="slot", table="active_run_capacity", hold="run_slot", identity="handle_digest")),
    ("trigger", "run_holds_binding_removed", _REMOVED.format(
        name="binding", table="host_run_bindings", hold="binding", identity="handle_digest")),
    ("trigger", "run_holds_probe_removed", _REMOVED.format(
        name="probe", table="account_billing_probe_requests", hold="probe", identity="request_key")),
)

# Adoption: each existing hold gets its row from the evidence it has; nothing is invented. A release time history
# does not record (an abandoned binding's, a committed probe's) stays unknown (NULL), never the creation time nor
# a later fallback's consumption time (O0b RS-A round 2 G2).
_ADOPT = (
    "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms,released_at_ms,release_kind,receipt) "
    "SELECT 'run_slot',a.project_id,a.handle_digest,"
    "CASE WHEN a.released_at_ms IS NOT NULL THEN 'released' ELSE 'held' END,a.claimed_at_ms,a.released_at_ms,"
    "CASE WHEN a.released_at_ms IS NOT NULL THEN 'completed' END,a.release_digest FROM active_run_capacity a",
    "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms,released_at_ms,release_kind) "
    "SELECT 'binding',h.project_id,h.handle_digest,"
    "CASE WHEN h.binding_state IS 'pending' THEN 'held' ELSE 'released' END,h.created_at_ms,"
    "CASE WHEN h.binding_state='terminal' THEN h.consumed_at_ms END,"  # O0b RS-A round 2 G2: as the take trigger
    "CASE WHEN h.binding_state IS 'pending' THEN NULL WHEN h.binding_state='terminal' THEN 'consumed' "
    "ELSE COALESCE(h.abandoned_reason,'abandoned') END FROM host_run_bindings h",
    "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms,released_at_ms,release_kind) "
    "SELECT 'probe',p.project_id,p.request_key,"
    "CASE WHEN p.state='committed' THEN 'released' ELSE 'held' END,p.created_at_ms,"
    "NULL,"
    "CASE WHEN p.state='committed' THEN 'completed' END FROM account_billing_probe_requests p",
)


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def assert_run_holds_schema(connection) -> None:
    for kind, name, sql in RUN_HOLDS_OBJECTS:
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
        if row is None or type(row[0]) is not str or _normalized(row[0]) != _normalized(sql):
            raise ValueError("run hold schema is incompatible")


def install_run_holds_schema(connection) -> None:
    """Create the register and adopt every existing account hold, in the owner's transaction."""
    if not connection.in_transaction:
        raise ValueError("run hold migration requires an owner transaction")
    for kind, _, sql in RUN_HOLDS_OBJECTS:
        # Idempotent: an object already there is left (and then checked exactly below).
        connection.execute(sql.replace(f"CREATE {kind.upper()} ", f"CREATE {kind.upper()} IF NOT EXISTS ", 1))
    adopt_run_holds(connection)
    assert_run_holds_schema(connection)
    if not check_run_holds_consistency(connection):
        raise ValueError("run hold adoption is incomplete")


def adopt_run_holds(connection) -> None:
    """Give every account hold without a register row its row from its evidence (idempotent)."""
    for sql in _ADOPT:
        connection.execute(sql)


# A slot released by a written lost or crashed decision: its register row is released, at the slot's own release
# time, with the slot's release digest as its receipt. Used by every completion coverage check (alias a).
DECISION_RELEASED = (
    "EXISTS(SELECT 1 FROM run_holds r WHERE r.project_id=a.project_id AND r.kind='run_slot' "
    "AND r.identity=a.handle_digest AND r.state='released' AND r.release_kind IN ('crashed','lost') "
    "AND a.release_digest IS NOT NULL AND r.receipt=a.release_digest AND r.released_at_ms=a.released_at_ms)")

# Round 1 F2: each check finds a row that breaks the register's agreement with its holds.
# O0b RS-A round 2 G11: every check reads one source table once, each row with one register lookup by primary key
# (a LEFT JOIN: a missing row is the coverage failure), or the held register rows through run_holds_due; never a
# join over the whole register, so a tombstone costs nothing at open and each source row one lookup. Each kind's
# rules are listed apart, one per line, and any one breaks the row (a rule's NULL never does, as in a WHERE).
_SLOT_RULES = (
    # every slot has its register row
    "r.kind IS NULL",
    # states agree: a slot is released exactly when its capacity row is
    "(r.state='released') <> (a.released_at_ms IS NOT NULL)",
    # a lost or crashed decision matches its slot exactly
    "(r.release_kind IN ('crashed','lost') AND (r.state<>'released' OR r.receipt IS NULL "
    "OR r.receipt IS NOT a.release_digest OR r.released_at_ms IS NOT a.released_at_ms))",
    # O0b RS-A round 2 G1: a released slot's kind is a slot's (an ordinary completion or a decision), and an
    # ordinary completion carries its slot's own release time and digest
    "(r.state='released' AND (r.release_kind NOT IN ('completed','crashed','lost') "
    "OR (r.release_kind='completed' AND (r.receipt IS NOT a.release_digest "
    "OR r.released_at_ms IS NOT a.released_at_ms))))",
)
_BINDING_RULES = (
    # every binding has its register row
    "r.kind IS NULL",
    # a binding no longer pending is released; a pending one is held unless it expired (a claimed binding stays
    # pending for its settlement)
    "(h.binding_state IS NOT 'pending' AND r.state<>'released')",
    "(h.binding_state IS 'pending' AND r.state='released' AND r.release_kind IS NOT 'expired')",
    # O0b RS-A round 2 G1: an expired binding is released no earlier than its expiry (an unknown time stays allowed)
    "(r.release_kind='expired' AND r.released_at_ms<h.expires_at_ms)",
)
_PROBE_RULES = (
    # every probe has its register row
    "r.kind IS NULL",
    # a probe is released exactly when its request is committed
    "(r.state='released') <> (p.state='committed')",
)
_INCONSISTENT = (
    "SELECT 1 FROM active_run_capacity a LEFT JOIN run_holds r ON r.project_id=a.project_id AND r.kind='run_slot' "
    "AND r.identity=a.handle_digest WHERE " + " OR ".join(_SLOT_RULES) + " LIMIT 1",
    "SELECT 1 FROM host_run_bindings h LEFT JOIN run_holds r ON r.project_id=h.project_id AND r.kind='binding' "
    "AND r.identity=h.handle_digest WHERE " + " OR ".join(_BINDING_RULES) + " LIMIT 1",
    "SELECT 1 FROM account_billing_probe_requests p LEFT JOIN run_holds r ON r.project_id=p.project_id "
    "AND r.kind='probe' AND r.identity=p.request_key WHERE " + " OR ".join(_PROBE_RULES) + " LIMIT 1",
    # every held row has its hold (a released row may outlive its pruned source as a tombstone)
    "SELECT 1 FROM run_holds r INDEXED BY run_holds_due WHERE r.state<>'released' AND ((r.kind='run_slot' "
    "AND NOT EXISTS(SELECT 1 FROM active_run_capacity a WHERE a.project_id=r.project_id "
    "AND a.handle_digest=r.identity)) OR (r.kind='binding' AND NOT EXISTS(SELECT 1 FROM host_run_bindings h "
    "WHERE h.project_id=r.project_id AND h.handle_digest=r.identity)) OR (r.kind='probe' AND NOT EXISTS("
    "SELECT 1 FROM account_billing_probe_requests p WHERE p.project_id=r.project_id "
    "AND p.request_key=r.identity))) LIMIT 1",
)


def _raw_digest(text):
    """The digest of evidence stored as text, as its own bytes (None for any other value)."""
    return hashlib.sha256(text.encode("ascii")).hexdigest() if type(text) is str and text.isascii() else None


def _evidence_matches(connection) -> bool:
    """O0b RS-A round 2 G1: retained decision evidence hashes to its receipt (evidence pruned to NULL is allowed).
    O0b RS-A round 2 G10: a lost or crashed decision keeps its evidence for the row's life (its only proof).
    G11: one pass over the register for both. Evidence whose own bytes hash to its receipt (every writer stores
    it as its canonical bytes) passes in SQL; any other row is decoded and re-encoded as before.
    Round 3: retained evidence with no receipt never passes in SQL (its NULL would equal the NULL digest of
    evidence that is not ASCII text); it goes to the decoding check, which refuses it, as before G11."""
    from .learning_contracts import content_digest

    connection.create_function("run_hold_raw_digest", 1, _raw_digest, deterministic=True)
    for receipt, evidence in connection.execute(
            "SELECT receipt,evidence FROM run_holds WHERE (evidence IS NOT NULL "
            "AND (receipt IS NULL OR receipt IS NOT run_hold_raw_digest(evidence))) "
            "OR (kind='run_slot' AND release_kind IN ('crashed','lost') AND evidence IS NULL)"):
        if evidence is None or type(evidence) is not str or receipt is None:
            return False  # G10: a decision without its evidence; round 3: evidence without its receipt
        try:
            if receipt != content_digest(json.loads(evidence)):
                return False
        except (ValueError, TypeError, RecursionError):
            return False
    return True


# G11: the source passes are random reads of the register and its sources; memory-mapped reads roughly halve
# them on a ledger with long history, so the checks map the file while they run (the setting is put back after).
CHECK_MMAP_BYTES = 1 << 30


@contextlib.contextmanager
def _mapped(connection):
    row = connection.execute("PRAGMA mmap_size").fetchone()
    if row is None:  # a database with no file to map (in memory)
        yield
        return
    previous = int(row[0])
    connection.execute(f"PRAGMA mmap_size={CHECK_MMAP_BYTES}")
    try:
        yield
    finally:
        connection.execute(f"PRAGMA mmap_size={previous}")


def check_run_holds_consistency(connection) -> bool:
    """Round 1 F2: every hold has its row, every row its hold, and their states agree (used at open, at restore
    and by every backup check). G11: each source table read once, the register's held rows by index, and one pass
    over the register for decision evidence."""
    with _mapped(connection):
        return all(connection.execute(sql).fetchone() is None for sql in _INCONSISTENT) \
            and _evidence_matches(connection)


def check_run_holds_coverage(connection) -> bool:
    """Every account hold has its register row, and no row is left without its hold."""
    return connection.execute(
        "SELECT 1 FROM active_run_capacity a WHERE NOT EXISTS(SELECT 1 FROM run_holds r WHERE r.kind='run_slot' "
        "AND r.project_id=a.project_id AND r.identity=a.handle_digest) "
        "UNION ALL SELECT 1 FROM host_run_bindings h WHERE NOT EXISTS(SELECT 1 FROM run_holds r "
        "WHERE r.kind='binding' AND r.project_id=h.project_id AND r.identity=h.handle_digest) "
        "UNION ALL SELECT 1 FROM account_billing_probe_requests p WHERE NOT EXISTS(SELECT 1 FROM run_holds r "
        "WHERE r.kind='probe' AND r.project_id=p.project_id AND r.identity=p.request_key) LIMIT 1"
    ).fetchone() is None
