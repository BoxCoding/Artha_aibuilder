"""Domain events: outbox, relay and sinks (Pub/Sub, structured logs).

Business code calls `publish_event(...)`, which only writes a row to the `outbox_events` table. The
relay (in the worker, or a background task in single-process mode) sends pending rows to the
configured sinks and marks them published: at-least-once delivery, consumers dedupe on `event_id`.

In Google Cloud the Pub/Sub topic carries a BigQuery subscription, so every event lands in the
`ledgerlens.events` table with no code of ours in between; `analytics/bigquery/*.sql` are the views
built on it. Events never carry personal data: no names, PAN, mobile numbers, narrations or account
numbers. Case IDs, codes, amounts, bands, industry and state only.
"""
from __future__ import annotations

import base64
import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

from app.config import get_settings

log = logging.getLogger("artha.events")
events_log = logging.getLogger("artha.events.stream")

TOPICS = ("case.created", "document.uploaded", "case.submitted", "case.scored", "fraud.flagged",
          "compliance.alert", "decision.recorded", "kfs.accepted")
SCHEMA_VERSION = "ledgerlens.event.v1"


def publish_event(topic: str, key: str, **payload: Any) -> None:
    """Record an event for delivery. Never raises: analytics must not break lending."""
    if topic not in TOPICS:
        raise ValueError(f"unknown event topic {topic}")
    try:
        from app.domain.store import get_store

        body = {"schema": SCHEMA_VERSION, "topic": topic, "key": key,
                "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds")} | payload
        get_store().emit(topic, key, json.loads(json.dumps(body, default=str)))
    except Exception:  # noqa: BLE001
        log.warning("event_emit_failed", extra={"detail": topic}, exc_info=True)


class Sink(Protocol):
    name: str

    def send(self, events: list[dict[str, Any]]) -> None: ...


class LogSink:
    """One JSON line per event on stdout: Cloud Logging can route these to BigQuery too."""
    name = "log"

    def send(self, events: list[dict[str, Any]]) -> None:
        for e in events:
            events_log.info("domain_event", extra={"detail": json.dumps(e["payload"], separators=(",", ":"))})


class PubSubSink:
    name = "pubsub"

    def __init__(self, topic: str) -> None:
        self.topic = topic  # projects/<project>/topics/<topic>

    def send(self, events: list[dict[str, Any]]) -> None:
        from app.gcp import get_gcp

        messages = [{"data": base64.b64encode(json.dumps(e["payload"], separators=(",", ":")).encode()).decode(),
                     "attributes": {"topic": e["topic"], "event_id": str(e["id"]), "schema": SCHEMA_VERSION},
                     "orderingKey": e["key"][:100]} for e in events]
        get_gcp().call("POST", f"https://pubsub.googleapis.com/v1/{self.topic}:publish", {"messages": messages})


def configured_sinks() -> list[Sink]:
    settings = get_settings()
    sinks: list[Sink] = []
    for name in (s.strip() for s in settings.event_sinks.split(",") if s.strip()):
        if name == "log":
            sinks.append(LogSink())
        elif name == "pubsub" and settings.pubsub_topic:
            sinks.append(PubSubSink(settings.pubsub_topic))
    return sinks


def relay_once(limit: int = 200, sinks: list[Sink] | None = None) -> int:
    """Deliver pending events to every sink. Returns how many were published. A sink failure leaves the
    batch pending for the next round."""
    from app.domain.store import get_store

    store = get_store()
    pending = store.pending_events(limit)
    if not pending:
        return 0
    sinks = configured_sinks() if sinks is None else sinks
    ids = [int(e["id"]) for e in pending]
    try:
        for sink in sinks:
            sink.send(pending)
    except Exception:  # noqa: BLE001
        log.warning("event_relay_failed", exc_info=True)
        store.mark_events(ids, published=False)
        return 0
    store.mark_events(ids, published=True)
    return len(ids)


# --------------------------------------------------------------------------- event payloads

def case_scored_payload(case: Any) -> dict[str, Any]:
    applicant = case.applicant
    return {
        "case_id": case.case_id, "channel": case.channel,
        "requested_amount": float(case.requested_amount), "tenure_months": case.tenure_months,
        "band": case.score.band if case.score else None, "pd": round(case.score.pd, 5) if case.score else None,
        "recommendation": case.policy.outcome.value if case.policy else None,
        "recommended_amount": float(case.policy.recommended_amount) if case.policy else None,
        "signal_codes": sorted({s.code for s in case.signals}),
        "fraud_high": sum(1 for s in case.signals if s.category == "FRAUD" and s.severity in ("HIGH", "CRITICAL")),
        "industry": applicant.industry if applicant else None, "state": applicant.state if applicant else None,
        "transactions": len(case.transactions),
        "readers": sorted({d.parser for d in case.documents}),
    }
