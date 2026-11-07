"""Password hashing, signed bearer tokens, rotating refresh tokens, TOTP MFA and counter RBAC."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models import RefreshSession, User, UserCounterAssignment, UserRole

bearer = HTTPBearer(auto_error=False)

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"

def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, salt_hex, digest_hex = encoded.split("$", 2)
        if algorithm != "pbkdf2_sha256":
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 310_000)
        return hmac.compare_digest(candidate.hex(), digest_hex)
    except (ValueError, TypeError):
        return False

def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="ace-canteen-auth-v1")

def create_access_token(user: User, *, mfa_verified: bool = False) -> str:
    now = datetime.now(timezone.utc)
    return _serializer().dumps({
        "sub": str(user.id),
        "role": user.role.value,
        "ver": user.token_version,
        "mfa": bool(mfa_verified or not user.mfa_enabled),
        "iat": int(now.timestamp()),
    })

def decode_access_token(raw_token: str, db: Session) -> tuple[User | None, dict | None]:
    try:
        payload = _serializer().loads(raw_token, max_age=get_settings().access_token_minutes * 60)
        if not isinstance(payload, dict):
            return None, None
        user_id = int(payload.get("sub", ""))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None, None
    user = db.get(User, user_id)
    if user is None or not user.is_active or payload.get("ver", 0) != user.token_version:
        return None, None
    return user, payload

def _reset_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="ace-canteen-password-reset-v1")

def _email_verify_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="ace-canteen-email-verify-v1")

def _mfa_challenge_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt="ace-canteen-mfa-challenge-v1")

def _password_fingerprint(user: User) -> str:
    return hashlib.sha256(user.password_hash.encode()).hexdigest()[:24]

def create_password_reset_token(user: User) -> str:
    return _reset_serializer().dumps({"sub": str(user.id), "fp": _password_fingerprint(user)})

def read_password_reset_token(token: str, db: Session) -> User | None:
    try:
        payload = _reset_serializer().loads(token, max_age=get_settings().password_reset_minutes * 60)
        user = db.get(User, int(payload.get("sub", "")))
    except (BadSignature, SignatureExpired, ValueError, TypeError, AttributeError):
        return None
    if user is None or not user.is_active or not hmac.compare_digest(payload.get("fp", ""), _password_fingerprint(user)):
        return None
    return user

def create_email_verification_token(user: User) -> str:
    return _email_verify_serializer().dumps({"sub": str(user.id), "email": user.email.lower()})

def read_email_verification_token(token: str, db: Session, max_age_seconds: int = 86400 * 3) -> User | None:
    try:
        payload = _email_verify_serializer().loads(token, max_age=max_age_seconds)
        user = db.get(User, int(payload.get("sub", "")))
    except (BadSignature, SignatureExpired, ValueError, TypeError, AttributeError):
        return None
    if user is None or not user.is_active or user.email.lower() != str(payload.get("email", "")).lower():
        return None
    return user

def create_mfa_challenge_token(user: User) -> str:
    return _mfa_challenge_serializer().dumps({"sub": str(user.id), "ver": user.token_version})

def read_mfa_challenge_token(token: str, db: Session, max_age_seconds: int = 300) -> User | None:
    try:
        payload = _mfa_challenge_serializer().loads(token, max_age=max_age_seconds)
        user = db.get(User, int(payload.get("sub", "")))
    except (BadSignature, SignatureExpired, ValueError, TypeError, AttributeError):
        return None
    if user is None or not user.is_active or payload.get("ver", -1) != user.token_version:
        return None
    return user

def _fernet() -> Fernet:
    key_material = hashlib.sha256(f"ace-mfa-fernet:{get_settings().secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key_material))

def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")

def encrypt_mfa_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")

def decrypt_mfa_secret(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("ascii")
    except (InvalidToken, ValueError):
        return None

def totp_code_at(secret: str, timestamp: int | None = None, step: int = 30, digits: int = 6) -> str:
    normalized = secret.strip().replace(" ", "").upper()
    pad = "=" * ((8 - len(normalized) % 8) % 8)
    key = base64.b32decode(normalized + pad, casefold=True)
    counter = int((time.time() if timestamp is None else timestamp) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code_int).zfill(digits)

def verify_totp_code(secret: str, supplied_code: str, *, window: int = 1, now_ts: int | None = None) -> bool:
    cleaned = (supplied_code or "").strip().replace(" ", "")
    if not cleaned.isdigit() or len(cleaned) != 6:
        return False
    base_ts = int(time.time() if now_ts is None else now_ts)
    for delta in range(-window, window + 1):
        if hmac.compare_digest(totp_code_at(secret, base_ts + delta * 30), cleaned):
            return True
    return False

def totp_provisioning_uri(email: str, secret: str, issuer: str | None = None) -> str:
    iss = issuer or get_settings().app_name
    label = quote(f"{iss}:{email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(iss)}&algorithm=SHA1&digits=6&period=30"

def generate_recovery_codes(count: int = 8) -> tuple[list[str], str]:
    plain = [f"{secrets.token_hex(2).upper()}-{secrets.token_hex(2).upper()}" for _ in range(count)]
    hashes = [hash_password(code) for code in plain]
    return plain, json.dumps(hashes)

def consume_recovery_code(user: User, supplied: str) -> bool:
    if not user.mfa_recovery_hashes_json:
        return False
    cleaned = (supplied or "").strip().upper()
    try:
        hashes: list[str] = json.loads(user.mfa_recovery_hashes_json)
    except ValueError:
        return False
    for idx, encoded in enumerate(hashes):
        if verify_password(cleaned, encoded):
            remaining = hashes[:idx] + hashes[idx + 1:]
            user.mfa_recovery_hashes_json = json.dumps(remaining)
            return True
    return False

def hash_opaque_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

def issue_refresh_session(
    db: Session,
    user: User,
    request: Request | None = None,
    *,
    family_id: str | None = None,
) -> tuple[str, RefreshSession]:
    raw_token = f"rt_{secrets.token_urlsafe(36)}"
    token_hash = hash_opaque_token(raw_token)
    fam = family_id or secrets.token_hex(16)
    expires = datetime.now(timezone.utc) + timedelta(days=get_settings().refresh_token_days)
    ua = request.headers.get("user-agent", "")[:255] if request else None
    ip = (request.client.host if request and request.client else None)
    session = RefreshSession(
        user_id=user.id,
        token_hash=token_hash,
        family_id=fam,
        expires_at=expires,
        user_agent=ua,
        ip_address=ip,
    )
    db.add(session)
    return raw_token, session

def rotate_refresh_session(db: Session, raw_token: str, request: Request | None = None) -> tuple[User, str]:
    token_hash = hash_opaque_token((raw_token or "").strip())
    session = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == token_hash))
    now = datetime.now(timezone.utc)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "INVALID_REFRESH_TOKEN", "message": "Refresh token is invalid."})

    if session.revoked_at is not None:
        if session.replaced_by_hash is not None:
            family_rows = db.scalars(select(RefreshSession).where(RefreshSession.family_id == session.family_id, RefreshSession.revoked_at.is_(None))).all()
            for row in family_rows:
                row.revoked_at = now
            db.commit()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"code": "REFRESH_TOKEN_REPLAYED", "message": "Rotated refresh token reuse detected; session family revoked."},
            )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "REFRESH_TOKEN_REVOKED", "message": "Refresh session was revoked. Sign in again."})
    exp = session.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp <= now:
        session.revoked_at = now
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "REFRESH_TOKEN_EXPIRED", "message": "Refresh token has expired."})
    user = db.get(User, session.user_id)
    if user is None or not user.is_active:
        session.revoked_at = now
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail={"code": "ACCOUNT_DISABLED", "message": "Account is not active."})
    new_raw, new_session = issue_refresh_session(db, user, request, family_id=session.family_id)
    session.revoked_at = now
    session.replaced_by_hash = new_session.token_hash
    return user, new_raw

def revoke_refresh_sessions(db: Session, *, raw_token: str | None = None, user_id: int | None = None) -> int:
    now = datetime.now(timezone.utc)
    revoked = 0
    if raw_token:
        th = hash_opaque_token(raw_token.strip())
        row = db.scalar(select(RefreshSession).where(RefreshSession.token_hash == th, RefreshSession.revoked_at.is_(None)))
        if row is not None:
            row.revoked_at = now
            revoked += 1
    elif user_id is not None:
        rows = db.scalars(select(RefreshSession).where(RefreshSession.user_id == user_id, RefreshSession.revoked_at.is_(None))).all()
        for r in rows:
            r.revoked_at = now
            revoked += 1
    return revoked

def get_current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    unauthorized = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Valid bearer token required", headers={"WWW-Authenticate": "Bearer"})
    if credentials is None:
        raise unauthorized
    user, payload = decode_access_token(credentials.credentials, db)
    if user is None or payload is None:
        raise unauthorized
    if user.mfa_enabled and not payload.get("mfa", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "MFA_REQUIRED", "message": "Multi-factor verification is required for this account."})
    return user

def get_optional_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User | None:
    if credentials is None:
        return None
    user, _ = decode_access_token(credentials.credentials, db)
    return user

def require_roles(*roles: str):
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role.value not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to perform this action")
        if user.must_change_password:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Password change required: change your temporary password before using staff or admin tools")
        if user.role == UserRole.ADMIN and get_settings().admin_require_mfa and not user.mfa_enabled:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "ADMIN_MFA_ENROLLMENT_REQUIRED", "message": "Administrators must enable MFA before performing administrative actions."})
        return user
    return dependency

def get_staff_assigned_counter_ids(db: Session, user: User) -> set[int] | None:
    """Return None when the user has global counter visibility (ADMIN), else assigned counter IDs."""
    if user.role == UserRole.ADMIN:
        return None
    rows = db.scalars(select(UserCounterAssignment.counter_id).where(UserCounterAssignment.user_id == user.id)).all()
    return {int(cid) for cid in rows}

def assert_counter_access(db: Session, user: User, counter_id: int | None, *, require_explicit_when_assigned: bool = True) -> None:
    """Enforce that a staff member only operates counters assigned to them."""
    if user.role == UserRole.ADMIN:
        return
    assigned = get_staff_assigned_counter_ids(db, user)
    if assigned is None:
        return

    if assigned:
        if counter_id is None and require_explicit_when_assigned:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "COUNTER_FORBIDDEN", "message": "You can only operate orders assigned to your counter."})
        if counter_id is not None and counter_id not in assigned:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code": "COUNTER_FORBIDDEN", "message": "You are not assigned to this canteen counter."})
