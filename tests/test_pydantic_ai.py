"""Runs a real Pydantic AI agent on its offline test model and checks what the processor collects."""

from __future__ import annotations

import pytest

pydantic_ai = pytest.importorskip("pydantic_ai")

from pydantic_ai import Agent, InstrumentationSettings  # noqa: E402
from pydantic_ai.capabilities import Instrumentation  # noqa: E402
from pydantic_ai.models.test import TestModel  # noqa: E402

import emet_sdk  # noqa: E402
from emet_sdk._processor import CapturedTurn  # noqa: E402
from tests.conftest import Harness  # noqa: E402

TURN_VALUES = ("team_editorial", "thread-1", "ada@acme.example")


def make_agent(harness: Harness) -> Agent[None, str]:
    agent = Agent(
        TestModel(),
        system_prompt="You answer spend questions.",
        capabilities=[Instrumentation(InstrumentationSettings(tracer_provider=harness.provider))],
    )

    @agent.tool_plain
    def get_spend(channel: str) -> dict[str, object]:  # pyright: ignore[reportUnusedFunction]
        """Returns spend for a channel."""
        return {"spend": 48210.55, "currency": "USD", "channel": channel}

    @agent.tool_plain
    def get_list(n: int) -> list[int]:  # pyright: ignore[reportUnusedFunction]
        """Returns a list, a non-object result."""
        return [1, 2, 3]

    return agent


def operations(captured: CapturedTurn) -> list[str]:
    return sorted(str((span.attributes or {}).get("gen_ai.operation.name")) for span in captured.spans)


async def test_a_run_inside_a_turn_is_collected(harness: Harness) -> None:
    with emet_sdk.turn(*TURN_VALUES):
        await make_agent(harness).run("What did we spend on paid search?")
    [captured] = harness.turns
    # Sync tools run in a worker thread and still belong to the turn
    assert operations(captured) == ["chat", "chat", "execute_tool", "execute_tool", "invoke_agent"]


async def test_a_run_outside_a_turn_is_not(harness: Harness) -> None:
    await make_agent(harness).run("What did we spend on paid search?")
    assert harness.turns == []
    assert harness.other_exporter.get_finished_spans()


async def test_turn_values_never_reach_other_exporters(harness: Harness) -> None:
    with emet_sdk.turn(*TURN_VALUES):
        await make_agent(harness).run("What did we spend on paid search?")
    for span in harness.other_exporter.get_finished_spans():
        values = {str(v) for v in (span.attributes or {}).values()}
        assert not values & {"team_editorial", "ada@acme.example", "thread-1"}, span.name


async def test_pydantic_ai_makes_up_its_own_conversation_id(harness: Harness) -> None:
    """Why the envelope takes conversation_id from turn(): the spans carry a fresh id per run."""
    with emet_sdk.turn(*TURN_VALUES):
        await make_agent(harness).run("What did we spend on paid search?")
    ids = [(span.attributes or {}).get("gen_ai.conversation.id") for span in harness.turns[0].spans]
    assert ids and all(isinstance(i, str) and i for i in ids)
    assert "thread-1" not in ids
