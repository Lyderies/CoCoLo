"""RSF-S4 retained evidence for historical slots. Missing evidence never grants release."""
from pathlib import Path
import json
import os
import stat

from .contracts import ContractError
from .learning_contracts import content_digest, keyed_digest, validate_dispatch_intent

DEADLINE_REASON = 'waiting for the recorded deadline and one-hour grace'
OWNER_REASON = 'waiting for process evidence or owner confirmation'
MISSING_REASON = 'dispatch evidence is missing'


def dispatch_deadline(raw, digest, run_id, bridge_run_id):
    """SQLite scalar: only a validated, digest-matching intent for this run counts."""
    try:
        record = validate_dispatch_intent(json.loads(raw))
        if (content_digest(record) == digest and record['run_id'] == run_id
                and (bridge_run_id is None or record['bridge_run_id'] == bridge_run_id)):
            return record['deadline_at_ms']
    except (ContractError, ValueError, TypeError, RecursionError):
        pass
    return None


def deadline_sql(table):
    # Ambiguous duplicate intents provide no single recorded deadline.
    return ("(SELECT CASE WHEN COUNT(*)=1 THEN MAX(legacy_dispatch_deadline(d.content,d.content_digest,"
            f"{table}.run_id,{table}.bridge_run_id)) END FROM records d WHERE d.project_id={table}.project_id "
            "AND d.record_type='dispatch_intent' AND d.source='lifecycle' "
            f"AND json_extract(CAST(d.content AS TEXT),'$.run_id')={table}.run_id)")


def process_proof(state, project_id, handle_digest, run_id, bridge_run_id):
    """Read a private stopped instance's validated v1 journal, bound by the run handle.

    Instance locks are never reused by another writer. A live or unreadable lock,
    absent journal, incomplete spawn, or inconclusive process observation is no proof.
    Nothing here signals or launches a process or writes a journal.
    """
    from . import launch_journal as journal
    directory = Path(state.root) / 'projects' / project_id / 'launches'
    try:
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            return None
        names = sorted(directory.glob('brg_*.json'))
        matched = []
        for path in names:
            entry = journal._read_entry_or_none(path)
            if (entry is None or entry['version'] != 1
                    or keyed_digest(state.digest_key(), entry['run_handle']) != handle_digest
                    or (bridge_run_id is not None and entry['bridge_run_id'] != bridge_run_id)):
                continue
            matched.append((path, entry))
        if len(matched) != 1:
            return None
        path, entry = matched[0]
        lock = directory / (entry['instance'] + '.lock')
        try:
            descriptor = os.open(lock, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            descriptor = None  # A released instance removes its unique lock; it can never write again.
        except OSError:
            return None
        if descriptor is not None:
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                    return None
                journal.fcntl.flock(descriptor, journal.fcntl.LOCK_SH | journal.fcntl.LOCK_NB)
            except OSError:
                return None
            finally:
                os.close(descriptor)
        if journal._read_entry_or_none(path) != entry:
            return None
        observed = journal.tree_state({**entry, 'marker_path': str(path.with_suffix('.done'))})
        if observed != 'gone':
            return None
        return {'kind': 'unsent' if entry['state'] == 'claimed' else 'process_gone',
                'run_id': run_id, 'journal': entry, 'journal_digest': content_digest(entry), 'observation': observed}
    except (OSError, ValueError, TypeError):
        return None
