"""LedgerLens HTTP API.

Every state-changing route is authenticated, role-checked, rate-limited and written to the
hash-chained audit log. Responses are shaped per role by app.api.views.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.ai.explain import build_explanation, coach_reply
from app.ai.gateway import UsageMeter, get_gateway
from app.api.security import (COOKIE_NAME, Principal, authorise_case, clear_session, client_ip, current_principal,
                              enforce, issue_session, limiter, origin_allowed, require_roles, validate_upload)
from app.api.views import (CallbackRequest, ChangePasswordRequest, CoachRequest, CopilotRequest, CreateCaseRequest,
                           CreateUserRequest, DecisionRequest, LoginRequest, RegisterRequest, case_view)
from app.config import get_settings
from app.domain.receipt import ReceiptSigner, build_payload, verify as verify_receipt
from app.observability import configure_logging, new_request_id, request_id_var
from app.documents.render import render_crop, render_page_png
from app.domain.models import (OUTCOME_TO_STATUS, Case, CaseStatus, Decision, Document, Explanation, Outcome, Role)
from app.domain.store import CaseNotFound, get_store
from app.domain.users import DEMO_ACCOUNTS, AccountExists, AuthError, get_accounts
from app.pipeline.workflow import mark_needs_manual, submit_case
from app.synthetic.generator import PERSONAS, persona_statement, render_pdf

log = logging.getLogger("artha.api")
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
DECIDABLE = {CaseStatus.READY_FOR_REVIEW, CaseStatus.PENDING_SECOND_APPROVAL, CaseStatus.NEEDS_MANUAL_PROCESSING}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; "
       "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

_signer: ReceiptSigner | None = None
_ready = {"value": False}


@asynccontextmanager
async def lifespan(_app: FastAPI):  # type: ignore[no-untyped-def]
    """Start: configure logging and the receipt key, then report ready. Stop: drain cleanly."""
    global _signer
    settings = get_settings()  # raises ConfigurationError on an unsafe production configuration
    configure_logging("INFO" if settings.is_prod else "DEBUG")
    if settings.database_url and settings.database_url.startswith("sqlite"):
        # Local single-process convenience. PostgreSQL schemas are managed by Alembic (`alembic upgrade head`).
        from app.db import get_engine, metadata

        metadata.create_all(get_engine())
    get_accounts()  # seeds demo accounts when demo mode is on
    if settings.pipeline_mode == "inline" and settings.database_url:
        # A restart kills in-process pipeline runs. Nothing else will resume them in inline mode
        # (queue mode recovers through job leases), so pick them up again here.
        stranded = get_store().ids_with_status(CaseStatus.PROCESSING)
        for case_id in stranded:
            submit_case(case_id)
        if stranded:
            log.info("resumed_stranded_cases", extra={"detail": f"count={len(stranded)}"})
    seed = settings.receipt_signing_seed.get_secret_value() if settings.receipt_signing_seed else None
    _signer = ReceiptSigner(seed)
    log.info("startup", extra={"detail": f"env={settings.app_env} ai_mode={settings.llm_mode} "
                                         f"pipeline={settings.pipeline_mode} "
                                         f"store={'sql' if settings.database_url else 'memory'} "
                                         f"receipt_key={_signer.key_id}"})
    _ready["value"] = True
    try:
        yield
    finally:
        _ready["value"] = False
        log.info("shutdown", extra={"detail": "draining"})


app = FastAPI(title="LedgerLens", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)


def signer() -> ReceiptSigner:
    global _signer
    if _signer is None:  # direct TestClient use without the lifespan
        settings = get_settings()
        seed = settings.receipt_signing_seed.get_secret_value() if settings.receipt_signing_seed else None
        _signer = ReceiptSigner(seed)
    return _signer


@app.middleware("http")
async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
    token = request_id_var.set(_safe_request_id(request.headers.get("X-Request-Id", "")) or new_request_id())
    started = datetime.now(timezone.utc)
    settings = get_settings()
    if request.url.path.startswith("/api") and not origin_allowed(request, settings):
        response: Response = JSONResponse({"detail": "Cross-site request refused."},
                                          status_code=status.HTTP_403_FORBIDDEN)
    else:
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - never leak a stack trace, always return the request id
            log.exception("unhandled_error", extra={"route": request.url.path, "method": request.method})
            response = JSONResponse({"detail": "Something went wrong on our side. Quote this reference if you "
                                               f"contact support: {request_id_var.get()}"},
                                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    response.headers["X-Request-Id"] = request_id_var.get()
    if request.url.path.startswith("/api"):
        log.info("request", extra={"route": request.url.path, "method": request.method,
                                   "status": response.status_code,
                                   "duration_ms": int((datetime.now(timezone.utc) - started).total_seconds() * 1000)})
    request_id_var.reset(token)
    response.headers.update({
        "Content-Security-Policy": CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
        "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    })
    if "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api") else "private, max-age=300"
    if settings.is_prod:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def _safe_request_id(value: str) -> str:
    """Accept a caller's correlation id only if it is short and boring; it ends up in every log line."""
    value = value[:40]
    return value if value and all(c.isalnum() or c in "-_." for c in value) else ""


