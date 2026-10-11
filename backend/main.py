"""FastAPI server for Advocate's live tenant/landlord interpreter."""

from __future__ import annotations

import asyncio
import sys
from array import array
import os
import base64
import binascii
import contextlib
import hashlib
import json
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from google.genai import types
from pydantic import BaseModel, Field

from backend.ai import (
    TTS_CANDIDATES,
    GeminiGateway,
    call_with_service_retries,
    wav_to_pcm16k,
    wav_to_pcm24k,
)


LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
MAX_AUDIO_BYTES = 512_000
MAX_BUFFERED_AUDIO_BYTES = 2_000_000
MAX_TEXT_CHARS = 8_000
LIVE_AUDIO_GRACE_SECONDS = 12.0
MAX_SESSIONS = int(os.environ.get("ADVOCATE_MAX_SESSIONS", "12"))  # concurrent visitors on a shared demo link
FALLBACK_QUIET_SECONDS = 0.8
# Tenant speech goes through the reliable transcribe-then-translate route unless this is "1".
TENANT_LIVE_AUDIO = os.environ.get("ADVOCATE_TENANT_LIVE") == "1"
MIN_SPEECH_BYTES = 16000 * 2 * 4 // 10  # 0.4 s of 16 kHz PCM16
SILENCE_RMS = 180  # below this the clip is treated as silence (int16 scale)
REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"
LANDLORD_LINES_PATH = REPO_ROOT / "data" / "landlord_lines.json"
EVAL_RESULTS_PATH = REPO_ROOT / "eval" / "results.json"
TTS_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "landlord-audio"


