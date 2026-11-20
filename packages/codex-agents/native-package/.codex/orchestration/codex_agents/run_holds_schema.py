"""O0b run supervisor, stage RS-A: the hold register for the account's own holds (DESIGN-0.4.0-RS-A.md).

One row per account hold: an account run slot (active_run_capacity), a pending binding (host_run_bindings) or a
billing-probe request (account_billing_probe_requests). Triggers on those tables write the row in the same
transaction as every take and every ordinary release, on every code path; the supervisor's own releases
(crashed, lost, expired, unlaunched) set the row's release kind first, and the triggers keep it. A released row
stays as a tombstone. The register is derived from that evidence, so install (and a restore) rebuild it by
adoption.
"""
from __future__ import annotations

RUN_HOLD_KINDS = ("run_slot", "binding", "probe")
RELEASE_KINDS = ("completed", "consumed", "lost", "crashed", "expired", "withdrawn", "unlaunched",
                 "service_lost", "claim_denied", "upgrade_superseded", "abandoned")

RUN_HOLDS_SCHEMA = '''CREATE TABLE run_holds(
kind TEXT NOT NULL CHECK(kind IN ('run_slot','binding','probe')),
project_id TEXT NOT NULL,identity TEXT NOT NULL,
state TEXT NOT NULL CHECK(state IN ('held','releasing','released')),
generation INTEGER NOT NULL DEFAULT 1 CHECK(generation>=1),
taken_at_ms INTEGER NOT NULL,released_at_ms INTEGER,release_kind TEXT,receipt TEXT,
next_check_ms INTEGER NOT NULL DEFAULT 0,
CHECK((state='released')=(released_at_ms IS NOT NULL AND release_kind IS NOT NULL)),
PRIMARY KEY(project_id,kind,identity),
FOREIGN KEY(project_id) REFERENCES projects(project_id) ON DELETE CASCADE)'''

_RELEASE = ("UPDATE run_holds SET state='released',generation=generation+1,"
            "released_at_ms=COALESCE(released_at_ms,{at}),release_kind=COALESCE(release_kind,{kind}) "
            "WHERE kind='{hold}' AND project_id=NEW.project_id AND identity={identity} AND state<>'released';")

RUN_HOLDS_OBJECTS = (
    ("table", "run_holds", RUN_HOLDS_SCHEMA),
    ("index", "run_holds_due",
     "CREATE INDEX run_holds_due ON run_holds(state,next_check_ms) WHERE state<>'released'"),
    ("trigger", "run_holds_slot_taken",
     "CREATE TRIGGER run_holds_slot_taken AFTER INSERT ON active_run_capacity BEGIN "
     "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms) "
     "VALUES('run_slot',NEW.project_id,NEW.handle_digest,'held',NEW.claimed_at_ms); END"),
    ("trigger", "run_holds_slot_released",
     "CREATE TRIGGER run_holds_slot_released AFTER UPDATE OF released_at_ms ON active_run_capacity "
     "WHEN OLD.released_at_ms IS NULL AND NEW.released_at_ms IS NOT NULL BEGIN "
     + _RELEASE.format(at="NEW.released_at_ms", kind="'completed'", hold="run_slot", identity="NEW.handle_digest")
     + " END"),
    ("trigger", "run_holds_binding_taken",
     "CREATE TRIGGER run_holds_binding_taken AFTER INSERT ON host_run_bindings BEGIN "
     "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms) "
     "VALUES('binding',NEW.project_id,NEW.handle_digest,'held',NEW.created_at_ms); END"),
    ("trigger", "run_holds_binding_released",
     "CREATE TRIGGER run_holds_binding_released AFTER UPDATE OF binding_state ON host_run_bindings "
     "WHEN OLD.binding_state IS 'pending' AND NEW.binding_state IS NOT 'pending' BEGIN "
     + _RELEASE.format(at="COALESCE(NEW.consumed_at_ms,NEW.created_at_ms)",
                       kind="CASE WHEN NEW.binding_state='terminal' THEN 'consumed' "
                            "ELSE COALESCE(NEW.abandoned_reason,'abandoned') END",
                       hold="binding", identity="NEW.handle_digest")
     + " END"),
    ("trigger", "run_holds_probe_taken",
     "CREATE TRIGGER run_holds_probe_taken AFTER INSERT ON account_billing_probe_requests BEGIN "
     "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms) "
     "VALUES('probe',NEW.project_id,NEW.request_key,'held',NEW.created_at_ms); END"),
    ("trigger", "run_holds_probe_released",
     "CREATE TRIGGER run_holds_probe_released AFTER UPDATE OF state ON account_billing_probe_requests "
     "WHEN OLD.state<>'committed' AND NEW.state='committed' BEGIN "
     + _RELEASE.format(at="NEW.created_at_ms", kind="'completed'", hold="probe", identity="NEW.request_key")
     + " END"),
)

# Adoption: each existing hold gets its row from the evidence it has; nothing is invented.
_ADOPT = (
    "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms,released_at_ms,release_kind) "
    "SELECT 'run_slot',a.project_id,a.handle_digest,"
    "CASE WHEN a.released_at_ms IS NOT NULL THEN 'released' ELSE 'held' END,a.claimed_at_ms,a.released_at_ms,"
    "CASE WHEN a.released_at_ms IS NOT NULL THEN 'completed' END FROM active_run_capacity a",
    "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms,released_at_ms,release_kind) "
    "SELECT 'binding',h.project_id,h.handle_digest,"
    "CASE WHEN h.binding_state IS 'pending' THEN 'held' ELSE 'released' END,h.created_at_ms,"
    "CASE WHEN h.binding_state IS 'pending' THEN NULL ELSE COALESCE(h.consumed_at_ms,h.created_at_ms) END,"
    "CASE WHEN h.binding_state IS 'pending' THEN NULL WHEN h.binding_state='terminal' THEN 'consumed' "
    "ELSE COALESCE(h.abandoned_reason,'abandoned') END FROM host_run_bindings h",
    "INSERT OR IGNORE INTO run_holds(kind,project_id,identity,state,taken_at_ms,released_at_ms,release_kind) "
    "SELECT 'probe',p.project_id,p.request_key,"
    "CASE WHEN p.state='committed' THEN 'released' ELSE 'held' END,p.created_at_ms,"
    "CASE WHEN p.state='committed' THEN p.created_at_ms END,"
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
    if not check_run_holds_coverage(connection):
        raise ValueError("run hold adoption is incomplete")


def adopt_run_holds(connection) -> None:
    """Give every account hold without a register row its row from its evidence (idempotent)."""
    for sql in _ADOPT:
        connection.execute(sql)


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
