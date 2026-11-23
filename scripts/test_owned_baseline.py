"""TF-26: immutable translation of dependencies at the reviewed product/test boundary."""
from pathlib import Path
import subprocess
import test_framework as fw

SOURCE_REVISION = '5ab0a9d3c146980bf8c8a3fcbfa6a07883204c1c'
POLICIES = ('test-owned-v1', 'test-owned-v2')
NAME = 'scripts/test-legacy-' + POLICIES[-1] + '.json'


def validate_document(document, active):
    errors = []
    if document.get('schema') != 1 or document.get('policy') not in POLICIES or document.get('source_revision') != SOURCE_REVISION:
        errors.append('wrong test-owned baseline provenance')
    for name, record in document.get('tests', {}).items():
        if name not in active or record.get('fingerprint') != active.get(name):
            errors.append(name + ': test-owned migration changed test identity or implementation')
        if not record.get('dependencies') or not record.get('dependency_values'):
            errors.append(name + ': unresolved test-owned dependency')
    return errors


def checked_version(root, admitted, policy):
    name = "scripts/test-legacy-" + policy + ".json"
    path = root / name
    if not path.exists():
        return {}
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL)
    document = fw.decode_json(path.read_text())
    commits = git('log', '--reverse', '--format=%H', 'HEAD', '--', name).split()
    if not commits or document != fw.decode_json(git('show', commits[0] + ':' + name)):
        raise ValueError('immutable test-owned baseline changed or uncommitted')
    if document['policy'] != policy:
        raise ValueError('wrong test-owned baseline policy version')
    source = git('rev-parse', SOURCE_REVISION + '^{commit}').strip()
    if document['source_revision'] != source or document['source_tree'] != git('rev-parse', source + '^{tree}').strip():
        raise ValueError('wrong test-owned baseline source')
    import hashlib
    algorithm = git('show', document['algorithm_revision'] + ':scripts/test_framework.py')
    if hashlib.sha256(algorithm.encode()).hexdigest() != document['algorithm_sha256']:
        raise ValueError('wrong test-owned algorithm provenance')
    from generate_test_owned_baseline import admitted_at
    historical = admitted_at(root, source)
    if document['admitted'] != historical or set(document['tests']) != set(historical):
        raise ValueError('test-owned baseline changed historical test IDs')
    errors = validate_document(document, historical)
    if errors:
        raise ValueError('; '.join(errors))
    # Current shrink-only eligibility remains authoritative. A later, separately
    # admitted bookkeeping assertion gets its own translated record, not this one.
    return {name: row for name, row in document['tests'].items()
            if name in admitted and row['fingerprint'] == admitted[name]}


def checked(root, admitted):
    records = {}
    for policy in POLICIES:
        records.update(checked_version(root, admitted, policy))
    return records


def effective_records(bookkeeping, translated):
    """Translate historical bindings, but never roll back a later release proof."""
    result = {**bookkeeping, **translated}
    result.update({name: row for name, row in bookkeeping.items() if row.get('dependency_policy') == 'test-owned'})
    return result
