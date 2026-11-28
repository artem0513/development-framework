"""The environment ase gives the processes it starts.

API keys of the judgment backends ase calls itself (not CLIs that bring their own login) reach
only that backend's pinned worker. Importing ase moves them out of this process's environment
into memory, so every child, including ones started without an explicit environment (Git and
its hooks or fsmonitor, project test and access commands, the other backends), starts without
them and project-controlled code cannot read them. The owner's own shell still holds them:
this narrows exposure, it is not a sandbox.
"""
import os

from .adapters.deepseek_worker import KEY_VARIABLE as DEEPSEEK_KEY_VARIABLE

# Backend name -> the API key variables only its own worker receives.
BACKEND_KEYS = {'deepseek': (DEEPSEEK_KEY_VARIABLE,)}
BACKEND_KEY_VARIABLES = tuple(name for names in BACKEND_KEYS.values() for name in names)
# Taken out of os.environ at import; only backend_environment hands them back to one worker.
_HELD = {name: os.environ.pop(name) for name in BACKEND_KEY_VARIABLES if name in os.environ}


def _key(name):
    # A value set after import (a test, an embedding host) wins over the one held at import.
    return os.environ.get(name) or _HELD.get(name)


def child_environment(base=None):
    """A copy of base (default: this process's environment) without any backend API key."""
    environment = dict(os.environ if base is None else base)
    for name in BACKEND_KEY_VARIABLES:
        environment.pop(name, None)
    return environment


def backend_key_present(backend):
    """Whether every API key the backend needs is available, without exposing any value."""
    names = BACKEND_KEYS.get(backend, ())
    return bool(names) and all(_key(name) for name in names)


def backend_environment(backend):
    """The environment for one pinned backend worker: its own API keys only, no other backend's."""
    environment = child_environment()
    for name in BACKEND_KEYS.get(backend, ()):
        if _key(name):
            environment[name] = _key(name)
    return environment
