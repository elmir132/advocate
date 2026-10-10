# Advocate backend

FastAPI service for the live tenant/landlord interpreter. It uses the official `google-genai` SDK, reads `GEMINI_API_KEY` from the ignored repository-root `.env` or process environment, and never returns or logs the key. Models are selected only after the authenticated SDK `models.list()` response advertises the required action. Calls have a 30-second SDK timeout and at most three attempts, with a 0.5-second pause, only for HTTP 503 or explicit overloaded errors. Streaming audio chunks are never retried. If Live setup fails or no audio transcription arrives within 12 seconds, buffered tenant audio is sent to text-mode `generateContent` as WAV for transcription and translation; typed tenant text similarly falls back to text-mode translation.

## Confirmed model IDs

Checked on 2026-10-10 with `google-genai` 1.75.0 using the key in the ignored `.env`. These IDs appeared in the authenticated model list with the stated action; code still discovers them at runtime rather than assuming a saved list remains current.

| Use | Model IDs confirmed for this key | Advertised action |
| --- | --- | --- |
| Live interpretation | `gemini-3.8-live`, `gemini-3.8-live-extended-thinking` | `bidiGenerateContent` |
| Text fallback / flag analysis (selection order) | `gemini-3.5-flash-lite`, `gemini-3.8-flash`, `gemini-3.7-flash`, `gemini-3.6-flash`, `gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gemini-3-flash-preview`, `gemini-2.5-flash` | `generateContent` |
| TTS | `gemini-3.8-flash-tts`, `gemini-3.8-flash-lite-tts` | `generateContent` |
| Transcription (available, not selected by this backend) | `gemini-3.5-transcribe`, `gemini-3.5-transcribe-live` | `generateContent`, `bidiGenerateContent` respectively |
| Speech translation (available, not selected by this backend) | `gemini-3.5-live-translate-preview` | `bidiGenerateContent` |

The current Google model directory and the authenticated list use `gemini-3.5-live-translate-preview`; the slide shorthand `gemini-3.5-live-translate` is not the exact ID returned by the key. The other slide-named live, transcribe-live, and TTS IDs above were returned by the key. This is an availability check, not a guarantee about quota, billing, latency, or future access. To refresh the complete account-visible catalog (model IDs and supported actions only):

```bash
backend/.venv/bin/python -m backend.list_models
```

The 20-line text evaluation completed with the first selected model, `gemini-3.5-flash-lite`: micro precision `0.897`, micro recall `1.000`, promise accuracy `0.950`. A structured generation attempt on `gemini-3.8-flash` returned HTTP 504 (`DEADLINE_EXCEEDED`), so it is kept as a later candidate rather than preferred. Elmir reports that `gemini-3.8-live` with AUDIO output and `output_audio_transcription` returned a Ukrainian audio translation using `google-genai` 1.75.0, and that `gemini-3.8-flash-tts` returns `audio/wav`. Unary TTS WAV is parsed and converted to headerless 16 kHz PCM before Live input; Live's raw 24 kHz PCM audio is sent to the browser, with a WAV-to-24 kHz PCM normalization path as a safeguard. The text fallback successfully transcribed and translated the official sample PCM clip.

Current SDK documentation: [Live API SDK guide](https://ai.google.dev/gemini-api/docs/live-api/get-started-sdk), [model directory](https://ai.google.dev/gemini-api/docs/models), [structured output guide](https://ai.google.dev/gemini-api/docs/structured-output), and [Google Gen AI Python SDK](https://googleapis.github.io/python-genai/).

## Setup and run

From the repository root:

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -r backend/requirements.txt
backend/.venv/bin/python -m backend.list_models
backend/.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
```

Put the key only in the already git-ignored repository-root `.env` as `GEMINI_API_KEY=...`. `/health` reports `live: true` when the key is present; it does not promise a model request will succeed. The key stays server-side.

Run the offline tests:

```bash
backend/.venv/bin/pytest -q backend/tests eval/tests
```

Run the 20-line text-mode evaluation and write `eval/results.json`:

```bash
backend/.venv/bin/python eval/run_eval.py
```

Run its no-key fake-model path:

```bash
backend/.venv/bin/python eval/run_eval.py --dry-run
```

## Endpoints

- `GET /health` returns `{"ok":true,"live":true|false}`.
- `GET /` and `/web/*` serve the frontend; `/data/landlord_lines.json` serves the simulator lines; `/eval/results.json` serves the evaluation output or returns 404 when it is missing. CORS allows `http://127.0.0.1` and `http://localhost` on any port.
- `WS /ws` accepts the JSON message types in `CONTRACT.md`. Audio input is base64 PCM16, 16 kHz, mono. `tenant_text` is treated as a tenant transcript, sent through Live text input where available, and otherwise translated to English in text mode. Live audio outputs are sent to the browser as base64 headerless PCM16, 24 kHz, mono; structured landlord analysis is separately requested in text mode and filtered to the five contract flags.
- `POST /landlord/say` sends the transcript, analysis, and complete text translation before returning `{"ok":true}`. It then generates landlord speech in a background task and feeds the cached WAV to Live, whose translated audio continues streaming to the browser. WAV files and one-attempt markers are stored under the ignored `backend/.cache/landlord-audio/` folder; each distinct ID/text pair is synthesized at most once.
- `POST /landlord/warmup` explicitly processes the 20 demo lines sequentially and returns generated/cached/unavailable counts. It is never started automatically; repeated calls reuse cached audio, and a failed attempted line is not regenerated unless its cache entry is removed.
- Live output transcription fragments are accumulated and sent as one complete `translation` message at `turn_complete`; audio chunks remain streamed as they arrive.

To warm the TTS cache on demand, start the server and run:

```bash
curl -X POST http://127.0.0.1:8000/landlord/warmup
```

Conversation content is kept in memory for the current WebSocket session only. The backend does not send or sign anything. A two-language summary is generated only when that session sends `approve_summary`; summaries and notes are instructed not to provide legal advice.
