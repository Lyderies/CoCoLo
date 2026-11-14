"""One project host as its own process, with the bridge tests' trusted fake clients.

`tests/test_lost_run_settlement.py` starts this so it can SIGKILL a real host
process while its child runs. It applies the same patches `BridgeTestCase`
applies in the test process (scratch folder, managed-settings paths, the fake
clients as the trusted search path), then runs the product's `serve_project`.
Usage: python -B crash_host_fixture.py '<json config>' from packages/codex-agents.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, '.')
import tests.support  # noqa: E402,F401 -- the source package import path
from unittest import mock  # noqa: E402

from tests.learning_support import trusted_tools  # noqa: E402
from codex_agents import bridge, claude_host  # noqa: E402
from codex_agents.project_service import serve_project  # noqa: E402


def main(config):
    base = Path(config['base'])
    for name, value in (
        ('WORKSPACE_PARENT', str(base / 'dispatch')),
        ('temp_roots', lambda: set()),
        ('MANAGED_SETTINGS_PATHS', {'darwin': (str(base / 'managed-settings.json'),),
                                    'linux': (str(base / 'managed-settings.json'),)}),
        ('MANAGED_PREFERENCE_PATHS', {
            'darwin': (str(base / 'managed-preferences' / '{user}' / 'claude.plist'),),
            'linux': (str(base / 'managed-preferences' / '{user}' / 'claude.plist'),)}),
        ('CODEX_MANAGED_PATHS', {
            'darwin': (str(base / 'codex-managed' / '{user}' / 'managed_config.toml'),),
            'linux': (str(base / 'codex-managed' / '{user}' / 'managed_config.toml'),)}),
        ('CODEX_SYSTEM_DIRECTORY', str(base / 'etc-codex')),
    ):
        mock.patch.object(bridge, name, value).start()
    mock.patch.object(claude_host, 'USER_SETTINGS_DIRECTORY', str(base / 'user-claude')).start()
    with trusted_tools(base / 'tools'):
        report = serve_project(Path(config['state']), Path(config['project']), bundle_root=Path(config['bundle']))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main(json.loads(sys.argv[1]))
