"""The LiveAvatar face for the video guide: off without a key, key never leaves the server, restricted mode."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.ai import liveavatar
from app.api.main import app
from app.config import get_settings
from app.domain.models import Role
from tests.test_intake_and_roles import customer, staff


@pytest.fixture
def with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "liveavatar_api_key", SecretStr("test-liveavatar-key"))


def fake_provider(seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/sessions/token"):
            return httpx.Response(200, json={"code": 100, "data": {"session_id": "s-1", "session_token": "jwt-1"}})
        if request.url.path.endswith("/sessions/start"):
            return httpx.Response(201, json={"code": 100, "data": {
                "session_id": "s-1", "livekit_url": "wss://x.livekit.cloud", "livekit_client_token": "lk-1",
                "max_session_duration": 60}})
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_off_without_a_key() -> None:
    client = customer()
    assert client.get("/api/config").json()["live_avatar"] is False
    assert client.post("/api/guide/avatar", json={"language": "en"}).status_code == 503
    assert "livekit" not in TestClient(app).get("/api/healthz").headers["Content-Security-Policy"]


def test_session_is_restricted_mode_and_the_key_stays_on_the_server(with_key: None) -> None:
    seen: list[httpx.Request] = []
    session = asyncio.run(liveavatar.start_session(get_settings(), "hi", fake_provider(seen)))
    token_call, start_call = seen
    body = json.loads(token_call.content)
    assert token_call.headers["X-API-KEY"] == "test-liveavatar-key"
    assert body["mode"] == "FULL" and body["is_sandbox"] is True
    assert body["avatar_persona"] == {"language": "hi"}  # no context_id: the avatar says only what we send
    assert start_call.headers["Authorization"] == "Bearer jwt-1"
    assert session["livekit_url"] == "wss://x.livekit.cloud" and session["livekit_token"] == "lk-1"
    assert "test-liveavatar-key" not in json.dumps(session)


def test_provider_refusal_becomes_unavailable(with_key: None) -> None:
    refusing = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(402, json={"message": "no credits"})))
    with pytest.raises(liveavatar.AvatarUnavailable):
        asyncio.run(liveavatar.start_session(get_settings(), "en", refusing))


def test_route_roles_config_and_csp(with_key: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_start(settings, language):  # type: ignore[no-untyped-def]
        return {"session_id": "s-1", "livekit_url": "wss://x.livekit.cloud", "livekit_token": "lk-1",
                "session_token": "jwt-1", "max_seconds": 60, "sandbox": True}
    monkeypatch.setattr(liveavatar, "start_session", fake_start)
    client = customer()
    assert client.get("/api/config").json()["live_avatar"] is True
    assert client.post("/api/guide/avatar", json={"language": "en"}).json()["livekit_token"] == "lk-1"
    assert staff(Role.UNDERWRITER).post("/api/guide/avatar", json={"language": "en"}).status_code == 403
    assert "wss://*.livekit.cloud" in client.get("/api/healthz").headers["Content-Security-Policy"]


def test_session_limit_is_looser_in_sandbox_than_with_real_credits(with_key: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_start(settings, language):  # type: ignore[no-untyped-def]
        return {"session_id": "s", "livekit_url": "wss://x.livekit.cloud", "livekit_token": "t",
                "session_token": "j", "max_seconds": 60, "sandbox": settings.liveavatar_sandbox}
    monkeypatch.setattr(liveavatar, "start_session", fake_start)
    settings = get_settings()

    sandbox = customer()
    codes = [sandbox.post("/api/guide/avatar", json={}).status_code
             for _ in range(settings.liveavatar_sessions_per_10_min_sandbox + 1)]
    assert codes[:-1] == [200] * settings.liveavatar_sessions_per_10_min_sandbox and codes[-1] == 429

    monkeypatch.setattr(settings, "liveavatar_sandbox", False)
    paid = customer()
    codes = [paid.post("/api/guide/avatar", json={}).status_code for _ in range(settings.liveavatar_sessions_per_10_min + 1)]
    assert codes[:-1] == [200] * settings.liveavatar_sessions_per_10_min and codes[-1] == 429
