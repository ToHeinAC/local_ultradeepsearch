# M1 implementation plan — Foundation and model infrastructure

Source of truth: [PRD.md](../../PRD.md), specifically §4 M1, §3.1 (models), §3.4 (conventions) and
§3.5 AD2 (typed LLM I/O). Rules: [AGENTS.md](../../AGENTS.md). Status is tracked only in
[IMPLEMENTATION.md](../../IMPLEMENTATION.md). If this plan and the PRD disagree, the PRD wins.
Raise the conflict with the owner instead of choosing silently.

## Outcome

M1 is done. The code is authoritative: interfaces below are the original design, and where they
differ, the module docs ([llm-layer.md](../llm-layer.md), [ollama-runtime.md](../ollama-runtime.md))
describe what exists. Deviations from this plan:

1. The `udr` console script was registered in step 10, once `app.cli` existed.
2. `DoctorSnapshot` carries `gpus` and `own_loaded_vram_bytes` instead of
   `foreign_gpu_processes`. The sharing check compares used VRAM with what our own instance
   reports loaded, because our daemon and the summarizer's are both named `ollama`.
3. The doctor emits one `model:<role>` check per role instead of a single `models` check.
4. `think` is always sent explicitly. A live probe showed that omitting it makes `gemma4:e2b`
   spend its whole budget on thinking.
5. The model store is the candidate with the most manifests, not the first that exists.
6. `--calibrate` unloads the model afterwards and refuses to run unless the own instance is up.
7. Additions: `tests/support.py` (`make_settings`), `tests/test_py311_syntax.py`, `remove_think`
   split out of `strip_think`, `LiveProbe` and `HostProbe` in the composition root.
8. The live tests skip the OCR role (an image model); M4 covers it with a real page image.

## 1. Scope

**In scope for M1:**
- configuration;
- the model-role registry;
- LLM types, errors and the structured-output service;
- the Ollama transport;
- the own-instance manager (`:11436`);
- the event sink and the think-safe artifact writer;
- num_ctx calibration;
- `udr doctor`;
- the project README;
- documentation of the M1 module boundaries.

**Out of scope, and the milestone that owns it:**
- outbound network, including the egress AST test: M2;
- the SQLite store: M3;
- LangGraph graphs: M4 and later;
- the API: M6;
- the GUI: M7;
- systemd units: M10.

M1 must not add LangGraph, FastAPI or any non-loopback network code.

## 2. Decisions for M1

These are implementation choices that sit inside the PRD's constraints.

| Topic | Decision | Reason |
|---|---|---|
| Sync vs async | Synchronous API with `ollama.Client`. Concurrency comes from threads and per-role `threading.BoundedSemaphore`. | Simplest option. M6 runs graphs in a worker thread. |
| Python level | Code must run on 3.11 **and** 3.14 (CI matrix): `StrEnum`, `typing.Self`; no PEP 695 `type` statements | `.github/workflows/ci.yml` |
| LLM client | `ollama` Python package (MIT), `format=<JSON schema>`, `think=bool`, `options={num_ctx,num_predict,temperature}`, `keep_alive` | The native schema format is more robust than `json_mode`. Prior art: `../KB_BS_local-deep-researcher-he/src/utils.py`. |
| CLI | `typer` (MIT); entry point `udr = "app.cli:main"` | The CLI grows to `doctor`, `brief`, `run`, `apikey`, `denylist` and `backup`. |
| Config | `pydantic-settings`, env prefix `UDR_`, reads `.env` | PRD §3.4 |
| Think default | `think=False` for every role; call sites opt in | PRD §3.1 says `think` is set per call site. Models without thinking support would reject `think=True`. |
| Semaphores | Per role: reason 1, extract 2, summarize 2, ocr 1 | PRD §3.7. The own instance runs `NUM_PARALLEL=1`, so extract calls also queue server-side, which is harmless. |
| Doctor strictness | Doctor treats an own instance that is not answering as an **error**. At runtime the same case fails open with a warning. | PRD M1 AC1 vs AC6 |
| Fake LLM location | `src/app/llm/fakes.py` (it ships, and it is covered) | Offline tests of later milestones reuse it. |

**Dependencies to add:**
- `uv add ollama pydantic pydantic-settings typer`
- no new dev dependencies.

## 3. Target layout

