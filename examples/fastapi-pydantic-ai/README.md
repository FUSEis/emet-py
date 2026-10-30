# Example: a FastAPI agent on Pydantic AI

A small spend assistant behind `POST /chat`, deliberately **not** connected to Emet yet. Use it to try the setup end to end, by hand or with your coding agent following [AGENT_SETUP.md](../../cli/AGENT_SETUP.md).

```sh
cd examples/fastapi-pydantic-ai
uv run uvicorn main:app --port 8001
curl -s localhost:8001/chat -H 'content-type: application/json' -H 'x-demo-user: maya@marlowhome.example' \
  -d '{"message": "What did we spend on paid search?", "thread_id": "thread-1"}'
```

- **No model key needed.** `MODEL` is a small scripted model (`scripted_model.py`) that calls the spend tool and answers from its result, like a real model would. Set it to a real model, such as `"openai:gpt-5"`, for real answers.
- **The three values Emet needs are all in the server**: the user's email from `current_user`, their Team's External ID from `data.USERS`, and the thread id the client names.
- **All data is made up**, for a fictional home goods retailer. The users belong to Teams with the External IDs `marketing` and `ecommerce`; create those in Emet, or change `data.USERS` to your own.
