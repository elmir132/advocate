# Antigravity, round 2: make the frontend real

Read `CONTRACT.md` again (it has new items: `tenant_text` and the Serving section). Work only in `web/`. Your first round looks good. Review found these gaps; fix them in this order and commit after each.

1. **Real microphone.** `app.js` has no `getUserMedia` or audio code, so the mic is only an animation. Add hold-to-talk capture: `getUserMedia`, an AudioWorklet (or ScriptProcessor fallback), downsample to 16 kHz mono PCM16, send about 100 ms chunks as `{"type":"audio","data":"<base64>"}`. Send `{"type":"start","tenant_lang":...}` when connected and whenever the language changes. Show a clear message if permission is denied.
2. **Typed fallback.** A small text box under the mic: Enter sends `{"type":"tenant_text","text":"..."}`. This is the demo fallback if the microphone or room noise fails.
3. **Play spoken translations.** Queue incoming `{"type":"audio"}` (PCM16 24 kHz mono) with the Web Audio API, no clicks between chunks, and a mute toggle.
4. **API base.** Today `/landlord/say` is a relative URL, which breaks if the page is not served by the backend. Use a single `API_BASE` rule from the contract's Serving section for the WebSocket and every fetch (`/landlord/say`, `/data/landlord_lines.json`, `/eval/results.json`). Load the simulator lines from `/data/landlord_lines.json`, falling back to the built-in list only if the fetch fails.
5. **Mock only on request.** Do not require editing `index.html`. Load `mock.js` only when the URL has `?mock=1`. Remove the visible placeholder `[Ukrainian translation]` from the mock: use a believable translated sentence.
6. **Projector polish.** Add a small header with the name "Advocate" and a tagline "Speak up, in any language". Make the chat text at least 22 px and flag chips at least 18 px. Keep the calm cream, teal and amber look. Add a visible state label ("Listening", "Thinking", "Speaking").
7. **Summary view.** When `{"type":"summary"}` arrives, show it in a clean panel with both languages side by side and a Copy button. No send button anywhere.

Check at 1440x810 and at 390 px wide. Update `web/screenshot.png` with the mock at `?mock=1`. Keep every key out of the browser. Do not push to any remote. Report what works and what you could not verify.
