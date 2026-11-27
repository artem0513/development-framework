"""Structural ports exposed to functional workflow services. No service locator."""
from typing import Any, Callable, Protocol


class CheckContext(Protocol):
    _authority_binding: Callable[..., Any]
    _call: Callable[..., Any]
    _execute_command: Callable[..., Any]
    _observe_action: Callable[..., Any]
    _save: Callable[..., Any]
    _stage_permits_command: Callable[..., Any]
    store: Any
    tasks: Any

class PacketContext(Protocol):
    _stage_history: Callable[..., Any]
    store: Any
    tasks: Any

class VerificationContext(Protocol):
    _authority_binding: Callable[..., Any]
    _call: Callable[..., Any]
    _request: Callable[..., Any]
    _runner: Callable[..., Any]
    _save: Callable[..., Any]
    _stop_requested: Callable[..., Any]
    store: Any
    tasks: Any

class ActionContext(Protocol):
    _access_client: Callable[..., Any]
    _access_stage: Callable[..., Any]
    _authority_binding: Callable[..., Any]
    _available: Callable[..., Any]
    _call: Callable[..., Any]
    _command_authority: Callable[..., Any]
    _command_control: Callable[..., Any]
    _declared_access: Callable[..., Any]
    _execute_command: Callable[..., Any]
    _observe_action: Callable[..., Any]
    _ownership: Callable[..., Any]
    _prerequisite_stages: Callable[..., Any]
    _revalidate_prerequisites: Callable[..., Any]
    _stage_authority: Callable[..., Any]
    store: Any
    tasks: Any
