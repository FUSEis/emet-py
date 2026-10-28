"""The turn() block: who and what a turn belongs to, carried in OpenTelemetry context."""

from __future__ import annotations

import functools
import inspect
import itertools
from collections.abc import Callable
from contextvars import Token
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol, TypeVar, cast

from opentelemetry import context as otel_context

# A plain context key, not baggage: baggage is propagated to downstream services, this never leaves the process.
TURN_KEY = otel_context.create_key("emet.turn")

F = TypeVar("F", bound=Callable[..., Any])

_ids = itertools.count(1)


@dataclass(frozen=True)
class TurnInfo:
    """The values a turn() block was given, plus an id unique within the process."""

    id: int
    tenant_ref: str
    conversation_id: str
    user: str | None


def current_turn(ctx: otel_context.Context | None = None) -> TurnInfo | None:
    """Returns the turn the given context, or the current one, belongs to."""
    value = otel_context.get_value(TURN_KEY, ctx)
    return value if isinstance(value, TurnInfo) else None


def _required(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"emet_sdk.turn() needs {what}")
    return value.strip()


def _optional(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("emet_sdk.turn() takes the user as a string, such as their email")
    return value.strip() or None


class turn:
    """Marks one turn of your agent for Emet. Only spans started inside it are sent.

    Use it as a context manager around the agent call, or as a decorator. Take every value from your server's own
    auth and storage, never from the request body.
    """

    def __init__(self, tenant_ref: str, conversation_id: str, user: str | None = None) -> None:
        self._values = (
            _required(tenant_ref, "a tenant_ref: the External ID of the Team in Emet"),
            _required(conversation_id, "a conversation_id: your app's own thread or session id"),
            _optional(user),
        )
        self._entered: list[tuple[TurnInfo, Token[otel_context.Context]]] = []

    def __enter__(self) -> TurnInfo:
        info = TurnInfo(next(_ids), *self._values)
        self._entered.append((info, otel_context.attach(otel_context.set_value(TURN_KEY, info))))
        for listener in _listeners:
            listener.turn_opened(info)
        return info

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        info, token = self._entered.pop()
        otel_context.detach(token)
        for listener in _listeners:
            listener.turn_closed(info)

    def __call__(self, func: F) -> F:
        values = self._values
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                with turn(*values):
                    return await func(*args, **kwargs)

            return cast(F, async_wrapper)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with turn(*values):
                return func(*args, **kwargs)

        return cast(F, wrapper)


class TurnListener(Protocol):
    def turn_opened(self, info: TurnInfo) -> None: ...

    def turn_closed(self, info: TurnInfo) -> None: ...


# The processor registers here, so it knows when a block closes even if no span is still open.
_listeners: list[TurnListener] = []
