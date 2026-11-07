import sys
from pathlib import Path

backend_path = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import SessionLocal
from app.models import User
from app.security import verify_password, hash_password
from sqlalchemy import select

db = SessionLocal()
for email, pw in [('25ag1a6767@aceec.ac.in', 'ace@25ag1a67676'), ('25ag1a6767@gmail.com', 'ace@25ag1a67676staff')]:
    u = db.scalar(select(User).where(User.email == email))
    if not u:
        print(f"User {email} NOT FOUND in db!")
    else:
        v = verify_password(pw, u.password_hash)
        print(f"User {email}: id={u.id}, role={u.role}, is_active={u.is_active}, password_valid={v}")
db.close()
