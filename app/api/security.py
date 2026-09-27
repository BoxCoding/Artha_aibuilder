"""Session handling, role checks, rate limiting and upload validation."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import Settings, get_settings
from app.domain.models import Case, Role

COOKIE_NAME = "artha_session"
PDF_MAGIC = b"%PDF-"
DEMO_USERS: dict[str, tuple[str, Role]] = {
    "meena@demo.artha": ("Meena Devi", Role.BORROWER),
    "arun@demo.artha": ("Arun Prakash", Role.UNDERWRITER),
    "divya@demo.artha": ("Divya Menon", Role.UNDERWRITER),
    "risk@demo.artha": ("Risk Console", Role.RISK_ADMIN),
}


@dataclass(frozen=True)
class Principal:
    uid: str
    name: str
    role: Role


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key.get_secret_value(), salt="artha-session")


def issue_session(response: Response, principal: Principal, settings: Settings) -> None:
    token = _serializer(settings).dumps({"uid": principal.uid, "name": principal.name, "role": principal.role.value})
    response.set_cookie(COOKIE_NAME, token, max_age=settings.session_ttl_seconds, httponly=True, samesite="strict",
                        secure=settings.app_env == "prod", path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def current_principal(request: Request, settings: Settings = Depends(get_settings)) -> Principal:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign in to continue.")
    try:
        data = _serializer(settings).loads(token, max_age=settings.session_ttl_seconds)
    except BadSignature as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Your session has expired. Sign in again.") from exc
    return Principal(uid=data["uid"], name=data["name"], role=Role(data["role"]))


def require_roles(*roles: Role):  # type: ignore[no-untyped-def]
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Your role cannot perform this action.")
        return principal
    return dependency


def authorise_case(case: Case, principal: Principal) -> None:
    """Borrowers see only their own cases; staff see all. 404 rather than 403 to avoid leaking existence."""
    if principal.role == Role.BORROWER and case.owner_uid != principal.uid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found.")


class RateLimiter:
    """Fixed-window per-principal limiter. Cloud Armor handles edge limits in production."""

    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit, self.window = limit, window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests. Please wait a moment.")
        hits.append(now)


def limiter(limit: int, window_seconds: int = 60):  # type: ignore[no-untyped-def]
    guard = RateLimiter(limit, window_seconds)

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        guard.check(principal.uid)
        return principal
    return dependency


def validate_upload(filename: str, data: bytes, settings: Settings) -> None:
    """Content-based validation. The declared content type and the file name are never trusted."""
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
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(__import__("io").BytesIO(data))
        pages = len(reader.pages)
    except (PdfReadError, ValueError, OSError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "This PDF could not be opened.") from exc
    if reader.is_encrypted:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "This PDF is password protected. Remove the password and upload it again.")
    if pages > settings.max_pages_per_document:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"Statements longer than {settings.max_pages_per_document} pages are not accepted here.")
