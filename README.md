# Emet SDK for Python

[![PyPI](https://img.shields.io/pypi/v/emet-sdk)](https://pypi.org/project/emet-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/emet-sdk)](https://pypi.org/project/emet-sdk/)
[![CI](https://github.com/FUSEis/emet-py/actions/workflows/ci.yml/badge.svg)](https://github.com/FUSEis/emet-py/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/emet-sdk)](LICENSE)

[Emet](https://emet.so) checks the claims an AI agent makes against the evidence behind them: the tool results and data the agent actually received. `emet-sdk` sends your agent's turns to Emet from the OpenTelemetry GenAI spans your framework already emits. There is no second instrumentation to maintain.

> Early release. The API may change before 1.0; every change is in the [changelog](CHANGELOG.md).

## Quick start

You need an Emet account, a key for a development Application, and the External ID of the Team your users belong to.

```sh
uv add emet-sdk  # or: pip install emet-sdk
```

```python
import emet_sdk
from pydantic_ai import Agent

# Once, at startup, after your own tracing setup (such as logfire.configure())
emet_sdk.configure(
    service_name="support-agent",
    on_delivery=emet_sdk.write_delivery_file(".emet/last-delivery.json"),
)
Agent.instrument_all()


@app.post("/chat")
async def chat(body: ChatIn, user=Depends(current_user)):
    # Take these from your server's own auth and storage, never from the request body
    thread = await threads.get_for_user(user, body.thread_id)
    with emet_sdk.turn(tenant_ref=user.team_external_id, conversation_id=thread.id, user=user.email):
        result = await agent.run(body.message)
    return result.output
```

Set `EMET_APPLICATION_KEY` in the app's environment. In production that is your host's secret settings; locally, load your `.env` the way your app already does, for example with `uvicorn --env-file .env` or `python-dotenv`. Without the key, the app logs "Emet is off" once and runs as before.

[`emet-cli`](cli/README.md) can do this setup with your coding agent, and tells you whether a turn reached Emet whole.

## How it works

- **Adds, never replaces.** `configure()` adds one span processor to your app's `TracerProvider`, and creates a provider only when the app has none. Logfire, Datadog or any other exporter keeps receiving the same spans, with the same sampling.
- **Opt in per turn.** Only spans that start inside `turn()` are sent. Other agents, HTTP calls and database queries are not.
- **Nothing written onto your spans.** The tenant, conversation and user stay inside the SDK. They never reach your other exporters, and never travel to downstream services as baggage.
- **Never in your way.** Turns are built and sent on a background thread, and the SDK never raises into your app.

[DESIGN.md](DESIGN.md) has the details: how a turn is collected, built into Emet's envelope, split and sent.

## What is sent, and what never leaves

Emet needs the content of a turn to check it: the user's question, each model call's messages, and each tool call's arguments and result. Before anything leaves the process:

- **Secrets are redacted** from every string: keys and tokens of common providers (OpenAI, Anthropic, AWS, GitHub, GitLab, Slack, Stripe, Google, npm), bearer tokens, JWTs, passwords in URLs, assignments such as `DB_PASSWORD=...`, values under keys such as `password` or `api_key`, and your Emet key itself. Redaction is best effort; tell us about a pattern it misses.
- **The system prompt** travels as a SHA-256 hash, never as text.
- **Images and files** sent inline are replaced by their type, size and digest.
- **Errors** carry their message, never the stack trace.
- **Span attributes** are limited to an allowlist of GenAI fields; framework dumps such as Pydantic AI's message history are dropped.

The user you pass to `turn()`, usually their email, is stored on the trace so Emet can show usage per person.

## Reference

| | |
| --- | --- |
| `configure(*, service_name=None, on_delivery=None, endpoint="https://app.emet.so")` | Adds Emet to the global `TracerProvider`. Call once at startup; later calls do nothing. `service_name` names the app in Emet. `on_delivery` receives each turn's delivery result on the SDK's thread. `endpoint` takes only production or a localhost address, for testing against Emet on your machine. |
| `turn(tenant_ref, conversation_id, user=None)` | A context manager or decorator, sync or async, around one turn of your agent. `tenant_ref` is the External ID of the Team in Emet; `conversation_id` is your app's own thread id; `user` is who asked. |
| `flush(timeout=10.0) -> bool` | Waits until every finished turn is sent; returns False when the timeout ran out first. Call it before a short-lived process exits. The SDK also flushes for up to 5 seconds at exit. |
| `write_delivery_file(path)` | An `on_delivery` callback that saves the latest result to a file, for `emet-cli check --last`. Its folder must exist. |
| `EMET_APPLICATION_KEY` | The key, read from the environment when `configure()` runs. |

**The delivery result** reports each turn as `sent`, `stopped` or `skipped`, with the reason, the trace and conversation ids, span and batch counts, whether the turn matched a Team (`unattributed`) or arrived after Emet closed it (`late`), and any tools Emet found missing arguments or results. A turn is **captured** when it was sent whole, attributed, on time and with no gaps. The result never holds the key, the tenant, the user or any content.

## Compatibility

| | Supported |
| --- | --- |
| Python | 3.10 to 3.14 |
| OpenTelemetry SDK | 1.27 or later |
| Pydantic AI | 2.63 tested in CI, on Python 3.11 or later |

Google ADK is planned. A TypeScript SDK will follow the same design.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for development and releases, and [SECURITY.md](SECURITY.md) to report a vulnerability privately. Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

[Apache-2.0](LICENSE)
