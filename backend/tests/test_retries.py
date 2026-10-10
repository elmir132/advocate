import asyncio

import pytest

from backend.ai import call_with_service_retries


class ServiceError(Exception):
    def __init__(self, code=None, message=""):
        super().__init__(message)
        self.code = code
        self.message = message


def test_retries_503_at_most_three_times_with_short_pauses(monkeypatch):
    attempts = []
    pauses = []

    async def inline_thread(operation):
        return operation()

    async def record_pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr("backend.ai.asyncio.to_thread", inline_thread)
    monkeypatch.setattr("backend.ai.asyncio.sleep", record_pause)

    def request():
        attempts.append(1)
        if len(attempts) < 3:
            raise ServiceError(code=503, message="unavailable")
        return "ok"

    assert asyncio.run(call_with_service_retries(request)) == "ok"
    assert len(attempts) == 3
    assert len(pauses) == 2
    assert pauses == [0.5, 0.5]


def test_retries_overload_message_but_not_other_errors(monkeypatch):
    attempts = []
    pauses = []

    async def inline_thread(operation):
        return operation()

    async def record_pause(seconds):
        pauses.append(seconds)

    monkeypatch.setattr("backend.ai.asyncio.to_thread", inline_thread)
    monkeypatch.setattr("backend.ai.asyncio.sleep", record_pause)

    def overloaded():
        attempts.append("overload")
        if len(attempts) == 1:
            raise ServiceError(message="model overloaded")
        return "recovered"

    assert asyncio.run(call_with_service_retries(overloaded)) == "recovered"
    assert len(attempts) == 2
    assert pauses == [0.5]

    def bad_request():
        attempts.append("bad")
        raise ServiceError(code=400, message="invalid request")

    with pytest.raises(ServiceError):
        asyncio.run(call_with_service_retries(bad_request))
    assert attempts[-1] == "bad"
    assert pauses == [0.5]
