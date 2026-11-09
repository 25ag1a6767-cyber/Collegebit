from datetime import datetime, timezone
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import CanteenCounter, User, UserCounterAssignment, UserRole
from app.security import hash_password
from app.seed import seed_demo_data


ACCOUNTS = (
    ("e2e-student@example.com", "E2E Student", UserRole.STUDENT, "E2E-student-password-2026!"),
    ("e2e-staff@example.com", "E2E Staff", UserRole.STAFF, "E2E-staff-password-2026!"),
    ("e2e-admin@example.com", "E2E Admin", UserRole.ADMIN, "E2E-admin-password-2026!"),
)


Base.metadata.create_all(bind=engine)
with SessionLocal() as db:
    seed_demo_data(db)
    accounts = {}
    for email, name, role, password in ACCOUNTS:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(full_name=name, email=email, role=role, is_active=True)
            db.add(user)
        user.full_name = name
        user.role = role
        user.is_active = True
        user.password_hash = hash_password(password)
        user.email_verified_at = datetime.now(timezone.utc)
        accounts[role] = user

    db.flush()
    main_counter = db.scalar(select(CanteenCounter).where(CanteenCounter.name == "Main Counter"))
    staff = accounts[UserRole.STAFF]
    assignment = db.scalar(
        select(UserCounterAssignment).where(
            UserCounterAssignment.user_id == staff.id,
            UserCounterAssignment.counter_id == main_counter.id,
        )
    )
    if assignment is None:
        db.add(UserCounterAssignment(user_id=staff.id, counter_id=main_counter.id))
    db.commit()
