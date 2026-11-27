"""One stdlib expected-output helper: TF-14/TF-15. Test-only, never shipped."""
import difflib
from pathlib import Path


class GoldenUpdated(AssertionError):
    """An update is review evidence; a normal rerun must establish passing evidence."""


def _destination(root, name):
    name = Path(name)
    if name.is_absolute() or '..' in name.parts or not name.parts:
        raise ValueError('golden path must stay within its root')
    root = Path(root).resolve()
    target = root / name
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink():
            raise ValueError('golden path must not use a symlink')
    return target


def compare(*, root, name, actual, requirement, declared, known, environ, expected_transform=None):
    if not requirement or requirement not in declared or requirement not in known:
        raise ValueError('golden needs a known requirement declared by the test')
    if not isinstance(actual, str):
        raise TypeError('goldens are canonical UTF-8 text; encode binary values explicitly')
    target = _destination(root, name)
    provenance = _destination(root, str(name) + '.requirement')
    expected = target.read_bytes().decode('utf-8') if target.exists() else None
    if expected is not None and expected_transform is not None:
        expected = expected_transform(expected)
    if expected == actual and environ.get('UPDATE_GOLDEN') != '1':
        if not provenance.is_file() or provenance.read_text(encoding='utf-8') != requirement + '\n':
            raise AssertionError('golden provenance missing or mismatched for ' + requirement)
        return
    if environ.get('UPDATE_GOLDEN') == '1':
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(actual, encoding='utf-8', newline='')
        provenance.write_text(requirement + '\n', encoding='utf-8')
        raise GoldenUpdated(f'golden updated for {requirement}; review and rerun without UPDATE_GOLDEN')
    diff = ''.join(difflib.unified_diff((expected or '').splitlines(True), actual.splitlines(True),
                                      fromfile='expected', tofile='actual'))[:4000]
    raise AssertionError(('missing golden\n' if expected is None else 'golden differs\n') + diff)


def compare_json(*, path, actual, requirement, declared, known, environ, keys=None):
    """Canonical JSON comparison; a subset still uses the shared provenance gate."""
    import json
    def canonical(value):
        return json.dumps(value, sort_keys=True, indent=2) + "\n"
    def expected(text):
        value = json.loads(text)
        return canonical(value if keys is None else {key: value[key] for key in keys})
    compare(root=path.parent, name=path.name, actual=canonical(actual), requirement=requirement,
            declared=declared, known=known, environ=environ, expected_transform=expected)


def legacy_route_availability(route, task, policy):
    """124b: remove only additive availability and newly introduced false markers."""
    # Only top-level keys and the verifier dict are changed, so copy just those (the route is not mutated);
    # a deep copy of the whole route, availability audit included, dominated the routing golden modules.
    result = dict(route)
    result.pop("availability", None)
    if isinstance(result.get("verifier"), dict):
        result["verifier"] = dict(result["verifier"])
    old_scope = (policy.get("escalation", {}).get("scope") == "risk_banded_judgment"
                 and task["risk"] in {"low", "medium"})
    if not (old_scope and task.get("verification_for_family") in {"coding", "research", "planning"}):
        result.pop("independent", None)
    if result.get("verifier") is not None and not (old_scope and result["routed_family"] in {"coding", "research", "planning"}):
        result["verifier"].pop("independent", None)
    return result


def legacy_merged_record(record):
    """124d: remove exactly restriction_provenance, recomputing the legacy ID."""
    from codex_agents.provider_restrictions import _headroom_record
    material = {key: value for key, value in record.items()
                if key not in {"restriction_provenance", "record_id", "schema_version", "record_type"}}
    return _headroom_record(**material)
