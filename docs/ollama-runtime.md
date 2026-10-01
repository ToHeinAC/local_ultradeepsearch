# Ollama runtime: own instance, calibration, doctor

How the app reaches its models and checks that it can. Requirements: [PRD.md](../PRD.md) §3.1 and
§4 M1. The call pipeline itself: [llm-layer.md](llm-layer.md).

## Two endpoints

| Endpoint | Default URL | Roles | Why |
|---|---|---|---|
| shared | `http://127.0.0.1:11434` (system daemon) | summarize, ocr | Other apps use it; we never change it |
| own | `http://127.0.0.1:11436` | reason, extract | Pinned to one GPU so its VRAM is predictable |

Both URLs must be loopback; `Settings` rejects anything else and the adapters refuse it again.

## Own instance

`ensure_own_instance` (`adapters/ollama_instance.py`) decides, in order:

1. `UDR_OWN_OLLAMA_ENABLED=false` → `disabled`; both endpoints use the shared daemon.
2. Something answers `/api/version` on the own port → `adopted`.
3. Otherwise it checks that the `ollama` binary, the configured GPU and a model store exist, then
   starts `ollama serve` detached (`start_new_session`) and polls every 0.5 s.
   - Up within `UDR_OWN_OLLAMA_STARTUP_TIMEOUT_S` (30) → `started`.
   - The process exits early → `fail_open`.
   - The timeout passes → the process is terminated (killed after 5 s if it ignores that) and the
     result is `fail_open`.
4. Any failed precondition → `fail_open` with a reason.

On `fail_open` an `ollama_fail_open` warning event is written and the own endpoint resolves to the
shared URL, so the app keeps working unpinned.

The daemon runs with `OLLAMA_HOST=127.0.0.1:<port>`, `CUDA_VISIBLE_DEVICES=<gpu>`,
`CUDA_DEVICE_ORDER=PCI_BUS_ID` (otherwise CUDA's fastest-first order can pick another card than
`nvidia-smi` index N), `OLLAMA_VULKAN=0` (otherwise a card hidden from CUDA reappears as a Vulkan
device), `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=2` and `OLLAMA_MODELS=<store>`.

The model store is `UDR_OLLAMA_MODELS_DIR` if it holds manifests, else the candidate with the most
manifests among `~/.ollama/models`, `/usr/share/ollama/.ollama/models` and
`/var/lib/ollama/.ollama/models`. Not "the first that exists": an empty `~/.ollama/models` would
otherwise shadow the real store, and models are not copied.

**Lifecycle.** The daemon outlives the command that started it and is adopted next time. Until the
systemd unit of M10 exists, stop it by hand: `ss -ltnp | grep :11436` shows its PID, then
`kill <pid>`.

## Calibration

`udr doctor --calibrate` finds the largest `reason` context that loads entirely into VRAM
(`/api/ps`: `size_vram >= size`), because a model that spills to the CPU runs several times slower.

- Candidates, largest first: 32768, 24576, 16384, 12288. Each is a 1-token chat with that `num_ctx`
  followed by `/api/ps`; it stops at the first fit.
- The model is unloaded afterwards (an empty chat with `keep_alive` 0s), so a doctor run does not
  hold the VRAM.
- Result: `<data_dir>/calibration.json` with `model`, `gpu`, `reason_num_ctx`, `measured_at` and the
  measurements. It only applies to the model and GPU it was measured for; otherwise the registry
  falls back to 16384 and the doctor warns.
- It refuses to run unless the own instance is `adopted` or `started`, and exits 1 if even 12288
  spills to the CPU.

Measured on this host (RTX 4090, `qwen3.8-27b:latest`): 32768 fits entirely (19.3 GB in VRAM),
in about 40 s including the model load.

## Doctor

`udr doctor [--json] [--calibrate]` gathers a snapshot (`bootstrap.collect_snapshot`), evaluates it
with the pure `doctor.evaluate`, prints one line per check and exits 1 only if a check is an error.
With `--json`, stdout is a JSON list and messages go to stderr.

| Check | Error when | Warning when |
|---|---|---|
| `shared_endpoint` | `/api/tags` does not answer | |
| `own_instance` | state is `fail_open` | |
| `model:<role>` (one each) | model not installed on its effective endpoint, or endpoint unreachable | |
| `disk` | free space below `UDR_MIN_FREE_DISK_GB` (20) | |
| `gpu` | configured GPU missing from `nvidia-smi`, or `nvidia-smi` unavailable | |
| `calibration` | | missing, or below 16384 (PRD risk R7) |
| `gpu_sharing` | | more than 2 GiB of used VRAM on our GPU is not explained by our own loaded models (PRD risk R6) |

`gpu` passes without checking when the own instance is disabled, and `gpu_sharing` only applies
while our instance is running. The doctor itself starts the own instance if needed, so its result
reflects what the app would get.

## Configuration

All variables are optional. They are read from the environment and from `.env`.

| Variable | Default |
|---|---|
| `UDR_DATA_DIR` | `data` |
| `UDR_SHARED_OLLAMA_URL` | `http://127.0.0.1:11434` |
| `UDR_OWN_OLLAMA_ENABLED` | `true` |
| `UDR_OWN_OLLAMA_PORT` | `11436` |
| `UDR_OWN_OLLAMA_GPU` | `1` (`nvidia-smi` index) |
| `UDR_OWN_OLLAMA_STARTUP_TIMEOUT_S` | `30` |
| `UDR_OLLAMA_BINARY` | `ollama` |
| `UDR_OLLAMA_MODELS_DIR` | auto (see above) |
| `UDR_MODEL_REASON` | `qwen3.8-27b:latest` |
| `UDR_MODEL_EXTRACT` | `LiquidAI/lfm2.5-1.2b-instruct:latest` |
| `UDR_MODEL_SUMMARIZE` | `gemma4:e4b` |
| `UDR_MODEL_OCR` | `deepseek-ocr:3b` |
| `UDR_NUM_CTX_EXTRACT` / `_SUMMARIZE` / `_OCR` | `8192` / `16384` / `8192` |
| `UDR_LLM_TIMEOUT_S` | `900` (per transport call, includes model load) |
| `UDR_MIN_FREE_DISK_GB` | `20` |

## Files written

Under `UDR_DATA_DIR` (gitignored): `events.jsonl` for commands outside a run (own-instance and
calibration events) and `calibration.json`.
