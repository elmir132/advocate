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


def compact_summary_records(conversation: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each landlord line with its translation and remove exact repeats."""
    records: list[dict[str, Any]] = []
    by_text: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(conversation):
        if item.get("speaker") != "landlord" or not str(item.get("text", "")).strip():
            continue
        text = " ".join(str(item["text"]).split())
        key = text.casefold()
        translation = None
        if index + 1 < len(conversation) and conversation[index + 1].get("speaker") == "translation":
            translation = str(conversation[index + 1].get("text", "")).strip() or None
        if key in by_text:
            existing = by_text[key]
            existing["promise"] = bool(existing.get("promise") or item.get("promise"))
            existing["flags"] = list(dict.fromkeys([*(existing.get("flags") or []), *(item.get("flags") or [])]))
            if not existing.get("tenant_translation") and translation:
                existing["tenant_translation"] = translation
            continue
        record: dict[str, Any] = {
            "speaker": "landlord",
            "text": text,
            "flags": list(item.get("flags") or []),
            "promise": bool(item.get("promise")),
        }
        if translation:
            record["tenant_translation"] = translation
        by_text[key] = record
        records.append(record)
    return records


ENGLISH_HINT_WORDS = {"the", "this", "that", "what", "when", "where", "we", "you", "your", "can", "could", "will", "would", "probably", "maybe", "rent", "deposit", "heater", "lease", "fix", "repair"}
SPANISH_HINT_WORDS = {"el", "la", "los", "las", "que", "qué", "cuando", "cuándo", "podemos", "puede", "alquiler", "depósito", "calefacción", "contrato", "arreglar"}


def obvious_text_speaker(text: str, tenant_lang: str) -> str | None:
    """Return a speaker only when script or multiple common words make it clear."""
    language = tenant_lang.split("-", 1)[0].lower()
    if language == "en":
        return None
    if language in {"uk", "ru"} and re.search(r"[\u0400-\u04ff]", text):
        return "tenant"
    if language == "zh" and re.search(r"[\u3400-\u9fff]", text):
        return "tenant"
    words = re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)
    if language == "es" and (re.search(r"[¿¡áéíóúüñ]", text.casefold()) or sum(word in SPANISH_HINT_WORDS for word in words) >= 2):
        return "tenant"
    if sum(word in ENGLISH_HINT_WORDS for word in words) >= 2:
        return "landlord"
    return None


SUMMARY_TOPIC_PATTERNS = [
    ("deposit", re.compile(r"\b(?:security )?deposit\b", re.IGNORECASE)),
    ("repair", re.compile(r"\b(?:repair|fix|heater|heating|window)\w*\b", re.IGNORECASE)),
    ("painting", re.compile(r"\b(?:paint|repaint)\w*\b", re.IGNORECASE)),
    ("rent_fees", re.compile(r"\b(?:rent|fee|payment|utilities|electricity)\b", re.IGNORECASE)),
    ("lease", re.compile(r"\b(?:lease|renew|notice)\w*\b", re.IGNORECASE)),
    ("subletting", re.compile(r"\bsublet(?:ting)?\b", re.IGNORECASE)),
    ("pets_guests", re.compile(r"\b(?:pet|pets|guest|guests)\b", re.IGNORECASE)),
]
SUMMARY_TOPIC_LABELS = {
    "en": {"deposit": "deposit terms", "repair": "repairs", "painting": "painting the apartment", "rent_fees": "rent and fees", "lease": "lease terms", "subletting": "subletting", "pets_guests": "pet and guest rules", "other": "other housing details"},
    "uk": {"deposit": "умови застави", "repair": "ремонт", "painting": "фарбування квартири", "rent_fees": "орендна плата та збори", "lease": "умови оренди", "subletting": "суборенда", "pets_guests": "правила щодо тварин і гостей", "other": "інші деталі житла"},
    "ru": {"deposit": "условия залога", "repair": "ремонт", "painting": "покраска квартиры", "rent_fees": "арендная плата и сборы", "lease": "условия аренды", "subletting": "субаренда", "pets_guests": "правила о животных и гостях", "other": "другие детали жилья"},
    "es": {"deposit": "las condiciones del depósito", "repair": "las reparaciones", "painting": "la pintura del apartamento", "rent_fees": "el alquiler y las tarifas", "lease": "las condiciones del contrato", "subletting": "el subarriendo", "pets_guests": "las reglas sobre mascotas y huéspedes", "other": "otros detalles de la vivienda"},
    "zh": {"deposit": "押金条件", "repair": "维修", "painting": "公寓粉刷", "rent_fees": "租金和费用", "lease": "租约条款", "subletting": "转租", "pets_guests": "宠物和访客规定", "other": "其他住房事项"},
}
SUMMARY_OVERVIEW_TEMPLATES = {
    "en": "The conversation covered: {topics}.",
    "uk": "Розмова стосувалася таких тем: {topics}.",
    "ru": "Разговор касался следующих тем: {topics}.",
    "es": "La conversación trató sobre: {topics}.",
    "zh": "对话涉及：{topics}。",
}
SUMMARY_EMPTY_OVERVIEWS = {
    "en": "No landlord statements were recorded.", "uk": "Висловлювань орендодавця не зафіксовано.",
    "ru": "Высказывания арендодателя не зафиксированы.", "es": "No se registraron declaraciones del propietario.",
    "zh": "未记录房东的陈述。",
}


def build_conversation_overview(records: list[dict[str, Any]], tenant_lang: str) -> dict[str, str]:
    topics: list[str] = []
    for record in records:
        text = str(record.get("text", ""))
        topic = next((name for name, pattern in SUMMARY_TOPIC_PATTERNS if pattern.search(text)), "other")
        if topic not in topics:
            topics.append(topic)
    language = tenant_lang.split("-", 1)[0].lower()
    if language not in SUMMARY_TOPIC_LABELS:
        language = "en"
    if not topics:
        return {"tenant": SUMMARY_EMPTY_OVERVIEWS[language], "english": SUMMARY_EMPTY_OVERVIEWS["en"]}
    tenant_topics = ", ".join(SUMMARY_TOPIC_LABELS[language][topic] for topic in topics)
    english_topics = ", ".join(SUMMARY_TOPIC_LABELS["en"][topic] for topic in topics)
    return {
        "tenant": SUMMARY_OVERVIEW_TEMPLATES[language].format(topics=tenant_topics),
        "english": SUMMARY_OVERVIEW_TEMPLATES["en"].format(topics=english_topics),
    }


class UsageLimitError(RuntimeError):
    """Raised when this server run has used its allowance of model calls."""


# A public demo link must not be able to burn the whole quota: cap model calls per server run.
MAX_MODEL_CALLS = int(os.environ.get("ADVOCATE_MAX_MODEL_CALLS", "1500"))
usage = {"model_calls": 0}


async def call_with_service_retries(
    operation: Callable[[], Any], *, threaded: bool = True
) -> Any:
    """Run at most three times, pausing briefly only for 503/overload failures."""
    if usage["model_calls"] >= MAX_MODEL_CALLS:
        raise UsageLimitError("The demo allowance of model calls is used up.")
    usage["model_calls"] += 1
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



_LATIN = re.compile(r"[A-Za-z]")
_CYRILLIC = re.compile(r"[\u0400-\u04FF]")
_CJK = re.compile(r"[\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF]")
_ARABIC = re.compile(r"[\u0600-\u06FF]")
_SCRIPT_BY_LANG = {"uk": _CYRILLIC, "ru": _CYRILLIC, "bg": _CYRILLIC, "sr": _CYRILLIC, "zh": _CJK, "ja": _CJK, "ko": _CJK, "ar": _ARABIC, "fa": _ARABIC, "ur": _ARABIC}


def parse_transcription(raw: str, tenant_lang: str) -> dict[str, str] | None:
    """Turn the model's two-line reply into {"text", "lang"}; lang is "en" or the tenant's code.

    When the tenant's language has its own script, the script of the transcript decides the
    language, because that is more dependable than the model's label.
    """
    text = raw.strip()
    if not text or "NOSPEECH" in text.upper():
        return None
    tenant = tenant_lang.lower().split("-")[0]
    label = ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 2 and re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z]{2,4})?[:.]?", lines[0]):
        label = lines[0].rstrip(":.").lower().split("-")[0]
        text = " ".join(lines[1:]).strip()
    if not text:
        return None
    lang = label if label in {"en", tenant} else ""
    script = _SCRIPT_BY_LANG.get(tenant)
    if script is not None and tenant != "en":
        own, latin = len(script.findall(text)), len(_LATIN.findall(text))
        if own or latin:
            lang = tenant if own >= latin else "en"
    return {"text": text, "lang": lang or tenant}


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

    async def transcribe_speech(self, pcm16: bytes, tenant_lang: str) -> dict[str, str] | None:
        """Transcribe one held-to-talk clip (16 kHz mono PCM16) and say which language it was.

        Returns {"text", "lang"} where lang is "en" or the tenant's language code, or None when
        the model is unavailable or heard no speech. Plain text output: structured JSON output
        sometimes garbled Ukrainian words.
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
            f"The speaker is talking in either English (code en) or the language with code {tenant_lang}. "
            "Transcribe the speech verbatim in the language it was spoken in. Do not translate. "
            f"Reply with exactly two lines: line 1 is the language code (en or {tenant_lang}), "
            "line 2 is the transcript. If there is no intelligible speech, reply with the single word NOSPEECH."
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
        return parse_transcription(out or "", tenant_lang)

    async def transcribe_tenant_audio(self, pcm16: bytes, tenant_lang: str) -> str | None:
        """Kept for callers that only need the words."""
        result = await self.transcribe_speech(pcm16, tenant_lang)
        return result["text"] if result else None

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

    async def classify_typed_speaker(self, text: str, tenant_lang: str) -> str | None:
        obvious = obvious_text_speaker(text, tenant_lang)
        if obvious:
            return obvious
        if tenant_lang.split("-", 1)[0].lower() == "en":
            return None
        await self.discover_models()
        model = self.select_model("generateContent", TEXT_CANDIDATES)
        if not model:
            return None
        schema = {
            "type": "OBJECT",
            "properties": {"speaker": {"type": "STRING", "enum": ["tenant", "landlord"]}},
            "required": ["speaker"],
        }
        prompt = (
            "Classify who typed this message in a tenant-landlord conversation. "
            f"The tenant uses language code {tenant_lang}; the landlord uses English. "
            "Return tenant only when the text is in the tenant language, and landlord only when it is English. "
            "Do not classify by meaning or legal content. Return JSON only. Text: "
            f"{json.dumps(text, ensure_ascii=False)}"
        )
        payload = await self._generate_json(model, prompt, schema)
        speaker = payload.get("speaker") if isinstance(payload, dict) else None
        return speaker if speaker in {"tenant", "landlord"} else None

    async def summarize(self, conversation: list[dict[str, Any]], tenant_lang: str) -> dict[str, Any] | None:
        records = compact_summary_records(conversation)
        await self.discover_models()
        model = self.select_model("generateContent", TEXT_CANDIDATES)
        if not model:
            return None
        prompt = (
            f"Prepare a concise, factual, two-language summary of this housing conversation, first in "
            f"{tenant_lang}, then in English. Use localized equivalents of these four headings in the tenant-language section, "
            "and use these exact names in the English section: "
            "Conversation overview (one or two short factual sentences covering the main topics, promises, and uncertainties without advice; "
            "use neutral topic wording and do not introduce labels such as penalty, violation, or legal right unless the original statement uses them; "
            "mention the topic of every supplied landlord record exactly once, including each repair, deposit, restriction, or promise), "
            "Promises (only landlord lines whose record has promise true, copied as stated), "
            "Not a commitment (landlord lines flagged vague_commitment), "
            "and Money terms, penalties and rules (lines flagged financial_term, penalty or restriction). "
            "Never list a vague commitment under Promises. Write None under a heading with no lines. "
            "Every supplied record must appear under every applicable heading; do not merge, omit, or duplicate records. "
            "Each record contains one original landlord statement and may contain tenant_translation. "
            "In the tenant-language section, copy tenant_translation verbatim; if absent, translate the original literally. "
            "In the English section, copy the original text verbatim. Preserve every amount, deadline, condition, penalty, "
            "restriction, and explicit promise, including deposit terms. Never infer facts, decide legality, give legal advice, "
            "or suggest what the tenant should do. This is not an agreement or legal document. "
            "Return JSON with markdown and languages.\n"
            f"Conversation records:\n{json.dumps(records, ensure_ascii=False)}"
        )
        payload = await self._generate_json(model, prompt, SUMMARY_SCHEMA)
        if not isinstance(payload, dict) or not isinstance(payload.get("markdown"), str):
            return None
        markdown = payload["markdown"].strip()
        if LEGAL_ADVICE_PATTERNS.search(markdown):
            return None
        return {"markdown": markdown, "languages": [tenant_lang, "en"], "overview": build_conversation_overview(records, tenant_lang)}

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
