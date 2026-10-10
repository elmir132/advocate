import base64
import io
import wave

import pytest
from fastapi.testclient import TestClient

from backend.ai import normalize_analysis, wav_to_pcm16k, wav_to_pcm24k
from backend.main import (
    ConnectionState,
    _watch_live_audio,
    app,
    gateway,
    validate_client_message,
)


def test_unknown_flags_are_dropped_and_id_is_forced():
    result = normalize_analysis(
        {
            "id": "model-invented-id",
            "translation": "The fee is $20.",
            "flags": ["financial_term", "legal_advice", "promise", "unknown"],
            "promise": False,
            "note": "Ask for this in writing.",
        },
        "L17",
    )
    assert result == {
        "id": "L17",
        "translation": "The fee is $20.",
        "flags": ["financial_term", "promise"],
        "promise": True,
        "note": "Ask for this in writing.",
    }


def test_malformed_analysis_does_not_emit_unknown_flags():
    result = normalize_analysis({"flags": "penalty", "promise": "yes"}, "L02")
    assert result["id"] == "L02"
    assert result["flags"] == []
    assert result["promise"] is False


def test_tts_wav_converter_rejects_empty_audio():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
    with pytest.raises(ValueError, match="Empty TTS audio"):
        wav_to_pcm16k(buffer.getvalue())


def test_wav_converter_strips_header_for_browser_pcm24k():
    samples = b"\x01\x00\x02\x00\x03\x00"
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(samples)
    assert wav_to_pcm24k(buffer.getvalue()) == samples


def test_client_message_validation():
    start = validate_client_message({"type": "start"})
    assert start["tenant_lang"] == "uk"
    assert validate_client_message({"type": "start", "tenant_lang": "ES"})["tenant_lang"] == "es"
    audio = base64.b64encode(b"\x00\x00").decode("ascii")
    assert validate_client_message({"type": "audio", "data": audio})["_audio_bytes"] == b"\x00\x00"
    with pytest.raises(ValueError, match="valid base64"):
        validate_client_message({"type": "audio", "data": "not base64!"})
    with pytest.raises(ValueError, match="Unsupported"):
        validate_client_message({"type": "exec"})
    assert validate_client_message({"type": "tenant_text", "text": "Please repair the heater."})[
        "text"
    ] == "Please repair the heater."
    with pytest.raises(ValueError, match="Tenant text"):
        validate_client_message({"type": "tenant_text", "text": "  "})


def test_health_contract_does_not_call_google():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"ok": True, "live": gateway.key_present}


def test_websocket_idle_and_stop_without_live_api():
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            assert websocket.receive_json() == {"type": "status", "state": "idle"}
            websocket.send_json({"type": "stop"})
            assert websocket.receive_json() == {"type": "status", "state": "idle"}


def test_live_audio_watchdog_uses_text_fallback_without_network(monkeypatch):
    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    async def fake_transcribe(_audio, _language):
        return "A spoken request."

    async def fake_translate_text(_text, _language):
        return "A request."

    monkeypatch.setattr("backend.main.LIVE_AUDIO_GRACE_SECONDS", 0)
    monkeypatch.setattr("backend.main.FALLBACK_QUIET_SECONDS", 0)
    monkeypatch.setattr(gateway, "transcribe_tenant_audio", fake_transcribe)
    monkeypatch.setattr(gateway, "translate_tenant_text", fake_translate_text)

    async def run():
        state = ConnectionState(FakeWebSocket())
        state.started = True
        # One second of a loud square wave, so the silence and too-short checks pass.
        state.unconfirmed_live_audio.extend((b"\x00\x20" * 80 + b"\x00\xe0" * 80) * 100)
        await _watch_live_audio(state)
        await state.fallback_worker
        assert state.live_failed is True
        assert any(message.get("type") == "translation" for message in state.websocket.messages)
        assert any(message.get("type") == "transcript" for message in state.websocket.messages)

    import asyncio

    asyncio.run(run())


