"""CollegeBits — Multi-College SaaS Admin Account, Credential, Invitation, Ownership Transfer,
Platform Super-Admin Separation, Onboarding Validation, Subscription Management & Non-Overwriting CSV Importers.
"""
from __future__ import annotations

import base64
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import io
import json
import re
import secrets
import uuid
from typing import Any

from cryptography.fernet import Fernet
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.envelope import ApiError, ok
from app.models import (
    AdminInvitation,
    AuditEvent,
    CanteenCounter,
    Category,
    EmailChangeRequest,
    Ingredient,
    OwnershipTransferRequest,
    PasswordResetTokenRecord,
    PlatformSuperAdmin,
    Product,
    RefreshSession,
    SaasSubscription,
    SaasTenant,
    SaasTenantMember,
    SaasTenantProfile,
    User,
    UserRole,
)
from app.modules.common import as_utc, record_audit
from app.modules.saas_crowd_pos_v3 import PLAN_ENTITLEMENTS
from app.security import create_access_token, get_current_user, hash_password, require_roles, verify_password

router = APIRouter(tags=["v3-saas-admin-governance"])

COLLEGE_ROLES = {
    "college_owner",
    "college_admin",
    "finance_admin",
    "canteen_manager",
    "kitchen_manager",
    "staff_admin",
    "read_only_auditor",
}
ADMIN_MANAGEMENT_ROLES = {"college_owner", "college_admin"}


def _sha256(val: str) -> str:
    return hashlib.sha256(val.strip().encode("utf-8")).hexdigest()


