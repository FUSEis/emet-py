# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `emet_sdk.turn()` marks one turn of an agent, as a context manager or a decorator, with the Team's External ID, the app's conversation id and the user. The values travel in OpenTelemetry context, never in baggage.
- `emet_sdk.configure()` adds the Emet span processor to the app's `TracerProvider`, creating one when the app has none. It never replaces the provider or changes sampling.
- The processor collects the spans started inside each `turn()` block without writing anything onto them, and finishes the turn once the block has closed and every one of its spans has ended. Turns are capped in number, size and age.
- Each finished turn is built into Emet's v1 envelope from its GenAI spans, following Emet's OpenTelemetry mapping: the agent span becomes the turn, model and tool calls keep their payloads, and the last model call that answered in text is the final output. Spans without a GenAI operation are left out, and framework attributes such as Pydantic AI's message dump are dropped.
- The conversation id comes from `turn()`, never from the spans, and an id Emet would refuse is sent as a readable prefix and a hash.
- Secrets are redacted from every string, the system prompt is sent as a SHA-256 hash, inline files and images are replaced by their type, size and digest, and a failed call sends its error message without the stack trace.
