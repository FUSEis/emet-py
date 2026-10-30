# Design

How `emet-sdk` turns an agent's OpenTelemetry spans into what Emet receives, and why it is built this way. The modules named below are in `src/emet_sdk/`.

## From spans to a turn

```
your app ── turn() ──┐
                     ▼
  TracerProvider ── EmetSpanProcessor ── finished turn ── Worker thread ── envelope ── batches ── Emet
       │             (_processor.py)                       (_worker.py)    (_envelope.py) (_transport.py)
       └── your other exporters, unchanged
```

1. **`turn()`** (`_turn.py`) puts the turn's values, the tenant, the conversation and the user, in a plain OpenTelemetry context key. A context key stays in the process; baggage would be forwarded to every downstream service.
2. **The processor** (`_processor.py`) is one more `SpanProcessor` on the app's provider. On each span's start it reads the context. Spans started outside a `turn()` are ignored, and the processor never writes anything onto a span, so other exporters see exactly what they saw before.
3. **A turn is finished** once its `turn()` block has closed and every span started inside it has ended; a background span that outlives the block keeps the turn open. Live turns are capped in number (1000), size (2000 spans) and age (10 minutes), and a turn given up on is reported, not silently dropped.
4. **The worker** (`_worker.py`) builds and sends turns on one daemon thread, in order, so the app's request thread only hands the turn over. It restarts after a fork, which matters under servers that fork workers after import.

## From a turn to an envelope

`_envelope.py` maps the GenAI spans onto Emet's v1 envelope, following the OpenTelemetry GenAI semantic conventions.

- **Only GenAI spans are kept**: those with a `gen_ai.operation.name`. A kept span whose parent was dropped reattaches to its nearest kept ancestor.
- **The agent span becomes the turn.** When the top of the tree is a bare model or tool call, a synthetic turn span is added above it, so its content is kept. The turn runs from the first span's start to the last span's end.
- **The final output** is the last model call that finished normally with text and no tool call.
- **The conversation id comes from `turn()`**, never from the spans: Pydantic AI generates a fresh one per run when the app passes none.
- **Identifiers** outside Emet's charset are sent as a readable prefix and a hash, so two distinct ids never collide.
- **Redaction** (`_redact.py`) runs over every string the envelope carries, as the README describes, and counts what it replaced.

The mapping is pinned by fixtures: spans recorded from real Pydantic AI runs, and the envelope each must build (`tests/fixtures/`). Emet keeps byte-identical copies and posts them through its real endpoint, so a change to either side's reading of the format fails a test before it reaches a customer.

## Sending

`_transport.py` sends one turn at a time.

- **Batches** hold at most 200 spans or 4 MiB and never clip a span; a larger span travels alone. The turn span and `end: true` go only in the last batch, because Emet closes a trace as soon as its turn span arrives.
- **Order and the stop rule.** Batches go in order. The first one Emet does not accept stops the turn: sending the rest would only add to a trace Emet will close incomplete. A batch Emet calls `late` is recorded and the rest still go.
- **Retries.** One per batch, on a timeout, a server error, or a rate limit asking to wait 5 seconds or less, inside a 15-second budget per batch, so each batch reaches Emet before its 30-second idle close. Retrying is safe: Emet ignores spans it already stored.
- **Where it goes.** Only `https://app.emet.so` or localhost, with the key as a bearer token and redirects refused, so the key cannot be carried anywhere else. Standard library HTTP only.

## The delivery result

`_delivery.py` gives every turn one result with a fixed set of keys: `sent`, `stopped` or `skipped`, the reason, the trace and conversation, counts, and the gaps Emet reported, named by tool. It defines **captured**: sent whole, attributed to a Team, on time, with no gaps. `emet-cli check --last` reads it from the file `write_delivery_file()` saves. The result never holds the key, the tenant, the user or any content.

## Choices we did not make

- **Stamping turn values onto spans or baggage.** Every other exporter in the app would receive the tenant and the user's email.
- **Framework callbacks.** One adapter per framework and version, where GenAI spans give one mapping for all of them.
- **Reading `.env` inside the SDK.** A library reaching into the filesystem for configuration surprises the app that owns it; the key comes from the environment, which the app controls.
- **A new transport such as OTLP.** Emet's JSON envelope needs no collector and no extra dependency.