def _fernet() -> Fernet:
    raw = hashlib.sha256(get_settings().secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def _validate_strong_password(pw: str) -> None:
    if (
        len(pw) < 10
        or not any(c.isupper() for c in pw)
        or not any(c.islower() for c in pw)
        or not any(c.isdigit() for c in pw)
    ):
        raise ApiError(
            422,
            "WEAK_PASSWORD",
            "Password must be at least 10 characters and include uppercase, lowercase, and numeric characters.",
        )


def require_platform_super_admin(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> User:
    """Strictly separates Platform Super Admins from College Tenant Administrators."""
    explicit_super = db.scalar(
        select(PlatformSuperAdmin).where(PlatformSuperAdmin.user_id == user.id, PlatformSuperAdmin.is_active.is_(True))
    )
    if explicit_super is not None:
        return user

    # Any user bound to a college tenant in SaasTenantMember (without explicit PlatformSuperAdmin) is strictly blocked
    is_tenant_member = db.scalar(
        select(SaasTenantMember.id).where(SaasTenantMember.user_id == user.id)
    )
    if is_tenant_member is not None or user.role != UserRole.ADMIN:
        raise ApiError(
            403,
            "PLATFORM_SUPER_ADMIN_REQUIRED",
            "Platform Super Admin privilege required. Ordinary college administrators cannot access platform controls.",
        )
    return user


def _assert_tenant_member_role(
    db: Session,
    tenant_id: int,
    user: User,
    allowed_roles: set[str] | None = None,
    x_tenant_id: int | None = None,
) -> tuple[SaasTenant, SaasTenantMember]:
    if x_tenant_id is not None and x_tenant_id != tenant_id:
        raise ApiError(403, "TENANT_ISOLATION_VIOLATION", "Forged X-Tenant-ID header rejected.")
    tenant = db.get(SaasTenant, tenant_id)
    if not tenant:
        raise ApiError(404, "TENANT_NOT_FOUND", "College tenant not found.")
    member = db.scalar(
        select(SaasTenantMember).where(
            SaasTenantMember.tenant_id == tenant_id,
            SaasTenantMember.user_id == user.id,
        )
    )
    if not member:
        raise ApiError(403, "TENANT_ISOLATION_VIOLATION", f"Access denied: User is not an authorized member of tenant #{tenant_id}.")
    if allowed_roles and member.tenant_role not in allowed_roles:
        raise ApiError(403, "TENANT_ROLE_FORBIDDEN", f"Operation requires one of: {', '.join(sorted(allowed_roles))}.")
    return tenant, member


# ==============================================================================
# 1. Platform Super Admin College Provisioning & Admin Invitation Flows
# ==============================================================================
class PlatformCollegeProvisionIn(BaseModel):
    collegeName: str = Field(min_length=3, max_length=180)
    tenantSlug: str | None = Field(default=None, min_length=3, max_length=80, pattern="^[a-z0-9-]+$")
    authorizedAdminName: str = Field(min_length=2, max_length=140)
    authorizedAdminEmail: str = Field(min_length=5, max_length=255)
    planTier: str = Field(default="PROFESSIONAL", pattern="^(STARTER|PROFESSIONAL|ENTERPRISE)$")


class InviteAdminIn(BaseModel):
    email: str = Field(min_length=5, max_length=255)
    fullName: str = Field(min_length=2, max_length=140)
    tenantRole: str = Field(default="college_admin", pattern="^(college_owner|college_admin|finance_admin|canteen_manager|kitchen_manager|staff_admin|read_only_auditor)$")
    expiryHours: int = Field(default=48, ge=1, le=168)


class AcceptInvitationIn(BaseModel):
    inviteToken: str = Field(min_length=12, max_length=200)
    password: str = Field(min_length=10, max_length=128)


class AdminRoleChangeIn(BaseModel):
    tenantRole: str = Field(pattern="^(college_owner|college_admin|finance_admin|canteen_manager|kitchen_manager|staff_admin|read_only_auditor)$")


class AdminSuspendPatchIn(BaseModel):
    isActive: bool
    reason: str = Field(default="Administrative governance update", min_length=3, max_length=255)


@router.post("/platform/super-admins/register", status_code=201)
def register_platform_super_admin_v1(
    request: Request,
    userId: int = Query(gt=0),
    privilegeLevel: str = Query(default="platform_operator", pattern="^(platform_owner|platform_operator)$"),
    operator: User = Depends(require_platform_super_admin),
    db: Session = Depends(get_db),
):
    target = db.get(User, userId)
    if not target:
        raise ApiError(404, "USER_NOT_FOUND", "Target user not found.")
    existing = db.scalar(select(PlatformSuperAdmin).where(PlatformSuperAdmin.user_id == target.id))
    if existing:
        existing.is_active = True
        existing.privilege_level = privilegeLevel
    else:
        db.add(PlatformSuperAdmin(user_id=target.id, privilege_level=privilegeLevel, granted_by_user_id=operator.id))
    target.role = UserRole.ADMIN
    record_audit(db, "platform.super_admin_granted", "platform_super_admin", target.id, operator.id, {"privilege": privilegeLevel})
    db.commit()
    return ok(request, {"userId": target.id, "privilegeLevel": privilegeLevel, "isActive": True}, status_code=201)


@router.post("/platform/colleges", status_code=201)
def provision_college_with_invitation_v1(
    request: Request,
    payload: PlatformCollegeProvisionIn,
    operator: User = Depends(require_platform_super_admin),
    db: Session = Depends(get_db),
):
    """Provisions a new college tenant with a single-use cryptographic owner invitation (NO default password)."""
    slug_base = (payload.tenantSlug or re.sub(r"[^a-z0-9]+", "-", payload.collegeName.lower()).strip("-"))[:60]
    slug = f"{slug_base}-{uuid.uuid4().hex[:5]}" if db.scalar(select(SaasTenant).where(SaasTenant.tenant_slug == slug_base)) else slug_base
    code = f"CLG-{uuid.uuid4().hex[:6].upper()}"
    ent = PLAN_ENTITLEMENTS[payload.planTier]

    tenant = SaasTenant(
        tenant_slug=slug,
        college_name=payload.collegeName.strip(),
        status="onboarding",
        plan_tier=payload.planTier,
        contact_email=payload.authorizedAdminEmail.strip().lower(),
        onboarding_step=1,
        onboarding_completed=False,
        max_campuses=ent["maxCampuses"],
        max_outlets=ent["maxOutlets"],
        max_staff_accounts=ent["maxStaffAccounts"],
        bulk_ordering_enabled=ent["bulkOrderingEnabled"],
        advanced_analytics_enabled=ent["advancedAnalyticsEnabled"],
        rate_limit_rpm=ent["rateLimitRpm"],
    )
    db.add(tenant)
    db.flush()

    db.add(
        SaasTenantProfile(
            tenant_id=tenant.id,
            tenant_code=code,
            official_email=payload.authorizedAdminEmail.strip().lower(),
        )
    )
    db.add(
        SaasSubscription(
            tenant_id=tenant.id,
            plan_tier=payload.planTier,
            billing_cycle="monthly",
            status="trial",
            amount_paise=ent["monthlyPricePaise"],
            invoice_number=f"INV-{code}-{uuid.uuid4().hex[:4].upper()}",
            current_period_end=datetime.now(timezone.utc) + timedelta(days=30),
            grace_period_end=datetime.now(timezone.utc) + timedelta(days=37),
        )
    )

    raw_token = f"inv_{tenant.id}_{secrets.token_urlsafe(24)}"
    inv = AdminInvitation(
        tenant_id=tenant.id,
        email=payload.authorizedAdminEmail.strip().lower(),
        full_name=payload.authorizedAdminName.strip(),
        tenant_role="college_owner",
        token_hash=_sha256(raw_token),
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        invited_by_user_id=operator.id,
    )
    db.add(inv)
    db.flush()
    record_audit(
        db,
        "platform.college_provisioned",
        "saas_tenant",
        tenant.id,
        operator.id,
        {"tenant_code": code, "plan": payload.planTier, "owner_email": inv.email},
    )
    db.commit()
    return ok(
        request,
        {
            "tenantId": tenant.id,
            "tenantSlug": tenant.tenant_slug,
            "tenantCode": code,
            "collegeName": tenant.college_name,
            "status": tenant.status,
            "planTier": tenant.plan_tier,
            "invitationId": inv.id,
            "activationToken": raw_token,
            "activationLink": f"/admin?invite={raw_token}",
            "expiresAt": as_utc(inv.expires_at),
        },
        status_code=201,
    )


@router.post("/saas/invitations/accept")
def accept_admin_invitation_v1(
    request: Request,
    payload: AcceptInvitationIn,
    db: Session = Depends(get_db),
):
    _validate_strong_password(payload.password)
    t_hash = _sha256(payload.inviteToken)
    inv = db.scalar(select(AdminInvitation).where(AdminInvitation.token_hash == t_hash))
    if not inv:
        raise ApiError(404, "INVALID_INVITATION", "Invitation token not found.")
    if inv.status == "revoked":
        raise ApiError(409, "INVITATION_REVOKED", "This invitation was revoked by an administrator.")
    if inv.status == "accepted":
        raise ApiError(409, "INVITATION_ALREADY_USED", "Invitations are single-use and cannot be reused.")
    exp = inv.expires_at if inv.expires_at.tzinfo else inv.expires_at.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        inv.status = "expired"
        db.commit()
        raise ApiError(410, "INVITATION_EXPIRED", "This invitation token has expired.")

    email_norm = inv.email.lower()
    user = db.scalar(select(User).where(User.email == email_norm))
    if not user:
        user = User(
            full_name=inv.full_name,
            email=email_norm,
            password_hash=hash_password(payload.password),
            role=UserRole.ADMIN if inv.tenant_role in {"college_owner", "college_admin", "finance_admin"} else UserRole.STAFF,
            is_active=True,
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.flush()
    else:
        user.password_hash = hash_password(payload.password)
        user.email_verified_at = user.email_verified_at or datetime.now(timezone.utc)
        user.is_active = True

    existing_mem = db.scalar(
        select(SaasTenantMember).where(SaasTenantMember.tenant_id == inv.tenant_id, SaasTenantMember.user_id == user.id)
    )
    if existing_mem:
        existing_mem.tenant_role = inv.tenant_role
    else:
        db.add(SaasTenantMember(tenant_id=inv.tenant_id, user_id=user.id, tenant_role=inv.tenant_role))

    now = datetime.now(timezone.utc)
    inv.status = "accepted"
    inv.accepted_by_user_id = user.id
    inv.accepted_at = now

    tenant = db.get(SaasTenant, inv.tenant_id)
    if tenant and tenant.onboarding_step < 3:
        tenant.onboarding_step = 3

    record_audit(
        db,
        "saas.invitation_accepted",
        "admin_invitation",
        inv.id,
        user.id,
        {"tenant_id": inv.tenant_id, "role": inv.tenant_role},
    )
    db.commit()
    db.refresh(user)
    jwt_tok = create_access_token(user, mfa_verified=True)
    return ok(
        request,
        {
            "activated": True,
            "tenantId": inv.tenant_id,
            "tenantRole": inv.tenant_role,
            "accessToken": jwt_tok,
            "user": {"id": user.id, "fullName": user.full_name, "email": user.email, "role": user.role.value},
        },
    )


@router.post("/saas/tenants/{tenant_id}/invitations", status_code=201)
def invite_additional_college_admin_v1(
    tenant_id: int,
    request: Request,
    payload: InviteAdminIn,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, user, allowed_roles=ADMIN_MANAGEMENT_ROLES, x_tenant_id=x_tenant_id)
    raw_token = f"inv_{tenant.id}_{secrets.token_urlsafe(24)}"
    inv = AdminInvitation(
        tenant_id=tenant.id,
        email=payload.email.strip().lower(),
        full_name=payload.fullName.strip(),
        tenant_role=payload.tenantRole,
        token_hash=_sha256(raw_token),
        status="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=payload.expiryHours),
        invited_by_user_id=user.id,
    )
    db.add(inv)
    db.flush()
    record_audit(
        db,
        "saas.admin_invited",
        "admin_invitation",
        inv.id,
        user.id,
        {"tenant_id": tenant.id, "invited_email": inv.email, "role": inv.tenant_role},
    )
    db.commit()
    return ok(
        request,
        {
            "invitationId": inv.id,
            "tenantId": tenant.id,
            "email": inv.email,
            "tenantRole": inv.tenant_role,
            "inviteToken": raw_token,
            "expiresAt": as_utc(inv.expires_at),
        },
        status_code=201,
    )


@router.post("/saas/tenants/{tenant_id}/invitations/{invitation_id}/revoke")
def revoke_admin_invitation_v1(
    tenant_id: int,
    invitation_id: int,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, user, allowed_roles=ADMIN_MANAGEMENT_ROLES, x_tenant_id=x_tenant_id)
    inv = db.scalar(select(AdminInvitation).where(AdminInvitation.id == invitation_id, AdminInvitation.tenant_id == tenant.id))
    if not inv:
        raise ApiError(404, "INVITATION_NOT_FOUND", "Invitation not found for this college.")
    inv.status = "revoked"
    inv.revoked_at = datetime.now(timezone.utc)
    record_audit(db, "saas.invitation_revoked", "admin_invitation", inv.id, user.id, {"tenant_id": tenant.id})
    db.commit()
    return ok(request, {"invitationId": inv.id, "status": inv.status})


# ==============================================================================
# 2. Additional Admin Governance, Role Changes, Suspend & Last-Owner Guard
# ==============================================================================
def _active_owner_count(db: Session, tenant_id: int) -> int:
    return int(
        db.scalar(
            select(func.count(SaasTenantMember.id))
            .join(User, User.id == SaasTenantMember.user_id)
            .where(
                SaasTenantMember.tenant_id == tenant_id,
                SaasTenantMember.tenant_role == "college_owner",
                User.is_active.is_(True),
            )
        )
        or 0
    )


@router.get("/saas/tenants/{tenant_id}/admins")
def list_college_admins_v1(
    tenant_id: int,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, user, allowed_roles=COLLEGE_ROLES, x_tenant_id=x_tenant_id)
    rows = db.execute(
        select(SaasTenantMember, User)
        .join(User, User.id == SaasTenantMember.user_id)
        .where(SaasTenantMember.tenant_id == tenant.id)
        .order_by(SaasTenantMember.id.asc())
    ).all()
    return ok(
        request,
        {
            "tenantId": tenant.id,
            "items": [
                {
                    "userId": u.id,
                    "fullName": u.full_name,
                    "email": u.email,
                    "tenantRole": m.tenant_role,
                    "isActive": bool(u.is_active),
                    "mfaEnabled": bool(u.mfa_enabled),
                }
                for m, u in rows
            ],
        },
    )


@router.patch("/saas/tenants/{tenant_id}/admins/{target_user_id}/role")
def change_college_admin_role_v1(
    tenant_id: int,
    target_user_id: int,
    request: Request,
    payload: AdminRoleChangeIn,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, actor, allowed_roles=ADMIN_MANAGEMENT_ROLES, x_tenant_id=x_tenant_id)
    if db.scalar(select(PlatformSuperAdmin).where(PlatformSuperAdmin.user_id == target_user_id)):
        raise ApiError(403, "SUPER_ADMIN_PROTECTED", "Platform Super Admin accounts cannot be edited by college administrators.")
    target_mem = db.scalar(
        select(SaasTenantMember).where(SaasTenantMember.tenant_id == tenant.id, SaasTenantMember.user_id == target_user_id)
    )
    if not target_mem:
        raise ApiError(404, "MEMBER_NOT_FOUND", "Administrator does not belong to this college tenant.")
    if target_mem.tenant_role == "college_owner" and payload.tenantRole != "college_owner" and _active_owner_count(db, tenant.id) <= 1:
        raise ApiError(409, "LAST_OWNER_PROTECTED", "Cannot demote the last active college owner without completing an ownership transfer.")
    prev = target_mem.tenant_role
    target_mem.tenant_role = payload.tenantRole
    record_audit(db, "saas.admin_role_changed", "user", target_user_id, actor.id, {"tenant_id": tenant.id, "from": prev, "to": payload.tenantRole})
    db.commit()
    return ok(request, {"userId": target_user_id, "tenantRole": target_mem.tenant_role})


@router.patch("/saas/tenants/{tenant_id}/admins/{target_user_id}/status")
def suspend_or_reactivate_college_admin_v1(
    tenant_id: int,
    target_user_id: int,
    request: Request,
    payload: AdminSuspendPatchIn,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    actor: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, actor, allowed_roles=ADMIN_MANAGEMENT_ROLES, x_tenant_id=x_tenant_id)
    if db.scalar(select(PlatformSuperAdmin).where(PlatformSuperAdmin.user_id == target_user_id)):
        raise ApiError(403, "SUPER_ADMIN_PROTECTED", "Platform Super Admin accounts cannot be suspended by college administrators.")
    target_mem = db.scalar(
        select(SaasTenantMember).where(SaasTenantMember.tenant_id == tenant.id, SaasTenantMember.user_id == target_user_id)
    )
    if not target_mem:
        raise ApiError(404, "MEMBER_NOT_FOUND", "Administrator does not belong to this college tenant.")
    if not payload.isActive and target_mem.tenant_role == "college_owner" and _active_owner_count(db, tenant.id) <= 1:
        raise ApiError(409, "LAST_OWNER_PROTECTED", "Cannot suspend the last active college owner without ownership transfer.")
    target_user = db.get(User, target_user_id)
    target_user.is_active = payload.isActive
    if not payload.isActive:
        target_user.token_version += 1
        db.execute(RefreshSession.__table__.update().where(RefreshSession.user_id == target_user.id).values(revoked_at=datetime.now(timezone.utc)))
    record_audit(db, "saas.admin_status_updated", "user", target_user_id, actor.id, {"tenant_id": tenant.id, "active": payload.isActive, "reason": payload.reason})
    db.commit()
    return ok(request, {"userId": target_user_id, "isActive": bool(target_user.is_active)})


# ==============================================================================
# 3. Verified Email Change, Password Reset & Active Session Revocation
# ==============================================================================
class EmailChangeRequestIn(BaseModel):
    currentPassword: str = Field(min_length=4, max_length=128)
    newEmail: str = Field(min_length=5, max_length=255)
    tenantId: int | None = Field(default=None, gt=0)


class EmailChangeVerifyIn(BaseModel):
    verificationToken: str = Field(min_length=12, max_length=200)


class PasswordChangeIn(BaseModel):
    currentPassword: str = Field(min_length=4, max_length=128)
    newPassword: str = Field(min_length=10, max_length=128)


class ForgotPasswordIn(BaseModel):
    email: str = Field(min_length=5, max_length=255)


class ResetPasswordIn(BaseModel):
    resetToken: str = Field(min_length=12, max_length=200)
    newPassword: str = Field(min_length=10, max_length=128)


@router.post("/auth/email-change/request")
def request_verified_email_change_v1(
    request: Request,
    payload: EmailChangeRequestIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.currentPassword, user.password_hash):
        raise ApiError(401, "INVALID_CREDENTIALS", "Current password verification failed.")
    new_email = payload.newEmail.strip().lower()
    if db.scalar(select(User).where(User.email == new_email, User.id != user.id)):
        raise ApiError(409, "EMAIL_ALREADY_IN_USE", "The requested email is already registered.")
    raw_tok = f"emc_{user.id}_{secrets.token_urlsafe(24)}"
    req_row = EmailChangeRequest(
        tenant_id=payload.tenantId,
        user_id=user.id,
        old_email=user.email,
        new_email=new_email,
        token_hash=_sha256(raw_tok),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(req_row)
    record_audit(db, "auth.email_change_requested", "user", user.id, user.id, {"old_email": user.email, "new_email": new_email})
    db.commit()
    return ok(request, {"requestId": req_row.id, "verificationToken": raw_tok, "expiresAt": as_utc(req_row.expires_at)})


@router.post("/auth/email-change/verify")
def verify_email_change_v1(
    request: Request,
    payload: EmailChangeVerifyIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    t_hash = _sha256(payload.verificationToken)
    req_row = db.scalar(select(EmailChangeRequest).where(EmailChangeRequest.token_hash == t_hash, EmailChangeRequest.user_id == user.id))
    if not req_row or req_row.status != "pending":
        raise ApiError(404, "INVALID_EMAIL_CHANGE_TOKEN", "Email change request not found or already used.")
    exp = req_row.expires_at if req_row.expires_at.tzinfo else req_row.expires_at.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        req_row.status = "expired"
        db.commit()
        raise ApiError(410, "TOKEN_EXPIRED", "Verification token expired.")

    old_email = user.email
    user.email = req_row.new_email
    user.email_verified_at = datetime.now(timezone.utc)
    req_row.status = "verified"
    req_row.verified_at = datetime.now(timezone.utc)
    record_audit(db, "auth.email_changed", "user", user.id, user.id, {"old_email": old_email, "new_email": user.email})
    db.commit()
    return ok(request, {"emailUpdated": True, "oldEmail": old_email, "newEmail": user.email})


@router.post("/auth/password/change")
def change_password_secure_v1(
    request: Request,
    payload: PasswordChangeIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.currentPassword, user.password_hash):
        raise ApiError(401, "INVALID_CREDENTIALS", "Current password is incorrect.")
    _validate_strong_password(payload.newPassword)
    user.password_hash = hash_password(payload.newPassword)
    user.token_version += 1
    db.execute(RefreshSession.__table__.update().where(RefreshSession.user_id == user.id).values(revoked_at=datetime.now(timezone.utc)))
    record_audit(db, "auth.password_changed", "user", user.id, user.id, {"sessions_revoked": True})
    db.commit()
    return ok(request, {"passwordChanged": True, "sessionsRevoked": True})


@router.post("/auth/password/forgot")
def forgot_password_request_v1(
    request: Request,
    payload: ForgotPasswordIn,
    db: Session = Depends(get_db),
):
    from app.main import _check_login_rate, _login_rate_key, _record_failed_login

    email_norm = payload.email.strip().lower()
    rate_key = "pwforgot:" + _login_rate_key(request, email_norm)
    retry_after = _check_login_rate(rate_key)
    if retry_after:
        raise ApiError(429, "RATE_LIMITED", "Too many password reset requests. Try again later.", headers={"Retry-After": str(retry_after)})
    _record_failed_login(rate_key)

    user = db.scalar(select(User).where(func.lower(User.email) == email_norm))
    is_dev = get_settings().environment.lower() in {"development", "test"}
    if not user:
        return ok(request, {"submitted": True})
    raw_tok = f"rst_{user.id}_{secrets.token_urlsafe(24)}"
    rec = PasswordResetTokenRecord(
        user_id=user.id,
        token_hash=_sha256(raw_tok),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    db.add(rec)
    record_audit(db, "auth.password_reset_requested", "user", user.id, user.id, {"method": "token_hash"})
    db.commit()
    data: dict[str, Any] = {"submitted": True}
    if is_dev:
        data["sandboxResetToken"] = raw_tok
    return ok(request, data)


@router.post("/auth/password/reset")
def reset_password_confirm_v1(
    request: Request,
    payload: ResetPasswordIn,
    db: Session = Depends(get_db),
):
    _validate_strong_password(payload.newPassword)
    t_hash = _sha256(payload.resetToken)
    rec = db.scalar(select(PasswordResetTokenRecord).where(PasswordResetTokenRecord.token_hash == t_hash))
    if not rec or rec.used_at is not None:
        raise ApiError(404, "INVALID_RESET_TOKEN", "Password reset token is invalid or already used.")
    exp = rec.expires_at if rec.expires_at.tzinfo else rec.expires_at.replace(tzinfo=timezone.utc)
    if exp < datetime.now(timezone.utc):
        raise ApiError(410, "RESET_TOKEN_EXPIRED", "Password reset token has expired.")

    user = db.get(User, rec.user_id)
    user.password_hash = hash_password(payload.newPassword)
    user.token_version += 1
    rec.used_at = datetime.now(timezone.utc)
    db.execute(RefreshSession.__table__.update().where(RefreshSession.user_id == user.id).values(revoked_at=datetime.now(timezone.utc)))
    record_audit(db, "auth.password_reset_completed", "user", user.id, user.id, {"sessions_revoked": True})
    db.commit()
    return ok(request, {"passwordReset": True, "sessionsRevoked": True})


@router.get("/auth/sessions")
def list_active_sessions_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(RefreshSession).where(RefreshSession.user_id == user.id, RefreshSession.revoked_at.is_(None)).order_by(RefreshSession.created_at.desc())
    ).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": s.id,
                    "userAgent": s.user_agent,
                    "ipAddress": s.ip_address,
                    "createdAt": as_utc(s.created_at),
                    "expiresAt": as_utc(s.expires_at),
                }
                for s in rows
            ]
        },
    )


@router.post("/auth/sessions/{session_id}/revoke")
def revoke_single_session_v1(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    sess = db.scalar(select(RefreshSession).where(RefreshSession.id == session_id, RefreshSession.user_id == user.id))
    if not sess:
        raise ApiError(404, "SESSION_NOT_FOUND", "Session not found.")
    sess.revoked_at = datetime.now(timezone.utc)
    user.token_version += 1
    db.commit()
    return ok(request, {"revoked": True, "sessionId": session_id})


# ==============================================================================
# 4. College Ownership Transfer, Recovery & Tenant Profile / Finance Gateway
# ==============================================================================
class OwnershipTransferInitiateIn(BaseModel):
    targetEmail: str = Field(min_length=5, max_length=255)
    previousOwnerAction: str = Field(default="demote_to_admin", pattern="^(demote_to_admin|revoke)$")
    reason: str = Field(min_length=3, max_length=500)


class OwnershipTransferAcceptIn(BaseModel):
    transferToken: str = Field(min_length=12, max_length=200)


class PlatformOwnershipRecoveryIn(BaseModel):
    targetEmail: str = Field(min_length=5, max_length=255)
    targetFullName: str = Field(min_length=2, max_length=140)
    reason: str = Field(min_length=5, max_length=500)


class TenantProfilePatchIn(BaseModel):
    officialEmail: str | None = Field(default=None, max_length=255)
    contactNumber: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=500)
    campusInfo: str | None = Field(default=None, max_length=500)
    operatingHours: dict[str, str] | None = None
    approvedDomains: list[str] | None = None
    termsUrl: str | None = Field(default=None, max_length=500)
    privacyUrl: str | None = Field(default=None, max_length=500)
    customSenderEmail: str | None = Field(default=None, max_length=255)


class SubscriptionPlanChangeIn(BaseModel):
    planTier: str = Field(pattern="^(STARTER|PROFESSIONAL|ENTERPRISE)$")
    billingCycle: str = Field(default="monthly", pattern="^(monthly|annual)$")


@router.post("/saas/tenants/{tenant_id}/ownership-transfer", status_code=201)
def initiate_college_ownership_transfer_v1(
    tenant_id: int,
    request: Request,
    payload: OwnershipTransferInitiateIn,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, user, allowed_roles={"college_owner"}, x_tenant_id=x_tenant_id)
    target_user = db.scalar(select(User).where(User.email == payload.targetEmail.strip().lower()))
    if not target_user:
        raise ApiError(404, "TARGET_USER_NOT_FOUND", "Replacement administrator user must be registered before ownership transfer.")
    raw_tok = f"own_{tenant.id}_{secrets.token_urlsafe(24)}"
    tr = OwnershipTransferRequest(
        tenant_id=tenant.id,
        from_owner_user_id=user.id,
        target_email=target_user.email,
        target_user_id=target_user.id,
        token_hash=_sha256(raw_tok),
        transfer_type="owner_initiated",
        previous_owner_action=payload.previousOwnerAction,
        status="pending_target",
        reason=payload.reason.strip(),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=48),
    )
    db.add(tr)
    record_audit(db, "saas.ownership_transfer_initiated", "saas_tenant", tenant.id, user.id, {"to_email": target_user.email})
    db.commit()
    return ok(request, {"transferId": tr.id, "transferToken": raw_tok, "status": tr.status}, status_code=201)


