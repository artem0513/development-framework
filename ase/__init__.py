"""Autonomous Software Engineering: evidence before completion."""

__version__ = "0.1.0"

# Before anything starts a child process: backend API keys leave os.environ (see child_environment).
from . import child_environment as _child_environment  # noqa: E402,F401
