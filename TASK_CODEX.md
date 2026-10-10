# Task for Codex: backend and eval

Read `CONTRACT.md`, `ADVOCATE_PROMPT.md`, `score.py` and `data/landlord_lines.json` first. Work only in `backend/` and `eval/`. Do not edit root files.

Deliver, in this order, committing after each step:
1. `backend/` FastAPI app implementing `/health`, `/ws` and `/landlord/say` exactly as in `CONTRACT.md`. Use the official `google-genai` Python SDK and check the current docs for the Live API. The hackathon slides name these models: `gemini-3.8-live` (and `gemini-3.8-live-extended-thinking` for background tool calls), `gemini-3.5-transcribe-live`, `gemini-3.5-live-translate`, `gemini-3.8-flash-tts`. Confirm the exact IDs available to the key before relying on them (list models). The API key is `GEMINI_API_KEY` from `.env`.
2. If a live model call fails or is unavailable, degrade to plain generate-content with text, so the demo never goes silent. The `analysis` message must be produced from structured JSON output that follows `ADVOCATE_PROMPT.md`; validate it and drop unknown flags.
3. `eval/run_eval.py` as specified, text mode only. Add a `--dry-run` that fakes the model so tests run without a key.
4. Tests (`pytest` is not installed on this Mac: use a venv, `python3 -m venv .venv`). Cover message validation, flag filtering, the no-key path and the dry-run eval.
5. `backend/README.md` with the run commands and the model IDs you actually confirmed.

Rules: never log or print the key; no real network calls in tests; no legal-advice wording; keep functions small. When finished, post a short report: what works, what you could not verify, and the exact commands to run. Do not push to any remote.
