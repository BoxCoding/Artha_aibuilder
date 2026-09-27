"""Session handling, role checks, rate limiting and upload validation."""
from __future__ import annotations

import io
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import Settings, get_settings
from app.domain.models import Case, Role
from app.domain.users import DEMO_ACCOUNTS, User, get_accounts

log = logging.getLogger("artha.security")

COOKIE_NAME = "artha_session"
PDF_MAGIC = b"%PDF-"
# Kept for callers of the prototype's name: {email: (name, role)}.
DEMO_USERS = DEMO_ACCOUNTS
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class Principal:
    uid: str
    name: str
    role: Role


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt="artha-session")


def issue_session(response: Response, user: User, settings: Settings) -> None:
    token = _serializer(settings).dumps({"uid": user.uid, "sv": user.session_version})
    response.set_cookie(COOKIE_NAME, token, max_age=settings.session_ttl_seconds, httponly=True, samesite="strict",
                        secure=settings.is_prod, path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_principal(request: Request, settings: Settings = Depends(get_settings)) -> Principal:
    """Resolve the cookie to a live account. A disabled account, a changed password or a sign-out
    elsewhere all invalidate the cookie immediately, because the version must still match."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue.")
    try:
        data = _serializer(settings).loads(token, max_age=settings.session_ttl_seconds)
    except BadSignature as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has expired. Sign in again.") from exc
    user = get_accounts().repo.get(str(data.get("uid", "")))
    if user is None or not user.active or user.session_version != data.get("sv"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has expired. Sign in again.")
    if user.is_demo and not settings.demo_mode:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Demo accounts are disabled.")
    principal = Principal(uid=user.uid, name=user.name, role=user.role)
    request.state.principal = principal
    return principal


def require_roles(*roles: Role):  # type: ignore[no-untyped-def]
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot perform this action.")
        return principal
    return dependency


def authorise_case(case: Case, principal: Principal) -> None:
    """Customers see their own applications and loan officers the ones they sourced; credit and risk staff
    see all. 404 rather than 403 to avoid leaking existence."""
    if principal.role in (Role.BORROWER, Role.LOAN_OFFICER) and case.owner_uid != principal.uid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found.")


def client_ip(request: Request, settings: Settings) -> str:
    """The caller's address. X-Forwarded-For is only honoured behind a trusted proxy (Cloud Run's
    front end appends the real client as the last hop it adds)."""
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in forwarded.split(",") if h.strip()]
        if hops:
            return hops[-1][:64]
    return (request.client.host if request.client else "unknown")[:64]


def origin_allowed(request: Request, settings: Settings) -> bool:
    """CSRF defence in depth on top of SameSite=Strict: a state-changing request that states an
    Origin must state ours."""
    if request.method not in UNSAFE_METHODS:
        return True
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        # Browsers always send Origin on cross-site POSTs; its absence means same-origin or a non-browser client.
        return origin is None
    origin = origin.rstrip("/")
    own = f"{request.url.scheme}://{request.url.netloc}"
    forwarded_host = request.headers.get("x-forwarded-host") if settings.trust_proxy_headers else None
    candidates = {own} | settings.allowed_origins
    if forwarded_host:
        candidates |= {f"https://{forwarded_host}", f"http://{forwarded_host}"}
    return origin in candidates


# --------------------------------------------------------------------------- rate limiting

class MemoryRateLimiter:
    """Sliding-window limiter for a single process (development and tests)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, bucket: str, limit: int, window_seconds: int) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[bucket]
            while hits and now - hits[0] > window_seconds:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


class SqlRateLimiter:
    """Fixed-window limiter shared by every instance through the database. One upsert per request."""

    def __init__(self, engine) -> None:  # type: ignore[no-untyped-def]
        self.engine = engine
        self._last_prune = 0.0

    def hit(self, bucket: str, limit: int, window_seconds: int) -> bool:
        from sqlalchemy import text

        from app.db import sqlite_write_lock

        now = int(time.time())
        window = now - now % window_seconds
        stmt = text("INSERT INTO rate_limits (bucket, window_start, hits) VALUES (:b, :w, 1) "
                    "ON CONFLICT (bucket, window_start) DO UPDATE SET hits = rate_limits.hits + 1 "
                    "RETURNING hits")
        try:
            with sqlite_write_lock, self.engine.begin() as conn:
                hits = int(conn.execute(stmt, {"b": bucket[:200], "w": window}).scalar() or 0)
                if now - self._last_prune > 300:
                    self._last_prune = now
                    conn.execute(text("DELETE FROM rate_limits WHERE window_start < :cutoff"),
                                 {"cutoff": now - 86400})
        except Exception:  # noqa: BLE001 - a limiter outage must not take the service down
            log.warning("rate_limiter_unavailable", exc_info=True)
            return True
        return hits <= limit


_limiter: MemoryRateLimiter | SqlRateLimiter | None = None


def get_rate_limiter() -> MemoryRateLimiter | SqlRateLimiter:
    global _limiter
    if _limiter is None:
        settings = get_settings()
        if settings.database_url:
            from app.db import get_engine

            _limiter = SqlRateLimiter(get_engine())
        else:
            _limiter = MemoryRateLimiter()
    return _limiter


def reset_rate_limiter() -> None:
    global _limiter
    _limiter = None


def enforce(bucket: str, limit: int, window_seconds: int) -> None:
    if not get_rate_limiter().hit(bucket, limit, window_seconds):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests. Please wait a moment.",
                            headers={"Retry-After": str(window_seconds)})


def limiter(limit: int, window_seconds: int = 60, scope: str = "default"):  # type: ignore[no-untyped-def]
    """Per-principal limit on an authenticated route."""
    def dependency(request: Request, principal: Principal = Depends(current_principal)) -> Principal:
        # Bucket by route template, not concrete path, so a limit cannot be dodged by varying the case id.
        route = getattr(request.scope.get("route"), "path", request.url.path)
        enforce(f"{scope}:{request.method}:{route}:{principal.uid}", limit, window_seconds)
        return principal
    return dependency


def validate_upload(filename: str, data: bytes, settings: Settings, password: str | None = None):  # type: ignore[no-untyped-def]
    """Content-based validation. The declared content type and the file name are never trusted.
    Decrypts a password-protected statement and returns the prepared file."""
    from app.documents.intake import IntakeError, prepare

    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "The file is empty.")
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            f"Each file must be under {settings.max_upload_mb} MB.")
    if not data.startswith(PDF_MAGIC):
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Upload a PDF bank statement.")
    if b"/JavaScript" in data or b"/Launch" in data or b"/EmbeddedFile" in data:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "This PDF contains active content and cannot be accepted. Download a fresh copy from your "
                            "bank and upload that.")
    try:
        prepared = prepare(data, password)
    except IntakeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if prepared.decrypted and (b"/JavaScript" in prepared.data or b"/Launch" in prepared.data
                               or b"/EmbeddedFile" in prepared.data):
        # Encryption hides active content from the first check; look again once it is decrypted.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "This PDF contains active content and cannot be accepted.")
    if prepared.page_count > settings.max_pages_per_document:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"Statements longer than {settings.max_pages_per_document} pages are not accepted here.")
    return prepared
