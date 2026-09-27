"""Structured logging and request correlation.

Cloud Logging parses JSON on stdout, so logs are emitted as single-line JSON with the fields it
recognises. Request IDs are carried in a ContextVar so any log line written while handling a
request can be traced back to it without threading a parameter through every function.

Nothing here ever logs a payload. Errors log their type and the route, never the body: a stack
trace containing a borrower's transaction history is a data breach in a log aggregator.
"""
from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

SAFE_EXTRA = ("step", "case_id", "prompt", "model", "attempt", "error_type", "tokens", "route",
              "status", "duration_ms", "method", "detail", "val_ece", "method_name")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "requestId": request_id_var.get(),
        }
        for field in SAFE_EXTRA:
            if hasattr(record, field):
                entry[field] = getattr(record, field)
        if record.exc_info:
            entry["errorType"] = record.exc_info[0].__name__ if record.exc_info[0] else "Unknown"
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Root stays at INFO whatever the app level.

    Third-party libraries are extremely chatty at DEBUG - pdfminer emits a line per PDF token -
    which in Cloud Logging is both a cost problem and a data-leak risk, since those lines contain
    document content. Only our own loggers follow the requested level.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("artha").setLevel(level)
    for noisy in ("uvicorn.access", "google_genai", "urllib3", "pdfminer", "pdfplumber",
                  "google.adk", "httpx", "PIL", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]
