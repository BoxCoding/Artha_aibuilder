"""User accounts: password hashing, lockout, session revocation.

Passwords are hashed with Argon2id (argon2-cffi defaults, which follow the RFC 9106 low-memory
profile). Sessions carry the user's `session_version`; signing out increments it, which revokes
every outstanding cookie for that user on every device.

Demo accounts exist only when DEMO_MODE is on (never in production). They have no password and
sign in with their email alone, which is exactly the prototype's role picker, kept for demos.
"""
from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.domain.models import Role

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")

DEMO_ACCOUNTS: dict[str, tuple[str, Role]] = {
    "meena@demo.artha": ("Meena Devi", Role.BORROWER),
    "arun@demo.artha": ("Arun Prakash", Role.UNDERWRITER),
    "divya@demo.artha": ("Divya Menon", Role.UNDERWRITER),
    "risk@demo.artha": ("Risk Console", Role.RISK_ADMIN),
}

_hasher = PasswordHasher()
# Verified against when the email is unknown, so response time does not reveal which emails exist.
_DUMMY_HASH = _hasher.hash("artha-timing-equaliser")


class AuthError(Exception):
    """Sign-in failed. The message is safe to show and never says which part was wrong."""


class AccountExists(Exception):
    pass


@dataclass(frozen=True)
class User:
    uid: str
    email: str
    name: str
    role: Role
    password_hash: str | None
    is_demo: bool = False
    active: bool = True
    session_version: int = 1
    failed_logins: int = 0
    locked_until: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_login_at: datetime | None = None


def normalise_email(email: str) -> str:
    return email.strip().lower()


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def password_problem(password: str, min_length: int) -> str | None:
    if len(password) < min_length:
        return f"Use at least {min_length} characters."
    if len(password) > 256:
        return "Use at most 256 characters."
    if password.strip() != password or len(set(password)) < 5:
        return "Choose a less predictable password."
    return None


class UserRepository(Protocol):
    def get(self, uid: str) -> User | None: ...
    def by_email(self, email: str) -> User | None: ...
    def create(self, user: User) -> User: ...
    def save(self, user: User) -> None: ...
    def list(self) -> list[User]: ...


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._users: dict[str, User] = {}

    def get(self, uid: str) -> User | None:
        with self._lock:
            return self._users.get(uid)

    def by_email(self, email: str) -> User | None:
        with self._lock:
            return next((u for u in self._users.values() if u.email == email), None)

    def create(self, user: User) -> User:
        with self._lock:
            if self.by_email(user.email) or user.uid in self._users:
                raise AccountExists(user.email)
            self._users[user.uid] = user
            return user

    def save(self, user: User) -> None:
        with self._lock:
            self._users[user.uid] = user

    def list(self) -> list[User]:
        with self._lock:
            return list(self._users.values())


class SqlUserRepository:
    def __init__(self, engine) -> None:  # type: ignore[no-untyped-def]
        self.engine = engine

    @staticmethod
    def _row_to_user(row) -> User:  # type: ignore[no-untyped-def]
        return User(uid=row.uid, email=row.email, name=row.name, role=Role(row.role), password_hash=row.password_hash,
                    is_demo=row.is_demo, active=row.active, session_version=row.session_version,
                    failed_logins=row.failed_logins, locked_until=_aware(row.locked_until),
                    created_at=_aware(row.created_at) or datetime.now(timezone.utc),
                    last_login_at=_aware(row.last_login_at))

    @staticmethod
    def _values(user: User) -> dict[str, object]:
        return {"email": user.email, "name": user.name, "role": user.role.value, "password_hash": user.password_hash,
                "is_demo": user.is_demo, "active": user.active, "session_version": user.session_version,
                "failed_logins": user.failed_logins, "locked_until": user.locked_until,
                "created_at": user.created_at, "last_login_at": user.last_login_at}

    def get(self, uid: str) -> User | None:
        from sqlalchemy import select

        from app.db import users

        with self.engine.connect() as conn:
            row = conn.execute(select(users).where(users.c.uid == uid)).first()
        return self._row_to_user(row) if row else None

    def by_email(self, email: str) -> User | None:
        from sqlalchemy import select

        from app.db import users

        with self.engine.connect() as conn:
            row = conn.execute(select(users).where(users.c.email == email)).first()
        return self._row_to_user(row) if row else None

    def create(self, user: User) -> User:
        from sqlalchemy import insert
        from sqlalchemy.exc import IntegrityError

        from app.db import sqlite_write_lock, users

        try:
            with sqlite_write_lock, self.engine.begin() as conn:
                conn.execute(insert(users).values(uid=user.uid, **self._values(user)))
        except IntegrityError as exc:
            raise AccountExists(user.email) from exc
        return user

    def save(self, user: User) -> None:
        from sqlalchemy import update

        from app.db import sqlite_write_lock, users

        with sqlite_write_lock, self.engine.begin() as conn:
            conn.execute(update(users).where(users.c.uid == user.uid).values(**self._values(user)))

    def list(self) -> list[User]:
        from sqlalchemy import select

        from app.db import users

        with self.engine.connect() as conn:
            return [self._row_to_user(r) for r in conn.execute(select(users).order_by(users.c.created_at)).all()]


