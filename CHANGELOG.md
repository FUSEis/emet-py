# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/FUSEis/emet-py/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/FUSEis/emet-py/releases/tag/v0.1.0
