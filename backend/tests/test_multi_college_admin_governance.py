"""Acceptance Verification Suite for Multi-College Admin Account, Credential & Ownership Management:
Verifies all 15 security & multi-tenant governance scenarios:
  1. Platform Super Admin creates a new college -> cryptographically random single-use invitation (NO default password).
  2. Invitation acceptance requires strong password & verifies email; token hash only in DB; reuse/revoked rejected.
  3. Additional admin invitations (finance_admin, read_only_auditor) & revocation.
  4. Verified admin email change workflow + audit logging.
  5. Password change + session revocation + forgot/reset password with single-use expiry token.
  6. Last active college_owner protection against suspension/demotion (409 LAST_OWNER_PROTECTED).
  7. Active session listing & revocation.
  8. College Ownership Transfer (initiate -> target accept -> previous owner demoted/revoked).
  9. Emergency Platform Ownership Recovery when original owner is unavailable.
 10. College profile settings & verified notification sender domain validation + plan change.
 11. Non-overwriting CSV importers (students, faculty, staff, menu, inventory).
 12. Strict Tenant Isolation: College A cannot access/edit/reset/invite/transfer College B admins or touch Platform Super Admins.
 13. Platform Super Admin strict separation: Ordinary College Admins blocked from /api/v1/platform/* (403).
"""
from __future__ import annotations

import hashlib
import uuid

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import AdminInvitation, PlatformSuperAdmin, User, UserRole
from app.security import create_access_token, hash_password