```
src/app/
  __init__.py
  config.py              Settings (pydantic-settings), is_loopback_url()
  events.py              EventSink protocol, JsonlEventSink, MemoryEventSink
  artifacts.py           write_text / write_json: atomic, <think> stripped
  calibration.py         CtxMeasurement, Calibration, pick_num_ctx(), load/save
  doctor.py              DoctorSnapshot → checks → exit code / rendering (pure)
  bootstrap.py           composition root: settings → instance → urls → LLMService
  cli.py                 typer app `udr` (doctor)
  llm/
    __init__.py
    types.py             Role, Endpoint, RoleSpec, Message, ChatRequest, ChatReply, Transport
    errors.py            LLMError hierarchy
    roles.py             DEFAULT_ROLES table, build_registry()
    structured.py        strip_think(), parse_structured(), repair_messages(), estimate_tokens()
    service.py           LLMService.structured() / .text()
    fakes.py             ScriptedTransport, reply()
  adapters/
    __init__.py
    ollama_transport.py  OllamaTransport (ollama.Client) + OllamaAdmin (tags, ps, version)
    ollama_instance.py   instance_env(), ensure_own_instance(), endpoint_urls()
    system_probe.py      nvidia-smi GPU list and processes, disk usage, binary lookup
tests/
  test_config.py test_roles.py test_structured.py test_service.py test_events.py
  test_artifacts.py test_ollama_transport.py test_ollama_instance.py test_calibration.py
  test_doctor.py test_cli.py
  live/test_live_ollama.py   (@pytest.mark.live; excluded from the gate)
```

Removed: `src/app/core.py` and `tests/test_core.py` (the template example; an open issue in
IMPLEMENTATION.md).

## 4. Key interfaces

These are the signatures to implement. Bodies must stay ≤ 50 lines with complexity ≤ 10.

```python
# llm/types.py
class Role(StrEnum): REASON = "reason"; EXTRACT = "extract"; SUMMARIZE = "summarize"; OCR = "ocr"
class Endpoint(StrEnum): OWN = "own"; SHARED = "shared"

@dataclass(frozen=True)
class RoleSpec:
    role: Role; model: str; endpoint: Endpoint; num_ctx: int; num_predict: int
    temperature: float; keep_alive: str; max_concurrency: int

class Message(TypedDict):
    role: Literal["system", "user", "assistant"]; content: str
    images: NotRequired[list[str]]          # base64, used by the ocr role in M4

@dataclass(frozen=True)
class ChatRequest:
    model: str; messages: tuple[Message, ...]; schema: dict[str, Any] | None
    think: bool; num_ctx: int; num_predict: int; temperature: float; keep_alive: str

@dataclass(frozen=True)
class ChatReply:
    content: str; thinking: str | None; done_reason: str | None
    prompt_tokens: int; eval_tokens: int; total_ms: int; load_ms: int

class Transport(Protocol):
    def chat(self, base_url: str, request: ChatRequest, timeout_s: float) -> ChatReply: ...
```

```python
# llm/errors.py
class LLMError(Exception)
class LLMUnavailableError(LLMError)          # connection refused, 5xx, timeout (after retries)
class LLMModelMissingError(LLMError)         # 404 "model not found", never retried
class LLMOutputError(LLMError): raw: str     # still invalid after 2 repairs
class LLMTruncatedError(LLMError): raw: str  # done_reason == "length" after 1 retry
class PromptTooLargeError(LLMError): estimate: int; limit: int
```

```python
# llm/service.py
class LLMService:
    def __init__(self, registry: Mapping[Role, RoleSpec], urls: Mapping[Endpoint, str],
                 transport: Transport, events: EventSink, *, timeout_s: float,
                 sleep: Callable[[float], None] = time.sleep) -> None: ...
    def structured(self, role: Role, messages: Sequence[Message], schema: type[M],
                   *, think: bool = False) -> M: ...
    def text(self, role: Role, messages: Sequence[Message], *, think: bool = False) -> str: ...
```

The call pipeline in `structured()`:
1. **Budget check.** If `estimate_tokens(messages) > num_ctx - num_predict`, raise
   `PromptTooLargeError`.
2. **Acquire** the role's semaphore.
3. **Availability loop:** at most 3 retries with backoff 1 s / 2 s / 4 s on
   `LLMUnavailableError`, then re-raise.
