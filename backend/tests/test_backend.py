import base64

import pytest
from fastapi.testclient import TestClient

from backend.ai import normalize_analysis
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

    async def fake_translate(_audio, _language):
        return {"transcript": "A spoken request.", "translation": "A request."}

    monkeypatch.setattr("backend.main.LIVE_AUDIO_GRACE_SECONDS", 0)
    monkeypatch.setattr("backend.main.FALLBACK_QUIET_SECONDS", 0)
    monkeypatch.setattr(gateway, "translate_tenant_audio", fake_translate)

    async def run():
        state = ConnectionState(FakeWebSocket())
        state.unconfirmed_live_audio.extend(b"\x00\x00")
        await _watch_live_audio(state)
        await state.fallback_worker
        assert state.live_failed is True
        assert any(message.get("type") == "translation" for message in state.websocket.messages)
        assert any(message.get("type") == "transcript" for message in state.websocket.messages)

    import asyncio

    asyncio.run(run())
