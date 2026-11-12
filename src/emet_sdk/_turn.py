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
from uuid import UUID

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


# An id from the app's own database may be an integer or a UUID
Id = str | int | UUID


def _text(value: object) -> str:
    # Only the types Id names; any other object, such as a whole model, counts as missing so the turn is skipped
    try:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            # int() first: an IntEnum's str() is its name on Python 3.10
            return str(int(value))
        if isinstance(value, UUID):
            return str(value)
    except Exception:  # noqa: BLE001  (an integer past Python's digit limit, or a subclass that raises)
        return ""
    return ""


class turn:
    """Marks one turn of your agent for Emet. Only spans started inside it are sent.

    Use it as a context manager around the agent call, or as a decorator. Take every value from your server's own
    auth and storage, never from the request body. An empty tenant_ref or conversation_id never raises: the agent
    runs as usual and the turn is reported as skipped.
    """

    def __init__(self, tenant_ref: Id, conversation_id: Id, user: Id | None = None) -> None:
        self._values = (_text(tenant_ref), _text(conversation_id), _text(user) or None)
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
