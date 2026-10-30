# Contributing

Thanks for helping. Bug reports, redaction patterns and fixes are welcome. Everyone taking part follows our [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- **Security issues** go through private reporting, never a public issue. See [SECURITY.md](SECURITY.md).
- **Larger changes**, such as a new framework or a change to what the SDK sends, start as an issue so we can agree on the shape first.
- **Never paste real data**: no Emet keys, prompts, tool results or traces from a real app, in issues, pull requests or fixtures.

## Development

You need [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run pyright
```

CI runs the tests on Python 3.10 to 3.14 and on the oldest OpenTelemetry the package accepts, with coverage at 90% or more.

## Fixtures

`tests/fixtures` holds spans recorded from real Pydantic AI runs on its offline test model, and the envelope each one must build. Every value in them is synthetic, the secrets included. To change what the SDK sends, rerecord and rebuild them on Python 3.11 or later (Pydantic AI needs it), then review the diff:

```sh
uv run python -m tests.record_fixtures && uv run pytest --update-fixtures
```

## Pull requests

- One change per pull request, with tests.
- Update `CHANGELOG.md` under Unreleased when behavior or the data sent changes.
- Pull requests are squash merged once CI passes and a maintainer approves.
