"""FastAPI server for Advocate's live tenant/landlord interpreter."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google.genai import types
from pydantic import BaseModel, Field

from backend.ai import GeminiGateway, call_with_service_retries, wav_to_pcm16k, wav_to_pcm24k


LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
MAX_AUDIO_BYTES = 512_000
MAX_BUFFERED_AUDIO_BYTES = 2_000_000
MAX_TEXT_CHARS = 8_000
LIVE_AUDIO_GRACE_SECONDS = 12.0
FALLBACK_QUIET_SECONDS = 0.8
REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "web"
LANDLORD_LINES_PATH = REPO_ROOT / "data" / "landlord_lines.json"
EVAL_RESULTS_PATH = REPO_ROOT / "eval" / "results.json"


class LandlordSayRequest(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class ClientMessageError(ValueError):
    pass


def validate_client_message(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise ClientMessageError("Expected a JSON object with a message type.")
    kind = payload["type"]
    if kind not in {"start", "audio", "landlord_text", "tenant_text", "approve_summary", "stop"}:
        raise ClientMessageError("Unsupported message type.")
    if kind == "start":
        language = payload.get("tenant_lang", "uk")
        if not isinstance(language, str) or not LANGUAGE_RE.fullmatch(language):
            raise ClientMessageError("Use a valid tenant language code.")
        payload["tenant_lang"] = language.lower()
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
    elif kind == "tenant_text":
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
            raise ClientMessageError("Tenant text is empty or too long.")
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
        self.live_failed = False
        self.live_audio_confirmed = False
        self.started = False
        self.tenant_lang = "uk"
        self.live_input_speaker = "tenant"
        self.pending_landlord_translation: asyncio.Future[str] | None = None
        self.pending_tenant_translation: asyncio.Future[str] | None = None
        self.unconfirmed_live_audio = bytearray()
        self.fallback_audio = bytearray()
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
async def serve_frontend() -> FileResponse:
    index = WEB_DIR / "index.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="Frontend not found.")
    return FileResponse(index)


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
            if input_transcription and getattr(input_transcription, "text", None):
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
                translated = translated.strip()
                if translated:
                    if input_speaker == "tenant":
                        confirm_live_audio(state)
                    to = "tenant" if input_speaker == "landlord" else "landlord"
                    await state.send({"type": "translation", "to": to, "text": translated})
                    translation_lang = state.tenant_lang if input_speaker == "landlord" else "en"
                    state.history.append({"speaker": "translation", "lang": translation_lang, "text": translated})
                    pending = state.pending_landlord_translation
                    if input_speaker == "landlord" and pending and not pending.done():
                        pending.set_result(translated)
                    pending_tenant = state.pending_tenant_translation
                    if input_speaker == "tenant" and pending_tenant and not pending_tenant.done():
                        pending_tenant.set_result(translated)

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
                state.live_input_speaker = "tenant"
                await state.status("listening")
    except asyncio.CancelledError:
        raise
    except Exception:
        state.live_failed = True
        state.live_session = None
        await move_unconfirmed_audio_to_fallback(state)
        pending = state.pending_landlord_translation
        if pending and not pending.done():
            pending.set_exception(RuntimeError("Live translation unavailable"))
        pending_tenant = state.pending_tenant_translation
        if pending_tenant and not pending_tenant.done():
            pending_tenant.set_exception(RuntimeError("Live translation unavailable"))


def confirm_live_audio(state: ConnectionState) -> None:
    state.live_audio_confirmed = True
    state.unconfirmed_live_audio.clear()
    task = state.live_audio_watchdog
    if task is not None and not task.done():
        task.cancel()
    state.live_audio_watchdog = None


async def _fallback_worker(state: ConnectionState) -> None:
    while state.fallback_audio:
        quiet_for = time.monotonic() - state.last_fallback_audio_at
        if quiet_for < FALLBACK_QUIET_SECONDS:
            await asyncio.sleep(FALLBACK_QUIET_SECONDS - quiet_for)
        audio = bytes(state.fallback_audio)
        state.fallback_audio.clear()
        await state.status("thinking")
        fallback = await gateway.translate_tenant_audio(audio, state.tenant_lang)
        if fallback:
            state.history.append(
                {"speaker": "tenant", "lang": state.tenant_lang, "text": fallback["transcript"]}
            )
            await state.send(
                {
                    "type": "transcript",
                    "speaker": "tenant",
                    "lang": state.tenant_lang,
                    "text": fallback["transcript"],
                }
            )
            await state.send(
                {"type": "translation", "to": "landlord", "text": fallback["translation"]}
            )
        else:
            await state.error("Live interpretation is unavailable; please try again in a moment.")
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


async def send_landlord_line(
    state: ConnectionState, line_id: str, text: str, *, synthesize: bool = False
) -> None:
    async with state.command_lock:
        state.summary_approved = False
        await state.status("thinking")
        state.history.append({"speaker": "landlord", "lang": "en", "text": text})
        result = await gateway.analyze_landlord_line(line_id, text, state.tenant_lang)
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

        live_translation_started = False
        live_translation_received = False
        if synthesize and state.live_session is not None and not state.live_failed:
            wav_data = await gateway.synthesize_landlord_wav(text)
            if wav_data:
                try:
                    pcm_data = wav_to_pcm16k(wav_data)
                    future = asyncio.get_running_loop().create_future()
                    state.pending_landlord_translation = future
                    state.live_input_speaker = "landlord"
                    async with state.live_send_lock:
                        await state.live_session.send_realtime_input(
                            text=(
                                f"The next audio is a landlord speaking in English. Translate it faithfully "
                                f"into {state.tenant_lang}; speak only the translation."
                            )
                        )
                        await state.live_session.send_realtime_input(
                            audio=types.Blob(data=pcm_data, mime_type="audio/pcm;rate=16000")
                        )
                    live_translation_started = True
                    try:
                        await asyncio.wait_for(asyncio.shield(future), timeout=8.0)
                        live_translation_received = True
                    except (asyncio.TimeoutError, RuntimeError):
                        state.live_failed = True
                        state.live_session = None
                    finally:
                        state.pending_landlord_translation = None
                        state.live_input_speaker = "tenant"
                except Exception:
                    state.live_failed = True
                    state.live_session = None

        if not live_translation_started or not live_translation_received:
            translation = result["translation"] or text
            await state.send({"type": "translation", "to": "tenant", "text": translation})
        await state.status("listening")


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
                if state.tenant_lang == "en"
                else await gateway.translate_tenant_text(text, state.tenant_lang)
            )
            if translation:
                await state.send({"type": "translation", "to": "landlord", "text": translation})
                state.history.append({"speaker": "translation", "lang": "en", "text": translation})
            else:
                await state.error("Typed tenant text could not be translated right now.")
        await state.status("listening")


@app.get("/health")
async def health() -> dict[str, bool]:
    return {"ok": True, "live": gateway.key_present}


@app.websocket("/ws")
async def websocket_session(websocket: WebSocket) -> None:
    await websocket.accept()
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
                if state.started:
                    await state.error("This interpreter session is already running.")
                    continue
                state.tenant_lang = payload.get("tenant_lang", "uk")
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
                await state.status("thinking")
                audio = payload["_audio_bytes"]
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
    await send_landlord_line(state, request.id, request.text, synthesize=True)
    return {"ok": True}
