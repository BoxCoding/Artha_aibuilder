"""Pipeline worker: `python -m app.worker`.

Claims queued cases from the database and runs the pipeline, `PIPELINE_CONCURRENCY` at a time.
Holds a lease per job and renews it while the pipeline runs, so a crashed worker's job is picked up
by another. On SIGTERM (Cloud Run scale-in, a deploy) it stops claiming, lets running cases finish
within the grace period, and exits. Also runs the retention sweep on a timer.

If PORT is set (Cloud Run services must listen on it) a minimal health endpoint answers there.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import time
import uuid

from app.config import get_settings
from app.domain.store import get_store
from app.observability import configure_logging
from app.pipeline.jobs import Claim, JobQueue, get_queue
from app.pipeline.workflow import mark_needs_manual, run_case

log = logging.getLogger("artha.worker")

GAVE_UP = ("Automatic processing could not be completed after several attempts. An officer will review "
           "the application manually.")


class Worker:
    def __init__(self, queue: JobQueue | None = None, concurrency: int | None = None) -> None:
        settings = get_settings()
        self.settings = settings
        self.queue = queue or get_queue()
        self.concurrency = concurrency or settings.pipeline_concurrency
        self.worker_id = f"{socket.gethostname()[:40]}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.stopping = asyncio.Event()
        self._last_sweep = 0.0

    async def process(self, claim: Claim) -> None:
        if claim.exhausted:
            log.error("job_exhausted", extra={"case_id": claim.case_id})
            await asyncio.to_thread(mark_needs_manual, claim.case_id, GAVE_UP)
            return
        log.info("job_started", extra={"case_id": claim.case_id, "attempt": claim.attempt})
        heartbeat = asyncio.create_task(self._heartbeat(claim))
        ok, error = True, None
        try:
            await run_case(claim.case_id)
        except Exception as exc:  # noqa: BLE001 - the job must be closed out whatever happened
            ok, error = False, type(exc).__name__
            log.exception("job_crashed", extra={"case_id": claim.case_id})
            await asyncio.to_thread(mark_needs_manual, claim.case_id,
                                    "Automatic processing stopped unexpectedly. An officer will review it.")
        finally:
            heartbeat.cancel()
        await asyncio.to_thread(self.queue.finish, claim.job_id, self.worker_id, ok=ok, error=error)
        log.info("job_finished", extra={"case_id": claim.case_id, "status": "ok" if ok else "failed"})

    async def _heartbeat(self, claim: Claim) -> None:
        interval = max(5.0, self.settings.job_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            still_ours = await asyncio.to_thread(self.queue.heartbeat, claim.job_id, self.worker_id)
            if not still_ours:
                log.warning("job_lease_lost", extra={"case_id": claim.case_id})
                return

    async def slot(self, index: int) -> None:
        while not self.stopping.is_set():
            try:
                claim = await asyncio.to_thread(self.queue.claim, self.worker_id)
            except Exception:  # noqa: BLE001 - database blips must not kill the worker
                log.warning("job_claim_failed", exc_info=True)
                claim = None
            if claim is None:
                if index == 0:
                    await self._maybe_sweep()
                    await self._relay_events()
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=self.settings.worker_poll_seconds)
                except TimeoutError:
                    pass
                continue
            await self.process(claim)

    async def _relay_events(self) -> None:
        from app.events import relay_once

        try:
            sent = await asyncio.to_thread(relay_once)
            if sent:
                log.info("events_relayed", extra={"detail": f"count={sent}"})
        except Exception:  # noqa: BLE001
            log.warning("event_relay_error", exc_info=True)

    async def _maybe_sweep(self) -> None:
        now = time.monotonic()
        if now - self._last_sweep < self.settings.retention_sweep_interval_seconds:
            return
        self._last_sweep = now
        try:
            removed = await asyncio.to_thread(get_store().sweep_expired, self.settings.case_retention_days * 86400)
            if removed:
                log.info("retention_sweep", extra={"detail": f"removed={removed}"})
        except Exception:  # noqa: BLE001
            log.warning("retention_sweep_failed", exc_info=True)

    async def run(self) -> None:
        log.info("worker_started", extra={"detail": f"id={self.worker_id} concurrency={self.concurrency}"})
        await asyncio.gather(*(self.slot(i) for i in range(self.concurrency)))
        log.info("worker_stopped")


async def _health_server(port: int, worker: Worker) -> asyncio.base_events.Server:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(reader.readline(), timeout=5)
            ok = not worker.stopping.is_set()
            body = b'{"status":"ok"}' if ok else b'{"status":"stopping"}'
            status_line = b"HTTP/1.1 200 OK" if ok else b"HTTP/1.1 503 Service Unavailable"
            writer.write(status_line + b"\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "0.0.0.0", port)  # noqa: S104 - container port


async def main() -> None:
    settings = get_settings()
    configure_logging("INFO" if settings.is_prod else "DEBUG")
    if not settings.database_url:
        raise SystemExit("The worker needs DATABASE_URL: there is no queue without a database.")
    worker = Worker()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stopping.set)
    server = None
    if os.environ.get("PORT"):
        server = await _health_server(int(os.environ["PORT"]), worker)
    try:
        await worker.run()
    finally:
        if server:
            server.close()


if __name__ == "__main__":
    asyncio.run(main())
