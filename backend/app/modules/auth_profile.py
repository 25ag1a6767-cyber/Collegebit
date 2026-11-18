"""/api/v1 Authentication, refresh-token rotation, TOTP MFA, email verification, profile & push."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import secrets
import time
from urllib.parse import quote, urlparse

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db
from app.emailer import send_email
from app.envelope import ApiError, ok
from app.models import (
    DevicePushToken,
    Notification,
    Order,
    PushSubscription,
    SecurityEvent,
    User,
    UserNotificationPreference,
    UserRole,
)
from app.modules.common import (
    commit_and_flush_outbox,
    get_or_create_notification_pref,
    record_audit,
    serialize_order_v1,
    serialize_user_v1,
)
from app.realtime import dispatch_external_alerts, normalize_e164_phone, stage_outbox_event
from app.security import (
    consume_recovery_code,
    create_access_token,
    create_email_verification_token,
    create_mfa_challenge_token,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    generate_recovery_codes,
    generate_totp_secret,
    get_current_user,
    hash_password,
    issue_refresh_session,
    read_email_verification_token,
    read_mfa_challenge_token,
    revoke_refresh_sessions,
    rotate_refresh_session,
    totp_provisioning_uri,
    verify_password,
    verify_totp_code,
)

router = APIRouter(tags=["v1-auth-profile"])

_PHONE_RE = re.compile(r"^\+?[0-9]{10,15}$")
_REFRESH_COOKIE_NAME = "cb_refresh_token"
_REFRESH_COOKIE_PATH = "/api/v1/auth"


def _cookie_is_secure(request: Request) -> bool:
    cfg = get_settings()
    return bool(
        cfg.environment.lower() in {"production", "prod"}
        or cfg.https_terminated
        or request.url.scheme == "https"
    )


def _set_refresh_cookie(response: Response, raw_token: str, request: Request) -> None:
    cfg = get_settings()
    response.set_cookie(
        key=_REFRESH_COOKIE_NAME,
        value=raw_token,
        max_age=cfg.refresh_token_days * 86400,
        path=_REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=_cookie_is_secure(request),
    )


def _clear_refresh_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        key=_REFRESH_COOKIE_NAME,
        path=_REFRESH_COOKIE_PATH,
        httponly=True,
        samesite="lax",
        secure=_cookie_is_secure(request),
    )


def _assert_same_origin_for_cookie(request: Request) -> None:
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    if not origin:
        return
    cfg = get_settings()
    allowed = {o.strip().rstrip("/") for o in cfg.cors_origins.split(",") if o.strip()}
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    req_scheme = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    if host:
        allowed.add(f"{req_scheme}://{host}")
    if cfg.public_base_url:
        parsed = urlparse(cfg.public_base_url)
        if parsed.scheme and parsed.netloc:
            allowed.add(f"{parsed.scheme}://{parsed.netloc}")
    if origin not in allowed:
        raise ApiError(403, "CSRF_ORIGIN_REJECTED", "Origin is not allowed for cookie session refresh.")


def _clean_phone(raw: str | None) -> str | None:
    if raw is None:
        return None
    cleaned = re.sub(r"[\s\-()]", "", raw.strip())
    if not cleaned:
        return None
    if not _PHONE_RE.match(cleaned):
        raise ApiError(422, "INVALID_PHONE", "Phone number must contain 10 to 15 digits.")
    return cleaned


class RegisterV1In(BaseModel):
    fullName: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=30)
    password: str = Field(min_length=12, max_length=128)
    dietaryPreference: str | None = Field(default=None, max_length=50)


class LoginV1In(BaseModel):
    identifier: str | None = Field(default=None, max_length=255, description="Email or phone")
    email: str | None = None
    password: str = Field(min_length=1, max_length=128)
    totpCode: str | None = Field(default=None, max_length=20)


class MfaVerifyLoginIn(BaseModel):
    mfaChallengeToken: str = Field(min_length=16, max_length=600)
    code: str = Field(min_length=6, max_length=20)


class MfaEnableIn(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class MfaDisableIn(BaseModel):
    password: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=6, max_length=20)


class RefreshIn(BaseModel):
    refreshToken: str | None = Field(default=None, min_length=20, max_length=300)


class LogoutIn(BaseModel):
    refreshToken: str | None = Field(default=None, max_length=300)
    allSessions: bool = False


class VerifyEmailIn(BaseModel):
    token: str = Field(min_length=16, max_length=600)


class ProfilePatchIn(BaseModel):
    fullName: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=30)
    dietaryPreference: str | None = Field(default=None, max_length=50)
    allergenPreferences: list[str] | None = None


class PushSubscribeIn(BaseModel):
    endpoint: str = Field(min_length=12, max_length=600)
    keys: dict[str, str]


class DevicePushRegisterIn(BaseModel):
    expoPushToken: str = Field(min_length=12, max_length=255)
    platform: str = Field(default="unknown", max_length=24)
    appVersion: str | None = Field(default=None, max_length=32)


@router.post("/auth/register", status_code=201)
def register_v1(
    payload: RegisterV1In,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    email = str(payload.email).strip().lower()
    full_name = payload.fullName.strip()
    if len(full_name) < 2:
        raise ApiError(422, "VALIDATION_ERROR", "Full name must contain at least two characters.")
    phone = _clean_phone(payload.phone)
    if db.scalar(select(User.id).where(func.lower(User.email) == email)) is not None:
        raise ApiError(409, "EMAIL_ALREADY_REGISTERED", "An account with this email already exists.")
    if phone and db.scalar(select(User.id).where(User.phone == phone)) is not None:
        raise ApiError(409, "PHONE_ALREADY_REGISTERED", "An account with this phone number already exists.")

    user = User(
        full_name=full_name,
        email=email,
        phone=phone,
        password_hash=hash_password(payload.password),
        role=UserRole.STUDENT,
        is_active=True,
        dietary_preference=(payload.dietaryPreference or "").strip()[:50] or None,
    )
    db.add(user)
    try:
        db.flush()
        record_audit(db, "user.registered", "user", user.id, user.id, {"role": "student"})
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "REGISTRATION_CONFLICT", "Account could not be created; email or phone is already registered.")
    except SQLAlchemyError:
        db.rollback()
        raise ApiError(503, "DATABASE_UNAVAILABLE", "Account registration is temporarily unavailable due to a database error.")

    cfg = get_settings()
    verify_token = create_email_verification_token(user)
    verify_url = f"{cfg.public_base_url.rstrip('/')}/customer/#verify-email={verify_token}"
    background.add_task(
        send_email,
        user.email,
        f"Verify your {cfg.app_name} email",
        f"Hello {user.full_name},\n\nConfirm your email address using this link:\n{verify_url}\n",
    )
    return ok(request, {"user": serialize_user_v1(db, user)}, status_code=201)


@router.post("/auth/login")
def login_v1(payload: LoginV1In, request: Request, db: Session = Depends(get_db)):
    from app.main import _check_login_rate, _clear_login_failures, _login_rate_key, _record_failed_login

    raw_ident = (payload.identifier or (str(payload.email) if payload.email else "")).strip()
    if not raw_ident:
        raise ApiError(422, "VALIDATION_ERROR", "Email or phone is required.")
    rate_key = _login_rate_key(request, raw_ident)
    retry_after = _check_login_rate(rate_key)
    if retry_after:
        db.add(SecurityEvent(event_type="auth.rate_limited", severity="high", ip_address=request.client.host if request.client else None, identifier=raw_ident[:120], path=request.url.path))
        db.commit()
        raise ApiError(429, "RATE_LIMITED", "Too many sign-in attempts. Try again later.", headers={"Retry-After": str(retry_after)})

    ident_lower = raw_ident.lower()
    if "@" in ident_lower:
        user = db.scalar(select(User).where(func.lower(User.email) == ident_lower))
    else:
        cleaned = re.sub(r"[\s\-()]", "", raw_ident)
        user = db.scalar(select(User).where(User.phone == cleaned))

    valid_pw = user is not None and verify_password(payload.password, user.password_hash)

    if user is None:
        _record_failed_login(rate_key)
        db.add(SecurityEvent(event_type="auth.login_failed", severity="warning", ip_address=request.client.host if request.client else None, identifier=raw_ident[:120], user_id=None, path=request.url.path))
        db.commit()
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid credentials.")
    if not user.is_active:
        _record_failed_login(rate_key)
        raise ApiError(403, "ACCOUNT_DISABLED", "This account has been deactivated.")
    if not valid_pw:
        _record_failed_login(rate_key)
        db.add(SecurityEvent(event_type="auth.login_failed", severity="warning", ip_address=request.client.host if request.client else None, identifier=raw_ident[:120], user_id=user.id, path=request.url.path))
        db.commit()
        raise ApiError(401, "INVALID_CREDENTIALS", "Invalid credentials.")
    cfg = get_settings()
    if cfg.email_verification_required and user.role == UserRole.STUDENT and user.email_verified_at is None:
        raise ApiError(403, "EMAIL_NOT_VERIFIED", "Please verify your email address before signing in.")

    if user.mfa_enabled:
        if not payload.totpCode:
            _clear_login_failures(rate_key)
            return ok(
                request,
                {
                    "mfaRequired": True,
                    "mfaChallengeToken": create_mfa_challenge_token(user),
                },
            )
        secret = decrypt_mfa_secret(user.mfa_secret_encrypted)
        totp_ok = bool(secret and verify_totp_code(secret, payload.totpCode))
        recovery_ok = False if totp_ok else consume_recovery_code(user, payload.totpCode)
        if not (totp_ok or recovery_ok):
            _record_failed_login(rate_key)
            raise ApiError(401, "INVALID_MFA_CODE", "Invalid authenticator or recovery code.")

    _clear_login_failures(rate_key)
    refresh_token, _ = issue_refresh_session(db, user, request)
    access_token = create_access_token(user, mfa_verified=True)
    record_audit(db, "user.login", "user", user.id, user.id, {"mfa": bool(user.mfa_enabled)})
    db.commit()
    resp = ok(
        request,
        {
            "mfaRequired": False,
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "tokenType": "Bearer",
            "expiresIn": cfg.access_token_minutes * 60,
            "user": serialize_user_v1(db, user),
        },
    )
    _set_refresh_cookie(resp, refresh_token, request)
    return resp


@router.post("/auth/mfa/verify-login")
def mfa_verify_login_v1(payload: MfaVerifyLoginIn, request: Request, db: Session = Depends(get_db)):
    from app.main import _check_login_rate, _clear_login_failures, _login_rate_key, _record_failed_login

    rate_key = "mfa:" + _login_rate_key(request, "challenge")
    retry_after = _check_login_rate(rate_key)
    if retry_after:
        raise ApiError(429, "RATE_LIMITED", "Too many MFA attempts.", headers={"Retry-After": str(retry_after)})

    user = read_mfa_challenge_token(payload.mfaChallengeToken, db)
    if user is None or not user.mfa_enabled:
        _record_failed_login(rate_key)
        raise ApiError(401, "INVALID_MFA_CHALLENGE", "MFA challenge has expired. Sign in again.")

    secret = decrypt_mfa_secret(user.mfa_secret_encrypted)
    totp_ok = bool(secret and verify_totp_code(secret, payload.code))
    recovery_ok = False if totp_ok else consume_recovery_code(user, payload.code)
    if not (totp_ok or recovery_ok):
        _record_failed_login(rate_key)
        raise ApiError(401, "INVALID_MFA_CODE", "Invalid authenticator or recovery code.")

    _clear_login_failures(rate_key)
    refresh_token, _ = issue_refresh_session(db, user, request)
    access_token = create_access_token(user, mfa_verified=True)
    record_audit(db, "user.mfa_verified", "user", user.id, user.id, {"used_recovery": recovery_ok})
    db.commit()
    resp = ok(
        request,
        {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "tokenType": "Bearer",
            "expiresIn": get_settings().access_token_minutes * 60,
            "user": serialize_user_v1(db, user),
        },
    )
    _set_refresh_cookie(resp, refresh_token, request)
    return resp


@router.post("/auth/mfa/setup")
def mfa_setup_v1(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.mfa_enabled:
        raise ApiError(409, "MFA_ALREADY_ENABLED", "MFA is already enabled on this account.")
    secret = generate_totp_secret()
    user.mfa_secret_encrypted = encrypt_mfa_secret(secret)
    db.commit()
    return ok(
        request,
        {
            "secret": secret,
            "otpauthUri": totp_provisioning_uri(user.email, secret),
            "issuer": get_settings().app_name,
        },
    )


@router.post("/auth/mfa/enable")
def mfa_enable_v1(payload: MfaEnableIn, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    secret = decrypt_mfa_secret(user.mfa_secret_encrypted)
    if not secret:
        raise ApiError(400, "MFA_NOT_INITIALIZED", "Run /auth/mfa/setup first.")
    if not verify_totp_code(secret, payload.code):
        raise ApiError(400, "INVALID_MFA_CODE", "Verification code did not match. Check your authenticator clock.")
    recovery_codes, hashes_json = generate_recovery_codes()
    user.mfa_enabled = True
    user.mfa_recovery_hashes_json = hashes_json
    user.token_version += 1
    record_audit(db, "user.mfa_enabled", "user", user.id, user.id, {})
    refresh_token, _ = issue_refresh_session(db, user, request)
    access_token = create_access_token(user, mfa_verified=True)
    db.commit()
    return ok(
        request,
        {
            "mfaEnabled": True,
            "recoveryCodes": recovery_codes,
            "accessToken": access_token,
            "refreshToken": refresh_token,
        },
    )


@router.post("/auth/mfa/disable")
def mfa_disable_v1(payload: MfaDisableIn, request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.mfa_enabled:
        raise ApiError(409, "MFA_NOT_ENABLED", "MFA is not enabled.")
    if not verify_password(payload.password, user.password_hash):
        raise ApiError(401, "INVALID_PASSWORD", "Password is incorrect.")
    secret = decrypt_mfa_secret(user.mfa_secret_encrypted)
    if not ((secret and verify_totp_code(secret, payload.code)) or consume_recovery_code(user, payload.code)):
        raise ApiError(401, "INVALID_MFA_CODE", "Invalid authenticator or recovery code.")
    user.mfa_enabled = False
    user.mfa_secret_encrypted = None
    user.mfa_recovery_hashes_json = None
    user.token_version += 1
    record_audit(db, "user.mfa_disabled", "user", user.id, user.id, {})
    db.commit()
    return ok(request, {"mfaEnabled": False})


@router.post("/auth/refresh")
def refresh_credentials_v1(request: Request, payload: RefreshIn = Body(default_factory=RefreshIn), db: Session = Depends(get_db)):
    raw_token = payload.refreshToken
    if not raw_token:
        _assert_same_origin_for_cookie(request)
        raw_token = (request.cookies.get(_REFRESH_COOKIE_NAME) or "").strip()
    if len(raw_token) < 20:
        raise ApiError(401, "INVALID_REFRESH_TOKEN", "Refresh token is required.")
    user, new_refresh = rotate_refresh_session(db, raw_token, request)
    access_token = create_access_token(user, mfa_verified=True)
    db.commit()
    resp = ok(
        request,
        {
            "accessToken": access_token,
            "refreshToken": new_refresh,
            "tokenType": "Bearer",
            "expiresIn": get_settings().access_token_minutes * 60,
            "user": serialize_user_v1(db, user),
        },
    )
    _set_refresh_cookie(resp, new_refresh, request)
    return resp


@router.post("/auth/logout")
def logout_v1(
    request: Request,
    payload: LogoutIn = Body(default_factory=LogoutIn),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cookie_token = (request.cookies.get(_REFRESH_COOKIE_NAME) or "").strip() or None
    target_token = payload.refreshToken or cookie_token
    if payload.allSessions:
        revoked = revoke_refresh_sessions(db, user_id=user.id)
        user.token_version += 1
    elif target_token:
        revoked = revoke_refresh_sessions(db, raw_token=target_token)
    else:
        revoked = revoke_refresh_sessions(db, user_id=user.id)
        user.token_version += 1
    record_audit(db, "user.logout", "user", user.id, user.id, {"all_sessions": payload.allSessions, "revoked": revoked})
    db.commit()
    resp = ok(request, {"revokedSessions": revoked})
    _clear_refresh_cookie(resp, request)
    return resp


@router.post("/auth/send-verification")
def send_verification_v1(
    request: Request,
    background: BackgroundTasks,
    user: User = Depends(get_current_user),
):
    if user.email_verified_at is not None:
        return ok(request, {"alreadyVerified": True})
    cfg = get_settings()
    token = create_email_verification_token(user)
    verify_url = f"{cfg.public_base_url.rstrip('/')}/customer/#verify-email={token}"
    background.add_task(
        send_email,
        user.email,
        f"Verify your {cfg.app_name} email",
        f"Hello {user.full_name},\n\nConfirm your email address:\n{verify_url}\n",
    )
    return ok(request, {"sent": True})


@router.post("/auth/verify-email")
def verify_email_v1(payload: VerifyEmailIn, request: Request, db: Session = Depends(get_db)):
    user = read_email_verification_token(payload.token, db)
    if user is None:
        raise ApiError(400, "INVALID_VERIFICATION_TOKEN", "Verification link is invalid or has expired.")
    if user.email_verified_at is None:
        user.email_verified_at = datetime.now(timezone.utc)
        record_audit(db, "user.email_verified", "user", user.id, user.id, {})
        db.commit()
    return ok(request, {"emailVerified": True})


@router.get("/users/me")
@router.get("/me")
def get_me_v1(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return ok(request, serialize_user_v1(db, user))


@router.patch("/users/me")
def update_me_v1(
    payload: ProfilePatchIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    updates = payload.model_dump(exclude_unset=True)
    if "fullName" in updates and updates["fullName"] is not None:
        name = updates["fullName"].strip()
        if len(name) < 2:
            raise ApiError(422, "VALIDATION_ERROR", "Full name must have at least two characters.")
        user.full_name = name
    if "phone" in updates:
        phone = _clean_phone(updates["phone"])
        if phone and db.scalar(select(User.id).where(User.phone == phone, User.id != user.id)) is not None:
            raise ApiError(409, "PHONE_ALREADY_REGISTERED", "Phone number is already linked to another account.")
        user.phone = phone
    if "dietaryPreference" in updates:
        pref = (updates["dietaryPreference"] or "").strip()[:50] or None
        user.dietary_preference = pref
    if "allergenPreferences" in updates:
        raw_list = updates["allergenPreferences"] or []
        cleaned = list(dict.fromkeys(str(item).strip().lower()[:40] for item in raw_list if str(item).strip()))[:20]
        user.allergen_preferences_json = json.dumps(cleaned, separators=(",", ":")) if cleaned else None
    record_audit(db, "user.profile_updated", "user", user.id, user.id, {"fields": sorted(updates.keys())})
    db.commit()
    db.refresh(user)
    return ok(request, serialize_user_v1(db, user))


class ChangePasswordV1In(BaseModel):
    currentPassword: str = Field(min_length=1, max_length=128)
    newPassword: str = Field(min_length=12, max_length=128)


@router.post("/users/me/change-password")
def change_password_v1(
    payload: ChangePasswordV1In,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.currentPassword, user.password_hash):
        raise ApiError(401, "INVALID_CURRENT_PASSWORD", "Current password is incorrect.")
    if payload.currentPassword == payload.newPassword:
        raise ApiError(422, "SAME_PASSWORD", "New password must be different from the current password.")
    user.password_hash = hash_password(payload.newPassword)
    user.must_change_password = False
    user.token_version += 1
    revoke_refresh_sessions(db, user_id=user.id)
    refresh_token, _ = issue_refresh_session(db, user, request)
    access_token = create_access_token(user, mfa_verified=True)
    record_audit(db, "user.password_changed", "user", user.id, user.id, {})
    db.commit()
    return ok(
        request,
        {
            "changed": True,
            "accessToken": access_token,
            "refreshToken": refresh_token,
        },
    )


@router.get("/users/me/orders")
def my_orders_v1(
    request: Request,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lim = max(1, min(limit, 100))
    off = max(0, offset)
    rows = db.scalars(
        select(Order)
        .where(Order.user_id == user.id)
        .options(joinedload(Order.items), joinedload(Order.status_history))
        .order_by(Order.created_at.desc(), Order.id.desc())
        .offset(off)
        .limit(lim)
    ).unique().all()
    return ok(request, {"items": [serialize_order_v1(o, include_history=True) for o in rows], "limit": lim, "offset": off})


@router.get("/users/me/notifications")
def my_notifications_v1(
    request: Request,
    unreadOnly: bool = False,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    lim = max(1, min(limit, 100))
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unreadOnly:
        stmt = stmt.where(Notification.is_read.is_(False))
    rows = db.scalars(stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(lim)).all()
    unread_count = db.scalar(select(func.count(Notification.id)).where(Notification.user_id == user.id, Notification.is_read.is_(False))) or 0
    return ok(
        request,
        {
            "unreadCount": int(unread_count),
            "items": [
                {
                    "id": n.id,
                    "type": n.notification_type,
                    "title": n.title,
                    "body": n.message,
                    "message": n.message,
                    "entityType": n.entity_type,
                    "entityId": n.entity_id,
                    "isRead": n.is_read,
                    "readAt": n.read_at,
                    "createdAt": n.created_at,
                }
                for n in rows
            ],
        },
    )


@router.patch("/users/me/notifications/{notification_id}/read")
def mark_notification_read_v1(
    notification_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    notif = db.scalar(select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id))
    if notif is None:
        raise ApiError(404, "NOT_FOUND", "Notification not found.")
    if not notif.is_read:
        notif.is_read = True
        notif.read_at = datetime.now(timezone.utc)
        db.commit()
    return ok(request, {"id": notif.id, "isRead": True})


@router.post("/users/me/notifications/read-all")
def mark_all_notifications_read_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    rows = db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.is_read.is_(False))).all()
    for n in rows:
        n.is_read = True
        n.read_at = now
    db.commit()
    return ok(request, {"markedRead": len(rows)})


@router.get("/push/vapid-public-key")
def vapid_public_key_v1(request: Request):
    cfg = get_settings()
    return ok(request, {"enabled": bool(cfg.vapid_public_key and cfg.vapid_private_key), "publicKey": cfg.vapid_public_key or None})


@router.post("/push/subscribe", status_code=201)
def push_subscribe_v1(
    payload: PushSubscribeIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p256dh = (payload.keys.get("p256dh") or "").strip()
    auth = (payload.keys.get("auth") or "").strip()
    if not p256dh or not auth:
        raise ApiError(422, "VALIDATION_ERROR", "Push subscription keys p256dh and auth are required.")
    existing = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == payload.endpoint.strip()))
    if existing:
        existing.user_id = user.id
        existing.p256dh = p256dh[:200]
        existing.auth = auth[:100]
        existing.user_agent = request.headers.get("user-agent", "")[:255]
    else:
        db.add(
            PushSubscription(
                user_id=user.id,
                endpoint=payload.endpoint.strip(),
                p256dh=p256dh[:200],
                auth=auth[:100],
                user_agent=request.headers.get("user-agent", "")[:255],
            )
        )
    db.commit()
    return ok(request, {"subscribed": True}, status_code=201)


@router.delete("/push/subscribe")
def push_unsubscribe_v1(
    endpoint: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sub = db.scalar(select(PushSubscription).where(PushSubscription.endpoint == endpoint, PushSubscription.user_id == user.id))
    if sub:
        db.delete(sub)
        db.commit()
    return ok(request, {"subscribed": False})


@router.post("/push/device/register", status_code=201)
def register_mobile_device_push_v1(
    payload: DevicePushRegisterIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tok = payload.expoPushToken.strip()
    if not tok.startswith(("ExponentPushToken[", "ExpoPushToken[")) or not tok.endswith("]"):
        raise ApiError(422, "VALIDATION_ERROR", "Invalid Expo push token format.")
    platform_clean = (payload.platform or "unknown").strip().lower()[:24] or "unknown"
    existing = db.scalar(select(DevicePushToken).where(DevicePushToken.expo_push_token == tok))
    if existing:
        existing.user_id = user.id
        existing.platform = platform_clean
        existing.app_version = (payload.appVersion or "").strip()[:32] or None
    else:
        db.add(
            DevicePushToken(
                user_id=user.id,
                expo_push_token=tok,
                platform=platform_clean,
                app_version=(payload.appVersion or "").strip()[:32] or None,
            )
        )
    db.commit()
    return ok(request, {"registered": True, "platform": platform_clean}, status_code=201)


@router.delete("/push/device/register")
def unregister_mobile_device_push_v1(
    expoPushToken: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tok = expoPushToken.strip()
    row = db.scalar(select(DevicePushToken).where(DevicePushToken.expo_push_token == tok, DevicePushToken.user_id == user.id))
    if row:
        db.delete(row)
        db.commit()
    return ok(request, {"registered": False})


_TEST_ALERT_LAST: dict[int, float] = {}


def _get_or_create_pref(db: Session, user_id: int) -> UserNotificationPreference:
    return get_or_create_notification_pref(db, user_id)


def _ntfy_urls(ntfy_base_url: str, topic: str | None) -> tuple[str | None, str | None]:
    if not topic:
        return (None, None)
    base = (ntfy_base_url or "https://ntfy.sh").strip().rstrip("/")
    parsed = urlparse(base)
    host = parsed.netloc
    return (f"ntfy://{host}/{topic}" if host else None, f"{base}/{topic}" if base else None)


@router.get("/push/free-channels")
def free_notification_channels_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    pref = _get_or_create_pref(db, user.id)
    db.commit()
    sub_url, https_url = _ntfy_urls(cfg.ntfy_base_url, pref.ntfy_topic)
    bot_user = (cfg.telegram_bot_username or "").strip().lstrip("@")
    _, canteen_wa_digits = normalize_e164_phone(cfg.canteen_whatsapp_number, cfg.canteen_default_country_code)
    wa_chat_url = (
        f"https://wa.me/{canteen_wa_digits}?text={quote('Hi CollegeBits, please enable my free WhatsApp order alerts.')}"
        if canteen_wa_digits
        else None
    )
    return ok(
        request,
        {
            "webPush": {"configured": bool(cfg.vapid_public_key and cfg.vapid_private_key)},
            "expoPush": {"enabled": bool(cfg.expo_push_enabled)},
            "ntfy": {
                "enabled": bool(cfg.ntfy_enabled),
                "connected": bool(pref.ntfy_topic),
                "topic": pref.ntfy_topic,
                "subscribeUrl": sub_url,
                "httpsUrl": https_url,
            },
            "telegram": {
                "enabled": bool(cfg.telegram_enabled and cfg.telegram_bot_token and bot_user),
                "connected": bool(pref.telegram_chat_id),
                "botUsername": bot_user or None,
                "startUrl": f"https://t.me/{bot_user}?start={pref.telegram_link_token}" if (bot_user and pref.telegram_link_token) else None,
            },
            "whatsapp": {
                "canteenNumber": canteen_wa_digits or None,
                "canteenChatUrl": wa_chat_url,
                "cloudApiEnabled": bool(cfg.whatsapp_cloud_enabled and cfg.whatsapp_phone_number_id and cfg.whatsapp_access_token),
                "optedIn": bool(pref.whatsapp_enabled),
            },
            "sms": {
                "webhookEnabled": bool(cfg.sms_enabled and cfg.sms_webhook_url),
                "preset": (cfg.sms_webhook_preset or "generic").strip().lower(),
                "optedIn": bool(pref.sms_enabled),
            },
        },
    )


@router.post("/push/ntfy/enable")
def enable_ntfy_push_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    if not cfg.ntfy_enabled:
        raise ApiError(409, "NTFY_DISABLED", "ntfy push notifications are disabled on this server.")
    pref = _get_or_create_pref(db, user.id)
    if not pref.ntfy_topic:
        pref.ntfy_topic = f"cb-{secrets.token_hex(12)}"
    db.commit()
    sub_url, https_url = _ntfy_urls(cfg.ntfy_base_url, pref.ntfy_topic)
    return ok(
        request,
        {"connected": True, "topic": pref.ntfy_topic, "subscribeUrl": sub_url, "httpsUrl": https_url},
    )


@router.delete("/push/ntfy")
def disable_ntfy_push_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pref = _get_or_create_pref(db, user.id)
    pref.ntfy_topic = None
    db.commit()
    return ok(request, {"connected": False})


@router.post("/push/telegram/link-start")
def start_telegram_link_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    bot_user = (cfg.telegram_bot_username or "").strip().lstrip("@")
    if not (cfg.telegram_enabled and cfg.telegram_bot_token.strip() and bot_user):
        raise ApiError(409, "TELEGRAM_NOT_CONFIGURED", "Telegram Bot alerts are not configured on this server yet.")
    pref = _get_or_create_pref(db, user.id)
    pref.telegram_link_token = f"cb{secrets.token_hex(8)}"
    db.commit()
    return ok(
        request,
        {"botUsername": bot_user, "startUrl": f"https://t.me/{bot_user}?start={pref.telegram_link_token}"},
    )


@router.post("/push/telegram/verify")
def verify_telegram_link_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    token = (cfg.telegram_bot_token or "").strip()
    if not (cfg.telegram_enabled and token):
        raise ApiError(409, "TELEGRAM_NOT_CONFIGURED", "Telegram Bot alerts are not configured on this server yet.")
    pref = _get_or_create_pref(db, user.id)
    if not pref.telegram_link_token:
        raise ApiError(400, "TELEGRAM_LINK_NOT_STARTED", "Generate a Telegram connect link first.")

    import httpx
    try:
        resp = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"limit": 100}, timeout=5.0)
        updates = (resp.json() or {}).get("result") or [] if resp.status_code == 200 else []
    except Exception:
        raise ApiError(502, "TELEGRAM_UNREACHABLE", "Could not reach Telegram Bot API right now.")

    matched_chat_id: str | None = None
    expected_code = pref.telegram_link_token
    for upd in reversed(updates):
        msg = upd.get("message") or upd.get("edited_message") or {}
        text = str(msg.get("text") or "")
        chat = msg.get("chat") or {}
        if expected_code in text and chat.get("id") is not None:
            matched_chat_id = str(chat["id"])
            break

    if not matched_chat_id:
        raise ApiError(409, "TELEGRAM_LINK_PENDING", "Open the Telegram link and tap Start first, then tap Verify again.")

    pref.telegram_chat_id = matched_chat_id
    pref.telegram_link_token = None
    db.commit()
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": matched_chat_id, "text": "✅ CollegeBits linked! You will receive free order status updates here."},
            timeout=3.0,
        )
    except Exception:
        pass
    return ok(request, {"connected": True})


@router.delete("/push/telegram")
def disable_telegram_link_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pref = _get_or_create_pref(db, user.id)
    pref.telegram_chat_id = None
    pref.telegram_link_token = None
    db.commit()
    return ok(request, {"connected": False})


@router.post("/users/me/notifications/test")
def send_test_notification_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    now_mono = time.monotonic()
    last = _TEST_ALERT_LAST.get(user.id, 0.0)
    if now_mono - last < 3.0:
        raise ApiError(429, "RATE_LIMITED", "Please wait a few seconds before sending another test alert.")
    _TEST_ALERT_LAST[user.id] = now_mono

    title = "CollegeBits Test Alert"
    message = "Free order notifications are active for your account."
    notif = Notification(
        user_id=user.id,
        notification_type="order_status",
        title=title,
        message=message,
        entity_type="test",
        entity_id=str(user.id),
    )
    db.add(notif)
    stage_outbox_event(
        db,
        "notification.created",
        {"type": "order_status", "title": title, "message": message, "entityType": "test", "entityId": str(user.id)},
        target_user_id=user.id,
    )
    commit_and_flush_outbox(db)

    delivery = dispatch_external_alerts(
        db,
        user.id,
        title,
        message,
        notification_type="order_status",
        payload_data={"entityType": "test", "entityId": str(user.id), "url": "/customer/#my-orders-section", "tag": f"test-{user.id}"},
    )
    db.commit()
    return ok(request, {"inAppCreated": True, "delivery": delivery})
