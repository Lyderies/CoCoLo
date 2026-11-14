"""Installed account-owner limits and durable reservation schema.

Only the account persistence owner uses this schema. Rows and policy digests are
metadata, never caller authentication, launch permission or cleanup evidence.
No timer, lost connection or model-reported terminal state releases a reservation.
A claimed run whose project service was lost stops counting against the limits
once a later service of the project settles it on kernel evidence that nothing
of it still runs, or, as the last resort, one hour after its hard deadline (the
detached watchdog has enforced that deadline). Its row stays unreleased either way.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib


@dataclass(frozen=True)
class ActiveRunLimits:
    machine: int
    account: int

    def __post_init__(self):
        if any(type(value) is not int or not 1 <= value <= 64 for value in (self.machine, self.account)):
            raise ValueError("invalid installed active-run limits")

    @property
    def digest(self):
        self.__post_init__()
        return hashlib.sha256(f"active-run-limits-v1:{self.machine}:{self.account}".encode("ascii")).hexdigest()


# Conservative policy for the uninstalled two-project candidate. No request,
# model result or transport field can replace or raise these installed limits.
INSTALLED_ACTIVE_RUN_LIMITS = ActiveRunLimits(machine=2, account=2)

CAPACITY_PARENT_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS active_capacity_parent
ON launch_claims(project_id,handle_digest,run_id)
"""

CAPACITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_run_capacity (
    project_id TEXT NOT NULL,
    handle_digest TEXT NOT NULL,
    run_id TEXT NOT NULL,
    bridge_run_id TEXT,
    kind TEXT NOT NULL CHECK(kind IN ('claimed','legacy_unknown')),
    provider TEXT CHECK(provider IN ('codex','claude')),
    account_fingerprint TEXT CHECK(account_fingerprint IS NULL OR
        (typeof(account_fingerprint)='text' AND length(account_fingerprint)=64
        AND account_fingerprint NOT GLOB '*[^0-9a-f]*')),
    runtime_digest TEXT,
    policy_digest TEXT,
    claimed_at_ms INTEGER NOT NULL CHECK(typeof(claimed_at_ms)='integer' AND claimed_at_ms>=0),
    deadline_at_ms INTEGER,
    clock_reference_ns INTEGER,
    execution_deadline_ns INTEGER,
    clock_owner_digest TEXT,
    released_at_ms INTEGER,
    release_digest TEXT,
    PRIMARY KEY(project_id,handle_digest),
    CHECK((kind='claimed' AND provider IS NOT NULL AND bridge_run_id IS NOT NULL
        AND typeof(runtime_digest)='text' AND length(runtime_digest)=64
        AND runtime_digest NOT GLOB '*[^0-9a-f]*'
        AND typeof(policy_digest)='text' AND length(policy_digest)=64
        AND policy_digest NOT GLOB '*[^0-9a-f]*'
        AND typeof(deadline_at_ms)='integer' AND deadline_at_ms>claimed_at_ms
        AND typeof(clock_reference_ns)='integer' AND clock_reference_ns>=0
        AND typeof(execution_deadline_ns)='integer' AND execution_deadline_ns>clock_reference_ns
        AND execution_deadline_ns-clock_reference_ns=(deadline_at_ms-claimed_at_ms)*1000000
        AND typeof(clock_owner_digest)='text' AND length(clock_owner_digest)=64
        AND clock_owner_digest NOT GLOB '*[^0-9a-f]*')
        OR (kind='legacy_unknown' AND provider IS NULL AND account_fingerprint IS NULL
        AND runtime_digest IS NULL AND policy_digest IS NULL AND deadline_at_ms IS NULL
        AND clock_reference_ns IS NULL AND execution_deadline_ns IS NULL AND clock_owner_digest IS NULL)),
    CHECK((released_at_ms IS NULL AND release_digest IS NULL)
        OR (typeof(released_at_ms)='integer' AND released_at_ms>=claimed_at_ms
        AND typeof(release_digest)='text' AND length(release_digest)=64
        AND release_digest NOT GLOB '*[^0-9a-f]*')),
    FOREIGN KEY(project_id,handle_digest,run_id)
        REFERENCES launch_claims(project_id,handle_digest,run_id),
    FOREIGN KEY(project_id,handle_digest,run_id,bridge_run_id)
        REFERENCES launch_claims(project_id,handle_digest,run_id,bridge_run_id)
)
"""

CAPACITY_MACHINE_SCHEMA = """
CREATE INDEX IF NOT EXISTS active_capacity_machine
ON active_run_capacity(claimed_at_ms) WHERE released_at_ms IS NULL
"""

CAPACITY_ACCOUNT_SCHEMA = """
CREATE INDEX IF NOT EXISTS active_capacity_account
ON active_run_capacity(provider,account_fingerprint) WHERE released_at_ms IS NULL
"""

CAPACITY_PROJECT_SCHEMA = """
CREATE INDEX IF NOT EXISTS active_capacity_project
ON active_run_capacity(project_id) WHERE released_at_ms IS NULL
"""

CAPACITY_COMPLETIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS capacity_completions (
    project_id TEXT NOT NULL,
    handle_digest TEXT NOT NULL,
    run_id TEXT NOT NULL,
    bridge_run_id TEXT NOT NULL,
    terminal_record_id TEXT NOT NULL CHECK(typeof(terminal_record_id)='text'
        AND length(terminal_record_id) BETWEEN 1 AND 160),
    terminal_content_digest TEXT NOT NULL CHECK(typeof(terminal_content_digest)='text'
        AND length(terminal_content_digest)=64 AND terminal_content_digest NOT GLOB '*[^0-9a-f]*'),
    released_at_ms INTEGER,
    receipt_digest TEXT,
    PRIMARY KEY(project_id,handle_digest),
    UNIQUE(project_id,terminal_record_id),
    CHECK((released_at_ms IS NULL AND receipt_digest IS NULL)
        OR (typeof(released_at_ms)='integer' AND released_at_ms>=0
        AND typeof(receipt_digest)='text' AND length(receipt_digest)=64
        AND receipt_digest NOT GLOB '*[^0-9a-f]*')),
    FOREIGN KEY(project_id,handle_digest) REFERENCES active_run_capacity(project_id,handle_digest) ON DELETE CASCADE,
    FOREIGN KEY(project_id,handle_digest,run_id,bridge_run_id)
        REFERENCES launch_claims(project_id,handle_digest,run_id,bridge_run_id)
)
"""