4. **Length handling:** on `done_reason == "length"`, retry once with
   `num_predict = min(2 × num_predict, num_ctx − prompt estimate)`, then `LLMTruncatedError`.
5. **Parsing:** `strip_think(content)`, then `parse_structured`.
6. **Repair:** on a JSON or validation error, append `repair_messages(...)` and call again, at
   most 2 times, then `LLMOutputError(raw)`.
7. **Telemetry:** emit one `llm_call` event per transport call with role, model, endpoint,
   attempt, outcome, tokens, `total_ms`, `load_ms` and `done_reason`. Never content, never
   thinking.

`text()` runs the same pipeline without steps 5–6, but still strips `<think>`.

```python
# adapters/ollama_instance.py
class InstanceState(StrEnum): ADOPTED; STARTED; FAIL_OPEN; DISABLED
@dataclass(frozen=True)
class InstanceStatus: state: InstanceState; reason: str | None

def instance_env(base: Mapping[str, str], port: int, gpu: int, models_dir: str | None) -> dict[str, str]
def ensure_own_instance(settings: Settings, probe: InstanceProbe, spawner: Spawner,
                        clock: Clock, events: EventSink) -> InstanceStatus
def endpoint_urls(settings: Settings, status: InstanceStatus) -> dict[Endpoint, str]
```

`ensure_own_instance` decides as follows:
- disabled → `DISABLED`, with an `info` event;
- the port answers `/api/version` → `ADOPTED`;
- no `ollama` binary, an invalid GPU index, or no model store found → `FAIL_OPEN(reason)`;
- otherwise spawn `ollama serve` with `instance_env(...)` and `start_new_session=True`, then poll
  `/api/version` every 0.5 s until `UDR_OWN_OLLAMA_STARTUP_TIMEOUT_S`:
  - up → `STARTED`;
  - timeout → terminate the process, `FAIL_OPEN("startup timeout")`.

Every `FAIL_OPEN` emits an `ollama_fail_open` warning event. `endpoint_urls` maps
`Endpoint.OWN` to the shared URL in the `FAIL_OPEN` and `DISABLED` states.

The daemon deliberately outlives the CLI and is adopted next time, like the summarizer's pattern.
M10's `udr-ollama` unit takes over its lifecycle.

`instance_env` sets exactly these variables:
- `OLLAMA_HOST=127.0.0.1:<port>`
- `CUDA_VISIBLE_DEVICES=<gpu>`
- `CUDA_DEVICE_ORDER=PCI_BUS_ID`
- `OLLAMA_VULKAN=0`
- `OLLAMA_NUM_PARALLEL=1`
- `OLLAMA_MAX_LOADED_MODELS=2`
- `OLLAMA_MODELS=<models_dir>`

The model-store search order is `UDR_OLLAMA_MODELS_DIR`, then `/usr/share/ollama/.ollama/models`
(world-readable here), then `~/.ollama/models`. Port the logic of
`../local_summarizer/src/ollama_server.py` (`find_models_dir`, `_spawn`, `_wait_until_serving`)
and the device-order rules in `gpu_placement.py`. Both are the owner's Apache-2.0 code.

```python
# calibration.py
@dataclass(frozen=True)
class CtxMeasurement: num_ctx: int; size: int; size_vram: int
CANDIDATES = (32768, 24576, 16384, 12288)
def pick_num_ctx(measurements: Sequence[CtxMeasurement]) -> int | None   # largest with size_vram == size
def load_calibration(path: Path) -> Calibration | None
def save_calibration(path: Path, cal: Calibration) -> None                # via artifacts.write_json
```

Measurement lives in the adapter. For each candidate, largest first:
1. Send a 1-token chat to `reason` on the own endpoint with `options.num_ctx = candidate`.
2. Call `ps()` and read the model's `size` and `size_vram`.
3. Stop at the first candidate that fits.

The result goes to `data/calibration.json` as
`{model, gpu, reason_num_ctx, measured_at, measurements[]}`. `build_registry` uses it. Without
it, `reason` falls back to 16384 and doctor warns.

