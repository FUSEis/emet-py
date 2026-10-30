# emet-cli

Sets up [Emet](https://emet.so) in a Python app. Your coding agent runs the commands; you type the key yourself, so it never enters the agent's context.

> In development, not released yet.

```sh
uvx emet-cli login          # you, in your own terminal, in the app's folder
uvx emet-cli check          # what is set up, without sending anything
uvx emet-cli check --last   # whether the last turn reached Emet whole, and the fix if not
```

- **`login`** asks for your key at a hidden prompt and checks it with Emet without sending a trace. It saves the key to `.env` as `EMET_APPLICATION_KEY` and records only the key's public prefix in `.emet/key.json`, then adds both files to `.gitignore`. It refuses to run without a terminal, and refuses when either file is tracked by git.
- **`check`** reports the key, whether the app depends on `emet-sdk`, and where it calls `emet_sdk.configure()` and `emet_sdk.turn()`. With `--last`, it reads the delivery result the SDK saved in `.emet/last-delivery.json` and says whether the turn was captured. Add `--json` for your agent.

`check --last` needs the app to save results: `emet_sdk.configure(on_delivery=emet_sdk.write_delivery_file(".emet/last-delivery.json"))`.
