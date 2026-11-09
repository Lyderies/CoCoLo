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
