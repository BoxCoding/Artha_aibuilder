"""Case pipeline orchestrated as a Google ADK Workflow graph.

    START -> read_documents -> screen_content -> categorise -> (forensics | cashflow) -> join
          -> risk_engine -> memo_and_critic -> publish

Each node operates on a pipeline-owned working copy of the case and publishes a snapshot to the
store when it finishes, so API reads never wait on a model call. Failures are recorded, later
nodes skip, and the case moves to NEEDS_MANUAL_PROCESSING with a plain-language reason.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
import warnings
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from google.adk.agents.context import Context
from google.adk.runners import InMemoryRunner
from google.adk.workflow import START, JoinNode, Workflow, node
from google.genai import types

from app.ai.document_ai import categorise_with_gemini, extract_with_gemini, visual_inspection
from app.ai.gateway import LLMUnavailable, UsageMeter, get_gateway
from app.ai.memo import write_and_review
from app.cashflow.categorise import apply_rules
from app.cashflow.features import add_computed_evidence, cashflow_signals, compute_features
from app.config import get_settings
from app.documents.ingest import attach_parsed_statement
from app.documents.pdf_reader import read_statement
from app.domain.models import Case, CaseStatus, PipelineEvent, Signal
from app.domain.store import CaseNotFound, CaseStore, get_store
from app.forensics.rules import run_rules
from app.risk.engine import apply_policy, challenge_score, improvement_plan, score
from app.security.injection import detect
from app.security.model_armor import ModelArmorGuard

warnings.filterwarnings("ignore", category=DeprecationWarning, module="google.adk")
log = logging.getLogger("artha.pipeline")

STEP_LABELS = {
    "read_documents": "Reading the statements",
    "screen_content": "Screening document text for hidden instructions",
    "categorise": "Categorising transactions",
    "forensics": "Checking the documents are genuine",
    "cashflow": "Analysing business cash flow",
    "risk_engine": "Scoring and applying credit policy",
    "structuring": "Looking for a structure that would work",
    "memo_and_critic": "Writing the credit memo and reviewing it",
    "publish": "Publishing for underwriter review",
}


class UserFacingError(RuntimeError):
    """An error whose message is safe to show to users."""


@dataclass
class RunContext:
    case: Case
    store: CaseStore
    meter: UsageMeter
    files: dict[str, bytes]
    forensic_signals: list[Signal] = field(default_factory=list)
    cashflow_signals: list[Signal] = field(default_factory=list)
    failed: str | None = None


_RUNS: dict[str, RunContext] = {}


class CaseDeleted(RuntimeError):
    """The case was erased (deletion right or retention) while the pipeline was running."""


def _publish(rc: RunContext) -> None:
    rc.case.token_usage = rc.meter.used
    try:
        rc.store.replace(rc.case.model_copy(deep=True))
    except CaseNotFound as exc:
        raise CaseDeleted(rc.case.case_id) from exc


def step(name: str) -> Callable[[Callable[[RunContext], Awaitable[dict[str, str | int | float] | None]]], object]:
    def decorator(fn: Callable[[RunContext], Awaitable[dict[str, str | int | float] | None]]) -> object:
        # No functools.wraps here: the ADK binds node parameters from the callable's signature,
        # so the wrapper must expose (ctx) rather than the wrapped function's (rc).
        async def wrapper(ctx: Context) -> str:
            rc = _RUNS[ctx.state["run_id"]]
            if rc.failed:
                rc.case.events.append(PipelineEvent(step=name, label=STEP_LABELS[name], status="skipped"))
                return "skipped"
            rc.case.events.append(PipelineEvent(step=name, label=STEP_LABELS[name], status="started"))
            _publish(rc)
            started, tokens_before = time.monotonic(), rc.meter.used
            try:
                detail = await fn(rc) or {}
                detail["tokens"] = rc.meter.used - tokens_before
                rc.case.events.append(PipelineEvent(step=name, label=STEP_LABELS[name], status="completed",
                                                    duration_ms=int((time.monotonic() - started) * 1000), detail=detail))
            except CaseDeleted:
                raise
            except Exception as exc:  # noqa: BLE001 - convert every failure into a recorded, safe state
                safe = str(exc) if isinstance(exc, UserFacingError) else "An internal error stopped automatic processing."
                log.exception("pipeline_step_failed", extra={"step": name, "case_id": rc.case.case_id})
                rc.failed = safe
                rc.case.events.append(PipelineEvent(step=name, label=STEP_LABELS[name], status="failed",
                                                    duration_ms=int((time.monotonic() - started) * 1000),
                                                    detail={"error": safe}))
            _publish(rc)
            return name

        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return node(wrapper, name=name)
    return decorator


# --------------------------------------------------------------------------- nodes

@step("read_documents")
async def read_documents(rc: RunContext) -> dict[str, str | int | float]:
    gateway = get_gateway()
    parsers = []
    for doc_id, data in rc.files.items():
        parsed = await asyncio.to_thread(read_statement, data)
        parser = "template"
        if parsed.needs_ai:
            if not gateway.enabled:
                raise UserFacingError("This statement layout needs Gemini document reading, which is turned off in "
                                      "this deployment. Use a sample statement or enable AI mode.")
            try:
                parsed = await extract_with_gemini(data, gateway, rc.meter)
            except LLMUnavailable as exc:
                raise UserFacingError("Gemini could not read this statement. Please upload a clearer copy.") from exc
            parser = "gemini"
            if parsed.needs_ai:
                raise UserFacingError("No transactions could be read from this statement.")
        filename = next(d.filename for d in rc.case.documents if d.doc_id == doc_id)
        rc.case.documents = [d for d in rc.case.documents if d.doc_id != doc_id]
        attach_parsed_statement(rc.case, doc_id, filename, data, parsed, parser)
        parsers.append(parser)
    return {"documents": len(rc.files), "transactions": len(rc.case.transactions), "parser": ",".join(parsers)}


@step("screen_content")
async def screen_content(rc: RunContext) -> dict[str, str | int | float]:
    flagged = 0
    for t in rc.case.transactions:
        if detect(t.narration):
            t.redacted = True
            flagged += 1
    guard = ModelArmorGuard()
    armor = "not_configured"
    if guard.enabled:
        text = "\n".join(t.narration for t in rc.case.transactions if not t.redacted)
        verdict = await asyncio.to_thread(guard.screen_prompt, text)
        armor = verdict.detail
        if verdict.blocked:
            # Managed filter matched but the local layer did not localise it: keep every narration away from models.
            for t in rc.case.transactions:
                t.redacted = True
    return {"local_flags": flagged, "model_armor": armor}


@step("categorise")
async def categorise(rc: RunContext) -> dict[str, str | int | float]:
    unmatched = apply_rules(rc.case.transactions)
    by_model = 0
    gateway = get_gateway()
    if unmatched and gateway.enabled:
        try:
            by_model = await categorise_with_gemini(unmatched, gateway, rc.meter)
        except LLMUnavailable:
            by_model = 0
    return {"by_rules": len(rc.case.transactions) - len(unmatched), "by_gemini": by_model}


@step("forensics")
async def forensics(rc: RunContext) -> dict[str, str | int | float]:
    signals = await asyncio.to_thread(run_rules, rc.case)
    rc.case.signals = signals  # visual inspector uses rule findings to choose crops
    visual = 0
    gateway = get_gateway()
    if gateway.enabled and all(d.parser == "template" for d in rc.case.documents):
        try:
            extra = await visual_inspection(rc.case, rc.files, gateway, rc.meter)
            visual = len(extra)
            signals = signals + extra
        except LLMUnavailable:
            visual = -1
    rc.forensic_signals = signals
    return {"rule_findings": len(signals) - max(visual, 0), "visual_findings": visual}


@step("cashflow")
async def cashflow(rc: RunContext) -> dict[str, str | int | float]:
    features, monthly = await asyncio.to_thread(compute_features, rc.case)
    rc.case.features, rc.case.monthly = features, monthly
    add_computed_evidence(rc.case)
    rc.cashflow_signals = cashflow_signals(rc.case)
    return {"months": int(features["statement_span_months"]), "signals": len(rc.cashflow_signals)}


@step("risk_engine")
async def risk_engine(rc: RunContext) -> dict[str, str | int | float]:
    rc.case.signals = rc.forensic_signals + rc.cashflow_signals
    result = score(rc.case.features)
    rc.case.score = result
    challenge, disagreement = challenge_score(rc.case.features, result)
    rc.case.model_challenge = challenge
    if disagreement and "ev-computed-inflow" in rc.case.evidence:
        rc.case.signals = rc.case.signals + [disagreement]
    rc.case.policy = apply_policy(rc.case.features, rc.case.signals, result, rc.case.requested_amount,
                                  rc.case.tenure_months, rc.case.jurisdiction)
    rc.case.improvement_plan = improvement_plan(rc.case.features, result)
    return {"band": result.band, "outcome": rc.case.policy.outcome.value,
            "ml_pd": str(challenge.get("ml_pd", "n/a")), "models_disagree": str(bool(disagreement))}


@step("structuring")
async def structuring(rc: RunContext) -> dict[str, str | int | float]:
    """For anything short of an approval, ask whether a different amount or tenure would pass."""
    from app.risk.structuring import structure_options

    result = await asyncio.to_thread(structure_options, rc.case)
    rc.case.structuring = result.as_dict()
    return {"options": len(result.viable), "blocked_by": ",".join(result.blocked_by) or "none"}


@step("memo_and_critic")
async def memo_and_critic(rc: RunContext) -> dict[str, str | int | float]:
    memo = await write_and_review(rc.case, get_gateway(), rc.meter)
    rc.case.memo = memo
    return {"author": memo.author, "iterations": memo.iterations, "verdict": memo.critic_verdict,
            "dropped_claims": len(memo.dropped_claims)}


@step("publish")
async def publish(rc: RunContext) -> dict[str, str | int | float]:
    rc.case.status = CaseStatus.READY_FOR_REVIEW
    return {"llm_calls": len(rc.meter.calls)}


def build_workflow() -> Workflow:
    join = JoinNode(name="analysis_join")
    return Workflow(name="artha_case_pipeline", edges=[
        (START, read_documents, screen_content, categorise, (forensics, cashflow), join, risk_engine,
         structuring, memo_and_critic, publish),
    ])


# --------------------------------------------------------------------------- runner

async def run_case(case_id: str, store: CaseStore | None = None) -> Case | None:
    """Run the full pipeline for one case. Returns None if the case was deleted mid-run."""
    store = store or get_store()
    settings = get_settings()
    with store.mutate(case_id) as live:
        live.status = CaseStatus.PROCESSING
        live.ai_mode = settings.llm_mode
        live.events = []
        working = live.model_copy(deep=True)
        working.transactions, working.signals, working.evidence = [], [], {}
    files = {d.doc_id: store.get_file(case_id, d.doc_id) for d in working.documents}
    run_id = uuid.uuid4().hex
    rc = RunContext(case=working, store=store, meter=UsageMeter(budget=settings.token_budget_per_case), files=files)
    _RUNS[run_id] = rc
    try:
        runner = InMemoryRunner(node=build_workflow(), app_name="artha")
        session = await runner.session_service.create_session(app_name="artha", user_id="pipeline",
                                                               state={"run_id": run_id})
        message = types.Content(role="user", parts=[types.Part(text=f"process case {case_id}")])
        async for _event in runner.run_async(user_id="pipeline", session_id=session.id, new_message=message):
            pass
    except CaseDeleted:
        log.info("case_deleted_during_pipeline", extra={"case_id": case_id})
        return None
    except Exception:  # noqa: BLE001
        log.exception("workflow_crashed", extra={"case_id": case_id})
        rc.failed = rc.failed or "Automatic processing stopped unexpectedly."
    finally:
        _RUNS.pop(run_id, None)
    if rc.failed:
        rc.case.status = CaseStatus.NEEDS_MANUAL_PROCESSING
        rc.case.failure_reason = rc.failed
    elif rc.case.status != CaseStatus.READY_FOR_REVIEW:
        rc.case.status = CaseStatus.NEEDS_MANUAL_PROCESSING
        rc.case.failure_reason = "Automatic processing did not complete."
    try:
        _publish(rc)
    except CaseDeleted:
        return None
    return store.get(case_id)


def mark_needs_manual(case_id: str, reason: str, store: CaseStore | None = None) -> None:
    """Park a case for a person when automatic processing cannot finish, whatever the cause."""
    store = store or get_store()
    try:
        with store.mutate(case_id) as live:
            if live.status == CaseStatus.PROCESSING:
                live.status = CaseStatus.NEEDS_MANUAL_PROCESSING
                live.failure_reason = reason
    except CaseNotFound:
        pass


_executor: ThreadPoolExecutor | None = None


def submit_case(case_id: str) -> str:
    """Hand a case to the pipeline.

    queue mode (production): write a durable job for `python -m app.worker` to claim.
    inline mode (development): run on a thread with its own event loop, off the API loop.
    """
    settings = get_settings()
    if settings.pipeline_mode == "queue":
        from app.pipeline.jobs import get_queue

        return get_queue().enqueue(case_id)
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=get_settings().pipeline_concurrency, thread_name_prefix="pipeline")
    _executor.submit(asyncio.run, run_case(case_id))
    return "inline"
