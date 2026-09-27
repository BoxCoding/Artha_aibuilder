"""Operator commands: `python -m app.cli <command>`.

    generate-secrets                         print a SECRET_KEY and RECEIPT_SIGNING_SEED
    init-db                                  create tables directly (SQLite / throwaway databases only;
                                             production uses `alembic upgrade head`)
    create-user EMAIL NAME ROLE              create an account; password read from LEDGERLENS_PASSWORD or a prompt
    set-password EMAIL                       replace a password and sign the user out everywhere
    disable-user EMAIL / enable-user EMAIL   block or restore an account (revokes sessions)
    list-users
    verify-audit                             recompute the audit hash chain; non-zero exit if broken
    sweep                                    delete cases past the retention window now
    make-statements [--out DIR]              write a pack of realistic synthetic test statements
"""
from __future__ import annotations

import argparse
import base64
import getpass
import os
import secrets
import sys

from app.domain.models import Role


def _password() -> str:
    from_env = os.environ.get("LEDGERLENS_PASSWORD")
    if from_env:
        return from_env
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        raise SystemExit("Passwords do not match.")
    return first


def _user_by_email(email: str):  # type: ignore[no-untyped-def]
    from app.domain.users import get_accounts, normalise_email

    user = get_accounts().repo.by_email(normalise_email(email))
    if user is None:
        raise SystemExit(f"No account for {email}.")
    return user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-secrets")
    sub.add_parser("init-db")
    create = sub.add_parser("create-user")
    create.add_argument("email")
    create.add_argument("name")
    create.add_argument("role", choices=[r.value for r in Role])
    for name in ("set-password", "disable-user", "enable-user"):
        sub.add_parser(name).add_argument("email")
    sub.add_parser("list-users")
    sub.add_parser("verify-audit")
    sub.add_parser("sweep")
    make = sub.add_parser("make-statements")
    make.add_argument("--out", default="var/samples")
    make.add_argument("--months", type=int, default=6)
    args = parser.parse_args(argv)

    if args.command == "make-statements":
        from pathlib import Path

        from app.synthetic.realistic import generate_pack

        pack = generate_pack(Path(args.out), months=args.months)
        for item in pack:
            print(f"{item['file']:40} {item['bank_layout']:12} {item['transactions']:>4} rows"
                  + (f"   password {item['password']}" if item["password"] else ""))
        print(f"\nWrote {len(pack)} statements, manifest.json and README.txt to {args.out}")
        return 0

    if args.command == "generate-secrets":
        print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
        print(f"RECEIPT_SIGNING_SEED={base64.b64encode(secrets.token_bytes(32)).decode()}")
        return 0

    from app.config import get_settings

    settings = get_settings()

    if args.command == "init-db":
        if not settings.database_url:
            raise SystemExit("Set DATABASE_URL first.")
        if settings.is_prod:
            raise SystemExit("In production, run `alembic upgrade head` instead.")
        from app.db import get_engine, metadata

        metadata.create_all(get_engine())
        print("Tables created.")
        return 0

    if not settings.database_url:
        raise SystemExit("Set DATABASE_URL: account and audit commands act on the shared database.")

    from app.domain.store import get_store
    from app.domain.users import get_accounts

    accounts = get_accounts()
    store = get_store()

    if args.command == "create-user":
        try:
            user = accounts.register(args.email, args.name, _password(), role=Role(args.role))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        store.audit("operator", "cli", "ACCOUNT_CREATED", user.uid, {"role": user.role.value})
        print(f"Created {user.role.value} {user.email} ({user.uid})")
    elif args.command == "set-password":
        user = _user_by_email(args.email)
        try:
            accounts.set_password(user.uid, _password())
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        store.audit("operator", "cli", "PASSWORD_RESET", user.uid, {})
        print(f"Password replaced for {user.email}; existing sessions revoked.")
    elif args.command in ("disable-user", "enable-user"):
        user = _user_by_email(args.email)
        active = args.command == "enable-user"
        accounts.set_active(user.uid, active)
        store.audit("operator", "cli", "ACCOUNT_ENABLED" if active else "ACCOUNT_DISABLED", user.uid, {})
        print(f"{user.email} {'enabled' if active else 'disabled'}.")
    elif args.command == "list-users":
        for user in accounts.repo.list():
            flags = ",".join(f for f, on in (("demo", user.is_demo), ("disabled", not user.active)) if on)
            print(f"{user.uid:24} {user.role.value:12} {user.email:40} {flags}")
    elif args.command == "verify-audit":
        head, count = store.audit_head()
        ok = store.verify_audit_chain()
        print(f"{'VALID' if ok else 'BROKEN'}  {count} events, head {head[:16]}")
        return 0 if ok else 2
    elif args.command == "sweep":
        removed = store.sweep_expired(settings.case_retention_days * 86400)
        print(f"Removed {removed} case(s) older than {settings.case_retention_days} days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
