# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

- `configure()` works after `logfire.configure()`. It raised `TypeError` because Logfire's tracer provider wraps the OpenTelemetry SDK's without subclassing it. Any provider with `add_span_processor` is now accepted.
- A provider that cannot take a span processor turns Emet off with one log line, instead of raising at startup.
- `turn()` no longer raises on an empty or non-string value. The agent runs as usual and an empty `tenant_ref` or `conversation_id` is reported as skipped, with the reason `missing_turn_value`.
- `emet-cli check --last` names streaming as a cause when a turn had no spans.

### Changed

- The README shows how to stream inside `turn()` and when the delivery file is written, and its links work on PyPI.

## [0.1.2] - 2026-11-04

### Added

- `EMET_ENDPOINT`: when `configure()` gets no endpoint, the SDK sends to the localhost address in this variable, or to production when it is unset. A value outside production and localhost turns Emet off with one log line that does not repeat the value.
- The SDK logs one warning at startup when it sends anywhere but production, naming what set the endpoint.
- `emet-cli login --endpoint http://localhost:8000` saves `EMET_ENDPOINT` next to the key, so an app tested against Emet on your machine sends there with no code change. Logging in to production removes it.
- `emet-cli check` says where the app sends its turns, using the SDK's own rules; flags a key that came from a different Emet, including an endpoint passed to `emet_sdk.configure()`, with the exact command to fix it; flags an endpoint the SDK would refuse; and flags an app whose `emet-sdk` is too old to read `EMET_ENDPOINT`.

### Changed

- When the key came from Emet on your machine, a refused key's fix gives the `login --endpoint` command and says that a key works only on the Emet that issued it.
- `emet-cli login` to production says when it removes `EMET_ENDPOINT`.

## [0.1.1] - 2026-10-30

The first release of `emet-cli`, versioned together with `emet-sdk` from now on.

### Added

- `emet-cli login` saves the Emet key to the app's `.env` from a hidden prompt, after checking it with Emet without sending a trace. It runs only in a terminal, so a coding agent cannot run it for you, and it records only the key's public prefix elsewhere.
- `emet-cli init` installs the matching `emet-sdk` with the app's package manager and reports, as JSON for a coding agent, where the agent runs, whether the app loads `.env`, and what to wire. It edits no code.
- `emet-cli check` reports what is set up without sending anything; `--last` says whether the last turn was captured, and the fix if not.
- `cli/AGENT_SETUP.md`, the prompt that walks a coding agent through the setup.
- An example FastAPI agent on Pydantic AI to try the setup on, in `examples/fastapi-pydantic-ai`.

### Changed

- The delivery result names tools in its gap lists (`get_spend`), not their spans (`execute_tool get_spend`).
- The README is rewritten for a public reader, with a design document in `DESIGN.md`.

## [0.1.0] - 2026-10-30

The first release.

### Added

- `emet_sdk.turn()` marks one turn of an agent, as a context manager or a decorator, with the Team's External ID, the app's conversation id and the user. The values travel in OpenTelemetry context, never in baggage.
- `emet_sdk.configure()` adds the Emet span processor to the app's `TracerProvider`, creating one when the app has none. It never replaces the provider or changes sampling.
- The processor collects the spans started inside each `turn()` block without writing anything onto them, and finishes the turn once the block has closed and every one of its spans has ended. Turns are capped in number, size and age.
- Each finished turn is built into Emet's v1 envelope from its GenAI spans, following Emet's OpenTelemetry mapping: the agent span becomes the turn, model and tool calls keep their payloads, and the last model call that answered in text is the final output. Spans without a GenAI operation are left out, and framework attributes such as Pydantic AI's message dump are dropped.
- The conversation id comes from `turn()`, never from the spans, and an id Emet would refuse is sent as a readable prefix and a hash.
- Each turn is sent to `https://app.emet.so` on a background thread, in batches of at most 200 spans or 4 MiB that never clip a span, with the turn span and `end` in the last batch. Batches go in order and the first one Emet does not accept stops the turn. A batch is retried once on a timeout, a server error or a short rate limit, inside a 15-second budget.
- `configure()` reads the key from `EMET_APPLICATION_KEY`, logs "Emet is off" once when it is missing, and takes `service_name`, `on_delivery` and a localhost `endpoint` for testing.
- The delivery result reports each turn as `sent`, `stopped` or `skipped` with its reason; `write_delivery_file()` saves it atomically for `emet-cli check --last`. A log line names the outcome on the first turn and whenever it changes.
- `flush()` waits for finished turns to be sent, and runs for up to 5 seconds at exit.
- Secrets are redacted from every string, the Emet key included, the system prompt is sent as a SHA-256 hash, inline files and images are replaced by their type, size and digest, and a failed call sends its error message without the stack trace.

[Unreleased]: https://github.com/FUSEis/emet-py/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/FUSEis/emet-py/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/FUSEis/emet-py/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/FUSEis/emet-py/releases/tag/v0.1.0