```python
# doctor.py
class Level(StrEnum): OK; WARNING; ERROR
@dataclass(frozen=True)
class Check: name: str; level: Level; detail: str
@dataclass(frozen=True)
class DoctorSnapshot:
    settings: Settings; instance: InstanceStatus; models: Mapping[Endpoint, frozenset[str] | None]
    free_disk_bytes: int; gpu_indices: Sequence[int] | None
    foreign_gpu_processes: Sequence[str]; calibration: Calibration | None
def evaluate(snapshot: DoctorSnapshot, registry: Mapping[Role, RoleSpec]) -> list[Check]
def exit_code(checks: Sequence[Check]) -> int      # 1 if any ERROR, else 0
def render(checks: Sequence[Check]) -> str         # one line per check; --json → list of dicts
```

**Doctor checks:**

| Check | ERROR when | WARNING when |
|---|---|---|
| `shared_endpoint` | `/api/tags` unreachable | – |
| `own_instance` | not ADOPTED or STARTED (unless DISABLED, which is OK/info) | – |
| `models` | a role's model is not on its effective endpoint. Every missing `role → model @ url` is named. Tags compare normalized, so a missing tag equals `:latest`. | – |
| `disk` | free space under `UDR_MIN_FREE_DISK_GB` (20) | – |
| `gpu` | `UDR_OWN_OLLAMA_GPU` not in the `nvidia-smi` index list | – |
| `calibration` | – | missing, or `reason_num_ctx` < 16384 (R7) |
| `gpu_sharing` | – | processes other than our daemon are on the pinned GPU (R6) |

## 5. Configuration (all optional; defaults apply)

| Env var | Default | Notes |
|---|---|---|
| `UDR_DATA_DIR` | `data` | runtime root; gitignored |
| `UDR_SHARED_OLLAMA_URL` | `http://127.0.0.1:11434` | must be loopback (AC7) |
| `UDR_OWN_OLLAMA_ENABLED` | `true` | `false` → both endpoints use the shared URL |
| `UDR_OWN_OLLAMA_PORT` | `11436` | 11435 belongs to local_summarizer |
| `UDR_OWN_OLLAMA_GPU` | `1` | nvidia-smi index (PCI bus order) |
| `UDR_OWN_OLLAMA_STARTUP_TIMEOUT_S` | `30` | AC6 |
| `UDR_OLLAMA_BINARY` | `ollama` | looked up on PATH |
| `UDR_OLLAMA_MODELS_DIR` | auto | see §4 search order |
| `UDR_MODEL_REASON` / `_EXTRACT` / `_SUMMARIZE` / `_OCR` | PRD §3.1 table | |
| `UDR_NUM_CTX_EXTRACT` / `_SUMMARIZE` / `_OCR` | 8192 / 16384 / 8192 | `reason` comes from calibration |
| `UDR_LLM_TIMEOUT_S` | `900` | per transport call; covers model load |
| `UDR_MIN_FREE_DISK_GB` | `20` | AC1 |

**Role defaults** (`llm/roles.py`, overridable by the env vars above):

| Role | Endpoint | num_ctx | num_predict | temperature | keep_alive | max concurrency |
|---|---|---|---|---|---|---|
| reason | own | calibrated, else 16384 | 8192 | 0.2 | `30m` | 1 |
| extract | own | 8192 | 2048 | 0.0 | `30m` | 2 |
| summarize | shared | 16384 | 4096 | 0.1 | `10m` | 2 |
| ocr | shared | 8192 | 4096 | 0.0 | `5m` | 1 |

## 6. Work steps (red → green → gate → commit)

Each step follows the loop in [AGENTS.md](../../AGENTS.md) §5.3:
1. write the tests;
2. see them fail for the expected reason;
3. implement the minimum;
4. `uv run pre-commit run --all-files`;
5. commit.

Report the red and green results in each commit summary.

1. **Housekeeping.**
   - Delete `core.py` and `test_core.py`.
   - `uv add ollama pydantic pydantic-settings typer`.
   - In `pyproject.toml`: add the pytest marker `live` (`markers = [...]`) and
     `addopts += ["-m", "not live"]`; add `[project.scripts] udr = "app.cli:main"`.
   - `tests/conftest.py`: `_block_network` skips tests marked `live`.
   - `tests/test_conftest.py`: a new test proves unmarked tests are still blocked.
   - Commit: `chore: drop template example, add M1 dependencies and live marker`.
