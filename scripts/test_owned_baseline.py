"""TF-26: immutable translation of dependencies at the reviewed product/test boundary."""
from pathlib import Path
import subprocess
import test_framework as fw

SOURCE_REVISION = '5ab0a9d3c146980bf8c8a3fcbfa6a07883204c1c'
NAME = 'scripts/test-legacy-test-owned-v1.json'


def validate_document(document, active):
    errors = []
    if document.get('schema') != 1 or document.get('policy') != 'test-owned-v1' or document.get('source_revision') != SOURCE_REVISION:
        errors.append('wrong test-owned baseline provenance')
    for name, record in document.get('tests', {}).items():
        if name not in active or record.get('fingerprint') != active.get(name):
            errors.append(name + ': test-owned migration changed test identity or implementation')
        if not record.get('dependencies') or not record.get('dependency_values'):
            errors.append(name + ': unresolved test-owned dependency')
    return errors


def checked(root, admitted):
    path = root / NAME
    if not path.exists():
        return {}
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True, stderr=subprocess.DEVNULL)
    document = fw.decode_json(path.read_text())
    commits = git('log', '--reverse', '--format=%H', 'HEAD', '--', NAME).split()
    if not commits or document != fw.decode_json(git('show', commits[0] + ':' + NAME)):
        raise ValueError('immutable test-owned baseline changed or uncommitted')
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
