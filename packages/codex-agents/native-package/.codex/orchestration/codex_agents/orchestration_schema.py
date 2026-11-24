"""Fixed project lifecycle schema; persistence owners only."""

ORCHESTRATION_SCHEMA = """
CREATE TABLE project_orchestrations(
 project_id TEXT NOT NULL,orchestration_handle TEXT NOT NULL,scope_digest TEXT NOT NULL,
 sequence INTEGER NOT NULL,checkpoint_digest TEXT NOT NULL,
 PRIMARY KEY(project_id,orchestration_handle),
 FOREIGN KEY(project_id) REFERENCES history_identity(project_id));
CREATE TABLE project_orchestration_checkpoints(
 project_id TEXT NOT NULL,orchestration_handle TEXT NOT NULL,sequence INTEGER NOT NULL,
 content BLOB NOT NULL,checkpoint_digest TEXT NOT NULL,
 PRIMARY KEY(project_id,orchestration_handle,sequence),
 FOREIGN KEY(project_id,orchestration_handle) REFERENCES project_orchestrations(project_id,orchestration_handle));
CREATE TABLE project_orchestration_requests(
 project_id TEXT NOT NULL,request_key TEXT NOT NULL,request_digest TEXT NOT NULL,
 orchestration_handle TEXT NOT NULL,sequence INTEGER NOT NULL,
 PRIMARY KEY(project_id,request_key),
 FOREIGN KEY(project_id,orchestration_handle,sequence)
 REFERENCES project_orchestration_checkpoints(project_id,orchestration_handle,sequence));
"""
ORCHESTRATION_COLUMNS = {
    'project_orchestrations': ('project_id', 'orchestration_handle', 'scope_digest', 'sequence', 'checkpoint_digest'),
    'project_orchestration_checkpoints': ('project_id', 'orchestration_handle', 'sequence', 'content', 'checkpoint_digest'),
    'project_orchestration_requests': ('project_id', 'request_key', 'request_digest', 'orchestration_handle', 'sequence'),
}

# RS-B B6 (decisions 131, 185, 187), step 20 -> 21: each orchestration's state as an indexed column, written with its
# checkpoint pointer, so counting open or closed ones reads no checkpoint. Added with ALTER TABLE ... ADD COLUMN, which
# SQLite writes into the table's text after its last column: the fresh format is that same text.
ORCHESTRATION_STATE_COLUMN = "state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed'))"
_ORCHESTRATION_LAST_COLUMN = 'sequence INTEGER NOT NULL,checkpoint_digest TEXT NOT NULL,'
assert ORCHESTRATION_SCHEMA.count(_ORCHESTRATION_LAST_COLUMN) == 1


def with_orchestration_state(schema: str) -> str:
    """A format's schema text with the step-21 column, exactly as SQLite stores it after the ALTER."""
    if schema.count(_ORCHESTRATION_LAST_COLUMN) != 1:
        raise ValueError('not the orchestration table')
    return schema.replace(_ORCHESTRATION_LAST_COLUMN,
                          _ORCHESTRATION_LAST_COLUMN[:-1] + ', ' + ORCHESTRATION_STATE_COLUMN + ',')


ORCHESTRATION_STATE_INDEX_SCHEMA = """
CREATE INDEX project_orchestrations_state ON project_orchestrations(project_id,state);
"""
ORCHESTRATION_STATE_COLUMNS = {
    'project_orchestrations': (*ORCHESTRATION_COLUMNS['project_orchestrations'], 'state'),
}
