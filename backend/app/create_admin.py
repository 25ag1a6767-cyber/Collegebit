"""Create or promote an administrator account from the command line.
Usage: python -m app.create_admin --email admin@college.edu --name "Canteen Admin"
The password is prompted securely via TTY when interactive, or read from stdin when piped/--password-stdin.
"""
import argparse
from getpass import getpass
import sys
from sqlalchemy import select
from app.database import Base, SessionLocal, engine
from app.models import User, UserRole
from app.security import hash_password


def upsert_admin(*, email: str, name: str, password: str) -> User:
    if len(password) < 12:
        raise SystemExit("Password must contain at least 12 characters.")
    # Never create tables outside Alembic on a real database; only bootstrap SQLite for local development.
    if engine.dialect.name == "sqlite":
        Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        clean_email = email.strip().lower()
        clean_name = name.strip()
        user = db.scalar(select(User).where(User.email == clean_email))
        if user:
            user.full_name = clean_name
            user.password_hash = hash_password(password)
            user.role = UserRole.ADMIN
            user.is_active = True
            user.must_change_password = False
            user.token_version += 1
        else:
            user = User(
                full_name=clean_name,
                email=clean_email,
                password_hash=hash_password(password),
                role=UserRole.ADMIN,
                is_active=True,
                must_change_password=False,
            )
            db.add(user)
        db.commit()
        db.refresh(user)
        return user


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an ACE Canteen administrator")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--password-stdin", action="store_true", help="Read password from standard input instead of interactive prompt")
    args = parser.parse_args(argv)
    if args.password_stdin or not sys.stdin.isatty():
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass("Admin password (minimum 12 characters): ")
    user = upsert_admin(email=args.email, name=args.name, password=password)
    print(f"Administrator account ready: {user.email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