@router.post("/saas/ownership-transfer/accept")
def accept_college_ownership_transfer_v1(
    request: Request,
    payload: OwnershipTransferAcceptIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    t_hash = _sha256(payload.transferToken)
    tr = db.scalar(select(OwnershipTransferRequest).where(OwnershipTransferRequest.token_hash == t_hash))
    if not tr or tr.status != "pending_target":
        raise ApiError(404, "INVALID_TRANSFER_TOKEN", "Ownership transfer token not found or already completed.")
    if tr.target_user_id != user.id and tr.target_email.lower() != user.email.lower():
        raise ApiError(403, "TRANSFER_AUTHORITY_MISMATCH", "Only the designated replacement administrator may accept ownership.")

    target_mem = db.scalar(select(SaasTenantMember).where(SaasTenantMember.tenant_id == tr.tenant_id, SaasTenantMember.user_id == user.id))
    if target_mem:
        target_mem.tenant_role = "college_owner"
    else:
        db.add(SaasTenantMember(tenant_id=tr.tenant_id, user_id=user.id, tenant_role="college_owner"))

    if tr.from_owner_user_id and tr.from_owner_user_id != user.id:
        old_mem = db.scalar(select(SaasTenantMember).where(SaasTenantMember.tenant_id == tr.tenant_id, SaasTenantMember.user_id == tr.from_owner_user_id))
        if old_mem:
            if tr.previous_owner_action == "revoke":
                db.delete(old_mem)
            else:
                old_mem.tenant_role = "college_admin"

    tr.status = "completed"
    tr.completed_at = datetime.now(timezone.utc)
    record_audit(
        db,
        "saas.ownership_transferred",
        "saas_tenant",
        tr.tenant_id,
        user.id,
        {"from_owner_id": tr.from_owner_user_id, "new_owner_id": user.id, "prev_action": tr.previous_owner_action},
    )
    db.commit()
    return ok(request, {"transferred": True, "tenantId": tr.tenant_id, "newOwnerUserId": user.id})


@router.post("/platform/colleges/{tenant_id}/ownership-recovery")
def platform_emergency_ownership_recovery_v1(
    tenant_id: int,
    request: Request,
    payload: PlatformOwnershipRecoveryIn,
    operator: User = Depends(require_platform_super_admin),
    db: Session = Depends(get_db),
):
    """Emergency recovery when the original college owner is unavailable; requires Platform Super Admin authorization."""
    tenant = db.get(SaasTenant, tenant_id)
    if not tenant:
        raise ApiError(404, "TENANT_NOT_FOUND", "College tenant not found.")
    email_norm = payload.targetEmail.strip().lower()
    target_user = db.scalar(select(User).where(User.email == email_norm))
    if not target_user:
        raise ApiError(404, "TARGET_USER_NOT_FOUND", "Recovery target administrator must already have a verified user account.")

    # Demote previous unavailable owner(s)
    prev_owners = db.scalars(
        select(SaasTenantMember).where(SaasTenantMember.tenant_id == tenant.id, SaasTenantMember.tenant_role == "college_owner")
    ).all()
    for po in prev_owners:
        po.tenant_role = "college_admin"

    target_mem = db.scalar(select(SaasTenantMember).where(SaasTenantMember.tenant_id == tenant.id, SaasTenantMember.user_id == target_user.id))
    if target_mem:
        target_mem.tenant_role = "college_owner"
    else:
        db.add(SaasTenantMember(tenant_id=tenant.id, user_id=target_user.id, tenant_role="college_owner"))

    tr = OwnershipTransferRequest(
        tenant_id=tenant.id,
        from_owner_user_id=prev_owners[0].user_id if prev_owners else None,
        target_email=email_norm,
        target_user_id=target_user.id,
        token_hash=_sha256(secrets.token_hex(16)),
        transfer_type="platform_recovery",
        previous_owner_action="demote_to_admin",
        status="completed",
        reason=payload.reason.strip(),
        platform_approved_by_id=operator.id,
        expires_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    )
    db.add(tr)
    record_audit(db, "platform.ownership_recovered", "saas_tenant", tenant.id, operator.id, {"new_owner": email_norm, "reason": payload.reason})
    db.commit()
    return ok(request, {"recovered": True, "tenantId": tenant.id, "newOwnerUserId": target_user.id, "transferType": "platform_recovery"})


@router.get("/saas/tenants/{tenant_id}/profile")
def get_tenant_profile_settings_v1(
    tenant_id: int,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, user, allowed_roles=COLLEGE_ROLES, x_tenant_id=x_tenant_id)
    prof = db.scalar(select(SaasTenantProfile).where(SaasTenantProfile.tenant_id == tenant.id))
    if not prof:
        prof = SaasTenantProfile(tenant_id=tenant.id, tenant_code=f"CLG-{tenant.id:04d}", official_email=tenant.contact_email)
        db.add(prof)
        db.commit()
        db.refresh(prof)
    return ok(
        request,
        {
            "tenantId": tenant.id,
            "tenantCode": prof.tenant_code,
            "collegeName": tenant.college_name,
            "officialEmail": prof.official_email,
            "contactNumber": prof.contact_number,
            "address": prof.address,
            "campusInfo": prof.campus_info,
            "operatingHours": json.loads(prof.operating_hours_json or "{}"),
            "approvedDomains": json.loads(prof.approved_domains_json or "[]"),
            "termsUrl": prof.terms_url,
            "privacyUrl": prof.privacy_url,
            "customSenderEmail": prof.custom_sender_email,
            "senderDomainVerified": bool(prof.sender_domain_verified),
        },
    )


@router.patch("/saas/tenants/{tenant_id}/profile")
def update_tenant_profile_settings_v1(
    tenant_id: int,
    request: Request,
    payload: TenantProfilePatchIn,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, user, allowed_roles={"college_owner", "college_admin"}, x_tenant_id=x_tenant_id)
    prof = db.scalar(select(SaasTenantProfile).where(SaasTenantProfile.tenant_id == tenant.id))
    if not prof:
        prof = SaasTenantProfile(tenant_id=tenant.id, tenant_code=f"CLG-{tenant.id:04d}")
        db.add(prof)
    if payload.officialEmail is not None:
        prof.official_email = payload.officialEmail.strip().lower()
    if payload.contactNumber is not None:
        prof.contact_number = payload.contactNumber.strip()
    if payload.address is not None:
        prof.address = payload.address.strip()
    if payload.campusInfo is not None:
        prof.campus_info = payload.campusInfo.strip()
    if payload.operatingHours is not None:
        prof.operating_hours_json = json.dumps(payload.operatingHours)
    if payload.approvedDomains is not None:
        prof.approved_domains_json = json.dumps([d.strip().lower() for d in payload.approvedDomains])
    if payload.termsUrl is not None:
        prof.terms_url = payload.termsUrl.strip()
    if payload.privacyUrl is not None:
        prof.privacy_url = payload.privacyUrl.strip()
    if payload.customSenderEmail is not None:
        prof.custom_sender_email = payload.customSenderEmail.strip().lower()
        sender_domain = prof.custom_sender_email.split("@")[-1] if "@" in prof.custom_sender_email else ""
        approved = json.loads(prof.approved_domains_json or "[]")
        prof.sender_domain_verified = sender_domain in approved

    record_audit(db, "saas.profile_updated", "saas_tenant", tenant.id, user.id, {"sender_verified": prof.sender_domain_verified})
    db.commit()
    return ok(request, {"updated": True, "tenantId": tenant.id, "senderDomainVerified": bool(prof.sender_domain_verified)})


@router.post("/saas/tenants/{tenant_id}/subscription/change-plan")
def change_tenant_subscription_plan_v1(
    tenant_id: int,
    request: Request,
    payload: SubscriptionPlanChangeIn,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, user, allowed_roles={"college_owner", "finance_admin"}, x_tenant_id=x_tenant_id)
    ent = PLAN_ENTITLEMENTS[payload.planTier]
    tenant.plan_tier = payload.planTier
    tenant.max_campuses = ent["maxCampuses"]
    tenant.max_outlets = ent["maxOutlets"]
    tenant.max_staff_accounts = ent["maxStaffAccounts"]
    tenant.bulk_ordering_enabled = ent["bulkOrderingEnabled"]
    tenant.advanced_analytics_enabled = ent["advancedAnalyticsEnabled"]
    tenant.rate_limit_rpm = ent["rateLimitRpm"]

    inv_num = f"INV-CHG-{tenant.id}-{uuid.uuid4().hex[:6].upper()}"
    sub = SaasSubscription(
        tenant_id=tenant.id,
        plan_tier=payload.planTier,
        billing_cycle=payload.billingCycle,
        status="active",
        amount_paise=ent["monthlyPricePaise"] * (10 if payload.billingCycle == "annual" else 1),
        invoice_number=inv_num,
        current_period_end=datetime.now(timezone.utc) + timedelta(days=365 if payload.billingCycle == "annual" else 30),
    )
    db.add(sub)
    record_audit(db, "saas.subscription_plan_changed", "saas_tenant", tenant.id, user.id, {"plan": payload.planTier, "invoice": inv_num})
    db.commit()
    return ok(request, {"tenantId": tenant.id, "planTier": tenant.plan_tier, "invoiceNumber": inv_num, "status": "active"})


# ==============================================================================
# 5. Non-Overwriting College Data CSV Importers (Students, Faculty, Staff, Menu, Inventory)
# ==============================================================================
class TenantCsvImportIn(BaseModel):
    csvContent: str = Field(min_length=3)
    dryRun: bool = False


@router.post("/saas/tenants/{tenant_id}/import/{entity_type}")
def non_overwriting_tenant_csv_import_v1(
    tenant_id: int,
    entity_type: str,
    request: Request,
    payload: TenantCsvImportIn,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Validates and imports students, faculty, staff, menu, or inventory rows without overwriting existing records."""
    if entity_type not in {"students", "faculty", "staff", "menu", "inventory"}:
        raise ApiError(422, "UNSUPPORTED_IMPORT_ENTITY", f"Unsupported import type '{entity_type}'.")
    tenant, _ = _assert_tenant_member_role(db, tenant_id, user, allowed_roles={"college_owner", "college_admin", "canteen_manager", "staff_admin"}, x_tenant_id=x_tenant_id)

    reader = csv.DictReader(io.StringIO(payload.csvContent.strip()))
    imported = 0
    skipped_existing = 0
    invalid_rows: list[dict[str, Any]] = []

    for idx, raw_row in enumerate(reader, start=1):
        row = {str(k or "").strip().lower(): str(v or "").strip() for k, v in raw_row.items()}
        if entity_type in {"students", "faculty", "staff"}:
            email = row.get("email", "").lower()
            name = row.get("full_name") or row.get("name") or ""
            if "@" not in email or not name:
                invalid_rows.append({"row": idx, "reason": "Missing valid email or full_name"})
                continue
            existing_u = db.scalar(select(User).where(User.email == email))
            if existing_u:
                skipped_existing += 1
                continue
            if not payload.dryRun:
                role_enum = UserRole.STAFF if entity_type == "staff" else UserRole.STUDENT
                t_role = "staff" if entity_type == "staff" else ("faculty" if entity_type == "faculty" else "student")
                u = User(full_name=name, email=email, password_hash=hash_password(secrets.token_urlsafe(16)), role=role_enum, is_active=True)
                db.add(u)
                db.flush()
                db.add(SaasTenantMember(tenant_id=tenant.id, user_id=u.id, tenant_role=t_role))
            imported += 1
        elif entity_type == "menu":
            name = row.get("name") or ""
            price_str = row.get("price_paise") or ""
            if not name or not price_str.isdigit():
                invalid_rows.append({"row": idx, "reason": "Missing item name or integer price_paise"})
                continue
            if db.scalar(select(Product).where(Product.name == name)):
                skipped_existing += 1
                continue
            if not payload.dryRun:
                cat = db.scalar(select(Category))
                if not cat:
                    cat = Category(name="Campus Menu", slug=f"campus-{uuid.uuid4().hex[:5]}", item_type="food", is_active=True)
                    db.add(cat)
                    db.flush()
                cnt = db.scalar(select(CanteenCounter))
                db.add(
                    Product(
                        category_id=cat.id,
                        counter_id=cnt.id if cnt else None,
                        name=name,
                        item_type="food",
                        price=int(price_str) / 100.0,
                        price_paise=int(price_str),
                        stock_quantity=int(row.get("stock_quantity") or 20),
                        is_available=True,
                    )
                )
            imported += 1
        elif entity_type == "inventory":
            name = row.get("name") or ""
            unit = row.get("unit") or "kg"
            if not name:
                invalid_rows.append({"row": idx, "reason": "Missing ingredient name"})
                continue
            if db.scalar(select(Ingredient).where(Ingredient.name == name)):
                skipped_existing += 1
                continue
            if not payload.dryRun:
                db.add(Ingredient(name=name, unit=unit, current_quantity=Decimal(str(row.get("stock") or 10))))
            imported += 1

    if not payload.dryRun:
        record_audit(db, f"saas.import_{entity_type}", "saas_tenant", tenant.id, user.id, {"imported": imported, "skipped": skipped_existing, "invalid": len(invalid_rows)})
        db.commit()

    return ok(
        request,
        {
            "entityType": entity_type,
            "dryRun": payload.dryRun,
            "importedCount": imported,
            "skippedExistingCount": skipped_existing,
            "invalidCount": len(invalid_rows),
            "invalidRows": invalid_rows[:50],
        },
    )


@router.get("/saas/tenants/{tenant_id}/audit-logs")
def get_tenant_scoped_audit_logs_v1(
    tenant_id: int,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant, _ = _assert_tenant_member_role(db, tenant_id, user, allowed_roles={"college_owner", "college_admin", "read_only_auditor"}, x_tenant_id=x_tenant_id)
    member_ids = db.scalars(select(SaasTenantMember.user_id).where(SaasTenantMember.tenant_id == tenant.id)).all()
    events = db.scalars(
        select(AuditEvent)
        .where(AuditEvent.actor_user_id.in_(member_ids) if member_ids else AuditEvent.id == -1)
        .order_by(AuditEvent.id.desc())
        .limit(100)
    ).all()
    return ok(
        request,
        {
            "tenantId": tenant.id,
            "items": [
                {
                    "id": e.id,
                    "eventType": e.action,
                    "entityType": e.entity_type,
                    "entityId": e.entity_id,
                    "actorUserId": e.actor_user_id,
                    "createdAt": as_utc(e.created_at),
                }
                for e in events
            ],
        },
    )
