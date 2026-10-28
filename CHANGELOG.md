# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `emet_sdk.turn()` marks one turn of an agent, as a context manager or a decorator, with the Team's External ID, the app's conversation id and the user. The values travel in OpenTelemetry context, never in baggage.
- `emet_sdk.configure()` adds the Emet span processor to the app's `TracerProvider`, creating one when the app has none. It never replaces the provider or changes sampling.
- The processor collects the spans started inside each `turn()` block without writing anything onto them, and finishes the turn once the block has closed and every one of its spans has ended. Turns are capped in number, size and age.