CAPACITY_EXECUTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS capacity_executions (
    project_id TEXT NOT NULL,
    handle_digest TEXT NOT NULL,
    run_id TEXT NOT NULL,
    bridge_run_id TEXT NOT NULL,
    owner_digest TEXT NOT NULL CHECK(typeof(owner_digest)='text' AND length(owner_digest)=64
        AND owner_digest NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(project_id,handle_digest),
    FOREIGN KEY(project_id,handle_digest) REFERENCES active_run_capacity(project_id,handle_digest) ON DELETE CASCADE,
    FOREIGN KEY(project_id,handle_digest,run_id,bridge_run_id)
        REFERENCES launch_claims(project_id,handle_digest,run_id,bridge_run_id)
)
"""

CAPACITY_DELETE_GUARD_SCHEMA = """
CREATE TRIGGER IF NOT EXISTS preserve_active_capacity_on_project_deletion
BEFORE UPDATE OF status ON projects
WHEN NEW.status='deleted' AND OLD.status<>'deleted'
BEGIN
    SELECT CASE WHEN EXISTS(SELECT 1 FROM active_run_capacity
        WHERE project_id=NEW.project_id AND released_at_ms IS NULL)
        THEN RAISE(ABORT,'active launch capacity remains reserved') END;
    DELETE FROM active_run_capacity WHERE project_id=NEW.project_id;
END
"""

CAPACITY_OBJECTS = (
    ("index", "active_capacity_parent", CAPACITY_PARENT_SCHEMA),
    ("table", "active_run_capacity", CAPACITY_SCHEMA),
    ("index", "active_capacity_machine", CAPACITY_MACHINE_SCHEMA),
    ("index", "active_capacity_account", CAPACITY_ACCOUNT_SCHEMA),
    ("index", "active_capacity_project", CAPACITY_PROJECT_SCHEMA),
    ("table", "capacity_completions", CAPACITY_COMPLETIONS_SCHEMA),
    ("table", "capacity_executions", CAPACITY_EXECUTIONS_SCHEMA),
    ("trigger", "preserve_active_capacity_on_project_deletion", CAPACITY_DELETE_GUARD_SCHEMA),
)


# A claimed run whose project host crashed never settles. Well past its hard
# deadline its child cannot still be running, so it stops counting against the
# limits (and no longer holds back a drained migration) while its row stays
# unreleased. "Well past" must hold on both clocks: the wall clock alone can
# be set forward while the child, whose own deadline is monotonic, is alive.
# This is the last resort: a later service of the project settles such a run
# much sooner, once kernel evidence shows that nothing of it still runs
# (`account_lost_run`), and from then on it does not count either.
CRASHED_CLAIM_GRACE_MS = 3_600_000
# A run settled that way has this fixed abandonment reason on its host binding.
LOST_RUN_REASON = 'service_lost'


def crashed_claim(alias, now_ms, monotonic_ns):
    """SQL for "this unreleased claim's run cannot still be alive", with its parameters.

    True when the run's project service settled it as lost (its binding is
    abandoned with `service_lost`), or when the wall clock is more than the
    grace past the claim's deadline AND the monotonic clock is too (or has
    restarted below the claim's reference: the machine rebooted since, and no
    child survives that). A claim without a monotonic deadline (an earlier
    version's) never qualifies by time.
    """
    column = (alias + ".") if alias else ""
    table = alias or "active_run_capacity"
    return (
        f"(({column}deadline_at_ms IS NOT NULL AND {column}deadline_at_ms<? "
        f"AND {column}execution_deadline_ns IS NOT NULL "
        f"AND ({column}execution_deadline_ns<=? OR {column}clock_reference_ns>?)) "
        f"OR EXISTS(SELECT 1 FROM host_run_bindings lost WHERE lost.project_id={table}.project_id "
        f"AND lost.handle_digest={table}.handle_digest AND lost.binding_state='abandoned' "
        f"AND lost.abandoned_reason='{LOST_RUN_REASON}'))",
        (now_ms - CRASHED_CLAIM_GRACE_MS, monotonic_ns - CRASHED_CLAIM_GRACE_MS * 1_000_000, monotonic_ns),
    )
