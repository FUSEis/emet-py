"""Records the Pydantic AI fixtures from real runs on the offline test model.

Run `uv run python -m tests.record_fixtures`, then review the diff of tests/fixtures before committing.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import Agent, BinaryContent, InstrumentationSettings
from pydantic_ai.capabilities import Instrumentation
from pydantic_ai.models.test import TestModel

from tests.fixtures import dump_spans, write

RESOURCE = {
    "service.name": "support-agent",
    "service.version": "1.4.0",
    "deployment.environment.name": "development",
    "telemetry.sdk.name": "opentelemetry",
    "telemetry.sdk.language": "python",
}
# Values that change on every run
STABLE = {
    "gen_ai.agent.call.id": "agent-call-1",
    "gen_ai.conversation.id": "01a17f7e-d244-7192-8444-d31616de69a6",
    "exception.stacktrace": 'Traceback (most recent call last):\n  File "/srv/app/tools.py", line 12, in broken\n',
}
# Test vectors shaped like real secrets; none is a working credential
FAKE_ANTHROPIC_KEY = "sk-ant-" + "a1" * 12
FAKE_DB_URL = "postgres://app:hunter2@db.internal:5432/spend"


def make_agent(provider: TracerProvider) -> Agent[None, str]:
    agent = Agent(
        TestModel(),
        system_prompt="You answer spend questions.",
        instructions="Be brief.",
        capabilities=[Instrumentation(InstrumentationSettings(tracer_provider=provider))],
    )

    @agent.tool_plain
    def get_spend(channel: str) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        """Returns spend for a channel."""
        return {"spend": 48210.55, "currency": "USD", "channel": channel}

    @agent.tool_plain
    def get_daily(channel: str) -> list[int]:  # pyright: ignore[reportUnusedFunction]
        """Returns daily spend, a non-object result."""
        return [6800, 7100, 6950]

    @agent.tool_plain
    def mark_reviewed(channel: str) -> None:  # pyright: ignore[reportUnusedFunction]
        """Returns nothing."""

    @agent.tool_plain
    def read_config() -> str:  # pyright: ignore[reportUnusedFunction]
        """Returns text holding secrets."""
        return f"ANTHROPIC_API_KEY={FAKE_ANTHROPIC_KEY}\nDATABASE_URL={FAKE_DB_URL}"

    return agent


def make_failing_agent(provider: TracerProvider) -> Agent[None, str]:
    agent = Agent(TestModel(), capabilities=[Instrumentation(InstrumentationSettings(tracer_provider=provider))])

    @agent.tool_plain
    def broken(channel: str) -> str:  # pyright: ignore[reportUnusedFunction]
        """Always fails."""
        raise ConnectionError(f"could not reach {FAKE_DB_URL}")

    return agent


def stable(spans: tuple[ReadableSpan, ...]) -> list[ReadableSpan]:
    out: list[ReadableSpan] = []
    for span in spans:
        attributes = {k: STABLE.get(k, v) for k, v in (span.attributes or {}).items()}
        events = [
            type(e)(e.name, {k: STABLE.get(k, v) for k, v in (e.attributes or {}).items()}, e.timestamp)
            for e in span.events
        ]
        out.append(
            ReadableSpan(
                name=span.name,
                context=span.context,
                parent=span.parent,
                resource=Resource(RESOURCE),
                attributes=attributes,
                events=events,
                status=span.status,
                start_time=span.start_time,
                end_time=span.end_time,
            )
        )
    return out


async def record(name: str, turn: dict[str, Any], run: Callable[[TracerProvider], Awaitable[Any]]) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with contextlib.suppress(ConnectionError):
        await run(provider)
    write(name, "spans", dump_spans(stable(exporter.get_finished_spans()), turn))


async def main() -> None:
    await record(
        "pydantic_ai_tool_turn",
        {"tenant_ref": "team_editorial", "conversation_id": "thread-1", "user": "ada@acme.example"},
        lambda p: make_agent(p).run(
            ["What did we spend on paid search?", BinaryContent(data=b"\x89PNG\r\n" * 8, media_type="image/png")]
        ),
    )
    await record(
        "pydantic_ai_failed_tool",
        {"tenant_ref": "Team Editorial", "conversation_id": "thread #42/ü"},
        lambda p: make_failing_agent(p).run("Is paid search up?"),
    )


if __name__ == "__main__":
    asyncio.run(main())
