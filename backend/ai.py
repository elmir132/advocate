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
from typing import Any

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


def wav_to_pcm16k(wav_data: bytes) -> bytes:
    """Convert a TTS WAV payload to mono signed PCM16 at 16 kHz."""
    with wave.open(io.BytesIO(wav_data), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        raw = wav_file.readframes(wav_file.getnframes())
    if sample_width != 2 or channels < 1 or sample_rate < 1:
        raise ValueError("Unsupported TTS WAV format")

    samples = array("h")
    samples.frombytes(raw)
    if os.sys.byteorder != "little":
        samples.byteswap()
    if channels > 1:
        mono = array("h")
        for offset in range(0, len(samples), channels):
            frame = samples[offset : offset + channels]
            mono.append(int(sum(frame) / channels))
        samples = mono
    if sample_rate == 16000:
        output = samples
    else:
        output_count = max(1, round(len(samples) * 16000 / sample_rate))
        output = array("h")
        for index in range(output_count):
            position = index * sample_rate / 16000
            left = min(int(position), len(samples) - 1)
            right = min(left + 1, len(samples) - 1)
            fraction = position - left
            output.append(round(samples[left] * (1 - fraction) + samples[right] * fraction))
    if os.sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


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
            model_rows = await asyncio.to_thread(lambda: list(self.client.models.list()))
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
            text = await asyncio.to_thread(request)
            if not text:
                return None
            return json.loads(text)
        except Exception:
            return None

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
        payload = await self._generate_json(model, prompt, ANALYSIS_SCHEMA)
        if payload is None:
            return normalize_analysis(
                {
                    "translation": text,
                    "note": "Automated analysis is unavailable; no flags could be confirmed.",
                },
                line_id,
            )
        return normalize_analysis(payload, line_id)

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

    async def summarize(self, conversation: list[dict[str, str]], tenant_lang: str) -> dict[str, Any] | None:
        await self.discover_models()
        model = self.select_model("generateContent", TEXT_CANDIDATES)
        if not model:
            return None
        prompt = (
            f"Prepare a concise, factual, two-language summary of this housing conversation, first in "
            f"{tenant_lang}, then in English. Include only stated promises and flagged topics. "
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
            return await asyncio.to_thread(request)
        except Exception:
            return None

    async def close(self) -> None:
        if self.client is not None:
            try:
                await asyncio.to_thread(self.client.close)
            except Exception:
                pass
