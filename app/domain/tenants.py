"""Lenders on the platform (tenants).

Every user and case belongs to exactly one lender. Staff only ever see their own lender's
applications; customers join a lender through its sign-up link (`/?lender=<id>`). Lenders are
added by the platform operator with `python -m app.cli create-tenant`.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import Engine, insert, select
from sqlalchemy.exc import IntegrityError

from app.db import sqlite_write_lock
from app.db import tenants as tenants_table
from app.domain.models import DEFAULT_TENANT

TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$")


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    name: str
    active: bool = True


class TenantDirectory:
    """The lenders table, or a dict when there is no database (development and unit tests)."""

    def __init__(self, engine: Engine | None = None) -> None:
        self.engine = engine
        self._lock = threading.Lock()
        self._memory: dict[str, Tenant] = {}

    def get(self, tenant_id: str) -> Tenant | None:
        if self.engine is None:
            return self._memory.get(tenant_id)
        with self.engine.connect() as conn:
            row = conn.execute(select(tenants_table).where(tenants_table.c.tenant_id == tenant_id)).first()
        return Tenant(row.tenant_id, row.name, row.active) if row else None

    def list(self) -> list[Tenant]:
        if self.engine is None:
            return list(self._memory.values())
        with self.engine.connect() as conn:
            rows = conn.execute(select(tenants_table).order_by(tenants_table.c.created_at)).all()
        return [Tenant(r.tenant_id, r.name, r.active) for r in rows]

    def create(self, tenant_id: str, name: str) -> Tenant:
        tenant_id, name = tenant_id.strip().lower(), " ".join(name.split())[:120]
        if not TENANT_ID_RE.match(tenant_id):
            raise ValueError("Use 3 to 40 lowercase letters, digits or hyphens for the lender id.")
        if len(name) < 2:
            raise ValueError("Enter the lender's name.")
        tenant = Tenant(tenant_id, name)
        if self.engine is None:
            with self._lock:
                if tenant_id in self._memory:
                    raise ValueError("A lender with this id already exists.")
                self._memory[tenant_id] = tenant
            return tenant
        try:
            with sqlite_write_lock, self.engine.begin() as conn:
                conn.execute(insert(tenants_table).values(tenant_id=tenant_id, name=name, active=True,
                                                          created_at=datetime.now(timezone.utc)))
        except IntegrityError as exc:
            raise ValueError("A lender with this id already exists.") from exc
        return tenant


_directory: TenantDirectory | None = None
_directory_lock = threading.Lock()


def get_tenants() -> TenantDirectory:
    global _directory
    if _directory is None:
        with _directory_lock:
            if _directory is None:
                from app.config import get_settings

                engine = None
                if get_settings().database_url:
                    from app.db import get_engine

                    engine = get_engine()
                directory = TenantDirectory(engine)
                if directory.get(DEFAULT_TENANT) is None:
                    try:
                        directory.create(DEFAULT_TENANT, "LedgerLens")
                    except ValueError:
                        pass  # another instance created it first
                _directory = directory
    return _directory
