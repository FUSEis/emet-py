# emet-cli

Sets up [Emet](https://emet.so) in a Python app. Your coding agent runs the commands; you type the key yourself, so it never enters the agent's context.

> In development, not released yet.

```sh
uvx emet-cli login          # you, in your own terminal, in the app's folder
uvx emet-cli init --json    # installs emet-sdk and reports what to wire; edits no code
uvx emet-cli check          # what is set up, without sending anything
uvx emet-cli check --last   # whether the last turn reached Emet whole, and the fix if not
```

- **`login`** asks for your key at a hidden prompt and checks it with Emet without sending a trace. It saves the key to `.env` as `EMET_APPLICATION_KEY` and records only the key's public prefix in `.emet/key.json`, then adds both files to `.gitignore`. It refuses to run without a terminal, and refuses when either file is tracked by git.
- **`init`** installs the matching `emet-sdk` with the app's package manager (uv, Poetry, or pip in the app's own virtual environment) and reports where the agent runs, whether the app loads `.env`, and the exact snippets to add. It edits no code: your coding agent does, after you confirm the values.
- **`check`** reports the key, whether the app depends on `emet-sdk`, and where it calls `emet_sdk.configure()` and `emet_sdk.turn()`. With `--last`, it reads the delivery result the SDK saved in `.emet/last-delivery.json` and says whether the turn was captured. Add `--json` for your agent.

`check --last` needs the app to save results: `emet_sdk.configure(on_delivery=emet_sdk.write_delivery_file(".emet/last-delivery.json"))`.

## With a coding agent

Give your agent [AGENT_SETUP.md](AGENT_SETUP.md): "Follow https://github.com/FUSEis/emet-py/blob/main/cli/AGENT_SETUP.md to set up Emet." It runs these commands in order, asks you to run `login` yourself, and confirms the tenant, conversation and user values with you before wiring them.
