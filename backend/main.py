"""FastAPI server for Advocate's live tenant/landlord interpreter."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import re
from contextlib import AsyncExitStack
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from google.genai import types
from pydantic import BaseModel, Field

from backend.ai import GeminiGateway, wav_to_pcm16k


LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")
MAX_AUDIO_BYTES = 512_000
MAX_TEXT_CHARS = 8_000


class LandlordSayRequest(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


class ClientMessageError(ValueError):
    pass


def validate_client_message(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise ClientMessageError("Expected a JSON object with a message type.")
    kind = payload["type"]
    if kind not in {"start", "audio", "landlord_text", "approve_summary", "stop"}:
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
        self.live_failed = False
        self.started = False
        self.tenant_lang = "uk"
        self.live_input_speaker = "tenant"
        self.pending_landlord_translation: asyncio.Future[str] | None = None
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
        connector = gateway.client.aio.live.connect(model=model, config=config)
        state.live_session = await state.exit_stack.enter_async_context(connector)
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
            input_speaker = state.live_input_speaker
            input_transcription = getattr(content, "input_transcription", None)
            if input_transcription and getattr(input_transcription, "text", None):
                text = input_transcription.text.strip()
                if text and input_speaker == "tenant":
                    state.history.append({"speaker": "tenant", "lang": state.tenant_lang, "text": text})
                    await state.send(
                        {"type": "transcript", "speaker": "tenant", "lang": state.tenant_lang, "text": text}
                    )

            output_transcription = getattr(content, "output_transcription", None)
            translated = getattr(output_transcription, "text", None) if output_transcription else None
            if translated:
                translated = translated.strip()
                if translated:
                    to = "tenant" if input_speaker == "landlord" else "landlord"
                    await state.send({"type": "translation", "to": to, "text": translated})
                    state.history.append({"speaker": "translation", "lang": state.tenant_lang, "text": translated})
                    pending = state.pending_landlord_translation
                    if input_speaker == "landlord" and pending and not pending.done():
                        pending.set_result(translated)

            model_turn = getattr(content, "model_turn", None)
            for part in getattr(model_turn, "parts", []) or []:
                inline_data = getattr(part, "inline_data", None)
                data = getattr(inline_data, "data", None) if inline_data else None
                if data:
                    if isinstance(data, str):
                        encoded = data
                    else:
                        encoded = base64.b64encode(data).decode("ascii")
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
        pending = state.pending_landlord_translation
        if pending and not pending.done():
            pending.set_exception(RuntimeError("Live translation unavailable"))


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
                        pass
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
                    try:
                        async with state.live_send_lock:
                            await state.live_session.send_realtime_input(
                                audio=types.Blob(data=audio, mime_type="audio/pcm;rate=16000")
                            )
                    except Exception:
                        state.live_failed = True
                        state.live_session = None
                if state.live_session is None or state.live_failed:
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
            elif kind == "landlord_text":
                if not state.started:
                    await state.error("Start a session before sending landlord lines.")
                    continue
                await send_landlord_line(state, payload["id"], payload["text"])
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
