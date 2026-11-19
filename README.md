# Emet SDK for Python

[![PyPI](https://img.shields.io/pypi/v/emet-sdk)](https://pypi.org/project/emet-sdk/)
[![Python](https://img.shields.io/pypi/pyversions/emet-sdk)](https://pypi.org/project/emet-sdk/)
[![CI](https://github.com/FUSEis/emet-py/actions/workflows/ci.yml/badge.svg)](https://github.com/FUSEis/emet-py/actions/workflows/ci.yml)
[![License](https://img.shields.io/pypi/l/emet-sdk)](https://github.com/FUSEis/emet-py/blob/main/LICENSE)

[Emet](https://emet.so) checks the claims an AI agent makes against the evidence behind them: the tool results and data the agent actually received. `emet-sdk` sends your agent's turns to Emet from the OpenTelemetry GenAI spans your framework already emits. There is no second instrumentation to maintain.

> Early release. The API may change before 1.0; every change is in the [changelog](https://github.com/FUSEis/emet-py/blob/main/CHANGELOG.md).

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

`write_delivery_file` saves each turn's result for `emet-cli check --last`, and writes only when its folder exists. `emet-cli login` creates `.emet/` and adds it to `.gitignore`; if you are not using the CLI, run `mkdir .emet` locally and ignore it yourself. A deployed app without the folder writes nothing.

### Streaming

Spans are collected only while `turn()` is open, and a streaming response runs after your route returns. Open `turn()` inside the generator:

```python
@app.post("/chat/stream")
async def chat_stream(body: ChatIn, user=Depends(current_user)):
    thread = await threads.get_for_user(user, body.thread_id)

    async def answer():
        with emet_sdk.turn(tenant_ref=user.team_external_id, conversation_id=thread.id, user=user.email):
            async with agent.run_stream(body.message) as result:
                async for text in result.stream_text(delta=True):
                    yield text

    return StreamingResponse(answer())
```

### Read the report back

Emet checks each turn after it arrives. To act on the result in your app, keep what `turn()` gives you and ask for the report once the block has closed:

```python
with emet_sdk.turn(tenant_ref=user.team_external_id, conversation_id=thread.id, user=user.email) as sent:
    result = await agent.run(body.message)

report = await emet_sdk.areport(sent, timeout=30)  # emet_sdk.report(...) in sync code
if report.flagged:
    logger.warning("Emet flagged a turn: trace %s", report.trace)
    return HELD_REPLY  # or show the answer with its disputed claims marked
return result.output
```

The call waits until the turn is sent, then asks Emet, waiting as long as Emet says between asks. It stops at `timeout` seconds; `timeout=0` asks once. It never raises: every outcome, a network error included, is a result with a `status`.

| `status` | Meaning |
| --- | --- |
| `ready` | The report is in `document`. |
| `not_ready` | Emet had not finished within your timeout. Ask again later with `report.trace`. |
| `not_assessed` | Emet does not check this Application's turns automatically. |
| `failed` | The check ended in error. |
| `not_found` | Emet has no such trace, or this key may not read it. |
| `refused` | Emet refused the key. |
| `rate_limited` | The Application is over its limit of report reads. |
| `not_sent` | The turn did not reach Emet, so there is nothing to ask about. `reason` says why, such as `no_spans` or `emet_off`. |
| `unavailable` | Emet could not be reached or answered with an error. `reason` is `timeout`, `unreachable`, `server_error` or `unexpected_response`. |

`report.flagged` is true only for a finished report in which Emet disputed a claim. `report.document` is the report exactly as Emet returned it:

```python
document = report.document
if document and document["answer"]["text"] is not None:
    answer = document["answer"]["text"]
    for claim in document["claims"]:
        if claim["verdict"] == "disputed":
            passages = [answer[r["start"] : r["end"]] for r in claim["ranges"] if r["kind"] == "passage"]
            print(passages, claim["reason"], claim["correction"])
```

- `document["report"]["status"]` is `flagged`, `clarified`, `clear`, `unchecked`, or `nothing` when the answer stated no claims to check. `status_text` is the label to show.
- Each claim has a `verdict` (`verified`, `imprecise`, `disputed` or `unchecked`), a `reason` under a disputed claim, a `clarification` under an imprecise one, and a `correction` once Emet has verified one.
- `ranges` are offsets into `answer["text"]`, which Python slices directly. A `passage` is where the claim is stated and a `figure` is the number inside it. `answer["text"]` is null, and `ranges` empty, when the stored answer no longer matches the one Emet checked.
- Every text in the document is untrusted plain text: your user's answer, and reasons a model wrote after reading tool results. Escape it, and never render it as HTML or Markdown.
- The document has a `version`, and Emet may add fields to it. Read the fields you need and ignore the rest.

To read a report later, from a job or another request, store the `trace` of the turn's delivery result and pass it instead: `emet_sdk.report(trace, timeout=0)`. `on_delivery` runs on the SDK's own thread, so hand the id on from there; a `report()` called inside it returns `unavailable` at once.

[`emet-cli`](https://github.com/FUSEis/emet-py/blob/main/cli/README.md) can do this setup with your coding agent, and tells you whether a turn reached Emet whole.

## How it works

- **Adds, never replaces.** `configure()` adds one span processor to your app's `TracerProvider`, and creates a provider only when the app has none. Logfire, Datadog or any other exporter keeps receiving the same spans, with the same sampling.
- **Opt in per turn.** Only spans that start inside `turn()` are sent. Other agents, HTTP calls and database queries are not.
- **Nothing written onto your spans.** The tenant, conversation and user stay inside the SDK. They never reach your other exporters, and never travel to downstream services as baggage.
- **Never in your way.** Turns are built and sent on a background thread, and nothing that goes wrong while tracing or sending raises into your app.

[DESIGN.md](https://github.com/FUSEis/emet-py/blob/main/DESIGN.md) has the details: how a turn is collected, built into Emet's envelope, split and sent.

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
| `configure(*, service_name=None, on_delivery=None, endpoint=None)` | Adds Emet to the global `TracerProvider`. Call once at startup; later calls do nothing. `service_name` names the app in Emet. `on_delivery` receives each turn's delivery result on the SDK's thread. `endpoint` takes only production or a localhost address, and any other value raises `ValueError` at startup; without one, the SDK uses `EMET_ENDPOINT`, then production. A tracer provider that cannot take a span processor turns Emet off, with one log line. |
| `turn(tenant_ref, conversation_id, user=None)` | A context manager or decorator, sync or async, around one turn of your agent. `tenant_ref` is the External ID of the Team in Emet; `conversation_id` is your app's own thread id; `user` is who asked. It takes strings, integers and UUIDs, such as `thread.id` from your database; a value of any other type counts as missing. An empty or `None` `tenant_ref` or `conversation_id` never raises: the agent runs as usual and the turn is reported as skipped. |
| `report(turn, *, timeout) -> ReportResult` | Waits up to `timeout` seconds for Emet's report on a turn. `turn` is what `with turn(...) as sent` gave, asked after the block has closed, or the `trace` of a delivery result. It blocks the calling thread and never raises. A turn marked with the decorator has no `sent` value, so read its report by `trace`. |
| `areport(turn, *, timeout) -> ReportResult` | The same for async code. It does not block the event loop. |
| `ReportResult` | `status`, `reason`, `trace` (Emet's id of the trace), `document` (the report as returned, or `None`) and `flagged`. |
| `flush(timeout=10.0) -> bool` | Waits until every finished turn is sent; returns False when the timeout ran out first. Call it before a short-lived process exits. The SDK also flushes for up to 5 seconds at exit. |
| `write_delivery_file(path)` | An `on_delivery` callback that saves the latest result to a file, for `emet-cli check --last`. Its folder must exist; it is never created for you. |
| `EMET_APPLICATION_KEY` | The key, read from the environment when `configure()` runs. |
| `EMET_ENDPOINT` | A localhost address to send to instead of production, for testing against Emet on your machine. `emet-cli login --endpoint` sets it. A value outside production and localhost turns Emet off, with one log line. |

**The delivery result** reports each turn as `sent`, `stopped` or `skipped`, with the reason, the trace and conversation ids, span and batch counts, whether the turn matched a Team (`unattributed`) or arrived after Emet closed it (`late`), and any tools Emet found missing arguments or results. A turn is **captured** when it was sent whole, attributed, on time and with no gaps. The result never holds the key, the tenant, the user or any content.

## Compatibility

| | Supported |
| --- | --- |
| Python | 3.10 to 3.14 |
| OpenTelemetry SDK | 1.27 or later |
| Pydantic AI | 2.63 and the newest release, tested in CI, on Python 3.11 or later |
| Logfire | The newest release, tested in CI |

## Contributing and security

See [CONTRIBUTING.md](https://github.com/FUSEis/emet-py/blob/main/CONTRIBUTING.md) for development and releases, and [SECURITY.md](https://github.com/FUSEis/emet-py/blob/main/SECURITY.md) to report a vulnerability privately. Everyone taking part follows the [Code of Conduct](https://github.com/FUSEis/emet-py/blob/main/CODE_OF_CONDUCT.md).

## License

[Apache-2.0](https://github.com/FUSEis/emet-py/blob/main/LICENSE)
