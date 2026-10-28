# Emet SDK for Python

[Emet](https://emet.so) checks the claims an AI agent makes against the evidence behind them: the tool results and data the agent actually received. `emet-sdk` sends your agent's turns to Emet from the OpenTelemetry GenAI spans your framework already emits.

> In development. Turns are collected but not sent yet; the first release adds the envelope and transport.

## How it works

```python
import emet_sdk
from pydantic_ai import Agent

emet_sdk.configure()
Agent.instrument_all()


@app.post("/chat")
async def chat(body: ChatIn, user=Depends(current_user)):
    # Take these from your server's own auth and storage, never from the request body
    thread = await threads.get_for_user(user, body.thread_id)
    with emet_sdk.turn(tenant_ref=user.team_external_id, conversation_id=thread.id, user=user.email):
        result = await agent.run(body.message)
    return result.output
```

- **Adds, never replaces.** `configure()` adds one span processor to your app's `TracerProvider`. Logfire, Datadog or any other exporter keeps receiving the same spans, with the same sampling. Call it after your own tracing setup, such as `logfire.configure()`; it creates a provider only when the app has none.
- **Opt in per turn.** Only spans that start inside `turn()` are sent. Other agents and HTTP spans are not.
- **Nothing written onto your spans.** The tenant, conversation and user stay in the SDK and never reach your other exporters, or downstream services through baggage.
- **The user** is who triggered the turn, usually their email. Emet matches it to a member of your organization and stores it on the trace either way.

Supported: Python 3.10 or later; Pydantic AI (Python 3.11 or later). Google ADK is planned.

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

## License

Apache-2.0