2. **Config.** `test_config.py`, then `config.py`.
   - Loopback validation: `localhost`, `127.0.0.1`, `[::1]` pass; `10.0.0.1`, `example.com` and
     `192.168.1.5` raise `ValidationError`.
   - Env overrides, defaults.
   - Commit: `feat(config): settings with loopback-only Ollama URLs`.
3. **Types, errors, roles.** `test_roles.py`, then `llm/types.py`, `llm/errors.py`,
   `llm/roles.py`.
   - Defaults equal the §5 table.
   - An env model override wins.
   - The calibration value sets `reason.num_ctx`; without calibration it is 16384.
   - Commit: `feat(llm): role registry with PRD defaults`.
4. **Structured helpers.** `test_structured.py`, then `llm/structured.py`.
   - `strip_think` handles leading, multiple, unclosed and case-variant `<think>` blocks.
   - `parse_structured`: valid → model; invalid JSON or a schema violation → `StructuredParseError`
     carrying the message.
   - `repair_messages` appends the raw assistant output plus a user correction that names the
     error.
   - `estimate_tokens` = ceil(chars / 3).
   - Commit: `feat(llm): structured-output helpers`.
5. **Events and artifacts.** `test_events.py`, `test_artifacts.py`, then `events.py`,
   `artifacts.py`.
   - Events: one JSON line per event with `ts`, `type`, `level`, `data`.
   - `write_text` and `write_json` strip `<think>` from every string, including nested ones.
   - Writes are atomic: tmp file plus `os.replace`, and no temp file is left behind.
   - Commit: `feat: event sink and think-safe artifact writer`.
6. **LLM service.** `test_service.py` with `ScriptedTransport`, then `llm/service.py` and
   `llm/fakes.py`. Tests:
   - AC3: valid; invalid, invalid, then valid means 3 calls; invalid ×3 raises `LLMOutputError`
     with the raw output.
   - AC4: `num_predict` doubles and is capped, then `LLMTruncatedError`.
   - AC5: thinking stripped.
   - Unavailability: retries with the injected `sleep` and backoff 1/2/4, then
     `LLMUnavailableError`.
   - Model missing: no retry.
   - `PromptTooLargeError`.
   - Semaphore bound per role, checked with a recording semaphore.
   - Telemetry fields, with no content present.
   - Commit: `feat(llm): LLM service with repair, length and availability handling`.
7. **Transport.** `test_ollama_transport.py`, then `adapters/ollama_transport.py`. It uses an
   injected `client_factory`, so no network is needed.
   - Field mapping to `ChatReply` (ns → ms).
   - `ResponseError` 404 → `LLMModelMissingError`; 5xx, `ConnectError` and timeout →
     `LLMUnavailableError`.
   - `OllamaAdmin.tags` / `ps` / `version` parsing, and tag normalization.
   - Commit: `feat(adapters): ollama transport and admin client`.
8. **Own instance.** `test_ollama_instance.py`, then `adapters/ollama_instance.py` and
   `adapters/system_probe.py`. Fakes for probe, spawner and clock.
   - `instance_env` returns exactly the §4 variables.
   - Adopt when the port already serves.
   - Spawn, then serving → `STARTED`.
   - Timeout (fake clock past 30 s) → process terminated, `FAIL_OPEN`, warning event (AC6).
   - Missing binary, invalid GPU or no store → `FAIL_OPEN` with a reason.
   - `endpoint_urls` falls back to the shared URL.
   - Commit: `feat(adapters): own ollama instance with adopt and fail-open`.
9. **Calibration.** `test_calibration.py`, then `calibration.py` and the adapter measurement.
   - `pick_num_ctx` returns the largest full-VRAM candidate, or `None` if none fits.
   - Measurement stops at the first fit.
   - `calibration.json` round-trips (AC2).
   - Commit: `feat: reason num_ctx calibration`.
10. **Doctor and CLI.** `test_doctor.py` (pure `evaluate`) and `test_cli.py` (typer
    `CliRunner`, with `bootstrap.build_doctor_snapshot` monkeypatched).
    - AC1: all green → exit 0; missing models → exit 1 and every name listed.
    - Own instance down → error.
    - Disk under 20 GB → error; invalid GPU → error.
    - Calibration missing or under 16384 → warning with exit 0.
    - `--json` output.
    - `--calibrate` calls the measurement and writes the file.
    - Commit: `feat(cli): udr doctor with calibration`.
