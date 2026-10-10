# Task for Antigravity: frontend

Read `CONTRACT.md` and `ADVOCATE_PROMPT.md`. Work only in `web/`. The backend is built in parallel by another agent: develop against a small fake server inside `web/` (`web/mock_server.py` or a JS mock) that emits the messages in the contract, so you never wait for it.

Build a single-page app (plain HTML, CSS and JS, no build step, served by `python3 -m http.server`) with a calm, minimal, elegant look (light cream background, deep teal text, amber accent for flags; large readable text; works at 1440x810 for a projector). It connects to `ws://127.0.0.1:8000/ws`.

Screen layout:
- Left: the conversation. Each line shows the original text and its translation, labeled Tenant or Landlord. A live waveform and a visible "listening / thinking / speaking" state.
- Right: "Advocate" panel. Flag chips on each landlord line with plain words: money term, penalty, soft no (vague commitment), promise, rule. A "soft no" shows the note "ask for it in writing". A running list "Promises so far".
- Bottom: a big mic button (push to talk, hold space), a language selector for the tenant (default Ukrainian), a "Landlord simulator" drawer listing the 20 lines from `data/landlord_lines.json` with a Play button each (calls `POST /landlord/say`), and an "Approve and prepare summary" button that stays disabled until at least one promise or flag exists and shows the returned two-language summary.
- Honesty footer: "Not legal advice. Flags are candidates. Nothing is sent."
- Also a "Score" tab that loads `eval/results.json` if present and shows precision, recall and promise accuracy using the shape in `score.py`.

Requirements: keyboard shortcuts with visible buttons as fallback; respect reduced motion; no external JS libraries except what you can vendor locally; no API key in the browser; handle disconnects with a clear message and a retry button; test at narrow width too.

When finished: screenshot the main screen with the mock data and put it in `web/screenshot.png`, commit, and report what works and what you could not verify. Do not push to any remote.