def test_summary_is_generated_only_after_approval_without_network(monkeypatch):
    async def fake_start(_state):
        return None

    async def fake_analysis(line_id, _text, _language):
        return normalize_analysis(
            {
                "translation": "The heater will be fixed.",
                "flags": ["promise", "not_a_valid_flag"],
                "promise": True,
                "note": "This is a candidate promise.",
            },
            line_id,
        )

    async def fake_summary(_history, language):
        return {"markdown": "### Tenant summary\nRepair promise\n\n### English\nRepair promise", "languages": [language, "en"]}

    monkeypatch.setattr("backend.main.start_live_session", fake_start)
    monkeypatch.setattr(gateway, "analyze_landlord_line", fake_analysis)
    monkeypatch.setattr(gateway, "summarize", fake_summary)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            assert websocket.receive_json()["state"] == "idle"
            websocket.send_json({"type": "start", "tenant_lang": "uk"})
            assert websocket.receive_json() == {"type": "status", "state": "thinking"}
            assert websocket.receive_json() == {"type": "status", "state": "listening"}
            websocket.send_json({"type": "start", "tenant_lang": "uk"})
            assert websocket.receive_json() == {
                "type": "error",
                "message": "This interpreter session is already running.",
            }
            websocket.send_json({"type": "landlord_text", "id": "L04", "text": "I will repair the heater."})
            preapproval = [websocket.receive_json() for _ in range(5)]
            assert not any(message["type"] == "summary" for message in preapproval)
            analysis = next(message for message in preapproval if message["type"] == "analysis")
            assert analysis["flags"] == ["promise"]
            websocket.send_json({"type": "approve_summary"})
            approval_messages = [websocket.receive_json() for _ in range(3)]
            summary = next(message for message in approval_messages if message["type"] == "summary")
            assert summary["languages"] == ["uk", "en"]


def test_typed_tenant_text_is_transcript_then_english_translation(monkeypatch):
    async def fake_start(_state):
        return None

    async def fake_translation(text, language):
        assert text == "Будь ласка, полагодьте опалення."
        assert language == "uk"
        return "Please repair the heating."

    monkeypatch.setattr("backend.main.start_live_session", fake_start)
    monkeypatch.setattr(gateway, "translate_tenant_text", fake_translation)

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            assert websocket.receive_json() == {"type": "status", "state": "idle"}
            websocket.send_json({"type": "start", "tenant_lang": "uk"})
            assert websocket.receive_json()["state"] == "thinking"
            assert websocket.receive_json()["state"] == "listening"
            websocket.send_json({"type": "tenant_text", "text": "Будь ласка, полагодьте опалення."})
            messages = [websocket.receive_json() for _ in range(4)]
            assert messages[0] == {"type": "status", "state": "thinking"}
            assert messages[1] == {
                "type": "transcript",
                "speaker": "tenant",
                "lang": "uk",
                "text": "Будь ласка, полагодьте опалення.",
            }
            assert messages[2] == {
                "type": "translation",
                "to": "landlord",
                "text": "Please repair the heating.",
            }
            assert messages[3] == {"type": "status", "state": "listening"}


def test_serving_routes_and_cors_contract(monkeypatch, tmp_path):
    missing_results = tmp_path / "missing-results.json"
    monkeypatch.setattr("backend.main.EVAL_RESULTS_PATH", missing_results)
    with TestClient(app) as client:
        root = client.get("/")
        assert root.status_code == 200
        assert "Advocate" in root.text
        assert '<base href="/web/">' in root.text
        assert client.get("/web/style.css").status_code == 200
        assert client.get("/web/app.js").status_code == 200
        assert client.get("/data/landlord_lines.json").status_code == 200
        assert client.get("/eval/results.json").status_code == 404

        preflight = client.options(
            "/data/landlord_lines.json",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_silent_and_too_short_clips_are_reported_not_transcribed(monkeypatch):
    from backend.main import schedule_text_fallback

    class FakeWebSocket:
        def __init__(self):
            self.messages = []

        async def send_json(self, message):
            self.messages.append(message)

    called = []

    async def fake_transcribe(_audio, _language):
        called.append(1)
        return "should not happen"

    monkeypatch.setattr("backend.main.FALLBACK_QUIET_SECONDS", 0)
    monkeypatch.setattr(gateway, "transcribe_tenant_audio", fake_transcribe)

    async def run():
        import asyncio

        state = ConnectionState(FakeWebSocket())
        state.started = True
        schedule_text_fallback(state, b"\x00\x00" * 16000)  # one second of silence
        await state.fallback_worker
        schedule_text_fallback(state, b"\x00\x20" * 100)  # far too short
        await state.fallback_worker
        errors = [m["message"] for m in state.websocket.messages if m.get("type") == "error"]
        assert any("No speech" in e for e in errors) and any("too short" in e for e in errors)
        assert called == []

    import asyncio

    asyncio.run(run())


def test_audio_end_is_an_accepted_message():
    from backend.main import validate_client_message

    assert validate_client_message({"type": "audio_end"})["type"] == "audio_end"