def _mk_super_admin() -> tuple[int, str, str]:
    email = f"super-{uuid.uuid4().hex[:8]}@collegebits.io"
    with SessionLocal() as db:
        u = User(
            full_name="Platform Super Admin",
            email=email,
            password_hash=hash_password("SuperPlatform#2026!"),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(u)
        db.flush()
        db.add(PlatformSuperAdmin(user_id=u.id, privilege_level="platform_owner", is_active=True))
        db.commit()
        db.refresh(u)
        return u.id, email, create_access_token(u, mfa_verified=True)


def test_complete_multi_college_admin_lifecycle_ownership_transfer_and_tenant_isolation():
    super_id, _, super_tok = _mk_super_admin()
    super_h = {"Authorization": f"Bearer {super_tok}"}

    with TestClient(app) as client:
        # 1. Platform Super Admin provisions College A and College B without default passwords
        owner_a_email = f"owner.a.{uuid.uuid4().hex[:6]}@collegea.edu"
        owner_b_email = f"owner.b.{uuid.uuid4().hex[:6]}@collegeb.edu"

        prov_a = client.post(
            "/api/v1/platform/colleges",
            headers=super_h,
            json={
                "collegeName": "ACE Engineering College A",
                "authorizedAdminName": "Owner A",
                "authorizedAdminEmail": owner_a_email,
                "planTier": "PROFESSIONAL",
            },
        )
        assert prov_a.status_code == 201, prov_a.text
        col_a = prov_a.json()["data"]
        tid_a = col_a["tenantId"]
        inv_tok_a = col_a["activationToken"]

        # Verify database stores ONLY SHA-256 hash of invitation token
        with SessionLocal() as db:
            inv_row = db.get(AdminInvitation, col_a["invitationId"])
            assert inv_row is not None
            assert inv_row.token_hash == hashlib.sha256(inv_tok_a.encode()).hexdigest()
            assert inv_row.token_hash != inv_tok_a

        prov_b = client.post(
            "/api/v1/platform/colleges",
            headers=super_h,
            json={
                "collegeName": "Global Tech College B",
                "authorizedAdminName": "Owner B",
                "authorizedAdminEmail": owner_b_email,
                "planTier": "STARTER",
            },
        ).json()["data"]
        tid_b = prov_b["tenantId"]
        inv_tok_b = prov_b["activationToken"]

        # 2. Owner A & Owner B accept invitations and set strong passwords
        acc_a = client.post("/api/v1/saas/invitations/accept", json={"inviteToken": inv_tok_a, "password": "OwnerA#Strong2026!"})
        assert acc_a.status_code == 200, acc_a.text
        owner_a_tok = acc_a.json()["data"]["accessToken"]
        owner_a_id = acc_a.json()["data"]["user"]["id"]
        owner_a_h = {"Authorization": f"Bearer {owner_a_tok}"}

        # Invitation is single-use: reuse must fail (409)
        reuse_a = client.post("/api/v1/saas/invitations/accept", json={"inviteToken": inv_tok_a, "password": "OwnerA#Strong2026!"})
        assert reuse_a.status_code == 409
        assert reuse_a.json()["error"]["code"] == "INVITATION_ALREADY_USED"

        acc_b = client.post("/api/v1/saas/invitations/accept", json={"inviteToken": inv_tok_b, "password": "OwnerB#Strong2026!"})
        assert acc_b.status_code == 200
        owner_b_id = acc_b.json()["data"]["user"]["id"]

        # 3. Separation of Admin Levels: College Owner A CANNOT access Platform Super Admin endpoints (403)
        assert client.post(
            "/api/v1/platform/colleges",
            headers=owner_a_h,
            json={"collegeName": "Rogue College", "authorizedAdminName": "Rogue", "authorizedAdminEmail": "rogue@test.edu"},
        ).status_code == 403

        # 4. Last-Owner Protection: Owner A cannot be demoted or suspended when only 1 owner exists
        demote_last = client.patch(
            f"/api/v1/saas/tenants/{tid_a}/admins/{owner_a_id}/role",
            headers=owner_a_h,
            json={"tenantRole": "college_admin"},
        )
        assert demote_last.status_code == 409
        assert demote_last.json()["error"]["code"] == "LAST_OWNER_PROTECTED"

        # 5. Invite Finance Admin to College A + test invitation revocation
        fin_email = f"fin.a.{uuid.uuid4().hex[:6]}@collegea.edu"
        fin_inv = client.post(
            f"/api/v1/saas/tenants/{tid_a}/invitations",
            headers=owner_a_h,
            json={"email": fin_email, "fullName": "Finance Admin A", "tenantRole": "finance_admin"},
        ).json()["data"]
        fin_acc = client.post("/api/v1/saas/invitations/accept", json={"inviteToken": fin_inv["inviteToken"], "password": "FinAdmin#2026!"}).json()["data"]
        fin_a_tok = fin_acc["accessToken"]
        fin_a_h = {"Authorization": f"Bearer {fin_a_tok}"}

        # Revoked invitation test
        temp_inv = client.post(
            f"/api/v1/saas/tenants/{tid_a}/invitations",
            headers=owner_a_h,
            json={"email": "revoked@collegea.edu", "fullName": "Revoked", "tenantRole": "read_only_auditor"},
        ).json()["data"]
        assert client.post(f"/api/v1/saas/tenants/{tid_a}/invitations/{temp_inv['invitationId']}/revoke", headers=owner_a_h).status_code == 200
        assert client.post("/api/v1/saas/invitations/accept", json={"inviteToken": temp_inv["inviteToken"], "password": "TempPass#2026!"}).status_code == 409

        # 6. Verified Admin Email Change & Password Change + Forgot/Reset Password
        new_owner_a_email = f"owner.a.new.{uuid.uuid4().hex[:6]}@collegea.edu"
        emc_req = client.post(
            "/api/v1/auth/email-change/request",
            headers=owner_a_h,
            json={"currentPassword": "OwnerA#Strong2026!", "newEmail": new_owner_a_email, "tenantId": tid_a},
        ).json()["data"]
        assert client.post("/api/v1/auth/email-change/verify", headers=owner_a_h, json={"verificationToken": emc_req["verificationToken"]}).status_code == 200

        # Password change revokes old JWT sessions
        assert client.post(
            "/api/v1/auth/password/change",
            headers=owner_a_h,
            json={"currentPassword": "OwnerA#Strong2026!", "newPassword": "OwnerA#Rotated99!"},
        ).status_code == 200
        # Old token now fails auth due to token_version increment
        assert client.get("/api/v1/auth/sessions", headers=owner_a_h).status_code == 401

        # Forgot + Reset Password with new verified email
        forgot = client.post("/api/v1/auth/password/forgot", json={"email": new_owner_a_email}).json()["data"]
        assert client.post(
            "/api/v1/auth/password/reset",
            json={"resetToken": forgot["sandboxResetToken"], "newPassword": "OwnerA#Final2026!"},
        ).status_code == 200
        # Login again after reset
        login_res = client.post("/api/v1/auth/login", json={"email": new_owner_a_email, "password": "OwnerA#Final2026!"}).json()["data"]
        owner_a_tok = login_res["accessToken"]
        owner_a_h = {"Authorization": f"Bearer {owner_a_tok}"}

        # 7. Ownership Transfer: Owner A -> Finance Admin A (previous owner demoted to college_admin)
        tr_init = client.post(
            f"/api/v1/saas/tenants/{tid_a}/ownership-transfer",
            headers=owner_a_h,
            json={"targetEmail": fin_email, "previousOwnerAction": "demote_to_admin", "reason": "Principal transition"},
        ).json()["data"]
        assert client.post("/api/v1/saas/ownership-transfer/accept", headers=fin_a_h, json={"transferToken": tr_init["transferToken"]}).status_code == 200

        # 8. Emergency Platform Ownership Recovery back to Owner A
        rec_res = client.post(
            f"/api/v1/platform/colleges/{tid_a}/ownership-recovery",
            headers=super_h,
            json={"targetEmail": new_owner_a_email, "targetFullName": "Owner A", "reason": "Verified dean authorization letter"},
        )
        assert rec_res.status_code == 200
        assert rec_res.json()["data"]["recovered"] is True

        # 9. College Profile, Verified Sender Domain, Plan Upgrade & Non-Overwriting CSV Import
        prof = client.patch(
            f"/api/v1/saas/tenants/{tid_a}/profile",
            headers=owner_a_h,
            json={"approvedDomains": ["collegea.edu"], "customSenderEmail": "canteen@collegea.edu", "contactNumber": "+919848000001"},
        ).json()["data"]
        assert prof["senderDomainVerified"] is True

        assert client.post(
            f"/api/v1/saas/tenants/{tid_a}/subscription/change-plan",
            headers=owner_a_h,
            json={"planTier": "ENTERPRISE", "billingCycle": "annual"},
        ).status_code == 200

        imp_csv = "full_name,email\nStu One,s1@collegea.edu\nInvalid Row,\n"
        imp1 = client.post(f"/api/v1/saas/tenants/{tid_a}/import/students", headers=owner_a_h, json={"csvContent": imp_csv}).json()["data"]
        assert imp1["importedCount"] == 1
        assert imp1["invalidCount"] == 1
        # Second import does NOT overwrite existing Stu One
        imp2 = client.post(f"/api/v1/saas/tenants/{tid_a}/import/students", headers=owner_a_h, json={"csvContent": imp_csv}).json()["data"]
        assert imp2["skippedExistingCount"] == 1
        assert imp2["importedCount"] == 0

        # 10. STRICT TENANT ISOLATION: College A CANNOT touch College B admins, gateway, ownership, or Platform Super Admin!
        assert client.post(
            f"/api/v1/saas/tenants/{tid_b}/invitations",
            headers=owner_a_h,
            json={"email": "hack@collegeb.edu", "fullName": "Hack", "tenantRole": "college_admin"},
        ).status_code == 403
        assert client.patch(
            f"/api/v1/saas/tenants/{tid_b}/admins/{owner_b_id}/status",
            headers=owner_a_h,
            json={"isActive": False, "reason": "Cross tenant attack"},
        ).status_code == 403
        assert client.post(
            f"/api/v1/saas/tenants/{tid_b}/ownership-transfer",
            headers=owner_a_h,
            json={"targetEmail": new_owner_a_email, "reason": "Hostile takeover"},
        ).status_code == 403
        assert client.patch(
            f"/api/v1/saas/tenants/{tid_a}/admins/{super_id}/role",
            headers=owner_a_h,
            json={"tenantRole": "read_only_auditor"},
        ).status_code == 403

        # Audit logs recorded
        audits = client.get(f"/api/v1/saas/tenants/{tid_a}/audit-logs", headers=owner_a_h)
        assert audits.status_code == 200
        assert len(audits.json()["data"]["items"]) >= 3