class LandlordSayRequest(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class LandlordAudioCache:
    """Persistent, single-attempt-per-line cache for generated landlord WAVs."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _key(line_id: str, text: str) -> str:
        identity = f"{line_id}\0{text}".encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    async def get_or_create(
        self, line_id: str, text: str, synthesize: Callable[[str], Awaitable[bytes | None]]
    ) -> tuple[bytes | None, str]:
        key = self._key(line_id, text)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            audio_path = self.directory / f"{key}.wav"
            attempted_path = self.directory / f"{key}.attempted"
            if audio_path.is_file():
                audio = await asyncio.to_thread(audio_path.read_bytes)
                try:
                    wav_to_pcm16k(audio)
                except Exception:
                    return None, "attempted"
                return audio, "cached"
            if attempted_path.exists():
                return None, "attempted"

            await asyncio.to_thread(self.directory.mkdir, parents=True, exist_ok=True)
            try:
                await asyncio.to_thread(attempted_path.touch, exist_ok=False)
            except FileExistsError:
                return None, "attempted"

            try:
                audio = await synthesize(text)
            except Exception:
                return None, "attempted"
            if not audio:
                return None, "attempted"
            try:
                wav_to_pcm16k(audio)
            except Exception:
                return None, "attempted"

            temporary_path = self.directory / f"{key}.tmp"
            await asyncio.to_thread(temporary_path.write_bytes, audio)
            await asyncio.to_thread(temporary_path.replace, audio_path)
            return audio, "generated"


class ClientMessageError(ValueError):
    pass


def validate_client_message(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise ClientMessageError("Expected a JSON object with a message type.")
    kind = payload["type"]
    if kind not in {"start", "audio", "audio_end", "landlord_text", "tenant_text", "auto_text", "approve_summary", "stop"}:
        raise ClientMessageError("Unsupported message type.")
    if kind == "start":
        language = payload.get("tenant_lang", "uk")
        if not isinstance(language, str) or not LANGUAGE_RE.fullmatch(language):
            raise ClientMessageError("Use a valid tenant language code.")
        payload["tenant_lang"] = language.lower()
    elif kind == "audio_end":
        speaker = payload.get("speaker", "auto")
        if speaker not in {"auto", "tenant", "landlord"}:
            raise ClientMessageError("Speaker must be auto, tenant or landlord.")
        payload["speaker"] = speaker
    elif kind == "audio":
        encoded = payload.get("data")
        if not isinstance(encoded, str):
            raise ClientMessageError("Audio data must be base64 text.")
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ClientMessageError("Audio data must be valid base64.") from None
        if not audio or len(audio) > MAX_AUDIO_BYTES or len(audio) % 2:
            raise ClientMessageError("Audio chunk must be non-empty PCM16 under 512 KB.")
        payload["_audio_bytes"] = audio
    elif kind == "landlord_text":
        line_id, text = payload.get("id"), payload.get("text")
        if not isinstance(line_id, str) or not 1 <= len(line_id) <= 80:
            raise ClientMessageError("Landlord line needs an ID under 80 characters.")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise ClientMessageError("Landlord line text is empty or too long.")
    elif kind in {"tenant_text", "auto_text"}:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            label = "Tenant text" if kind == "tenant_text" else "Auto text"
            raise ClientMessageError(f"{label} is empty or too long.")
    return payload


class ConnectionState:
    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self.send_lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.live_send_lock = asyncio.Lock()
        self.exit_stack = AsyncExitStack()
        self.live_session: Any | None = None
        self.live_reader: asyncio.Task[None] | None = None
        self.live_audio_watchdog: asyncio.Task[None] | None = None
        self.fallback_worker: asyncio.Task[None] | None = None
        self.background_audio_tasks: set[asyncio.Task[None]] = set()
        self.landlord_audio_lock = asyncio.Lock()
        self.live_failed = False
        self.live_audio_confirmed = False
        self.started = False
        self.tenant_lang = "uk"
        self.live_input_speaker = "tenant"
        self.pending_tenant_translation: asyncio.Future[str] | None = None
        self.live_translation_parts: list[str] = []
        self.live_translation_speaker: str | None = None
        self.suppress_live_translation = False
        self.unconfirmed_live_audio = bytearray()
        self.fallback_audio = bytearray()
        self.speaker_hint = "auto"   # auto | tenant | landlord, set by the last audio_end
        self.spoken_lines = 0          # counter for landlord lines heard by microphone
        self.last_fallback_audio_at = 0.0
        self.history: list[dict[str, Any]] = []
        self.summary_approved = False

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_json(message)

    async def status(self, state: str) -> None:
        await self.send({"type": "status", "state": state})

    async def error(self, message: str) -> None:
        await self.send({"type": "error", "message": message})


class SessionHub:
    def __init__(self) -> None:
        self.sessions: list[ConnectionState] = []
        self.lock = asyncio.Lock()

    async def add(self, state: ConnectionState) -> None:
        async with self.lock:
            self.sessions.append(state)

    async def remove(self, state: ConnectionState) -> None:
        async with self.lock:
            with contextlib.suppress(ValueError):
                self.sessions.remove(state)

    async def latest_active(self) -> ConnectionState | None:
        async with self.lock:
            for state in reversed(self.sessions):
                if state.started:
                    return state
        return None


gateway = GeminiGateway()
hub = SessionHub()
landlord_audio_cache = LandlordAudioCache(TTS_CACHE_DIR)
app = FastAPI(title="Advocate API", version="0.1.0")
app.state.gateway = gateway
app.state.hub = hub
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^http://(?:127\.0\.0\.1|localhost)(?::\d+)?$",
    allow_methods=["GET", "HEAD", "OPTIONS", "POST"],
    allow_headers=["*"],
)


@app.get("/")
async def serve_frontend() -> HTMLResponse:
    index = WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Frontend not found.")
    document = index.read_text(encoding="utf-8")
    document = document.replace("<head>", '<head>\n  <base href="/web/">', 1)
    return HTMLResponse(document)


@app.get("/data/landlord_lines.json")
async def serve_landlord_lines() -> FileResponse:
    if not LANDLORD_LINES_PATH.is_file():
        raise HTTPException(status_code=404, detail="Landlord lines not found.")
    return FileResponse(LANDLORD_LINES_PATH, media_type="application/json")


@app.get("/eval/results.json")
async def serve_eval_results() -> FileResponse:
    if not EVAL_RESULTS_PATH.is_file():
        raise HTTPException(status_code=404, detail="Evaluation results not found.")
    return FileResponse(EVAL_RESULTS_PATH, media_type="application/json")


app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")


async def start_live_session(state: ConnectionState) -> None:
    await gateway.initialize()
    model = gateway.select_model("bidiGenerateContent", gateway_live_candidates())
    if model is None or gateway.client is None:
        return
    config = {
        "response_modalities": ["AUDIO"],
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "system_instruction": (
            "You are a faithful, concise live interpreter for a tenant and an English-speaking landlord. "
            f"The tenant speaks {state.tenant_lang}; landlord speech is English. Translate tenant speech into "
            "English for the landlord, and landlord speech into the tenant's language. Never add, soften, "
            "or remove meaning. Do not give legal advice. Speak the translation naturally."
        ),
    }
    try:
        state.live_session = await call_with_service_retries(
            lambda: asyncio.wait_for(
                state.exit_stack.enter_async_context(
                    gateway.client.aio.live.connect(model=model, config=config)
                ),
                timeout=12.0,
            ),
            threaded=False,
        )
        state.live_reader = asyncio.create_task(read_live_session(state))
    except Exception:
        state.live_session = None
        state.live_failed = True


def gateway_live_candidates() -> tuple[str, ...]:
    from backend.ai import LIVE_CANDIDATES

    return LIVE_CANDIDATES


async def read_live_session(state: ConnectionState) -> None:
    try:
        async for response in state.live_session.receive():
            content = getattr(response, "server_content", None)
            if content is None:
                continue
            if state.live_failed:
                continue
            input_speaker = state.live_input_speaker
            input_transcription = getattr(content, "input_transcription", None)
            if (
                input_transcription
                and getattr(input_transcription, "text", None)
                and state.pending_tenant_translation is None
            ):
                text = input_transcription.text.strip()
                if text and input_speaker == "tenant":
                    confirm_live_audio(state)
                    state.history.append({"speaker": "tenant", "lang": state.tenant_lang, "text": text})
                    await state.send(
                        {"type": "transcript", "speaker": "tenant", "lang": state.tenant_lang, "text": text}
                    )

            output_transcription = getattr(content, "output_transcription", None)
            translated = getattr(output_transcription, "text", None) if output_transcription else None
            if translated:
                if input_speaker == "tenant":
                    confirm_live_audio(state)
                if state.live_translation_speaker is None:
                    state.live_translation_speaker = input_speaker
                if state.live_translation_speaker == input_speaker:
                    state.live_translation_parts.append(translated)

            model_turn = getattr(content, "model_turn", None)
            for part in getattr(model_turn, "parts", []) or []:
                inline_data = getattr(part, "inline_data", None)
                data = getattr(inline_data, "data", None) if inline_data else None
                if data:
                    try:
                        pcm_data = base64.b64decode(data, validate=True) if isinstance(data, str) else bytes(data)
                        mime_type = (getattr(inline_data, "mime_type", "") or "").lower()
                        if "wav" in mime_type or "wave" in mime_type:
                            pcm_data = wav_to_pcm24k(pcm_data)
                        if not pcm_data or len(pcm_data) % 2:
                            continue
                    except (ValueError, TypeError):
                        continue
                    encoded = base64.b64encode(pcm_data).decode("ascii")
                    await state.status("speaking")
                    await state.send({"type": "audio", "data": encoded})

            if getattr(content, "turn_complete", False):
                await finish_live_turn(state, input_speaker)
    except asyncio.CancelledError:
        raise
    except Exception:
        state.live_failed = True
        state.live_session = None
        await move_unconfirmed_audio_to_fallback(state)
        state.live_translation_parts.clear()
        state.live_translation_speaker = None
        state.suppress_live_translation = False
        pending_tenant = state.pending_tenant_translation
        if pending_tenant and not pending_tenant.done():
            pending_tenant.set_exception(RuntimeError("Live translation unavailable"))


async def finish_live_turn(state: ConnectionState, input_speaker: str) -> None:
    speaker = state.live_translation_speaker or input_speaker
    translation = join_translation_chunks(state.live_translation_parts)
    should_send_translation = bool(translation) and not state.suppress_live_translation
    state.live_translation_parts.clear()
    state.live_translation_speaker = None

    if should_send_translation:
        to = "tenant" if speaker == "landlord" else "landlord"
        translation_lang = state.tenant_lang if speaker == "landlord" else "en"
        await state.send({"type": "translation", "to": to, "text": translation})
        state.history.append(
            {"speaker": "translation", "lang": translation_lang, "text": translation}
        )

    pending_tenant = state.pending_tenant_translation
    if speaker == "tenant" and pending_tenant and not pending_tenant.done():
        if translation:
            pending_tenant.set_result(translation)
        else:
            pending_tenant.set_exception(RuntimeError("Live translation was empty"))

    state.suppress_live_translation = False
    state.live_input_speaker = "tenant"
    await state.status("listening")


def join_translation_chunks(parts: list[str]) -> str:
    text = ""
    no_space_before = ",.;:!?%)]}»’'\""
    for part in parts:
        chunk = part.strip()
        if not chunk:
            continue
        if text and not text[-1].isspace() and chunk[0] not in no_space_before:
            text += " "
        text += chunk
    return text


def confirm_live_audio(state: ConnectionState) -> None:
    state.live_audio_confirmed = True
    state.unconfirmed_live_audio.clear()
    task = state.live_audio_watchdog
    if task is not None and not task.done():
        task.cancel()
    state.live_audio_watchdog = None


def pcm_rms(pcm16: bytes) -> float:
    """Root-mean-square level of little-endian PCM16, used to tell silence from speech."""
    samples = array("h")
    samples.frombytes(pcm16[: len(pcm16) - (len(pcm16) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    step = max(1, len(samples) // 20000)
    picked = samples[::step]
    return (sum(v * v for v in picked) / len(picked)) ** 0.5


async def _fallback_worker(state: ConnectionState) -> None:
    while state.fallback_audio:
        quiet_for = time.monotonic() - state.last_fallback_audio_at
        if quiet_for < FALLBACK_QUIET_SECONDS:
            await asyncio.sleep(FALLBACK_QUIET_SECONDS - quiet_for)
            continue
        audio = bytes(state.fallback_audio)
        state.fallback_audio.clear()
        if len(audio) < MIN_SPEECH_BYTES:
            await state.error("That was too short to hear. Hold the button while you speak, then let go.")
            await state.status("listening")
            continue
        if pcm_rms(audio) < SILENCE_RMS:
            await state.error("No speech was picked up. Check that the right microphone is selected and try again.")
            await state.status("listening")
            continue
        await state.status("thinking")
        heard = await gateway.transcribe_speech(audio, state.tenant_lang)
        if not heard:
            await state.error("I could not make out any words. Please try again, or type the line.")
            await state.status("listening")
            continue
        # Who spoke? The tenant chose, or we go by language: English is the landlord.
        speaker = state.speaker_hint
        if speaker == "auto":
            if state.tenant_lang == "en":
                speaker = "tenant"
                await state.send({"type": "speaker", "speaker": "tenant", "detected": False,
                                  "note": "Both sides speak English, so pick who is speaking."})
            else:
                speaker = "landlord" if heard["lang"] == "en" else "tenant"
        await state.send({"type": "speaker", "speaker": speaker, "detected": state.speaker_hint == "auto",
                          "lang": heard["lang"]})
        if speaker == "landlord":
            state.spoken_lines += 1
            await send_landlord_line(state, f"V{state.spoken_lines:02d}", heard["text"])
        else:
            await send_tenant_text(state, heard["text"])
        await state.status("listening")


def schedule_text_fallback(state: ConnectionState, audio: bytes) -> None:
    if len(state.fallback_audio) + len(audio) > MAX_BUFFERED_AUDIO_BYTES:
        state.fallback_audio.clear()
        asyncio.create_task(state.error("Audio buffer is full; pause speaking and try again."))
        return
    state.fallback_audio.extend(audio)
    state.last_fallback_audio_at = time.monotonic()
    if state.fallback_worker is None or state.fallback_worker.done():
        state.fallback_worker = asyncio.create_task(_fallback_worker(state))


async def move_unconfirmed_audio_to_fallback(state: ConnectionState) -> None:
    if state.live_audio_watchdog is not None and state.live_audio_watchdog is not asyncio.current_task():
        state.live_audio_watchdog.cancel()
    state.live_audio_watchdog = None
    if state.unconfirmed_live_audio:
        audio = bytes(state.unconfirmed_live_audio)
        state.unconfirmed_live_audio.clear()
        schedule_text_fallback(state, audio)


async def _watch_live_audio(state: ConnectionState) -> None:
    try:
        await asyncio.sleep(LIVE_AUDIO_GRACE_SECONDS)
        if not state.live_audio_confirmed and state.unconfirmed_live_audio:
            state.live_failed = True
            state.live_session = None
            await move_unconfirmed_audio_to_fallback(state)
    except asyncio.CancelledError:
        raise


async def send_landlord_line(state: ConnectionState, line_id: str, text: str) -> dict[str, Any]:
    async with state.command_lock:
        state.summary_approved = False
        await state.status("thinking")
        entry: dict[str, Any] = {"speaker": "landlord", "lang": "en", "text": text}
        state.history.append(entry)
        result = await gateway.analyze_landlord_line(line_id, text, state.tenant_lang)
        entry["flags"] = result["flags"]
        entry["promise"] = result["promise"]
        await state.send({"type": "transcript", "speaker": "landlord", "lang": "en", "text": text})
        await state.send(
            {
                "type": "analysis",
                "id": line_id,
                "flags": result["flags"],
                "promise": result["promise"],
                "note": result["note"],
            }
        )
        translation = result["translation"] or text
        await state.send({"type": "translation", "to": "tenant", "text": translation})
        state.history.append({"speaker": "translation", "lang": state.tenant_lang, "text": translation})
        await state.status("listening")
        return result


async def generate_and_stream_landlord_audio(
    state: ConnectionState, line_id: str, text: str
) -> None:
    """Generate cached landlord speech off-request and feed it to Live for translated audio."""
    try:
        async with state.landlord_audio_lock:
            wav_data, _cache_state = await landlord_audio_cache.get_or_create(
                line_id, text, gateway.synthesize_landlord_wav
            )
            if not wav_data or state.live_session is None or state.live_failed:
                return

            pcm_data = wav_to_pcm16k(wav_data)
            session = state.live_session
            state.live_input_speaker = "landlord"
            state.suppress_live_translation = True
            async with state.live_send_lock:
                await session.send_realtime_input(
                    text=(
                        f"The next audio is a landlord speaking in English. Translate it faithfully "
                        f"into {state.tenant_lang}; speak only the translation."
                    )
                )
                await session.send_realtime_input(
                    audio=types.Blob(data=pcm_data, mime_type="audio/pcm;rate=16000")
                )
    except Exception:
        state.suppress_live_translation = False
        state.live_input_speaker = "tenant"
        state.live_failed = True
        state.live_session = None


async def send_tenant_text(state: ConnectionState, text: str) -> None:
    """Treat typed tenant text as the transcript of tenant speech."""
    async with state.command_lock:
        state.summary_approved = False
        state.history.append({"speaker": "tenant", "lang": state.tenant_lang, "text": text})
        await state.status("thinking")
        await state.send(
            {"type": "transcript", "speaker": "tenant", "lang": state.tenant_lang, "text": text}
        )

        live_translation_received = False
        if state.live_session is not None and not state.live_failed:
            future = asyncio.get_running_loop().create_future()
            state.pending_tenant_translation = future
            state.live_input_speaker = "tenant"
            try:
                async with state.live_send_lock:
                    await state.live_session.send_realtime_input(text=text)
                await asyncio.wait_for(asyncio.shield(future), timeout=8.0)
                live_translation_received = True
            except Exception:
                state.live_failed = True
                state.live_session = None
            finally:
                state.pending_tenant_translation = None
                state.live_input_speaker = "tenant"

        if not live_translation_received:
            translation = (
                text
                if state.tenant_lang.split("-", 1)[0] == "en"
                else await gateway.translate_tenant_text(text, state.tenant_lang)
            )
            if translation:
                await state.send({"type": "translation", "to": "landlord", "text": translation})
                state.history.append({"speaker": "translation", "lang": "en", "text": translation})
            else:
                await state.error("Typed tenant text could not be translated right now.")
        await state.status("listening")


@app.get("/health")
async def health() -> dict[str, Any]:
    from .ai import MAX_MODEL_CALLS, usage

    return {"ok": True, "live": gateway.key_present, "sessions": len(hub.sessions),
            "model_calls": usage["model_calls"], "model_call_limit": MAX_MODEL_CALLS}


@app.websocket("/ws")
async def websocket_session(websocket: WebSocket) -> None:
    await websocket.accept()
    if len(hub.sessions) >= MAX_SESSIONS:
        await websocket.send_json({"type": "error", "message": "The demo is busy right now. Please try again in a minute."})
        await websocket.close()
        return
    state = ConnectionState(websocket)
    await hub.add(state)
    await state.status("idle")
    try:
        while True:
            try:
                payload = validate_client_message(await websocket.receive_json())
            except ClientMessageError as exc:
                await state.error(str(exc))
                continue
            except (ValueError, TypeError):
                await state.error("Send a valid JSON message.")
                continue

            kind = payload["type"]
            if kind == "start":
                requested_language = payload.get("tenant_lang", "uk")
                if state.started:
                    if requested_language == state.tenant_lang:
                        await state.error("This interpreter session is already running.")
                        continue
                    if requested_language != state.tenant_lang:
                        await state.status("thinking")
                        state.tenant_lang = requested_language
                        state.summary_approved = False
                        state.speaker_hint = "auto"
                        state.unconfirmed_live_audio.clear()
                        state.fallback_audio.clear()
                        # The Live session prompt is language-bound. Retire it and use the
                        # reliable text path after a language change rather than translating
                        # with the old language instruction.
                        state.live_failed = True
                        state.live_session = None
                        if state.live_reader is not None:
                            state.live_reader.cancel()
                            with contextlib.suppress(asyncio.CancelledError):
                                await state.live_reader
                            state.live_reader = None
                    await state.status("listening")
                    continue
                state.tenant_lang = requested_language
                state.started = True
                state.history.clear()
                state.summary_approved = False
                await state.status("thinking")
                await start_live_session(state)
                await state.status("listening")
            elif kind == "audio":
                if not state.started:
                    await state.error("Start a session before sending audio.")
                    continue
                audio = payload["_audio_bytes"]
                if not TENANT_LIVE_AUDIO:
                    schedule_text_fallback(state, audio)
                    continue
                await state.status("thinking")
                if state.live_session is not None and not state.live_failed:
                    if not state.live_audio_confirmed:
                        if len(state.unconfirmed_live_audio) + len(audio) <= MAX_BUFFERED_AUDIO_BYTES:
                            state.unconfirmed_live_audio.extend(audio)
                        else:
                            state.live_failed = True
                            state.live_session = None
                            await move_unconfirmed_audio_to_fallback(state)
                            schedule_text_fallback(state, audio)
                        if state.live_audio_watchdog is None and not state.live_failed:
                            state.live_audio_watchdog = asyncio.create_task(_watch_live_audio(state))
                    try:
                        if state.live_session is not None and not state.live_failed:
                            async with state.live_send_lock:
                                await state.live_session.send_realtime_input(
                                    audio=types.Blob(data=audio, mime_type="audio/pcm;rate=16000")
                                )
                    except Exception:
                        state.live_failed = True
                        state.live_session = None
                        if state.live_audio_confirmed:
                            schedule_text_fallback(state, audio)
                        else:
                            await move_unconfirmed_audio_to_fallback(state)
                elif state.live_session is None or state.live_failed:
                    schedule_text_fallback(state, audio)
                await state.status("listening")
            elif kind == "audio_end":
                # The tenant let go of the button: transcribe now instead of waiting for a pause.
                state.speaker_hint = payload.get("speaker", "auto")
                if state.fallback_audio:
                    state.last_fallback_audio_at = 0.0
                    if state.fallback_worker is None or state.fallback_worker.done():
                        state.fallback_worker = asyncio.create_task(_fallback_worker(state))
            elif kind == "auto_text":
                if not state.started:
                    await state.error("Start a session before sending text.")
                    continue
                speaker = await gateway.classify_typed_speaker(payload["text"], state.tenant_lang)
                if speaker is None:
                    await state.error("I could not tell who typed that. Choose Me or Landlord and send it again.")
                    continue
                await state.send({"type": "speaker", "speaker": speaker, "detected": True,
                                  "lang": "en" if speaker == "landlord" else state.tenant_lang})
                if speaker == "landlord":
                    state.spoken_lines += 1
                    await send_landlord_line(state, f"T{state.spoken_lines:02d}", payload["text"])
                else:
                    await send_tenant_text(state, payload["text"])
            elif kind == "landlord_text":
                if not state.started:
                    await state.error("Start a session before sending landlord lines.")
                    continue
                await send_landlord_line(state, payload["id"], payload["text"])
            elif kind == "tenant_text":
                if not state.started:
                    await state.error("Start a session before sending tenant text.")
                    continue
                await send_tenant_text(state, payload["text"])
            elif kind == "approve_summary":
                if not state.started or not state.history:
                    await state.error("There is no conversation to summarize yet.")
                    continue
                state.summary_approved = True
                await state.status("thinking")
                summary = await gateway.summarize(state.history, state.tenant_lang)
                if summary:
                    await state.send({"type": "summary", **summary})
                else:
                    await state.error("The approved summary could not be prepared right now.")
                await state.status("listening")
            elif kind == "stop":
                await state.status("idle")
                break
    except WebSocketDisconnect:
        pass
    finally:
        if state.live_reader is not None:
            state.live_reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.live_reader
        for task in (state.live_audio_watchdog, state.fallback_worker):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        with contextlib.suppress(Exception):
            await state.exit_stack.aclose()
        await hub.remove(state)


@app.post("/landlord/say")
async def landlord_say(request: LandlordSayRequest) -> dict[str, bool]:
    state = await hub.latest_active()
    if state is None:
        raise HTTPException(status_code=409, detail="No active interpreter session.")
    await send_landlord_line(state, request.id, request.text)
    # Spoken landlord audio goes through the Live model and sometimes returned garbled text
    # in the demo, so it is opt-in (ADVOCATE_LANDLORD_AUDIO=1). Text, flags and translation always work.
    if os.environ.get("ADVOCATE_LANDLORD_AUDIO") == "1":
        task = asyncio.create_task(generate_and_stream_landlord_audio(state, request.id, request.text))
        state.background_audio_tasks.add(task)
        task.add_done_callback(state.background_audio_tasks.discard)
    return {"ok": True}


@app.post("/landlord/warmup")
async def landlord_audio_warmup() -> dict[str, int | bool]:
    await gateway.initialize()
    if gateway.select_model("generateContent", TTS_CANDIDATES) is None:
        raise HTTPException(status_code=503, detail="No text-to-speech model is available.")
    try:
        lines = json.loads(LANDLORD_LINES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(status_code=500, detail="Warm-up lines are unavailable.") from None
    if not isinstance(lines, list) or len(lines) != 20:
        raise HTTPException(status_code=500, detail="Warm-up expects the 20 landlord demo lines.")

    generated = 0
    cached = 0
    unavailable = 0
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("id"), str) or not isinstance(line.get("text"), str):
            unavailable += 1
            continue
        audio, cache_state = await landlord_audio_cache.get_or_create(
            line["id"], line["text"], gateway.synthesize_landlord_wav
        )
        if cache_state == "generated" and audio:
            generated += 1
        elif cache_state == "cached" and audio:
            cached += 1
        else:
            unavailable += 1
    return {"ok": True, "total": len(lines), "generated": generated, "cached": cached, "unavailable": unavailable}
