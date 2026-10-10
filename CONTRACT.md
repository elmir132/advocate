# Advocate: shared contract (do not change without telling Elmir)

Product: a live interpreter for a tenant and a landlord that also flags what matters (money terms, penalties, vague "maybe" commitments, promises, restrictions) and prepares a two-language summary only after the tenant approves. Not legal advice. Built for The New Agentic Economy Hackathon (Gemini live voice, transcription, translation, agent with human approval).

## Directories (stay in your lane, so merges are clean)
- `backend/` and `eval/`: Codex.
- `web/`: Antigravity.
- Root files (`score.py`, `data/`, `ADVOCATE_PROMPT.md`, `CONTRACT.md`): Elmir/Claude only.

## Secrets
Keys only in a git-ignored `.env` (GEMINI_API_KEY). Never print, log or commit them. Never put them in browser code.

## Server: `http://127.0.0.1:8000`
- `GET /health` returns `{"ok": true, "live": true|false}` (live = key present).
- `WS /ws` is the live session. JSON text frames both ways.

Client to server:
- `{"type":"start","tenant_lang":"uk"|"ru"|"es"|"zh"|"en"...}` begins a session (default "uk").
- `{"type":"audio","data":"<base64 PCM16 16 kHz mono>"}` tenant microphone chunks.
- `{"type":"audio_end"}` the tenant released the button: transcribe what was sent now. Without it the server waits for a 0.8 s pause.
- `{"type":"landlord_text","id":"L03","text":"..."}` a landlord line (typed, or from the simulator).
- `{"type":"tenant_text","text":"..."}` typed tenant line, the fallback when the microphone fails. Server treats it exactly like transcribed tenant speech.
- `{"type":"approve_summary"}` only after the tenant approves.
- `{"type":"stop"}`.

Server to client:
- `{"type":"status","state":"idle|listening|thinking|speaking"}`
- `{"type":"transcript","speaker":"tenant|landlord","lang":"uk","text":"..."}`
- `{"type":"translation","to":"tenant|landlord","text":"..."}`
- `{"type":"audio","data":"<base64 PCM16 24 kHz mono>"}` spoken translation (optional).
- `{"type":"analysis","id":"L03","flags":["vague_commitment"],"promise":false,"note":"..."}`
  flags use ONLY: financial_term, penalty, vague_commitment, promise, restriction (see `ADVOCATE_PROMPT.md`).
- `{"type":"summary","markdown":"...","languages":["uk","en"]}` sent only after `approve_summary`.
- `{"type":"error","message":"..."}` plain words, no stack traces, no key material.

Also `POST /landlord/say` body `{"id":"L03","text":"..."}`: the landlord simulator. It speaks the line with Gemini TTS into the session as the landlord (fallback: treat as `landlord_text`) and returns `{"ok":true}`.

## Evaluation (no microphone)
`python3 eval/run_eval.py` sends each line in `data/landlord_lines.json` to the model in TEXT mode with `ADVOCATE_PROMPT.md`, writes `eval/results.json` in the shape `score.py` expects (`[{"id","flags","promise"}]`), then prints `score.py` output. It needs a key; with no key it must exit with a clear message, not crash.

## Honesty rules (the UI and the model)
Say "candidate" or "flag", never "illegal" or "you should sue". No legal advice. Show uncertainty. Nothing is sent or signed. Mock data only; no real landlord is contacted.

## Serving (added later)
The backend also serves, same origin on port 8000: `/` and `/web/*` (the `web/` folder), `/data/landlord_lines.json`, and `/eval/results.json` (404 when missing). It allows CORS from `http://127.0.0.1` and `http://localhost` on any port. The frontend uses relative URLs when `location.port === "8000"` and `http://127.0.0.1:8000` otherwise.