@app.exception_handler(CaseNotFound)
async def case_not_found(_request: Request, _exc: CaseNotFound) -> JSONResponse:
    return JSONResponse({"detail": "Case not found."}, status_code=status.HTTP_404_NOT_FOUND)


# --------------------------------------------------------------------------- session

@app.get("/api/config")
async def public_config() -> dict[str, Any]:
    """What the sign-in page needs before anyone is signed in."""
    settings = get_settings()
    demo = settings.demo_mode and not settings.is_prod
    return {
        "product": "LedgerLens",
        "needs_setup": await asyncio.to_thread(get_accounts().needs_setup),
        "demo_mode": demo,
        "registration": settings.allow_self_registration,
        "password_min_length": settings.password_min_length,
        "languages": settings.supported_languages,
        "demo_users": [{"email": e, "name": n, "role": r.value} for e, (n, r) in DEMO_ACCOUNTS.items()] if demo else [],
    }


@app.post("/api/session")
async def sign_in(payload: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    settings = get_settings()
    email = payload.email.strip().lower()
    ip = client_ip(request, settings)
    enforce(f"login-ip:{ip}", 30, 300)
    enforce(f"login-email:{email}", 10, 300)
    try:
        user = await asyncio.to_thread(get_accounts().authenticate, email, payload.password,
                                       demo_mode=settings.demo_mode and not settings.is_prod)
    except AuthError as exc:
        get_store().audit("anonymous", "none", "SIGN_IN_FAILED", "session", {"email_sha": _digest(email)})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    issue_session(response, user, settings)
    get_store().audit(user.uid, user.role.value, "SIGN_IN", "session", {"demo": user.is_demo})
    return {"uid": user.uid, "name": user.name, "role": user.role.value}


@app.post("/api/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, request: Request, response: Response) -> dict[str, Any]:
    """Borrower self-registration. Staff accounts are created with `python -m app.cli create-user`."""
    settings = get_settings()
    if not settings.allow_self_registration:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Registration is closed.")
    enforce(f"register-ip:{client_ip(request, settings)}", 5, 3600)
    try:
        user = await asyncio.to_thread(get_accounts().register, payload.email, payload.name, payload.password)
    except AccountExists as exc:
        # Deliberately the same shape as a validation error; the audit log records what happened.
        get_store().audit("anonymous", "none", "REGISTER_DUPLICATE", "account", {"email_sha": _digest(payload.email)})
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists. Sign in instead.") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    issue_session(response, user, settings)
    get_store().audit(user.uid, user.role.value, "REGISTER", "account", {})
    return {"uid": user.uid, "name": user.name, "role": user.role.value}


@app.delete("/api/session")
async def sign_out(request: Request, response: Response) -> dict[str, str]:
    """Signing out revokes every session for the account, not just this cookie."""
    if request.cookies.get(COOKIE_NAME):
        try:
            principal = current_principal(request, get_settings())
            await asyncio.to_thread(get_accounts().revoke_sessions, principal.uid)
            get_store().audit(principal.uid, principal.role.value, "SIGN_OUT", "session", {})
        except HTTPException:
            pass  # already invalid; clearing the cookie is all that is left to do
    clear_session(response)
    return {"status": "signed_out"}


@app.post("/api/setup", status_code=status.HTTP_201_CREATED)
async def first_run_setup(payload: CreateUserRequest, request: Request, response: Response) -> dict[str, Any]:
    """Create the first administrator. Open only until one exists; after that, staff are added by an admin."""
    settings = get_settings()
    enforce(f"setup-ip:{client_ip(request, settings)}", 5, 3600)
    accounts = get_accounts()
    if not await asyncio.to_thread(accounts.needs_setup):
        raise HTTPException(status.HTTP_409_CONFLICT, "Setup is already complete. Sign in instead.")
    try:
        user = await asyncio.to_thread(accounts.register, payload.email, payload.name, payload.password,
                                       Role.RISK_ADMIN)
    except AccountExists as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    issue_session(response, user, settings)
    get_store().audit(user.uid, user.role.value, "FIRST_ADMIN_CREATED", "account", {})
    return {"uid": user.uid, "name": user.name, "role": user.role.value}


@app.get("/api/admin/users")
async def list_users(principal: Principal = Depends(require_roles(Role.RISK_ADMIN))) -> dict[str, Any]:
    users = await asyncio.to_thread(get_accounts().repo.list)
    return {"users": [{"uid": u.uid, "email": u.email, "name": u.name, "role": u.role.value, "active": u.active,
                       "demo": u.is_demo, "last_login_at": u.last_login_at, "created_at": u.created_at,
                       "locked": bool(u.locked_until and u.locked_until > datetime.now(timezone.utc))}
                      for u in users]}


@app.post("/api/admin/users", status_code=status.HTTP_201_CREATED)
async def create_user(payload: CreateUserRequest,
                      principal: Principal = Depends(require_roles(Role.RISK_ADMIN))) -> dict[str, Any]:
    """Add a loan officer, credit officer, administrator or customer account."""
    enforce(f"admin-create:{principal.uid}", 30, 3600)
    try:
        user = await asyncio.to_thread(get_accounts().register, payload.email, payload.name, payload.password,
                                       payload.role)
    except AccountExists as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with this email already exists.") from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    get_store().audit(principal.uid, principal.role.value, "ACCOUNT_CREATED", user.uid, {"role": user.role.value})
    return {"uid": user.uid, "email": user.email, "role": user.role.value}


@app.post("/api/admin/users/{uid}/{action}")
async def change_user(uid: str, action: str,
                      principal: Principal = Depends(require_roles(Role.RISK_ADMIN))) -> dict[str, str]:
    if action not in ("disable", "enable"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    if uid == principal.uid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot disable your own account.")
    try:
        await asyncio.to_thread(get_accounts().set_active, uid, action == "enable")
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found.") from exc
    get_store().audit(principal.uid, principal.role.value, f"ACCOUNT_{action.upper()}D", uid, {})
    return {"status": f"{action}d"}


@app.post("/api/me/password")
async def change_password(payload: ChangePasswordRequest, response: Response,
                          principal: Principal = Depends(limiter(5, 300))) -> dict[str, str]:
    """Change your own password. Every other session is signed out; this one is re-issued."""
    accounts = get_accounts()
    if not await asyncio.to_thread(accounts.verify_password, principal.uid, payload.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Your current password is not correct.")
    try:
        await asyncio.to_thread(accounts.set_password, principal.uid, payload.new_password)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    user = accounts.repo.get(principal.uid)
    if user:
        issue_session(response, user, get_settings())
    get_store().audit(principal.uid, principal.role.value, "PASSWORD_CHANGED", principal.uid, {})
    return {"status": "changed"}


@app.get("/api/me")
async def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    settings = get_settings()
    return {"uid": principal.uid, "name": principal.name, "role": principal.role.value,
            "ai_mode": settings.llm_mode, "ai": await _ai_status() if principal.role != Role.BORROWER else None,
            "languages": settings.supported_languages}


def _digest(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.strip().lower().encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- borrower flow

@app.post("/api/cases", status_code=status.HTTP_201_CREATED)
async def create_case(payload: CreateCaseRequest,
                      principal: Principal = Depends(require_roles(Role.BORROWER, Role.LOAN_OFFICER))) -> dict[str, Any]:
    """A customer applying for themselves, or a loan officer onboarding a customer (assisted)."""
    enforce(f"create-case:{principal.uid}", 20, 60)
    if not payload.consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Consent is required before documents can be analysed.")
    assisted = principal.role == Role.LOAN_OFFICER
    applicant = payload.applicant
    case = Case(case_id=f"case-{uuid.uuid4().hex[:10]}", owner_uid=principal.uid,
                business_name=(payload.business_name or applicant.business_name).strip(),
                requested_amount=Decimal(payload.requested_amount), tenure_months=payload.tenure_months,
                language=payload.language, applicant=applicant, channel="assisted" if assisted else "self",
                sourced_by=principal.uid if assisted else None,
                consent_given_at=datetime.now(timezone.utc), consent_notice_version="consent.v2.2026-09")
    store = get_store()
    store.add(case)
    store.audit(principal.uid, principal.role.value, "CREATE_CASE", case.case_id,
                {"amount": payload.requested_amount, "tenure": payload.tenure_months, "channel": case.channel})
    return {"case_id": case.case_id, "warnings": applicant.warnings()}


@app.post("/api/cases/{case_id}/documents")
async def upload_document(case_id: str, file: UploadFile = File(...), password: str | None = Form(default=None),
                          principal: Principal = Depends(limiter(30))) -> dict[str, Any]:
    """Accept a statement, decrypt it if needed, and read it immediately so the uploader sees at once
    which bank, account and period it covers, or why it cannot be used. The password is never stored."""
    settings = get_settings()
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    _check_can_attach(case, settings.max_documents_per_case)
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    prepared = await asyncio.to_thread(validate_upload, file.filename or "statement.pdf", data, settings,
                                       (password or "").strip() or None)
    from app.documents.intake import preview as preview_statement, unreadable_message

    reading = await asyncio.to_thread(preview_statement, prepared.data)
    gateway = get_gateway()
    problem = None if reading["readable"] else unreadable_message(
        reading["reason"], model_available=gateway.enabled, vision_available=gateway.supports_vision)  # type: ignore[arg-type]
    if problem:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, problem)
    doc_id = f"doc-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(store.put_file, case_id, doc_id, prepared.data)
    filename = Path(file.filename or "statement.pdf").name[:80]
    with store.mutate(case_id) as live:
        _check_can_attach(live, settings.max_documents_per_case)  # re-checked under the row lock
        live.documents.append(Document(
            doc_id=doc_id, filename=filename, sha256="", size_bytes=len(data), page_count=prepared.page_count,
            decrypted=prepared.decrypted, source_sha256=prepared.source_sha256,
            metadata=prepared.source_metadata if prepared.decrypted else {},
            incremental_updates=prepared.source_incremental_updates if prepared.decrypted else 0))
    store.audit(principal.uid, principal.role.value, "UPLOAD_DOCUMENT", f"{case_id}/{doc_id}",
                {"bytes": len(data), "decrypted": prepared.decrypted, "readable": reading["readable"]})
    note = None if reading["readable"] else "This layout will be read by the AI model when you submit."
    return {"doc_id": doc_id, "filename": filename, "preview": reading, "note": note}


@app.delete("/api/cases/{case_id}/documents/{doc_id}")
async def remove_document(case_id: str, doc_id: str, principal: Principal = Depends(limiter(30))) -> dict[str, str]:
    """Take back a statement before submitting (wrong file, wrong account)."""
    store = get_store()
    authorise_case(store.get(case_id), principal)
    with store.mutate(case_id) as live:
        if live.status != CaseStatus.DRAFT:
            raise HTTPException(status.HTTP_409_CONFLICT, "Documents cannot be changed after submission.")
        if not any(d.doc_id == doc_id for d in live.documents):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Document not found.")
        live.documents = [d for d in live.documents if d.doc_id != doc_id]
    store.audit(principal.uid, principal.role.value, "REMOVE_DOCUMENT", f"{case_id}/{doc_id}", {})
    return {"status": "removed"}


@app.post("/api/cases/{case_id}/sample")
async def attach_sample(case_id: str, persona: str = Body(embed=True),
                        principal: Principal = Depends(limiter(20))) -> dict[str, Any]:
    """Attach a generated sample statement, so the flow can be demonstrated without a real document.
    Demo mode only: a production lender must never accept a statement it generated itself."""
    if not get_settings().demo_mode or get_settings().is_prod:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found.")
    if persona not in PERSONAS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown sample.")
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    _check_can_attach(case, get_settings().max_documents_per_case)
    data = await asyncio.to_thread(lambda: render_pdf(persona_statement(persona)))
    doc_id = f"doc-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(store.put_file, case_id, doc_id, data)
    with store.mutate(case_id) as live:
        _check_can_attach(live, get_settings().max_documents_per_case)
        live.documents.append(Document(doc_id=doc_id, filename=f"{persona}-statement.pdf", sha256="",
                                       size_bytes=len(data), page_count=0))
    store.audit(principal.uid, principal.role.value, "ATTACH_SAMPLE", f"{case_id}/{doc_id}", {"persona": persona})
    return {"doc_id": doc_id, "filename": f"{persona}-statement.pdf", "size_bytes": len(data)}


@app.post("/api/cases/{case_id}/submit")
async def submit(case_id: str, principal: Principal = Depends(limiter(20))) -> dict[str, Any]:
    store = get_store()
    authorise_case(store.get(case_id), principal)
    with store.mutate(case_id) as live:  # check-and-set under the row lock: a double click queues once
        if not live.documents:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add at least one bank statement before submitting.")
        if live.status not in (CaseStatus.DRAFT, CaseStatus.NEEDS_MANUAL_PROCESSING):
            raise HTTPException(status.HTTP_409_CONFLICT, "This application is already being processed.")
        live.status = CaseStatus.PROCESSING
        live.failure_reason = None
        live.events = []
        documents = len(live.documents)
    store.audit(principal.uid, principal.role.value, "SUBMIT_CASE", case_id, {"documents": documents})
    try:
        job = await asyncio.to_thread(submit_case, case_id)
    except Exception as exc:
        log.exception("submit_failed", extra={"case_id": case_id})
        await asyncio.to_thread(mark_needs_manual, case_id, "The application could not be queued. Please try again.")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "We could not start processing just now. Please try again in a minute.") from exc
    return {"status": "processing", "job": job}


def _check_can_attach(case: Case, limit: int) -> None:
    if case.status != CaseStatus.DRAFT:
        raise HTTPException(status.HTTP_409_CONFLICT, "This application has already been submitted.")
    if len(case.documents) >= limit:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Up to {limit} statements per application.")


@app.get("/api/my/cases")
async def my_cases(principal: Principal = Depends(require_roles(Role.BORROWER, Role.LOAN_OFFICER))) -> dict[str, Any]:
    """A customer's own applications, or the applications a loan officer sourced, newest first.
    Officers also see the instant recommendation; customers never see internal scores."""
    rows = await asyncio.to_thread(get_store().list_for_owner, principal.uid, 200,
                                   principal.role == Role.LOAN_OFFICER)
    return {"cases": rows}


@app.get("/api/cases/{case_id}")
async def get_case(case_id: str, principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    explanation = case.explanations.get(case.language) if case.decision else None
    return case_view(case, principal.role, explanation)


@app.get("/api/cases/{case_id}/events")
async def case_events(case_id: str, principal: Principal = Depends(current_principal)) -> StreamingResponse:
    """Server-sent events: pipeline progress, then the final case payload."""
    store = get_store()
    authorise_case(store.get(case_id), principal)

    async def stream():  # type: ignore[no-untyped-def]
        seen, marker = 0, -1
        for _ in range(600):  # ~120 s ceiling; the client reconnects or falls back to polling
            try:
                current = await asyncio.to_thread(store.change_marker, case_id)
            except CaseNotFound:
                yield "event: gone\ndata: {}\n\n"
                return
            if current == marker:
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.4)
                continue
            marker = current
            case = await asyncio.to_thread(store.get, case_id)
            events = case.events
            while seen < len(events):
                payload = events[seen].model_dump(mode="json")
                seen += 1
                yield f"event: step\ndata: {json.dumps(payload)}\n\n"
            if case.status in (CaseStatus.READY_FOR_REVIEW, CaseStatus.NEEDS_MANUAL_PROCESSING) or case.decision:
                explanation = case.explanations.get(case.language) if case.decision else None
                final = case_view(case, principal.role, explanation)
                yield f"event: done\ndata: {json.dumps(final, default=str)}\n\n"
                return
            await asyncio.sleep(0.4)
        yield "event: timeout\ndata: {}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/cases/{case_id}/coach")
async def coach(case_id: str, payload: CoachRequest,
                principal: Principal = Depends(limiter(20, 60))) -> dict[str, Any]:
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    explanation = case.explanations.get(case.language)
    if not explanation:
        raise HTTPException(status.HTTP_409_CONFLICT, "The decision is not ready yet.")
    draft, author = await coach_reply(explanation, payload.message, get_gateway(),
                                      UsageMeter(budget=get_settings().token_budget_per_case))
    store.audit(principal.uid, principal.role.value, "COACH_MESSAGE", case_id, {"author": author})
    return {"reply": draft.reply, "offer_callback": draft.offer_callback, "author": author}


@app.post("/api/cases/{case_id}/callback")
async def request_callback(case_id: str, payload: CallbackRequest,
                           principal: Principal = Depends(limiter(5, 300))) -> dict[str, str]:
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    store.add_callback({"case_id": case_id, "uid": principal.uid, "note": payload.note,
                        "at": datetime.now(timezone.utc).isoformat()})
    store.audit(principal.uid, principal.role.value, "REQUEST_CALLBACK", case_id, {})
    return {"status": "queued"}


# --------------------------------------------------------------------------- underwriter flow

@app.get("/api/queue")
async def queue(principal: Principal = Depends(require_roles(Role.UNDERWRITER, Role.RISK_ADMIN))) -> dict[str, Any]:
    rows = await asyncio.to_thread(get_store().list_queue, get_settings().queue_page_size)
    return {"cases": rows}


@app.get("/api/cases/{case_id}/documents/{doc_id}/pages/{page}")
async def page_image(case_id: str, doc_id: str, page: int,
                     principal: Principal = Depends(require_roles(Role.LOAN_OFFICER, Role.UNDERWRITER, Role.RISK_ADMIN))) -> Response:
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    if page < 1 or page > 200 or not any(d.doc_id == doc_id for d in case.documents):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found.")
    data = await asyncio.to_thread(store.get_file, case_id, doc_id)
    try:
        png = await asyncio.to_thread(render_page_png, data, page)
    except IndexError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found.") from exc
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@app.get("/api/cases/{case_id}/evidence/{evidence_id}/crop")
async def evidence_crop(case_id: str, evidence_id: str,
                        principal: Principal = Depends(require_roles(Role.LOAN_OFFICER, Role.UNDERWRITER, Role.RISK_ADMIN))) -> Response:
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    evidence = case.evidence.get(evidence_id)
    if not evidence or not evidence.bbox:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence not found.")
    data = await asyncio.to_thread(store.get_file, case_id, evidence.doc_id)
    png = await asyncio.to_thread(render_crop, data, evidence.page, evidence.bbox)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@app.post("/api/cases/{case_id}/copilot")
async def copilot(case_id: str, payload: CopilotRequest,
                  principal: Principal = Depends(require_roles(Role.UNDERWRITER, Role.RISK_ADMIN))) -> dict[str, Any]:
    """Ask a question about one case. The agent holds read-only, case-scoped tools only."""
    from app.ai.copilot import ask

    store = get_store()
    case = store.get(case_id)
    answer = await ask(case, payload.question, get_gateway())
    store.audit(principal.uid, principal.role.value, "COPILOT_QUERY", case_id,
                {"author": answer.author, "tools": len(answer.tool_calls)})
    return answer.as_dict()


@app.post("/api/cases/{case_id}/decision")
async def decide(case_id: str, payload: DecisionRequest,
                 principal: Principal = Depends(require_roles(Role.UNDERWRITER))) -> dict[str, Any]:
    store = get_store()
    try:
        outcome = Outcome(payload.outcome)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown outcome.") from exc

    # The whole check-and-set runs under the case's row lock, so two underwriters acting at once
    # cannot both be "first", and nobody can countersign a proposal that has just changed.
    with store.mutate(case_id) as live:
        if live.status not in DECIDABLE:
            raise HTTPException(status.HTTP_409_CONFLICT, "This case is not open for a decision.")
        if not live.policy:
            raise HTTPException(status.HTTP_409_CONFLICT, "Analysis has not finished for this case.")
        policy = live.policy
        override = outcome != policy.outcome
        if override and not (payload.reason and payload.reason.strip()):
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "Give a reason when your decision differs from the recommendation.")
        amount = Decimal(payload.approved_amount) if payload.approved_amount else None
        if outcome.value.startswith("APPROVE"):
            amount = amount or policy.recommended_amount
            if amount <= 0:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set an approved amount above zero.")
            if amount > live.requested_amount:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "The approved amount cannot exceed the amount requested.")
        else:
            amount = None

        four_eyes = amount is not None and amount > Decimal(str(_four_eyes_threshold(live)))
        pending = live.pending_decision
        countersigning = (four_eyes and pending is not None and pending.decided_by != principal.uid
                          and pending.final_outcome == outcome and pending.approved_amount == amount)
        if four_eyes and not countersigning:
            live.pending_decision = Decision(
                decision_id=f"dec-{uuid.uuid4().hex[:8]}", system_recommendation=policy.outcome,
                final_outcome=outcome, approved_amount=amount, conditions=list(policy.conditions),
                override=override, reason=payload.reason, decided_by=principal.uid)
            live.status = CaseStatus.PENDING_SECOND_APPROVAL
            recorded = False
        else:
            live.decision = Decision(
                decision_id=f"dec-{uuid.uuid4().hex[:8]}", system_recommendation=policy.outcome,
                final_outcome=outcome, approved_amount=amount, conditions=list(policy.conditions),
                override=override, reason=(pending.reason if countersigning and pending else None) or payload.reason,
                decided_by=(pending.decided_by if countersigning and pending else principal.uid),
                second_approver=principal.uid if countersigning else None)
            live.pending_decision = None
            live.status = OUTCOME_TO_STATUS[outcome]
            recorded = True
        second_approver = live.decision.second_approver if recorded and live.decision else None

    if not recorded:
        store.audit(principal.uid, principal.role.value, "DECISION_PROPOSED", case_id,
                    {"outcome": outcome.value, "amount": str(amount), "override": override})
        return {"status": "pending_second_approval", "decided_by": principal.uid}
    store.audit(principal.uid, principal.role.value, "DECISION_RECORDED", case_id,
                {"outcome": outcome.value, "amount": str(amount), "override": override,
                 "second_approver": second_approver})
    await _ensure_explanations(case_id)
    return {"status": "recorded", "outcome": outcome.value}


