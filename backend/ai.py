"""Google Gen AI integration and strict validation for Advocate."""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import wave
from array import array
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv
from google import genai
from google.genai import types


ROOT = Path(__file__).resolve().parents[1]
PROMPT_PATH = ROOT / "ADVOCATE_PROMPT.md"
PROMPT = PROMPT_PATH.read_text(encoding="utf-8")

ALLOWED_FLAGS = frozenset(
    {"financial_term", "penalty", "vague_commitment", "promise", "restriction"}
)

# IDs are selected only if the authenticated models.list response advertises
# the required action for the current key.
LIVE_CANDIDATES = ("gemini-3.8-live", "gemini-3.8-live-extended-thinking")
TEXT_CANDIDATES = (
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemini-2.5-flash",
)
TTS_CANDIDATES = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")

ANALYSIS_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "id": {"type": "STRING"},
        "translation": {"type": "STRING"},
        "flags": {"type": "ARRAY", "items": {"type": "STRING"}},
        "promise": {"type": "BOOLEAN"},
        "note": {"type": "STRING"},
    },
    "required": ["id", "translation", "flags", "promise", "note"],
}

AUDIO_FALLBACK_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "transcript": {"type": "STRING"},
        "translation": {"type": "STRING"},
    },
    "required": ["transcript", "translation"],
}

SUMMARY_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "markdown": {"type": "STRING"},
        "languages": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["markdown", "languages"],
}

LEGAL_ADVICE_PATTERNS = re.compile(
    r"\b(?:you should sue|you must sue|file (?:a )?lawsuit|the law requires|"
    r"legally required to|you are entitled to sue)\b",
    re.IGNORECASE,
)

SERVICE_MAX_ATTEMPTS = 3
SERVICE_RETRY_DELAY_SECONDS = 0.5


def is_retryable_service_error(error: Exception) -> bool:
    """Retry only explicit 503s or errors that identify an overloaded service."""
    code = getattr(error, "code", None)
    status = getattr(error, "status", None)
    message = getattr(error, "message", "")
    if code == 503 or status == 503:
        return True
    return isinstance(message, str) and "overload" in message.lower()


async def call_with_service_retries(
    operation: Callable[[], Any], *, threaded: bool = True
) -> Any:
    """Run at most three times, pausing briefly only for 503/overload failures."""
    for attempt in range(SERVICE_MAX_ATTEMPTS):
        try:
            if threaded:
                return await asyncio.to_thread(operation)
            return await operation()
        except Exception as error:
            if not is_retryable_service_error(error) or attempt == SERVICE_MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(SERVICE_RETRY_DELAY_SECONDS)


