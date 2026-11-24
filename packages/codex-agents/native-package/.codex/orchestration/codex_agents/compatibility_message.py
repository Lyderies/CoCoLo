"""Plain update advice; executable paths come only from the local install binding."""
from pathlib import Path
import json
import re
import shlex


def _local_install(project_root, orchestration_root):
    # Lazy imports: host_commands itself imports the collector identity.
    from . import host_commands

    orchestration = Path(orchestration_root) if orchestration_root is not None else Path(__file__).resolve().parent.parent
    try:
        binding = host_commands.runtime_binding(orchestration)
        if binding is None and project_root is None and orchestration.parts[-2:] == ('.codex', 'orchestration'):
            project_root = orchestration.parent.parent
        if binding is None and project_root is not None:
            copy = host_commands.runtime_copy_root(Path(project_root))
            binding = host_commands.runtime_binding(copy / '.codex/orchestration')
        if binding is not None:
            root = Path(binding['project_root'])
            if project_root is None or root == Path(project_root).resolve():
                return Path(binding['clone_root']), root
    except (host_commands.HostCommandError, OSError, ValueError):
        pass
    return None


def compatibility_message(version, *, project_root=None, orchestration_root=None,
                          state_name='shared learning state'):
    """Build the one terminal command for all three compatibility refusals.

    A collector may report its version, but never supplies executable text or
    a clone/project path. Missing local records produce guidance, not a guessed command.
    """
    known = type(version) is str and re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version) is not None
    holder = f'CoCoLo version {version}' if known else 'Another CoCoLo version (number unavailable)'
    lead = f'{holder} holds the {state_name}. '
    install = _local_install(project_root, orchestration_root)
    if install is None:
        return (lead + 'The local install record is unavailable. In a plain terminal, open your CoCoLo clone, '
                'run ./setup.sh with this project\'s path, then choose Configure, then '
                '"Update the install and walk through every step".')
    clone, project = install
    command = f'cd {shlex.quote(str(clone))} && ./setup.sh {shlex.quote(str(project))}'
    return (lead + f'In a plain terminal, run:\n{command}\nThen choose Configure, then '
            '"Update the install and walk through every step".')


def project_service_version(state_root, project_root):
    """Name a live service from its local hint and the installing clone's release catalogue."""
    from .collector_paths import CollectorPaths, StateError
    from .project_host_client import _hinted_host

    try:
        install = _local_install(project_root, None)
        if install is None:
            return None
        hinted = _hinted_host(CollectorPaths(state_root), Path(project_root).resolve(), any_runtime=True)
        if hinted is None or hinted[2] is not True:
            return None
        path = install[0] / 'packages/codex-agents/runtime-releases.json'
        with path.open('rb') as handle:
            raw = handle.read(262_145)
        if len(raw) > 262_144:
            return None
        releases = json.loads(raw)
        if type(releases) is not dict:
            return None
        return next((version for version, digest in releases.items() if digest == hinted[0].runtime_digest), None)
    except (OSError, ValueError, StateError):
        return None
