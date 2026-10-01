"""Prompts owned by the LLM layer itself."""

REPAIR_PROMPT = (
    "Your previous reply could not be used: {error}\n"
    "Reply again with ONLY a single JSON object that fits the requested schema. "
    "No prose, no code fences, no explanation."
)

CALIBRATION_PROBE_PROMPT = "Reply with the single word: ok"
