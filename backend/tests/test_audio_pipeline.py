import asyncio
import io
import wave
from types import SimpleNamespace

import backend.main as server
from backend.main import ConnectionState, LandlordAudioCache, LandlordSayRequest, app


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


class FakeLiveSession:
    def __init__(self, responses=()):
        self.responses = responses
        self.inputs = []

    async def send_realtime_input(self, **kwargs):
        self.inputs.append(kwargs)

    async def receive(self):
        for response in self.responses:
            yield response


def sample_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(b"\x01\x00" * 240)
    return buffer.getvalue()


def server_content(*, text=None, audio=None, turn_complete=False):
    parts = []
    if audio is not None:
        parts.append(
            SimpleNamespace(
                inline_data=SimpleNamespace(data=audio, mime_type="audio/pcm;rate=24000")
            )
        )
    return SimpleNamespace(
        server_content=SimpleNamespace(
            output_transcription=SimpleNamespace(text=text) if text is not None else None,
            input_transcription=None,
            model_turn=SimpleNamespace(parts=parts),
            turn_complete=turn_complete,
        )
    )


def test_live_text_is_sent_once_at_turn_end_while_audio_streams(monkeypatch):
    audio_one = b"\x01\x00"
    audio_two = b"\x02\x00"
    state = ConnectionState(FakeWebSocket())
    state.live_session = FakeLiveSession(
        [
            server_content(text="Good", audio=audio_one),
            server_content(text="morning.", audio=audio_two),
            server_content(turn_complete=True),
        ]
    )

    asyncio.run(server.read_live_session(state))

    messages = state.websocket.messages
    translations = [message for message in messages if message["type"] == "translation"]
    audio_messages = [message for message in messages if message["type"] == "audio"]
    assert translations == [{"type": "translation", "to": "landlord", "text": "Good morning."}]
    assert len(audio_messages) == 2
    assert max(i for i, message in enumerate(messages) if message["type"] == "audio") < next(
        i for i, message in enumerate(messages) if message["type"] == "translation"
    )
    assert state.history == [{"speaker": "translation", "lang": "en", "text": "Good morning."}]


def test_background_simulator_turn_keeps_audio_but_suppresses_duplicate_translation():
    state = ConnectionState(FakeWebSocket())
    state.live_input_speaker = "landlord"
    state.suppress_live_translation = True
    state.live_session = FakeLiveSession(
        [
            server_content(text="Already sent as text.", audio=b"\x01\x00"),
            server_content(turn_complete=True),
        ]
    )

    asyncio.run(server.read_live_session(state))

    assert [message for message in state.websocket.messages if message["type"] == "translation"] == []
    assert [message for message in state.websocket.messages if message["type"] == "audio"]
    assert state.suppress_live_translation is False


def test_simulator_returns_text_before_background_tts_and_uses_disk_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("ADVOCATE_LANDLORD_AUDIO", "1")
    state = ConnectionState(FakeWebSocket())
    state.started = True
    state.tenant_lang = "uk"
    state.live_session = FakeLiveSession()
    cache = LandlordAudioCache(tmp_path)
    monkeypatch.setattr(server, "landlord_audio_cache", cache)

    async def latest_active():
        return state

    async def analyze(line_id, _text, _language):
        return {
            "id": line_id,
            "translation": "Обігрівач буде відремонтовано.",
            "flags": ["promise"],
            "promise": True,
            "note": "Обіцянка щодо ремонту.",
        }

    tts_started = asyncio.Event()
    allow_tts_to_finish = asyncio.Event()
    tts_calls = []

    async def synthesize(text):
        tts_calls.append(text)
        tts_started.set()
        await allow_tts_to_finish.wait()
        return sample_wav()

    monkeypatch.setattr(server.hub, "latest_active", latest_active)
    monkeypatch.setattr(server.gateway, "analyze_landlord_line", analyze)
    monkeypatch.setattr(server.gateway, "synthesize_landlord_wav", synthesize)

    async def run():
        response = await asyncio.wait_for(
            server.landlord_say(
                LandlordSayRequest(id="L03", text="We will fix the heater.")
            ),
            timeout=0.2,
        )
        assert response == {"ok": True}
        assert [message["type"] for message in state.websocket.messages] == [
            "status",
            "transcript",
            "analysis",
            "translation",
            "status",
        ]
        assert state.websocket.messages[3]["text"] == "Обігрівач буде відремонтовано."

        await asyncio.wait_for(tts_started.wait(), timeout=0.2)
        assert tts_calls == ["We will fix the heater."]
        allow_tts_to_finish.set()
        await asyncio.gather(*list(state.background_audio_tasks))
        assert len(list(tmp_path.glob("*.wav"))) == 1
        assert len(state.live_session.inputs) == 2

        await server.landlord_say(LandlordSayRequest(id="L03", text="We will fix the heater."))
        await asyncio.gather(*list(state.background_audio_tasks))
        assert tts_calls == ["We will fix the heater."]
        assert len(list(tmp_path.glob("*.wav"))) == 1

    asyncio.run(run())


def test_warmup_generates_twenty_lines_once_then_uses_cache(monkeypatch, tmp_path):
    cache = LandlordAudioCache(tmp_path)
    monkeypatch.setattr(server, "landlord_audio_cache", cache)
    monkeypatch.setattr(server.gateway, "initialize", _async_noop)
    monkeypatch.setattr(server.gateway, "select_model", lambda *_args: "gemini-3.8-flash-tts")
    calls = []

    async def synthesize(text):
        calls.append(text)
        return sample_wav()

    monkeypatch.setattr(server.gateway, "synthesize_landlord_wav", synthesize)

    async def run():
        first = await server.landlord_audio_warmup()
        second = await server.landlord_audio_warmup()
        return first, second

    first, second = asyncio.run(run())
    assert first == {"ok": True, "total": 20, "generated": 20, "cached": 0, "unavailable": 0}
    assert second == {"ok": True, "total": 20, "generated": 0, "cached": 20, "unavailable": 0}
    assert len(calls) == 20
    assert len(list(tmp_path.glob("*.wav"))) == 20


async def _async_noop():
    return None
