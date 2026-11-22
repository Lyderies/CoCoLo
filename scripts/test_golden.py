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


def compare(*, root, name, actual, requirement, declared, known, environ):
    if not requirement or requirement not in declared or requirement not in known:
        raise ValueError('golden needs a known requirement declared by the test')
    if not isinstance(actual, str):
        raise TypeError('goldens are canonical UTF-8 text; encode binary values explicitly')
    target = _destination(root, name)
    provenance = _destination(root, str(name) + '.requirement')
    expected = target.read_bytes().decode('utf-8') if target.exists() else None
    if expected == actual:
        if environ.get('UPDATE_GOLDEN') == '1':
            raise GoldenUpdated(f'golden update mode for {requirement}; rerun without UPDATE_GOLDEN')
        return
    if environ.get('UPDATE_GOLDEN') == '1':
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(actual, encoding='utf-8', newline='')
        provenance.write_text(requirement + '\n', encoding='utf-8')
        raise GoldenUpdated(f'golden updated for {requirement}; review and rerun without UPDATE_GOLDEN')
    diff = ''.join(difflib.unified_diff((expected or '').splitlines(True), actual.splitlines(True),
                                      fromfile='expected', tofile='actual'))[:4000]
    raise AssertionError(('missing golden\n' if expected is None else 'golden differs\n') + diff)