11. **Docs.**
    - `README.md` replaces the template text: purpose, quickstart (`uv sync`, `.env`,
      `uv run udr doctor --calibrate`), links to the PRD and IMPLEMENTATION.md.
    - `.env.example` lists `TAVILY_API_KEY`, `OPENALEX_MAILTO` and `UDR_GUI_API_KEY`, plus
      commented `UDR_*` overrides.
    - AGENTS.md §5.2 gets the PRD §3.4 boundaries: prompts in `src/app/prompts/`, LangGraph only in
      `src/app/graphs/`, network and LLM only in `src/app/adapters/`, GUI only via the API client.
    - [docs/architecture.md](../architecture.md): the layers table with the real modules.
    - IMPLEMENTATION.md:
      - add `uv run udr doctor` and `uv run pytest -m live` to the run/verify table;
      - update the module map;
      - set phase 1 to `done` with the proving tests;
      - clear the open issues M1 resolved.
    - Run `/documentation-update`.
    - Commit: `docs: README, conventions and module map for M1`.

## 7. Acceptance criteria → tests

| PRD M1 AC | Proven by |
|---|---|
| 1 doctor exit 0 / 1 naming missing models and endpoints | `test_doctor.py::test_all_green_exits_zero`, `::test_missing_models_are_all_named`, `::test_own_instance_down_is_error`, `::test_low_disk_is_error`; `test_cli.py::test_doctor_exit_codes` |
| 2 `--calibrate` writes the largest full-VRAM num_ctx | `test_calibration.py::test_pick_largest_full_vram`, `::test_roundtrip`; `test_cli.py::test_calibrate_writes_file` |
| 3 structured: 2 repairs, then `LLMOutputError` with raw output | `test_service.py::test_repair_succeeds_on_third_call`, `::test_output_error_after_two_repairs` |
| 4 length → one doubled retry, then `LLMTruncatedError` | `test_service.py::test_length_retry_doubles_capped`, `::test_truncated_error` |
| 5 `<think>` stripped, never persisted | `test_structured.py::test_strip_think_*`; `test_artifacts.py::test_think_never_written` |
| 6 fail-open after 30 s with a warning event | `test_ollama_instance.py::test_startup_timeout_fails_open` |
| 7 non-loopback URL rejected | `test_config.py::test_rejects_non_loopback` |

| Edge case (PRD) | Handling / test |
|---|---|
| Ollama down | 3 retries with backoff, then `LLMUnavailableError`: `test_service.py::test_unavailable_retries` |
| Model evicted mid-run | No special code; the load time sits inside `UDR_LLM_TIMEOUT_S`, and `load_ms` is recorded: `test_service.py::test_telemetry_fields` |
| Port already served | Adopted: `test_ollama_instance.py::test_adopts_running_daemon` |
| Invalid GPU index | Runtime fail-open, doctor error: `test_ollama_instance.py::test_invalid_gpu`, `test_doctor.py::test_invalid_gpu_is_error` |
| Too many concurrent calls | Per-role semaphore: `test_service.py::test_role_semaphore_bound` |

## 8. Live verification (manual; results go to IMPLEMENTATION.md)

1. `uv run udr doctor --calibrate`:
   - expect exit 0;
   - record `reason_num_ctx` and whether the own instance was STARTED or ADOPTED.
2. `uv run pytest -m live`. Its checks:
   - one `structured()` call per role against the real models, with a tiny two-field schema;
   - `reason` with `think=True` returns clean JSON;
   - telemetry events are written.
3. `ss -ltn | grep 11436` shows `127.0.0.1:11436` only.

## 9. Risks specific to M1

| Risk | Handling |
|---|---|
| `qwen3.8-27b` does not fit 32k context on one 4090 | Calibration picks a lower value. Under 16384 is a doctor warning and PRD trigger R7; tell the owner. |
| `/usr/share/ollama/.ollama/models` is not readable for user `he` | Check during step 8. If it is not readable, set `UDR_OLLAMA_MODELS_DIR` explicitly and document it. Do not copy models. |
| `format` + `think=True` combination not honoured by a model | Covered by the live test in §8. If it fails, that role keeps `think=False` for structured calls, and the result goes into this file. |
| GPU 1 also chosen by local_summarizer's auto-pinned daemon | The doctor `gpu_sharing` warning. The owner decides on the index. |
