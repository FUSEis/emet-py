# Set up Emet in this app

These are instructions for a coding agent. A developer asked you to connect their Python app to [Emet](https://emet.so), which checks what their AI agent tells users against the evidence it received. Follow the steps in order and keep going on your own: the developer is needed only for the key (step 2), when the app has no team value or nothing picks one of several (step 4), and to send one question through the app, or start it if you cannot (step 7). Do not ask them to confirm anything else.

Use `uvx emet-cli@0.1.3` for every command below, so the CLI and the SDK it installs match.

## Rules

- **Never handle the Emet key.** Do not ask for it, read `.env`, print environment variables, or pass the key to any command. Only the developer types it, into `emet-cli login`, in their own terminal.
- **Take the turn's values from what the app's own auth has established, never from an unchecked request value.** `user` is the signed-in user that the app's auth dependency or middleware returns, such as `Depends(current_user)` or `request.user`. `tenant_ref` is the team, workspace or org on that user's record, or one the app's own auth or membership check has resolved for this request, such as a dependency that confirms the user belongs to the org in the path. A raw id from the body, path or headers that nothing checks never counts. Do not rewrite or audit the app's auth. The one exception is the thread id: when the app's only thread id comes from the request, scope it to the signed-in user, for example `f"{user.email}:{body.thread_id}"`, so one user's id can never land in another user's conversation.
- **Do not edit agent configuration** (`.claude/`, `AGENTS.md`, `CLAUDE.md`, `.mcp.json`, `.vscode/`), and never commit `.env` or `.emet/`.

## Steps

1. **Check the stack.** This setup supports Python apps on Pydantic AI. If the app uses something else, stop and point the developer to https://github.com/FUSEis/emet-py#quick-start.
2. **The key.** Run `uvx emet-cli@0.1.3 check --json`. If `key_saved` is true, skip to step 3. Otherwise ask the developer to open their own terminal in this folder and run `uvx emet-cli@0.1.3 login`. Not through you, and not with a `!` prefix: `login` refuses input it cannot read from a keyboard. They need a key for a development Application from Emet's Applications page. If they test against Emet on their own machine, they add `--endpoint http://localhost:8000` (or their local address). Wait until they say it is done.
3. **Install and survey.** Run `uvx emet-cli@0.1.3 init --json`. It installs `emet-sdk` with the app's package manager and reports where the agent runs (`agent_runs_at`), how the app loads `.env` (`env_loaded_by`), the snippets to add, and `next_steps`. It edits no code.
4. **Pick the three values.** For each place in `agent_runs_at`, find in the app's code:
   - `tenant_ref`: the Emet Team this user's work belongs to, sent as that Team's External ID, chosen as the rules say. When a user can belong to several teams and nothing in the app resolves which one for this request, ask the developer once which to use.
   - `conversation_id`: the app's own id for the thread or session, scoped to the user as the rules say when it comes from the request.
   - `user`: the signed-in user's email.

   Show them in one short table with file and line, then continue to step 5 without waiting. Ask the developer only when the app has no team or tenant value at all; then ask once, in one question, which Emet Team to send.
5. **Wire it.**
   - Add the `configure` snippet once where the app starts, after any tracing setup it already has. Never pass `endpoint=`: `login` saves a local endpoint to `.env` when there is one.
   - Make sure `Agent.instrument_all()` runs at startup.
   - Wrap each agent run in `with emet_sdk.turn(...)`, using the values from step 4.
   - If a run streams through a response generator, open `turn()` inside that generator, not around the code that returns the response.
   - If `env_loaded_by` is empty, make the app load `.env` at startup, with the `dotenv` snippet or its run command's `--env-file .env`.

   Show the developer the diff, and continue.
6. **Check the setup.** Run `uvx emet-cli@0.1.3 check --json` and fix every finding whose `ok` is false. Only the last-turn finding may stay open at this point.
7. **One real turn.** Start the app from this folder, in the background. If you cannot, because a sandbox blocks the network or the app logs "Emet is off", ask the developer to start it in their own terminal. Ask them to send one harmless question through the app, and to tell you when it has answered.
8. **Confirm and report.** Run `uvx emet-cli@0.1.3 check --last --json`. Tell the developer, in a few lines:
   - whether the turn was **captured**, with the trace id; or what went wrong, and the fix `check` gave;
   - the three values you wired, and that `tenant_ref` must match the External ID of the Team this user's work belongs to in Emet;
   - where to see the run in Emet, under AI usage. "Captured" comes only from `check --last`; the list in Emet shows that the run arrived.

If a step fails twice the same way, stop and show the developer the exact output instead of working around it.

## For the developer

To keep any agent from reading the key file, you can add a deny rule. In Claude Code, add `"Read(./.env)"` under `permissions.deny` in `.claude/settings.json`.
