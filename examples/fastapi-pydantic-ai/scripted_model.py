"""An offline model for the demo: it reads the question, calls the spend tool, and answers from the tool's result."""

from __future__ import annotations

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

CHANNELS = {"search": "paid_search", "social": "social", "display": "display"}


def _answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    last = messages[-1]
    returned = [p for p in last.parts if isinstance(p, ToolReturnPart)] if isinstance(last, ModelRequest) else []
    if returned:
        result = returned[0].content
        if isinstance(result, dict) and "spend" in result:
            name = str(result["channel"]).replace("_", " ")
            return ModelResponse(parts=[TextPart(f"Last month's {name} spend was ${result['spend']:,.2f}.")])
        return ModelResponse(parts=[TextPart("I could not find that channel.")])
    question = " ".join(
        str(p.content) for m in messages if isinstance(m, ModelRequest) for p in m.parts if p.part_kind == "user-prompt"
    ).lower()
    channel = next((value for word, value in CHANNELS.items() if word in question), "paid_search")
    return ModelResponse(parts=[ToolCallPart("get_spend", {"channel": channel})])


SCRIPTED_MODEL = FunctionModel(_answer, model_name="scripted-demo-model")
