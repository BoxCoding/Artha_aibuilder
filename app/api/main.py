"""ARTHA HTTP API.

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

from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.ai.explain import build_explanation, coach_reply
from app.ai.gateway import UsageMeter, get_gateway
from app.api.security import (DEMO_USERS, Principal, authorise_case, clear_session, current_principal, issue_session,
                              limiter, require_roles, validate_upload)
from app.api.views import (CallbackRequest, CoachRequest, CopilotRequest, CreateCaseRequest, DecisionRequest,
                           LoginRequest, case_view, staff_view)
from app.config import get_settings
from app.domain.receipt import ReceiptSigner, build_payload, verify as verify_receipt
from app.observability import configure_logging, new_request_id, request_id_var
from app.documents.render import render_crop, render_page_png
from app.domain.models import (OUTCOME_TO_STATUS, Case, CaseStatus, Decision, Document, Explanation, Outcome, Role)
from app.domain.store import CaseNotFound, get_store
from app.pipeline.workflow import submit_case
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
    settings = get_settings()
    configure_logging("INFO" if settings.app_env == "prod" else "DEBUG")
    seed = settings.receipt_signing_seed.get_secret_value() if settings.receipt_signing_seed else None
    _signer = ReceiptSigner(seed)
    log.info("startup", extra={"detail": f"ai_mode={settings.llm_mode} receipt_key={_signer.key_id}"})
    _ready["value"] = True
    try:
        yield
    finally:
        _ready["value"] = False
        log.info("shutdown", extra={"detail": "draining"})


app = FastAPI(title="ARTHA", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None,
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
    token = request_id_var.set(request.headers.get("X-Request-Id", "")[:40] or new_request_id())
    started = datetime.now(timezone.utc)
    try:
        response: Response = await call_next(request)
    finally:
        pass
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
        "Cache-Control": "no-store" if request.url.path.startswith("/api") else "private, max-age=300",
    })
    if get_settings().app_env == "prod":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(CaseNotFound)
async def case_not_found(_request: Request, _exc: CaseNotFound) -> JSONResponse:
    return JSONResponse({"detail": "Case not found."}, status_code=status.HTTP_404_NOT_FOUND)


# --------------------------------------------------------------------------- session

@app.post("/api/session")
async def sign_in(payload: LoginRequest, response: Response) -> dict[str, Any]:
    settings = get_settings()
    entry = DEMO_USERS.get(payload.email.strip().lower())
    if not entry or not settings.demo_mode:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Unknown sign-in. Pick one of the demo roles.")
    name, role = entry
    principal = Principal(uid=payload.email.strip().lower(), name=name, role=role)
    issue_session(response, principal, settings)
    get_store().audit(principal.uid, role.value, "SIGN_IN", "session", {})
    return {"uid": principal.uid, "name": name, "role": role.value}


@app.delete("/api/session")
async def sign_out(response: Response) -> dict[str, str]:
    clear_session(response)
    return {"status": "signed_out"}


@app.get("/api/me")
async def me(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    settings = get_settings()
    return {"uid": principal.uid, "name": principal.name, "role": principal.role.value,
            "ai_mode": settings.llm_mode, "languages": settings.supported_languages,
            "demo_users": [{"email": e, "name": n, "role": r.value} for e, (n, r) in DEMO_USERS.items()]}


# --------------------------------------------------------------------------- borrower flow

@app.post("/api/cases", status_code=status.HTTP_201_CREATED)
async def create_case(payload: CreateCaseRequest,
                      principal: Principal = Depends(limiter(20))) -> dict[str, Any]:
    if not payload.consent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Consent is required before documents can be analysed.")
    case = Case(case_id=f"case-{uuid.uuid4().hex[:10]}", owner_uid=principal.uid,
                business_name=payload.business_name.strip(), requested_amount=Decimal(payload.requested_amount),
                tenure_months=payload.tenure_months, language=payload.language,
                consent_given_at=datetime.now(timezone.utc), consent_notice_version="consent.v1.2026-09")
    store = get_store()
    store.add(case)
    store.audit(principal.uid, principal.role.value, "CREATE_CASE", case.case_id,
                {"amount": payload.requested_amount, "tenure": payload.tenure_months})
    return {"case_id": case.case_id}


@app.post("/api/cases/{case_id}/documents")
async def upload_document(case_id: str, file: UploadFile = File(...),
                          principal: Principal = Depends(limiter(30))) -> dict[str, Any]:
    settings = get_settings()
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    if case.status not in (CaseStatus.DRAFT,):
        raise HTTPException(status.HTTP_409_CONFLICT, "This application has already been submitted.")
    if len(case.documents) >= settings.max_documents_per_case:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Up to {settings.max_documents_per_case} statements per application.")
    data = await file.read(settings.max_upload_mb * 1024 * 1024 + 1)
    validate_upload(file.filename or "statement.pdf", data, settings)
    doc_id = f"doc-{uuid.uuid4().hex[:8]}"
    with store.mutate(case_id) as live:
        live.documents.append(Document(doc_id=doc_id, filename=Path(file.filename or "statement.pdf").name[:80],
                                       sha256="", size_bytes=len(data), page_count=0))
    store.put_file(case_id, doc_id, data)
    store.audit(principal.uid, principal.role.value, "UPLOAD_DOCUMENT", f"{case_id}/{doc_id}", {"bytes": len(data)})
    return {"doc_id": doc_id, "filename": file.filename}


@app.post("/api/cases/{case_id}/sample")
async def attach_sample(case_id: str, persona: str = Body(embed=True),
                        principal: Principal = Depends(limiter(20))) -> dict[str, Any]:
    """Attach a generated sample statement, so the flow can be demonstrated without a real document."""
    if persona not in PERSONAS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown sample.")
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    data = await asyncio.to_thread(lambda: render_pdf(persona_statement(persona)))
    doc_id = f"doc-{uuid.uuid4().hex[:8]}"
    with store.mutate(case_id) as live:
        live.documents.append(Document(doc_id=doc_id, filename=f"{persona}-statement.pdf", sha256="",
                                       size_bytes=len(data), page_count=0))
    store.put_file(case_id, doc_id, data)
    store.audit(principal.uid, principal.role.value, "ATTACH_SAMPLE", f"{case_id}/{doc_id}", {"persona": persona})
    return {"doc_id": doc_id, "filename": f"{persona}-statement.pdf", "size_bytes": len(data)}


@app.post("/api/cases/{case_id}/submit")
async def submit(case_id: str, principal: Principal = Depends(limiter(20))) -> dict[str, Any]:
    store = get_store()
    case = store.get(case_id)
    authorise_case(case, principal)
    if not case.documents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Add at least one bank statement before submitting.")
    if case.status not in (CaseStatus.DRAFT, CaseStatus.NEEDS_MANUAL_PROCESSING):
        raise HTTPException(status.HTTP_409_CONFLICT, "This application is already being processed.")
    store.audit(principal.uid, principal.role.value, "SUBMIT_CASE", case_id, {"documents": len(case.documents)})
    submit_case(case_id)
    return {"status": "processing"}


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
        seen = 0
        for _ in range(600):  # ~120 s ceiling
            case = store.get(case_id)
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
            await asyncio.sleep(0.2)
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
    cases = sorted(get_store().list(), key=lambda c: c.updated_at, reverse=True)
    return {"cases": [{
        "case_id": c.case_id, "business_name": c.business_name, "status": c.status.value,
        "requested_amount": str(c.requested_amount), "updated_at": c.updated_at,
        "band": c.score.band if c.score else None,
        "recommendation": c.policy.outcome.value if c.policy else None,
        "integrity_findings": sum(1 for s in c.signals if s.category == "FRAUD" and s.severity in ("HIGH", "CRITICAL")),
        "awaiting_second_approval": c.status == CaseStatus.PENDING_SECOND_APPROVAL,
    } for c in cases if c.status != CaseStatus.DRAFT]}


@app.get("/api/cases/{case_id}/documents/{doc_id}/pages/{page}")
async def page_image(case_id: str, doc_id: str, page: int,
                     principal: Principal = Depends(require_roles(Role.UNDERWRITER, Role.RISK_ADMIN))) -> Response:
    store = get_store()
    case = store.get(case_id)
    if page < 1 or page > 200 or not any(d.doc_id == doc_id for d in case.documents):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Page not found.")
    data = store.get_file(case_id, doc_id)
    png = await asyncio.to_thread(render_page_png, data, page)
    return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=600"})


@app.get("/api/cases/{case_id}/evidence/{evidence_id}/crop")
async def evidence_crop(case_id: str, evidence_id: str,
                        principal: Principal = Depends(require_roles(Role.UNDERWRITER, Role.RISK_ADMIN))) -> Response:
    store = get_store()
    case = store.get(case_id)
    evidence = case.evidence.get(evidence_id)
    if not evidence or not evidence.bbox:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Evidence not found.")
    data = store.get_file(case_id, evidence.doc_id)
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
    case = store.get(case_id)
    if case.status not in DECIDABLE:
        raise HTTPException(status.HTTP_409_CONFLICT, "This case is not open for a decision.")
    if not case.policy:
        raise HTTPException(status.HTTP_409_CONFLICT, "Analysis has not finished for this case.")
    try:
        outcome = Outcome(payload.outcome)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown outcome.") from exc

    override = outcome != case.policy.outcome
    if override and not (payload.reason and payload.reason.strip()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Give a reason when your decision differs from the recommendation.")
    amount = Decimal(payload.approved_amount) if payload.approved_amount else None
    if outcome.value.startswith("APPROVE"):
        amount = amount or case.policy.recommended_amount
        if amount <= 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set an approved amount above zero.")
    else:
        amount = None

    four_eyes = amount is not None and amount > Decimal(str(_four_eyes_threshold(case)))
    pending = case.pending_decision
    if four_eyes and not (pending and pending.decided_by != principal.uid):
        decision = Decision(decision_id=f"dec-{uuid.uuid4().hex[:8]}", system_recommendation=case.policy.outcome,
                            final_outcome=outcome, approved_amount=amount, conditions=list(case.policy.conditions),
                            override=override, reason=payload.reason, decided_by=principal.uid)
        with store.mutate(case_id) as live:
            live.pending_decision = decision
            live.status = CaseStatus.PENDING_SECOND_APPROVAL
        store.audit(principal.uid, principal.role.value, "DECISION_PROPOSED", case_id,
                    {"outcome": outcome.value, "amount": str(amount), "override": override})
        return {"status": "pending_second_approval", "decided_by": principal.uid}

    decision = Decision(decision_id=f"dec-{uuid.uuid4().hex[:8]}", system_recommendation=case.policy.outcome,
                        final_outcome=outcome, approved_amount=amount,
                        conditions=list(case.policy.conditions), override=override, reason=payload.reason,
                        decided_by=(pending.decided_by if four_eyes and pending else principal.uid),
                        second_approver=principal.uid if four_eyes else None)
    with store.mutate(case_id) as live:
        live.decision = decision
        live.pending_decision = None
        live.status = OUTCOME_TO_STATUS[outcome]
    store.audit(principal.uid, principal.role.value, "DECISION_RECORDED", case_id,
                {"outcome": outcome.value, "amount": str(amount), "override": override,
                 "second_approver": decision.second_approver})
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
    cases = [c for c in store.list() if c.score]
    bands = {b: sum(1 for c in cases if c.score and c.score.band == b) for b in "ABCDE"}
    outcomes: dict[str, int] = {}
    for c in cases:
        if c.policy:
            outcomes[c.policy.outcome.value] = outcomes.get(c.policy.outcome.value, 0) + 1
    overrides = [c for c in cases if c.decision and c.decision.override]
    signal_counts: dict[str, int] = {}
    for c in cases:
        for s in c.signals:
            signal_counts[s.code] = signal_counts.get(s.code, 0) + 1
    memo_stats = [c.memo for c in cases if c.memo]
    return {
        "cases_scored": len(cases), "bands": bands, "recommendations": outcomes,
        "decisions_recorded": sum(1 for c in cases if c.decision), "overrides": len(overrides),
        "override_examples": [{"case_id": c.case_id, "from": c.decision.system_recommendation.value,
                               "to": c.decision.final_outcome.value, "reason": c.decision.reason}
                              for c in overrides[:10] if c.decision],
        "signal_frequency": dict(sorted(signal_counts.items(), key=lambda kv: -kv[1])),
        "memo_revisions": sum(m.iterations - 1 for m in memo_stats),
        "claims_dropped_by_gate": sum(len(m.dropped_claims) for m in memo_stats),
        "critic_issues_open": sum(len(m.critic_issues) for m in memo_stats),
        "callbacks_requested": len(store.callbacks()),
        "audit_chain_valid": store.verify_audit_chain(),
        "audit_tail": [e.model_dump(mode="json") for e in store.audit_events(25)],
        "ai_mode": get_settings().llm_mode,
    }


@app.get("/api/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies, so a slow model cannot cause a restart."""
    return {"status": "ok", "ai_mode": get_settings().llm_mode}


@app.get("/api/readyz")
async def readyz() -> Response:
    """Readiness: startup finished and the receipt key exists. Cloud Run withholds traffic until this passes."""
    if not _ready["value"]:
        return JSONResponse({"status": "starting"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
    return JSONResponse({"status": "ready", "receipt_key_id": signer().key_id,
                         "ephemeral_receipt_key": signer().ephemeral})


@app.get("/.well-known/artha-receipt-key.json")
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
async def verify_receipt_endpoint(receipt: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Verify a receipt. Deliberately unauthenticated: a receipt is worthless if checking it
    requires an account with the party that issued it."""
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

@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