def normalize_analysis(payload: Any, expected_id: str) -> dict[str, Any]:
    """Validate model fields, force the supplied ID, and drop unknown flags."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    raw_flags = payload.get("flags", [])
    if not isinstance(raw_flags, list):
        raw_flags = []
    flags = list(dict.fromkeys(flag for flag in raw_flags if isinstance(flag, str) and flag in ALLOWED_FLAGS))
    promise = payload.get("promise") is True or "promise" in flags
    if promise and "promise" not in flags:
        flags.append("promise")

    translation = payload.get("translation")
    note = payload.get("note")
    if not isinstance(translation, str):
        translation = ""
    if not isinstance(note, str):
        note = ""
    if LEGAL_ADVICE_PATTERNS.search(note):
        note = "This is a candidate flag, not legal advice. You can ask for the point in writing."

    return {
        "id": expected_id,
        "translation": translation.strip(),
        "flags": flags,
        "promise": promise,
        "note": note.strip()[:500],
    }


def _wav_to_pcm16(wav_data: bytes, target_rate: int) -> bytes:
    """Strip a WAV container and convert its PCM samples to mono at target_rate."""
    if target_rate < 1:
        raise ValueError("Target sample rate must be positive")
    with wave.open(io.BytesIO(wav_data), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        raw = wav_file.readframes(wav_file.getnframes())
    if sample_width != 2 or channels < 1 or sample_rate < 1:
        raise ValueError("Unsupported TTS WAV format")

    samples = array("h")
    samples.frombytes(raw)
    if not samples:
        raise ValueError("Empty TTS audio")
    if os.sys.byteorder != "little":
        samples.byteswap()
    if channels > 1:
        mono = array("h")
        for offset in range(0, len(samples), channels):
            frame = samples[offset : offset + channels]
            mono.append(int(sum(frame) / channels))
        samples = mono
    if sample_rate == target_rate:
        output = samples
    else:
        output_count = max(1, round(len(samples) * target_rate / sample_rate))
        output = array("h")
        for index in range(output_count):
            position = index * sample_rate / target_rate
            left = min(int(position), len(samples) - 1)
            right = min(left + 1, len(samples) - 1)
            fraction = position - left
            output.append(round(samples[left] * (1 - fraction) + samples[right] * fraction))
    if os.sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


def wav_to_pcm16k(wav_data: bytes) -> bytes:
    """Convert a TTS WAV payload to headerless mono PCM16 at 16 kHz for Live input."""
    return _wav_to_pcm16(wav_data, 16000)


def wav_to_pcm24k(wav_data: bytes) -> bytes:
    """Convert a WAV payload to headerless mono PCM16 at 24 kHz for browser output."""
    return _wav_to_pcm16(wav_data, 24000)


class GeminiGateway:
    """Lazily discovers key-accessible model IDs and calls the official SDK."""

    def __init__(self, root: Path = ROOT) -> None:
        load_dotenv(root / ".env", override=False)
        self.api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.client = (
            genai.Client(
                api_key=self.api_key,
                http_options=types.HttpOptions(
                    timeout=30_000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                ),
            )
            if self.api_key
            else None
        )
        self.models: dict[str, frozenset[str]] = {}
        self.discovery_attempted = False
        self.discovery_error = False

    @property
    def key_present(self) -> bool:
        return bool(self.api_key)

    async def discover_models(self) -> dict[str, frozenset[str]]:
        if self.discovery_attempted:
            return self.models
        self.discovery_attempted = True
        if self.client is None:
            return self.models
        try:
            model_rows = await call_with_service_retries(lambda: list(self.client.models.list()))
            discovered: dict[str, frozenset[str]] = {}
            for row in model_rows:
                name = getattr(row, "name", "") or ""
                model_id = name.rsplit("/", 1)[-1]
                actions = getattr(row, "supported_actions", None) or []
                discovered[model_id] = frozenset(str(action) for action in actions)
            self.models = discovered
        except Exception:
            # Do not log SDK exception text: it can contain request details.
            self.discovery_error = True
            self.models = {}
        return self.models

    def select_model(self, action: str, candidates: tuple[str, ...]) -> str | None:
        action_aliases = {action, action.lower()}
        for model_id in candidates:
            actions = self.models.get(model_id, frozenset())
            if actions.intersection(action_aliases):
                return model_id
        return None

    async def initialize(self) -> None:
        await self.discover_models()

    async def _generate_json(self, model: str, contents: Any, schema: dict[str, Any]) -> Any:
        if self.client is None:
            return None

        def request() -> str | None:
            response = self.client.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema,
                    temperature=0.1,
                ),
            )
            return response.text

        try:
            text = await call_with_service_retries(request)
            if not text:
                return None
            return json.loads(text)
        except Exception:
            return None

    async def _plain_translate(self, model: str, text: str, lang: str) -> str | None:
        """Translate with an ordinary text call. Structured JSON output sometimes garbled Ukrainian words."""
        if self.client is None:
            return None

        def request() -> str | None:
            response = self.client.models.generate_content(
                model=model,
                contents=(
                    f"Translate faithfully into the language with code {lang}. Use real, correct words only. "
                    f"Reply with the translation only, no quotes and no commentary.\n\n{text}"
                ),
                config=types.GenerateContentConfig(temperature=0.0),
            )
            return response.text

        try:
            out = await call_with_service_retries(request)
        except Exception:
            return None
        out = (out or "").strip()
        return out or None

    async def analyze_landlord_line(self, line_id: str, text: str, tenant_lang: str) -> dict[str, Any]:
        await self.discover_models()
        model = self.select_model("generateContent", TEXT_CANDIDATES)
        if not model:
            return normalize_analysis(
                {
                    "translation": text,
                    "note": "Automated analysis is unavailable; no flags could be confirmed.",
                },
                line_id,
            )
        prompt = (
            f"{PROMPT}\n\n"
            f"Return exactly one JSON object with keys id, translation, flags, promise, note. "
            f"Translate the landlord line faithfully into language code {tenant_lang}. "
            "Set id to the supplied line ID. Keep note short and in the tenant's language. "
            "Do not add legal advice or conclusions. If uncertain, omit the flag and say so.\n"
            f"LANDLORD LINE ID: {line_id}\nLANDLORD LINE: {text}"
        )
        payload, plain = await asyncio.gather(
            self._generate_json(model, prompt, ANALYSIS_SCHEMA),
            self._plain_translate(model, text, tenant_lang),
        )
        if payload is None:
            return normalize_analysis(
                {
                    "translation": plain or text,
                    "note": "Automated analysis is unavailable; no flags could be confirmed.",
                },
                line_id,
            )
        if plain:
            payload = {**payload, "translation": plain}
        return normalize_analysis(payload, line_id)

    async def transcribe_tenant_audio(self, pcm16: bytes, tenant_lang: str) -> str | None:
        """Plain-text transcription of one held-to-talk utterance (16 kHz mono PCM16).

        Returns None when the model is unavailable or says there was no speech.
        """
        await self.discover_models()
        model = self.select_model("generateContent", TEXT_CANDIDATES)
        if not model or self.client is None:
            return None
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(pcm16)
        audio_part = types.Part.from_bytes(data=wav_buffer.getvalue(), mime_type="audio/wav")
        prompt = (
            f"Transcribe this speech verbatim in its original language (expected language code: {tenant_lang}). "
            "Reply with the transcript only. If there is no intelligible speech, reply with the single word NOSPEECH."
        )

        def request() -> str | None:
            response = self.client.models.generate_content(
                model=model,
                contents=[prompt, audio_part],
                config=types.GenerateContentConfig(temperature=0.0),
            )
            return response.text

        try:
            out = await call_with_service_retries(request)
        except Exception:
            return None
        out = (out or "").strip()
        if not out or "NOSPEECH" in out.upper():
            return None
        return out

    async def translate_tenant_audio(self, pcm16: bytes, tenant_lang: str) -> dict[str, str] | None:
        """Generate-content fallback for a failed/unavailable Live API call."""
        await self.discover_models()
        model = self.select_model("generateContent", TEXT_CANDIDATES)
        if not model or self.client is None:
            return None
        wav_buffer = io.BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(pcm16)
        audio_part = types.Part.from_bytes(data=wav_buffer.getvalue(), mime_type="audio/wav")
        prompt = (
            f"Transcribe the tenant's speech in language {tenant_lang} exactly enough to preserve meaning, "
            "then translate it faithfully into English for the landlord. Return JSON with transcript and "
            "translation only. Do not add advice or commentary."
        )
        payload = await self._generate_json(model, [prompt, audio_part], AUDIO_FALLBACK_SCHEMA)
        if not isinstance(payload, dict):
            return None
        transcript, translation = payload.get("transcript"), payload.get("translation")
        if not isinstance(transcript, str) or not isinstance(translation, str):
            return None
        return {"transcript": transcript.strip(), "translation": translation.strip()}

    async def translate_tenant_text(self, text: str, tenant_lang: str) -> str | None:
        """Translate typed tenant speech to English using the text fallback."""
        await self.discover_models()
        model = self.select_model("generateContent", TEXT_CANDIDATES)
        if not model:
            return None
        schema = {
            "type": "OBJECT",
            "properties": {"translation": {"type": "STRING"}},
            "required": ["translation"],
        }
        prompt = (
            f"Translate the tenant's supplied text from {tenant_lang} into English faithfully. "
            "Return JSON with only a translation field. Preserve the speaker's meaning and tone. "
            "Do not add commentary, interpretation, or legal advice. Tenant text: "
            f"{json.dumps(text, ensure_ascii=False)}"
        )
        payload = await self._generate_json(model, prompt, schema)
        translation = payload.get("translation") if isinstance(payload, dict) else None
        return translation.strip() if isinstance(translation, str) and translation.strip() else None

    async def summarize(self, conversation: list[dict[str, str]], tenant_lang: str) -> dict[str, Any] | None:
        await self.discover_models()
        model = self.select_model("generateContent", TEXT_CANDIDATES)
        if not model:
            return None
        prompt = (
            f"Prepare a concise, factual, two-language summary of this housing conversation, first in "
            f"{tenant_lang}, then in English. Use exactly these three headings in each language: "
            "Promises (only landlord lines whose record has promise true, stated as the landlord said them), "
            "Not a commitment (landlord lines flagged vague_commitment: say they were vague and were not a promise), "
            "and Money terms, penalties and rules (lines flagged financial_term, penalty or restriction, with the amounts and conditions stated). "
            "Never list a vague_commitment line under Promises. Write None under a heading with no lines. "
            "For the tenant-language section, copy the tenant-language wording of each line verbatim from the translation record that follows that landlord line; "
            "if a line has no translation record, translate it carefully and literally. Never invent or paraphrase words, and write real words only. "
            "Do not infer facts, decide legality, give legal advice, or suggest what the tenant should do. "
            "This is not an agreement or legal document. Return JSON with markdown and languages.\n"
            f"Conversation records:\n{json.dumps(conversation, ensure_ascii=False)}"
        )
        payload = await self._generate_json(model, prompt, SUMMARY_SCHEMA)
        if not isinstance(payload, dict) or not isinstance(payload.get("markdown"), str):
            return None
        markdown = payload["markdown"].strip()
        if LEGAL_ADVICE_PATTERNS.search(markdown):
            return None
        return {"markdown": markdown, "languages": [tenant_lang, "en"]}

    async def synthesize_landlord_wav(self, text: str) -> bytes | None:
        await self.discover_models()
        model = self.select_model("generateContent", TTS_CANDIDATES)
        if not model or self.client is None:
            return None

        def request() -> bytes | None:
            response = self.client.models.generate_content(
                model=model,
                contents=f"Speak this landlord sentence clearly in English: {text}",
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
                        )
                    ),
                ),
            )
            for candidate in response.candidates or []:
                for part in candidate.content.parts or []:
                    if part.inline_data and part.inline_data.data:
                        data = part.inline_data.data
                        return data if isinstance(data, bytes) else bytes(data)
            return None

        try:
            return await call_with_service_retries(request)
        except Exception:
            return None

    async def close(self) -> None:
        if self.client is not None:
            try:
                await asyncio.to_thread(self.client.close)
            except Exception:
                pass
