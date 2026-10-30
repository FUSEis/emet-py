"""A small analytics agent behind a FastAPI route. It is not connected to Emet yet: that is the setup to demo."""

from __future__ import annotations

from typing import Annotated

from data import CHANNEL_SPEND, DAILY_SPEND, USERS
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from pydantic_ai import Agent
from scripted_model import SCRIPTED_MODEL

# An offline model that calls the spend tool and answers from it. Swap in a real model, such as "openai:gpt-5"
MODEL = SCRIPTED_MODEL

app = FastAPI(title="Spend assistant")
agent = Agent(
    MODEL,
    instructions="You answer questions about marketing spend. Use the tools; never guess a number.",
)


@agent.tool_plain
def get_spend(channel: str) -> dict[str, object]:
    """Returns last month's spend for a channel: paid_search, social or display."""
    return {"channel": channel, "currency": "USD", "spend": CHANNEL_SPEND.get(channel, 0.0)}


@agent.tool_plain
def get_daily_spend(channel: str) -> list[float]:
    """Returns the last seven days of spend for a channel."""
    return DAILY_SPEND.get(channel, [])


class User(BaseModel):
    email: str
    team: str


class ChatIn(BaseModel):
    message: str
    thread_id: str


def current_user(x_demo_user: Annotated[str, Header()]) -> User:
    """Stands in for real auth: the server looks the user up, it never trusts the request for who they are."""
    user = USERS.get(x_demo_user)
    if user is None:
        raise HTTPException(status_code=401, detail="unknown user")
    return User(email=x_demo_user, team=user["team"])


@app.post("/chat")
async def chat(body: ChatIn, user: Annotated[User, Depends(current_user)]) -> dict[str, str]:
    result = await agent.run(body.message)
    return {"answer": str(result.output)}
