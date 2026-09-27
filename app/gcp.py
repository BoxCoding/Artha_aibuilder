"""Minimal Google Cloud REST client shared by the Pub/Sub, BigQuery, Document AI, Vertex AI Search and
Vertex AI prediction adapters.

Why REST rather than five client SDKs: the image stays small, every adapter is a few lines over one
authenticated `httpx` call, and tests can exercise each adapter end to end by swapping the transport.
Credentials come from Application Default Credentials: the Cloud Run service account in production,
`gcloud auth application-default login` on a workstation. No API keys are ever used.

Every adapter built on this is optional. When its setting is empty, the local equivalent runs instead.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

import httpx

log = logging.getLogger("artha.gcp")
SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class GcpError(RuntimeError):
    """A Google Cloud call failed. Callers fall back to their local path."""


class GcpClient:
    def __init__(self, timeout: float = 30.0, transport: httpx.BaseTransport | None = None,
                 token_provider: Any = None) -> None:
        self.timeout = timeout
        self._transport = transport
        self._token_provider = token_provider
        self._lock = threading.Lock()
        self._credentials: Any = None

    def _token(self) -> str:
        if self._token_provider is not None:
            return str(self._token_provider())
        with self._lock:
            if self._credentials is None:
                import google.auth

                self._credentials, _ = google.auth.default(scopes=[SCOPE])
            if not self._credentials.valid:
                from google.auth.transport.requests import Request

                self._credentials.refresh(Request())
            return str(self._credentials.token)

    def call(self, method: str, url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        started = time.monotonic()
        try:
            headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
            with httpx.Client(timeout=self.timeout, transport=self._transport) as client:
                response = client.request(method, url, json=body, headers=headers)
        except GcpError:
            raise
        except Exception as exc:  # noqa: BLE001 - auth, DNS, timeouts
            raise GcpError(f"{type(exc).__name__} calling Google Cloud") from exc
        if response.status_code >= 400:
            log.warning("gcp_call_failed", extra={"status": response.status_code, "route": url.split("?")[0][-80:]})
            raise GcpError(f"Google Cloud returned {response.status_code}")
        log.debug("gcp_call", extra={"duration_ms": int((time.monotonic() - started) * 1000)})
        return response.json() if response.content else {}


_client: GcpClient | None = None


def get_gcp() -> GcpClient:
    global _client
    if _client is None:
        _client = GcpClient()
    return _client


def set_gcp(client: GcpClient | None) -> None:
    """Swap the client (tests)."""
    global _client
    _client = client
