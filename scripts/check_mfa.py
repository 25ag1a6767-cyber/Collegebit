import sys
from pathlib import Path

backend_path = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import SessionLocal
from app.models import User
from sqlalchemy import select

db = SessionLocal()
for email in ['25ag1a6767@aceec.ac.in', '25ag1a6767@gmail.com']:
    u = db.scalar(select(User).where(User.email == email))
    if u:
        print(f"User {email}: mfa_enabled={u.mfa_enabled}, is_active={u.is_active}")
db.close()
