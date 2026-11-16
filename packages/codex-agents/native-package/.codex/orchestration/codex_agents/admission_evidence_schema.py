"""Admission evidence index: the few content fields admission selects history by.

Admission reads only the history that can still change a decision. Three of its
rules select old records by a field inside their content: the account, the
latest window reset of a headroom record, and whether a decision admitted a
Claude run. The account owner's capture never parses record content, so a
trigger copies those fields into this table as each record is written, by any
path (append, restore, import, upgrade), and the capture reads them as columns.
A record's index row is removed with it. Content is not trusted here either:
a field that is missing or of another type is stored as NULL (or 0), and the
captured rows are still decoded and checked off-owner.
"""

ADMISSION_EVIDENCE_TYPES = ("provider_billing", "provider_headroom", "admission_decision", "probe_admission")
_TYPES = ",".join(f"'{kind}'" for kind in ADMISSION_EVIDENCE_TYPES)


def _fields(content: str) -> str:
    """The indexed values of one record, from its content expression; never raises on bad JSON."""
    text = f"CAST({content} AS TEXT)"

    def when_valid(expression: str, otherwise: str = "NULL") -> str:
        # CASE is evaluated lazily, so no JSON function sees malformed content.
        return f"CASE WHEN json_valid({text}) THEN {expression} ELSE {otherwise} END"

    def text_field(name: str) -> str:
        return when_valid(f"CASE WHEN json_type({text},'$.{name}')='text' THEN json_extract({text},'$.{name}') END")

    live_until = when_valid(
        f"CASE WHEN json_type({text},'$.windows')='array' THEN (SELECT MAX(json_extract(w.value,'$.resets_at')) "
        f"FROM json_each({text},'$.windows') w WHERE w.type='object' "
        f"AND json_type(w.value,'$.resets_at')='integer') END")
    claude_pass = when_valid(
        f"CASE WHEN json_type({text},'$.provider')='text' AND json_extract({text},'$.provider')='claude' "
        f"AND json_type({text},'$.decision')='text' AND json_extract({text},'$.decision')='pass' "
        f"AND json_type({text},'$.account_fingerprint')='text' THEN 1 ELSE 0 END", "0")
    return f"{text_field('provider')},{text_field('account_fingerprint')},{live_until},{claude_pass}"


ADMISSION_EVIDENCE_SCHEMA = '''CREATE TABLE admission_evidence(
project_id TEXT NOT NULL,record_id TEXT NOT NULL,record_type TEXT NOT NULL,source TEXT NOT NULL,
observed_at_ms INTEGER NOT NULL,provider TEXT,account_fingerprint TEXT,live_until_ms INTEGER,
claude_pass INTEGER NOT NULL CHECK(claude_pass IN (0,1)),
PRIMARY KEY(project_id,record_id),
FOREIGN KEY(project_id,record_id) REFERENCES records(project_id,record_id) ON DELETE CASCADE)'''

ADMISSION_EVIDENCE_TRIGGER = (
    "CREATE TRIGGER admission_evidence_insert AFTER INSERT ON records "
    f"WHEN NEW.record_type IN ({_TYPES}) BEGIN "
    "INSERT INTO admission_evidence(project_id,record_id,record_type,source,observed_at_ms,"
    "provider,account_fingerprint,live_until_ms,claude_pass) VALUES(NEW.project_id,NEW.record_id,"
    f"NEW.record_type,NEW.source,NEW.observed_at_ms,{_fields('NEW.content')}); END"
)

ADMISSION_EVIDENCE_OBJECTS = (
    ("table", "admission_evidence", ADMISSION_EVIDENCE_SCHEMA),
    ("index", "admission_evidence_live",
     "CREATE INDEX admission_evidence_live ON admission_evidence(live_until_ms) WHERE live_until_ms IS NOT NULL"),
    # The first passing Claude decision per account: a seek per account, not a scan.
    ("index", "admission_evidence_claude_pass",
     "CREATE INDEX admission_evidence_claude_pass ON admission_evidence"
     "(account_fingerprint,observed_at_ms,project_id,record_id) WHERE claude_pass=1"),
    ("index", "admission_evidence_account",
     "CREATE INDEX admission_evidence_account ON admission_evidence"
     "(provider,source,record_type,account_fingerprint,observed_at_ms) WHERE account_fingerprint IS NOT NULL"),
    ("trigger", "admission_evidence_insert", ADMISSION_EVIDENCE_TRIGGER),
)

_BACKFILL = (
    "INSERT INTO admission_evidence(project_id,record_id,record_type,source,observed_at_ms,"
    "provider,account_fingerprint,live_until_ms,claude_pass) "
    f"SELECT r.project_id,r.record_id,r.record_type,r.source,r.observed_at_ms,{_fields('r.content')} "
    f"FROM records r WHERE r.record_type IN ({_TYPES})"
)


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


def assert_admission_evidence_schema(connection) -> None:
    for kind, name, sql in ADMISSION_EVIDENCE_OBJECTS:
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
        if row is None or type(row[0]) is not str or _normalized(row[0]) != _normalized(sql):
            raise ValueError("admission evidence schema is incompatible")


def install_admission_evidence_schema(connection) -> None:
    """Create the index and fill it from every existing record, in the owner's transaction."""
    if not connection.in_transaction:
        raise ValueError("admission evidence migration requires an owner transaction")
    for _, _, sql in ADMISSION_EVIDENCE_OBJECTS:
        connection.execute(sql)
    connection.execute(_BACKFILL)
    assert_admission_evidence_schema(connection)
    if not check_admission_evidence_coverage(connection):
        raise ValueError("admission evidence backfill is incomplete")


def check_admission_evidence_coverage(connection) -> bool:
    """Every evidence record has exactly its own index row, with every field as its content gives it,
    and no index row is left over."""
    return connection.execute(
        "SELECT 1 FROM records r LEFT JOIN admission_evidence e "
        "ON e.project_id=r.project_id AND e.record_id=r.record_id "
        f"WHERE r.record_type IN ({_TYPES}) AND (e.record_id IS NULL OR e.record_type<>r.record_type "
        "OR e.source<>r.source OR e.observed_at_ms<>r.observed_at_ms "
        f"OR (e.provider,e.account_fingerprint,e.live_until_ms,e.claude_pass) IS NOT ({_fields('r.content')})) "
        "UNION ALL SELECT 1 FROM admission_evidence e LEFT JOIN records r "
        "ON r.project_id=e.project_id AND r.record_id=e.record_id "
        f"WHERE r.record_id IS NULL OR r.record_type NOT IN ({_TYPES}) LIMIT 1").fetchone() is None