def _four_eyes_threshold(case: Case) -> str:
    from app.risk.engine import load_policy
    return load_policy(case.jurisdiction)["four_eyes_above"]


async def _ensure_explanations(case_id: str) -> None:
    store = get_store()
    case = store.get(case_id)
    meter = UsageMeter(budget=get_settings().token_budget_per_case)
    languages = {case.language, "en"}
    built: dict[str, Explanation] = {}
    for lang in languages:
        built[lang] = await build_explanation(case, lang, get_gateway(), meter)
    with store.mutate(case_id) as live:
        live.explanations.update(built)


@app.get("/api/cases/{case_id}/explanation")
async def explanation(case_id: str, language: str = "en",
                      principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    if not case.decision:
        raise HTTPException(status.HTTP_409_CONFLICT, "The decision is not ready yet.")
    language = language if language in get_settings().supported_languages else "en"
    if language not in case.explanations:
        built = await build_explanation(case, language, get_gateway(),
                                        UsageMeter(budget=get_settings().token_budget_per_case))
        with store.mutate(case_id) as live:
            live.explanations[language] = built
    return get_store().get(case_id).explanations[language].model_dump()


# --------------------------------------------------------------------------- risk console

@app.get("/api/risk/overview")
async def risk_overview(principal: Principal = Depends(require_roles(Role.RISK_ADMIN))) -> dict[str, Any]:
    store = get_store()
    summaries = [s for s in await asyncio.to_thread(store.summaries) if s.get("scored")]
    bands = {b: sum(1 for s in summaries if s.get("band") == b) for b in "ABCDE"}
    outcomes: dict[str, int] = {}
    signal_counts: dict[str, int] = {}
    for s in summaries:
        if s.get("recommendation"):
            outcomes[s["recommendation"]] = outcomes.get(s["recommendation"], 0) + 1
        for code in s.get("signal_codes", []):
            signal_counts[code] = signal_counts.get(code, 0) + 1
    overrides = [s for s in summaries if s.get("override")]
    memos = [s["memo"] for s in summaries if s.get("memo")]
    payload: dict[str, Any] = {
        "cases_scored": len(summaries), "bands": bands, "recommendations": outcomes,
        "decisions_recorded": sum(1 for s in summaries if s.get("decided")), "overrides": len(overrides),
        "override_examples": [{"case_id": s["case_id"]} | s["override"] for s in overrides[:10]],
        "signal_frequency": dict(sorted(signal_counts.items(), key=lambda kv: -kv[1])),
        "memo_revisions": sum(m["revisions"] for m in memos),
        "claims_dropped_by_gate": sum(m["dropped"] for m in memos),
        "critic_issues_open": sum(m["critic_issues"] for m in memos),
        "callbacks_requested": await asyncio.to_thread(store.callback_count),
        "audit_chain_valid": await asyncio.to_thread(store.verify_audit_chain),
        "audit_tail": [e.model_dump(mode="json") for e in await asyncio.to_thread(store.audit_events, 25)],
        "ai_mode": get_settings().llm_mode,
    }
    if get_settings().pipeline_mode == "queue":
        from app.pipeline.jobs import get_queue

        payload["pipeline_jobs"] = await asyncio.to_thread(get_queue().depth)
    return payload


@app.get("/api/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies, so a slow model cannot cause a restart."""
    return {"status": "ok", "ai_mode": get_settings().llm_mode}


@app.get("/api/readyz")
async def readyz() -> Response:
    """Readiness: startup finished, the receipt key exists and the database answers."""
    if not _ready["value"]:
        return JSONResponse({"status": "starting"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    if not await asyncio.to_thread(get_store().ping):
        return JSONResponse({"status": "database_unavailable"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse({"status": "ready", "receipt_key_id": signer().key_id,
                         "ephemeral_receipt_key": signer().ephemeral, "ai": await _ai_status()})


async def _ai_status() -> dict[str, Any]:
    """Whether the configured model is reachable. Informational: the service runs without it."""
    settings = get_settings()
    if settings.llm_mode == "ollama":
        from app.ai.gateway import OllamaClient

        ok, detail = await OllamaClient(settings.ollama_url).ping(settings.ollama_model)
        return {"mode": "ollama", "model": settings.ollama_model, "vision_model": settings.ollama_vision_model,
                "reachable": ok, "detail": detail}
    return {"mode": settings.llm_mode, "reachable": settings.llm_mode != "offline"}


@app.get("/.well-known/ledgerlens-receipt-key.json")
async def receipt_key() -> dict[str, Any]:
    """The public half of the receipt signing key, so anyone can verify a receipt without us."""
    issuer = signer()
    return {"algorithm": "Ed25519", "public_key": issuer.public_key_b64, "key_id": issuer.key_id,
            "verify_with": "tools/verify_receipt.py"}


@app.get("/api/cases/{case_id}/receipt")
async def issue_receipt(case_id: str, principal: Principal = Depends(limiter(30))) -> dict[str, Any]:
    """Issue a portable, signed record of the decision. The applicant can take this anywhere."""
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    if not case.decision:
        raise HTTPException(status.HTTP_409_CONFLICT, "A receipt is issued once a decision is recorded.")
    head, count = store.audit_head()
    receipt = signer().sign(build_payload(case, head, count))
    store.audit(principal.uid, principal.role.value, "RECEIPT_ISSUED", case_id,
                {"payload_sha256": receipt.payload_sha256})
    return receipt.as_dict()


@app.post("/api/receipts/verify")
async def verify_receipt_endpoint(request: Request, receipt: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Verify a receipt. Deliberately unauthenticated: a receipt is worthless if checking it
    requires an account with the party that issued it. Rate-limited per address instead."""
    enforce(f"verify-ip:{client_ip(request, get_settings())}", 60, 60)
    ok, reason = verify_receipt(receipt)
    issued_by_us = receipt.get("public_key") == signer().public_key_b64
    return {"valid": ok, "reason": reason, "issued_by_this_service": issued_by_us,
            "note": None if issued_by_us else "Signature is valid but was issued by a different key."}


@app.delete("/api/cases/{case_id}")
async def delete_case(case_id: str, principal: Principal = Depends(limiter(20))) -> dict[str, str]:
    """Data deletion right. The hash-chained audit log is intentionally retained: it holds digests,
    not personal data, and it is what keeps a past decision reviewable."""
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    store.audit(principal.uid, principal.role.value, "CASE_DELETED", case_id,
                {"documents": len(case.documents)})
    store.delete_case(case_id)
    return {"status": "deleted", "case_id": case_id}


# --------------------------------------------------------------------------- static app

def _asset_version() -> str:
    """Content hash of the front end, so a deploy is picked up at once instead of after the cache expires."""
    import hashlib

    digest = hashlib.sha256()
    for name in ("app.js", "styles.css", "index.html"):
        digest.update((STATIC_DIR / name).read_bytes())
    return digest.hexdigest()[:12]


@app.get("/")
async def index() -> Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8").replace("__ASSET_VERSION__", _asset_version())
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
