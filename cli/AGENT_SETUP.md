# Set up Emet in this app

These are instructions for a coding agent. A developer asked you to connect their Python app to [Emet](https://emet.so), which checks what their AI agent tells users against the evidence it received. Follow the steps in order, and stop to ask the developer wherever a step says so.

Use `uvx emet-cli@0.1.0.dev0` for every command below, so the CLI and the SDK it installs match.

## Rules

- **Never handle the Emet key.** Do not ask for it, read `.env`, print environment variables, or pass the key to any command. Only the developer types it, into `emet-cli login`, in their own terminal.
- **Take the turn's values from the server's own auth and storage**, never from the request body.
- **Do not edit agent configuration** (`.claude/`, `AGENTS.md`, `CLAUDE.md`, `.mcp.json`, `.vscode/`), and never commit `.env` or `.emet/`.

## Steps

1. **Check the stack.** This setup supports Python apps on Pydantic AI. If the app uses something else, stop and point the developer to https://github.com/FUSEis/emet-py#quick-start.
2. **The key.** Ask the developer to open their own terminal in this folder and run `uvx emet-cli@0.1.0.dev0 login`. Not through you, and not with a `!` prefix: `login` refuses input it cannot read from a keyboard. They need a key for a development Application from Emet's Applications page. Wait until they say it is done.
3. **Install and survey.** Run `uvx emet-cli@0.1.0.dev0 init --json`. It installs `emet-sdk` with the app's package manager and reports where the agent runs (`agent_runs_at`), how the app loads `.env` (`env_loaded_by`), the snippets to add, and `next_steps`. It edits no code.
4. **Propose the three values, then wait.** For each place in `agent_runs_at`, find in the app's code:
   - `tenant_ref`: the Emet Team this user's work belongs to, sent as that Team's External ID. If the app has no such value, ask the developer which Team to use, and where the mapping should live.
   - `conversation_id`: the app's own id for the thread or session.
   - `user`: the signed-in user's email.

   Show the developer each expression with its file and line, and wait for their confirmation before wiring anything.
5. **Wire it.**
   - Add the `configure` snippet once where the app starts, after any tracing setup it already has.
   - Make sure `Agent.instrument_all()` runs at startup.
   - Wrap each agent run in `with emet_sdk.turn(...)`, using the confirmed values.
   - If `env_loaded_by` is empty, make the app load `.env` at startup, with the `dotenv` snippet or its run command's `--env-file .env`.

   Show the developer the diff.
6. **Check the setup.** Run `uvx emet-cli@0.1.0.dev0 check --json` and fix every finding whose `ok` is false. Only the last-turn finding may stay open at this point.
7. **One real turn.** Start the app from this folder, in the background. If you cannot, because a sandbox blocks the network or the app logs "Emet is off", ask the developer to start it in their own terminal. Ask them to send one harmless question through the app, and to tell you when it has answered.
8. **Confirm and report.** Run `uvx emet-cli@0.1.0.dev0 check --last --json`. Tell the developer:
   - whether the turn was **captured**, with the trace id; or what went wrong, and the fix `check` gave;
   - that they can see the run under AI usage at https://app.emet.so/ai-usage. The list there shows that the run arrived; "captured" comes only from `check --last`.

If a step fails twice the same way, stop and show the developer the exact output instead of working around it.

## For the developer

To keep any agent from reading the key file, you can add a deny rule. In Claude Code, add `"Read(./.env)"` under `permissions.deny` in `.claude/settings.json`.
