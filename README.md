# Emet SDK for Python

[Emet](https://emet.so) checks the claims an AI agent makes against the evidence behind them: the tool results and data the agent actually received. `emet-sdk` sends your agent's turns to Emet from the OpenTelemetry GenAI spans your framework already emits.

> In development, not released yet.

## How it works

```python
import emet_sdk
from pydantic_ai import Agent

emet_sdk.configure(service_name="support-agent", on_delivery=emet_sdk.write_delivery_file(".emet/last-delivery.json"))
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
- **No secrets, no system prompt.** Secrets are redacted from everything sent, and the system prompt travels as a hash.
- **The user** is who triggered the turn, usually their email. Emet matches it to a member of your organization and stores it on the trace either way.
- **Never in your way.** Turns are sent on a background thread and never raise into your app. Without `EMET_APPLICATION_KEY`, the app logs "Emet is off" once and runs as before. Call `emet_sdk.flush()` before a short-lived process exits; Emet also flushes for up to 5 seconds at exit.
- **Every turn reports back.** `on_delivery` receives a result per turn: `sent`, `stopped` (with the reason, such as `timeout` or `http_4xx` and Emet's error code) or `skipped`, plus the trace, the span count and any tools Emet found missing arguments or results. `write_delivery_file` saves the latest one for `emet-cli check --last`; it never holds the key, the tenant, the user or any content.

Supported: Python 3.10 or later; Pydantic AI (Python 3.11 or later). Google ADK is planned.

## Development

```sh
uv sync
uv run pytest
uv run python -m tests.record_fixtures && uv run pytest --update-fixtures  # rerecord, then review the diff
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

To release, bump `src/emet_sdk/_version.py` and the changelog in a PR. Once it has merged, tag the merge commit `vX.Y.Z` and push the tag. The release workflow checks that the tag is on `main` and matches the version, builds and smoke-tests the wheel, then waits for approval in the `release` environment before publishing to PyPI through trusted publishing.

The repository also holds [`emet-cli`](cli/README.md), which sets the SDK up in your app with your coding agent. It is in development.

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to contribute, and [SECURITY.md](SECURITY.md) to report a vulnerability.

## License

Apache-2.0
