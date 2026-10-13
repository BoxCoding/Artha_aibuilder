"""HeyGen LiveAvatar sessions for the video guide.

The API key stays on the server: this module mints a session and starts it, and the browser receives
only the LiveKit room URL and a client token for that one session. Sessions are created with no
context, which LiveAvatar calls restricted mode: its own model never answers the customer, and the
avatar only says the text the guide sends it (avatar.speak_text). What the guide says is therefore
still decided by /api/guide and its guardrails.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger("artha.liveavatar")
API = "https://api.liveavatar.com/v1"
LANGUAGES = {"en": "en", "hi": "hi", "id": "id"}
SANDBOX_MAX_SECONDS = 60


class AvatarUnavailable(RuntimeError):
    """Any failure to get a session. The guide falls back to its illustrated presenter."""


def sessions_per_10_min(settings: Settings) -> int:
    return (settings.liveavatar_sessions_per_10_min_sandbox if settings.liveavatar_sandbox
            else settings.liveavatar_sessions_per_10_min)


def enabled(settings: Settings) -> bool:
    return settings.liveavatar_api_key is not None and bool(settings.liveavatar_api_key.get_secret_value())


async def start_session(settings: Settings, language: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    if not enabled(settings):
        raise AvatarUnavailable("not configured")
    key = settings.liveavatar_api_key.get_secret_value()  # type: ignore[union-attr]
    token_request = {
        "mode": "FULL",
        "avatar_id": settings.liveavatar_avatar_id,
        "is_sandbox": settings.liveavatar_sandbox,
        # Sandbox sessions are refused above 60 seconds; paid tiers allow longer.
        "max_session_duration": min(settings.liveavatar_max_session_seconds, SANDBOX_MAX_SECONDS)
        if settings.liveavatar_sandbox else settings.liveavatar_max_session_seconds,
        # No context_id: restricted mode, so the avatar speaks only what we send it.
        "avatar_persona": {"language": LANGUAGES.get(language, "en")},
        "video_settings": {"quality": "high", "encoding": "H264"},
    }
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        minted = await client.post(f"{API}/sessions/token", json=token_request, headers={"X-API-KEY": key})
        token = _data(minted, "token")
        started = await client.post(f"{API}/sessions/start",
                                    headers={"Authorization": f"Bearer {token['session_token']}"})
        session = _data(started, "start")
    except httpx.HTTPError as exc:
        log.warning("liveavatar_unreachable", extra={"error_type": type(exc).__name__})
        raise AvatarUnavailable("unreachable") from exc
    finally:
        if own:
            await client.aclose()
    return {"session_id": session["session_id"], "livekit_url": session["livekit_url"],
            "livekit_token": session["livekit_client_token"], "session_token": token["session_token"],
            "max_seconds": session.get("max_session_duration"), "sandbox": settings.liveavatar_sandbox}


async def stop_session(session_id: str, session_token: str, client: httpx.AsyncClient | None = None) -> None:
    """End the session now rather than letting it idle out, so no credits are spent on an empty room."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=10.0)
    try:
        await client.post(f"{API}/sessions/stop", json={"session_id": session_id},
                          headers={"Authorization": f"Bearer {session_token}"})
    except httpx.HTTPError:
        log.warning("liveavatar_stop_failed")
    finally:
        if own:
            await client.aclose()


def _data(response: httpx.Response, step: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        body = {}
    data = body.get("data") if isinstance(body, dict) else None
    if response.status_code >= 400 or not isinstance(data, dict):
        # The provider's message helps an operator; it never reaches the customer.
        log.warning("liveavatar_refused", extra={"detail": f"{step} {response.status_code} {str(body)[:300]}"})
        raise AvatarUnavailable(f"{step} refused ({response.status_code})")
    return data
