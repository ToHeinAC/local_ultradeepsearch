# LLM layer

How a role plus messages becomes a validated result. Requirements: [PRD.md](../PRD.md) §3.1, §3.5
(AD2) and §4 M1. Everything here lives in `src/app/llm/` unless noted.

## Modules

| Module | Holds |
|---|---|
| `types.py` | `Role`, `Endpoint`, `RoleSpec`, `Message`, `ChatRequest`, `ChatReply`, the `Transport` protocol |
| `roles.py` | `build_registry(settings, reason_num_ctx)`: the four `RoleSpec`s |
| `structured.py` | `strip_think`/`remove_think`, `parse_structured`, `repair_messages`, `estimate_tokens` |
| `service.py` | `LLMService.structured()` and `.text()` |
| `errors.py` | `LLMError` and its subclasses |
| `fakes.py` | `ScriptedTransport` and `reply()` for offline tests |
| `../adapters/ollama_transport.py` | `OllamaTransport`: the real `Transport` |
| `../prompts/llm.py` | The repair and calibration prompts |

## Roles

Defaults live in `roles.py`; models and the non-reason contexts come from `Settings`. Roles and
endpoints: PRD §3.1.

| Role | Endpoint | num_ctx | num_predict | temperature | keep_alive | Max concurrent |
|---|---|---|---|---|---|---|
| reason | own | calibrated, else 16384 | 8192 | 0.2 | 30m | 1 |
| extract | shared | 16384 | 2048 | 0.0 | 30m | 2 |
| summarize | shared | 16384 | 4096 | 0.1 | 10m | 2 |
| ocr | shared | 8192 | 4096 | 0.0 | 5m | 1 |

`num_predict` is capped at half of `num_ctx`, so a prompt always has room.

## A call, step by step

`structured(role, messages, schema, think=False, num_predict=None)`. `num_predict` asks for a larger
output budget than the role's default, capped at half of `num_ctx`; steps 1 and 2 use it for their
thinking calls (`thinking_num_predict` in `config/profiles.toml`), because a call cut at 8192 is
thrown away and repeated. Below, `num_predict` is the budget of the call:

1. **Budget.** If the estimated prompt (characters / 3) exceeds `num_ctx - num_predict`, raise
   `PromptTooLargeError`. Nothing is sent and nothing is truncated.
2. **Send** with the role's semaphore held, once per transport call.
3. **Unavailable** (`LLMUnavailableError`): retry after 1 s, 2 s, 4 s, then re-raise. A missing
   model (`LLMModelMissingError`) is never retried.
4. **Length.** On `done_reason == "length"`, retry once with `min(2 × num_predict, num_ctx -
   prompt estimate)`. If that leaves no room to grow, or the retry stops at the limit again, raise
   `LLMTruncatedError` carrying the partial output.
5. **Parse.** Strip thinking, then validate against the Pydantic schema, which is also sent to
   Ollama as `format`.
6. **Repair.** On invalid JSON or a schema violation, call again with the original messages plus the
   rejected reply and the error, at most twice. Then raise `LLMOutputError` carrying the raw output.
   The history is not stacked: each repair starts from the original messages.

`text(role, messages)` runs steps 1–4 and returns the stripped text.

## Thinking

- `think` is always sent explicitly. A live probe showed that omitting it makes `gemma4:e2b` spend
  its whole output budget on thinking and return empty content, while `think=False` is accepted by
  models without thinking support.
- Thinking is removed from every string before it reaches `parse_structured`, an event, or an
  artifact (`strip_think`, `app.artifacts.scrub_think`). An unclosed `<think>` is cut to the end and a
  lone `</think>` removes everything before it.

## Telemetry

One `llm_call` event per transport call: `role, model, endpoint, attempt, outcome (ok |
unavailable | model_missing), prompt_tokens, eval_tokens, total_ms, load_ms, done_reason`. Also
`llm_repair` (attempt, error), `llm_length_retry`, `llm_truncated`, `llm_output_error`. Events never
carry prompts, answers or thinking. Sinks: [events](../src/app/events.py).

## Transport

`OllamaTransport` translates `ChatRequest` to `ollama.Client.chat` and maps errors:

| Ollama says | Raised |
|---|---|
| 404 | `LLMModelMissingError` |
| 5xx, connection refused, any `httpx` transport error or timeout | `LLMUnavailableError` |
| other 4xx (for example "does not support thinking") | `LLMError`, not retried |

It accepts loopback base URLs only and caches one client per `(base_url, timeout)`.

## Testing

Offline tests use `ScriptedTransport`: it plays back replies and exceptions in order and records
every call. The service tests were mutation-checked (no backoff step, no semaphore, stacked repair
history, no think-strip). Real models: `tests/live/` ([IMPLEMENTATION.md](../IMPLEMENTATION.md) §1).
