"""Pinned test-only Hypothesis profiles (TF-17); I/O timeouts belong to the runner."""
import os

from hypothesis import settings

for _name, _examples in (('dev', 20), ('ci', 100), ('soak', 1000)):
    settings.register_profile(_name, max_examples=_examples, derandomize=_name == 'ci', deadline=None)


def settings_for(name):
    if name not in ('dev', 'ci', 'soak'):
        raise ValueError(f'unknown test profile: {name}')
    return settings.get_profile(name)


def activate(name=None):
    name = name or os.environ.get('COCOLO_TEST_PROFILE', 'ci')
    settings_for(name)
    settings.load_profile(name)