def _aware(value: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes; everything here is UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


class AccountService:
    def __init__(self, repo: UserRepository, *, max_failures: int = 5, lockout_seconds: int = 900,
                 password_min_length: int = 12) -> None:
        self.repo = repo
        self.max_failures = max_failures
        self.lockout = timedelta(seconds=lockout_seconds)
        self.min_length = password_min_length

    # ------------------------------------------------------------------ provisioning
    def register(self, email: str, name: str, password: str, role: Role = Role.BORROWER) -> User:
        email = normalise_email(email)
        if not valid_email(email):
            raise ValueError("Enter a valid email address.")
        if email.endswith("@demo.artha"):
            raise ValueError("That address is reserved.")
        problem = password_problem(password, self.min_length)
        if problem:
            raise ValueError(problem)
        name = " ".join(name.split())[:120]
        if len(name) < 2:
            raise ValueError("Enter your name.")
        user = User(uid=f"usr-{uuid.uuid4().hex[:16]}", email=email, name=name, role=role,
                    password_hash=hash_password(password), created_at=datetime.now(timezone.utc))
        return self.repo.create(user)

    def seed_demo_accounts(self) -> None:
        for email, (name, role) in DEMO_ACCOUNTS.items():
            if self.repo.by_email(email) is None:
                try:
                    # The uid is the email, as in the prototype, so existing demo data still resolves.
                    self.repo.create(User(uid=email, email=email, name=name, role=role, password_hash=None,
                                          is_demo=True, created_at=datetime.now(timezone.utc)))
                except AccountExists:
                    pass  # another instance seeded it first

    def set_password(self, uid: str, password: str) -> None:
        user = self.repo.get(uid)
        if user is None:
            raise KeyError(uid)
        problem = password_problem(password, self.min_length)
        if problem:
            raise ValueError(problem)
        self.repo.save(replace(user, password_hash=hash_password(password), is_demo=False,
                               session_version=user.session_version + 1, failed_logins=0, locked_until=None))

    def set_active(self, uid: str, active: bool) -> None:
        user = self.repo.get(uid)
        if user is None:
            raise KeyError(uid)
        self.repo.save(replace(user, active=active, session_version=user.session_version + 1))

    # ------------------------------------------------------------------ sign-in
    def authenticate(self, email: str, password: str | None, *, demo_mode: bool) -> User:
        email = normalise_email(email)
        user = self.repo.by_email(email)
        now = datetime.now(timezone.utc)
        generic = AuthError("Email or password is incorrect.")

        if user and user.is_demo:
            if not demo_mode or not user.active:
                raise generic
            return self._record_success(user, now)

        if user is None or not user.password_hash or not password:
            _verify(_DUMMY_HASH, password or "")
            raise generic
        if user.locked_until and user.locked_until > now:
            raise AuthError("Too many failed attempts. Try again in a few minutes.")
        if not _verify(user.password_hash, password):
            failures = user.failed_logins + 1
            locked = now + self.lockout if failures >= self.max_failures else None
            self.repo.save(replace(user, failed_logins=0 if locked else failures, locked_until=locked))
            raise generic
        if not user.active:
            raise generic
        if _hasher.check_needs_rehash(user.password_hash):
            user = replace(user, password_hash=hash_password(password))
        return self._record_success(user, now)

    def _record_success(self, user: User, now: datetime) -> User:
        user = replace(user, failed_logins=0, locked_until=None, last_login_at=now)
        self.repo.save(user)
        return user

    def needs_setup(self) -> bool:
        """True until the first real administrator exists (demo accounts do not count)."""
        return not any(u.role == Role.RISK_ADMIN and u.active and not u.is_demo for u in self.repo.list())

    def verify_password(self, uid: str, password: str) -> bool:
        user = self.repo.get(uid)
        return bool(user and user.password_hash and _verify(user.password_hash, password))

    def revoke_sessions(self, uid: str) -> None:
        user = self.repo.get(uid)
        if user:
            self.repo.save(replace(user, session_version=user.session_version + 1))


def _verify(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


_service: AccountService | None = None
_service_lock = threading.Lock()


def get_accounts() -> AccountService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                from app.config import get_settings

                settings = get_settings()
                repo: UserRepository
                if settings.database_url:
                    from app.db import get_engine

                    repo = SqlUserRepository(get_engine())
                else:
                    repo = InMemoryUserRepository()
                service = AccountService(repo, max_failures=settings.login_max_failures,
                                         lockout_seconds=settings.login_lockout_seconds,
                                         password_min_length=settings.password_min_length)
                if settings.demo_mode and not settings.is_prod:
                    service.seed_demo_accounts()
                _service = service
    return _service


def reset_accounts() -> None:
    global _service
    with _service_lock:
        _service = None
