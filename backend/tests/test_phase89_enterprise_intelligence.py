"""Comprehensive Integration & Edge-Case Tests for Phase 89:
Enterprise Intelligence & Reliability 3.0 (30 Modules):
- Part 2: Decision Intelligence, Operational Memory + Expiry, Non-Autonomous Multi-Agent Gate, Drift & Compliance Assistant
- Part 3: Smart Utilities, Water Quality Compliance, Fire Drills & Stale Environmental Sensor Detection
- Part 4: Demand Collaboration (No PII), Procurement RFQ/Bids, Risk Scoring, Expired Goods Receiving Block, Provenance & Disruption Sim
- Part 5: Institutional Meal Credits (Atomic Concurrency + Cash Wallet Separation), Loyalty Sim, Journey Funnel, GST Validation & Multi-Outlet Consolidation
- Part 6: Deployment Profiles, Isolated Resilience Check, Verified Recovery Drill, Observability Timeline, SSRF-Protected Webhooks & Config Blueprint Rollback
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from decimal import Decimal
import uuid

from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import (
    Ingredient,
    PlatformSuperAdmin,
    SaasTenant,
    SaasTenantMember,
    SaasTenantProfile,
    Supplier,
    User,
    UserRole,
)
from app.security import create_access_token, hash_password


def _mk_actor(role: UserRole, prefix: str) -> tuple[int, str, str]:
    email = f"{prefix}-{uuid.uuid4().hex[:8]}@aceec.ac.in"
    with SessionLocal() as db:
        u = User(
            full_name=f"{prefix.title()} User",
            email=email,
            password_hash=hash_password("Pass#Phase89Enterprise!"),
            role=role,
            is_active=True,
        )
        db.add(u)
        db.commit()
        db.refresh(u)
        return u.id, email, create_access_token(u, mfa_verified=True)


def _mk_supply_fixtures() -> tuple[int, int]:
    with SessionLocal() as db:
        sup = Supplier(
            name=f"Ace Dairy {uuid.uuid4().hex[:5]}",
            contact_name="Ramesh",
            email=f"sup-{uuid.uuid4().hex[:6]}@vendor.example.org",
            phone="9876543210",
            is_active=True,
        )
        ing = Ingredient(
            name=f"Basmati Rice {uuid.uuid4().hex[:5]}",
            unit="kg",
            current_quantity=Decimal("50.00"),
            reorder_level=Decimal("10.00"),
            cost_per_unit=Decimal("80.00"),
        )
        db.add(sup)
        db.add(ing)
        db.commit()
        db.refresh(sup)
        db.refresh(ing)
        return sup.id, ing.id


def test_part2_ai_decision_memory_agents_drift_and_compliance():
    _, _, admin_tok = _mk_actor(UserRole.ADMIN, "p89admin")
    _, _, staff_tok = _mk_actor(UserRole.STAFF, "p89staff")
    admin_h = {"Authorization": f"Bearer {admin_tok}"}
    staff_h = {"Authorization": f"Bearer {staff_tok}"}

    with TestClient(app) as client:
        # 1. Decision Intelligence Evaluation
        ev = client.post(
            "/api/v1/enterprise/ai/decisions/evaluate",
            headers=admin_h,
            json={
                "actionCategory": "capacity",
                "title": "Added Counter 2 Pre-Pack Station",
                "metricName": "avg_pickup_wait_sec",
                "baselineValue": 120.0,
                "observedValue": 30.0,
            },
        )
        assert ev.status_code == 201, ev.text
        assert ev.json()["data"]["deltaPct"] == -75.0
        lst = client.get("/api/v1/enterprise/ai/decisions", headers=admin_h)
        assert lst.status_code == 200
        assert lst.json()["data"]["liveImpactSnapshot"]["dataMode"] == "empirical_db_only"

        # 2. Operational Memory Creation, Search & Expiry
        m_live = client.post(
            "/api/v1/enterprise/ai/memory",
            headers=staff_h,
            json={
                "category": "rush_playbook",
                "title": "Mid-Day Exam Rush Prepack",
                "summary": "Prepack 250 idli boxes before 12:15 PM on exam days.",
                "tags": ["exam", "rush", "idli"],
                "ttlDays": 60,
            },
        )
        assert m_live.status_code == 201
        m_stale = client.post(
            "/api/v1/enterprise/ai/memory",
            headers=staff_h,
            json={
                "category": "rush_playbook",
                "title": "Old Semester Rush Note",
                "summary": "Deprecated temporary tent rule for exam rush.",
                "tags": ["exam"],
                "ttlDays": -5,
            },
        )
        assert m_stale.status_code == 201 and m_stale.json()["data"]["isStale"] is True

        search = client.get("/api/v1/enterprise/ai/memory?q=exam", headers=staff_h)
        assert search.status_code == 200
        titles = [e["title"] for e in search.json()["data"]["entries"]]
        assert "Mid-Day Exam Rush Prepack" in titles
        assert "Old Semester Rush Note" not in titles

        # 3. Non-Autonomous Multi-Agent Proposals (unauthorized agent blocked, human approval enforced)
        bad_agent = client.post(
            "/api/v1/enterprise/ai/agents/proposals",
            headers=staff_h,
            json={
                "proposingAgent": "rogue_unlisted_agent",
                "proposalType": "price_drop",
                "title": "Free lunch",
                "rationale": "Test unauthorized agent rejection",
            },
        )
        assert bad_agent.status_code == 403
        assert bad_agent.json()["error"]["code"] == "UNAUTHORIZED_AGENT"

        prop = client.post(
            "/api/v1/enterprise/ai/agents/proposals",
            headers=staff_h,
            json={
                "proposingAgent": "procurement_agent",
                "proposalType": "reorder_rfq",
                "title": "Issue Rice RFQ before exam week",
                "rationale": "7-day forecast indicates stock falls below threshold.",
                "estimatedImpactPaise": 250000,
            },
        )
        assert prop.status_code == 201
        pdata = prop.json()["data"]
        assert pdata["autonomousExecution"] is False
        assert pdata["requiresHumanApproval"] is True
        pid = pdata["id"]

        dec = client.post(
            f"/api/v1/enterprise/ai/agents/proposals/{pid}/decide",
            headers=admin_h,
            json={"decision": "approved", "decisionNotes": "Approved after reviewing budget"},
        )
        assert dec.status_code == 200 and dec.json()["data"]["status"] == "approved"
        dup_dec = client.post(
            f"/api/v1/enterprise/ai/agents/proposals/{pid}/decide",
            headers=admin_h,
            json={"decision": "rejected"},
        )
        assert dup_dec.status_code == 409

        # 4. Continuous Learning Drift & Grounded Compliance Assistant
        drift = client.get("/api/v1/enterprise/ai/learning/drift", headers=admin_h)
        assert drift.status_code == 200
        comp = client.post(
            "/api/v1/enterprise/ai/compliance/query",
            headers=staff_h,
            json={"question": "What is our refund policy and food safety water rule?"},
        )
        assert comp.status_code == 200
        policies = [c["policy"] for c in comp.json()["data"]["citations"]]
        assert "Strict No-Refunds Business Rule" in policies
        assert "Food Safety, Expiry Gate & Water Potability" in policies


def test_part3_smart_infrastructure_utilities_water_fire_and_stale_sensors():
    _, _, staff_tok = _mk_actor(UserRole.STAFF, "p89infra")
    staff_h = {"Authorization": f"Bearer {staff_tok}"}

    with TestClient(app) as client:
        ut = client.post(
            "/api/v1/enterprise/infrastructure/utilities",
            headers=staff_h,
            json={"utilityType": "electricity_kwh", "readingValue": 42.5, "costPaise": 38250, "sourceMode": "manual_entry"},
        )
        assert ut.status_code == 201
        usum = client.get("/api/v1/enterprise/infrastructure/utilities/summary", headers=staff_h)
        assert usum.status_code == 200 and usum.json()["data"]["totalUtilityCostPaise"] >= 38250

        w_ok = client.post(
            "/api/v1/enterprise/infrastructure/water-quality",
            headers=staff_h,
            json={"sourcePoint": "RO Drinking Dispenser 1", "tdsPpm": 115, "phLevel": 7.2, "freeChlorinePpm": 0.4},
        )
        assert w_ok.status_code == 201 and w_ok.json()["data"]["isCompliant"] is True

        w_bad = client.post(
            "/api/v1/enterprise/infrastructure/water-quality",
            headers=staff_h,
            json={"sourcePoint": "Kitchen Wash Tap 2", "tdsPpm": 890, "phLevel": 9.3, "freeChlorinePpm": 0.05},
        )
        assert w_bad.status_code == 201 and w_bad.json()["data"]["isCompliant"] is False

        fe = client.post(
            "/api/v1/enterprise/infrastructure/fire-events",
            headers=staff_h,
            json={"zone": "Main Gas Bank", "eventType": "lpg_shutoff_drill", "isDrill": True},
        )
        assert fe.status_code == 201
        eid = fe.json()["data"]["id"]
        assert client.post(f"/api/v1/enterprise/infrastructure/fire-events/{eid}/resolve", headers=staff_h).status_code == 200

        # Sensor staleness detection + threshold breach
        s_stale = client.post(
            "/api/v1/enterprise/infrastructure/sensors",
            headers=staff_h,
            json={
                "sensorCode": "CHILLER-01",
                "sensorType": "cold_storage_temp",
                "readingValue": 11.5,
                "unit": "C",
                "thresholdMin": 0.0,
                "thresholdMax": 5.0,
                "ageMinutesOverride": 45,
            },
        )
        assert s_stale.status_code == 201
        sdata = s_stale.json()["data"]
        assert sdata["isStale"] is True
        assert sdata["outOfRange"] is True

        ov = client.get("/api/v1/enterprise/infrastructure/overview", headers=staff_h)
        assert ov.status_code == 200
        assert ov.json()["data"]["staleSensorCount"] >= 1
        assert ov.json()["data"]["waterComplianceAlerts"] >= 1


def test_part4_supply_chain_rfq_risk_expired_receiving_block_and_provenance():
    _, _, admin_tok = _mk_actor(UserRole.ADMIN, "p89supadm")
    _, _, staff_tok = _mk_actor(UserRole.STAFF, "p89supstf")
    admin_h = {"Authorization": f"Bearer {admin_tok}"}
    staff_h = {"Authorization": f"Bearer {staff_tok}"}
    sup_id, ing_id = _mk_supply_fixtures()

    with TestClient(app) as client:
        # 1. Demand collaboration excludes student PII
        dc = client.get("/api/v1/enterprise/supply-chain/demand-collaboration", headers=admin_h)
        assert dc.status_code == 200 and dc.json()["data"]["piiExcluded"] is True

        # 2. RFQ -> Bid -> Award + Closed Rejection
        rfq = client.post(
            "/api/v1/enterprise/supply-chain/rfqs",
            headers=admin_h,
            json={"ingredientId": ing_id, "title": "Monthly Rice Tender", "requiredQuantity": 100.0, "unit": "kg", "targetMaxUnitPaise": 8500},
        )
        assert rfq.status_code == 201
        rfq_id = rfq.json()["data"]["id"]

        bid = client.post(
            f"/api/v1/enterprise/supply-chain/rfqs/{rfq_id}/bids",
            headers=admin_h,
            json={"supplierId": sup_id, "unitPricePaise": 7900, "leadTimeDays": 1},
        )
        assert bid.status_code == 201
        bid_id = bid.json()["data"]["id"]

        awd = client.post(f"/api/v1/enterprise/supply-chain/rfqs/{rfq_id}/award/{bid_id}", headers=admin_h)
        assert awd.status_code == 200 and awd.json()["data"]["status"] == "awarded"

        # 3. Expired batch strictly rejected without adding stock!
        exp_rec = client.post(
            "/api/v1/enterprise/supply-chain/receiving/inspect",
            headers=staff_h,
            json={
                "ingredientId": ing_id,
                "supplierId": sup_id,
                "batchCode": "EXP-RICE-01",
                "receivedQuantity": 20.0,
                "expiryDate": (date.today() - timedelta(days=2)).isoformat(),
                "packagingIntact": True,
            },
        )
        assert exp_rec.status_code == 201
        edata = exp_rec.json()["data"]
        assert edata["inspectionStatus"] == "rejected_expired"
        assert edata["inventoryUpdated"] is False
        assert edata["ingredientCurrentStock"] == 50.0

        # Fresh batch accepted & increments stock
        ok_rec = client.post(
            "/api/v1/enterprise/supply-chain/receiving/inspect",
            headers=staff_h,
            json={
                "ingredientId": ing_id,
                "supplierId": sup_id,
                "batchCode": "FRESH-RICE-02",
                "receivedQuantity": 25.0,
                "expiryDate": (date.today() + timedelta(days=60)).isoformat(),
                "packagingIntact": True,
            },
        )
        assert ok_rec.status_code == 201
        assert ok_rec.json()["data"]["inspectionStatus"] == "accepted"
        assert ok_rec.json()["data"]["inventoryUpdated"] is True
        assert ok_rec.json()["data"]["ingredientCurrentStock"] == 75.0

        # 4. Provenance, Risk & Non-mutating Disruption Sim
        prov = client.get("/api/v1/enterprise/supply-chain/provenance/FRESH-RICE-02", headers=staff_h)
        assert prov.status_code == 200 and len(prov.json()["data"]["receivingInspections"]) == 1

        risk = client.get(f"/api/v1/enterprise/supply-chain/suppliers/{sup_id}/risk", headers=admin_h)
        assert risk.status_code == 200 and risk.json()["data"]["metrics"]["rejectedDeliveries"] == 1

        sim = client.post(
            "/api/v1/enterprise/supply-chain/disruption-simulation",
            headers=admin_h,
            json={"ingredientId": ing_id, "outageDays": 30, "demandMultiplierPct": 150},
        )
        assert sim.status_code == 200 and sim.json()["data"]["simulation"] is True


def test_part5_meal_credits_concurrency_cash_wallet_isolation_gst_and_finance():
    _, _, admin_tok = _mk_actor(UserRole.ADMIN, "p89finadm")
    stu_id, _, stu_tok = _mk_actor(UserRole.STUDENT, "p89finstu")
    admin_h = {"Authorization": f"Bearer {admin_tok}"}
    stu_h = {"Authorization": f"Bearer {stu_tok}"}
    sup_id, _ = _mk_supply_fixtures()

    with TestClient(app) as client:
        # 1. Institutional Meal Credit separate from Cash Wallet + Atomic Concurrent Redemption
        alloc = client.post(
            "/api/v1/enterprise/finance/meal-credits",
            headers=admin_h,
            json={
                "userId": stu_id,
                "departmentName": "CSE Scholarship Cell",
                "programName": "Merit Lunch Grant",
                "allocatedPaise": 15000,
                "expiresOn": (date.today() + timedelta(days=30)).isoformat(),
            },
        )
        assert alloc.status_code == 201
        alloc_id = alloc.json()["data"]["id"]
        assert alloc.json()["data"]["separateFromCashWallet"] is True

        # Cash wallet remains 0 paise!
        wal = client.get("/api/v1/wallet/me", headers=stu_h)
        assert wal.status_code == 200 and wal.json()["data"]["balancePaise"] == 0

        # 5 concurrent ₹60 (6000p) redemptions against ₹150 (15000p) -> exactly 2 succeed, 3 rejected!
        def try_redeem(i: int) -> int:
            r = client.post(
                "/api/v1/enterprise/finance/meal-credits/redeem",
                headers=stu_h,
                json={"allocationId": alloc_id, "amountPaise": 6000, "idempotencyKey": f"cred-{alloc_id}-{i}"},
            )
            return r.status_code

        with ThreadPoolExecutor(max_workers=5) as ex:
            codes = list(ex.map(try_redeem, range(5)))
        assert codes.count(200) == 2, codes
        assert codes.count(409) == 3, codes

        # 2. Loyalty Sim, Customer Journey, Scenario Forecast, GST Check & Multi-Outlet Consolidation
        lsim = client.post("/api/v1/enterprise/finance/loyalty-simulator", headers=admin_h, json={"cashbackRatePct": 5.0})
        assert lsim.status_code == 200 and lsim.json()["data"]["simulation"] is True

        journey = client.get("/api/v1/enterprise/analytics/customer-journey", headers=admin_h)
        assert journey.status_code == 200 and len(journey.json()["data"]["funnel"]) == 4

        scen = client.get("/api/v1/enterprise/finance/scenario-forecast?horizonDays=30", headers=admin_h)
        assert scen.status_code == 200 and set(scen.json()["data"]["scenarios"].keys()) == {"conservative", "expected", "optimistic"}

        gst_ok = client.post(
            "/api/v1/enterprise/finance/tax/validate-invoice",
            headers=admin_h,
            json={
                "gstin": "36AABCU9603R1ZM",
                "invoiceNumber": f"INV-{uuid.uuid4().hex[:6]}",
                "supplierId": sup_id,
                "subtotalPaise": 10000,
                "taxPaise": 500,
                "totalPaise": 10500,
            },
        )
        assert gst_ok.status_code == 200 and gst_ok.json()["data"]["valid"] is True

        gst_bad = client.post(
            "/api/v1/enterprise/finance/tax/validate-invoice",
            headers=admin_h,
            json={
                "gstin": "BADGSTIN1234567",
                "invoiceNumber": "INV-MISMATCH",
                "supplierId": sup_id,
                "subtotalPaise": 10000,
                "taxPaise": 500,
                "totalPaise": 99999,
            },
        )
        assert gst_bad.status_code == 200 and gst_bad.json()["data"]["valid"] is False

        consol = client.get("/api/v1/enterprise/finance/multi-outlet-consolidation", headers=admin_h)
        assert consol.status_code == 200 and consol.json()["data"]["refundsIssuedPaise"] == 0


def test_part6_platform_engineering_ssrf_recovery_drill_blueprints_and_tenant_isolation():
    super_id, _, super_tok = _mk_actor(UserRole.ADMIN, "p89super")
    other_id, _, other_tok = _mk_actor(UserRole.ADMIN, "p89tenantother")
    super_h = {"Authorization": f"Bearer {super_tok}"}
    other_h = {"Authorization": f"Bearer {other_tok}"}

    # Provision an isolated college tenant & explicit PlatformSuperAdmin separation
    with SessionLocal() as db:
        db.add(PlatformSuperAdmin(user_id=super_id, privilege_level="platform_owner"))
        t = SaasTenant(
            college_name=f"P89 College {uuid.uuid4().hex[:5]}",
            tenant_slug=f"p89-col-{uuid.uuid4().hex[:6]}",
            plan_tier="ENTERPRISE",
            status="active",
        )
        db.add(t)
        db.flush()
        db.add(SaasTenantProfile(tenant_id=t.id, tenant_code=f"CD{uuid.uuid4().hex[:6].upper()}", canteen_settings_json='{"parcelFeePaise":200}'))
        db.commit()
        tenant_id = t.id

    with TestClient(app) as client:
        # 1. Deployment profiles & Isolated resilience check
        dp = client.get("/api/v1/enterprise/platform/deployment-profiles", headers=super_h)
        assert dp.status_code == 200 and len(dp.json()["data"]["profiles"]) >= 4

        rc = client.post("/api/v1/enterprise/platform/resilience-check", headers=super_h)
        assert rc.status_code == 200 and rc.json()["data"]["destructiveTrafficImpact"] is False

        # 2. Encrypted Database Recovery Drill + Correlated Observability Timeline
        drill = client.post("/api/v1/enterprise/platform/recovery-drill", headers=super_h, json={"notes": "Phase 89 automated drill"})
        assert drill.status_code == 201 and drill.json()["data"]["status"] == "passed"
        assert drill.json()["data"]["verifiedTableCounts"]["users"] >= 1

        tl = client.get("/api/v1/enterprise/platform/observability/timeline", headers=super_h)
        assert tl.status_code == 200 and isinstance(tl.json()["data"]["timeline"], list)

        # 3. Developer Portal Webhook SSRF Blocking
        ssrf1 = client.post(
            "/api/v1/developer/webhooks/subscribe",
            headers=super_h,
            json={"name": "Bad Loopback", "targetUrl": "http://127.0.0.1:8000/internal", "eventTypes": ["order.paid"], "signingSecret": "1234567890abcdef1234"},
        )
        assert ssrf1.status_code == 422

        ssrf2 = client.post(
            "/api/v1/developer/webhooks/subscribe",
            headers=super_h,
            json={"name": "AWS Metadata", "targetUrl": "http://169.254.169.254/latest/meta-data", "eventTypes": ["order.paid"], "signingSecret": "1234567890abcdef1234"},
        )
        assert ssrf2.status_code == 422

        wh_ok = client.post(
            "/api/v1/developer/webhooks/subscribe",
            headers=super_h,
            json={"name": "ERP Sink", "targetUrl": "https://erp.example.org/webhooks/canteen", "eventTypes": ["order.paid"], "signingSecret": "1234567890abcdef1234"},
        )
        assert wh_ok.status_code == 201
        assert client.get("/api/v1/developer/portal", headers=super_h).json()["data"]["ssrfProtectionActive"] is True

        # 4. Versioned Tenant Config Blueprint Apply + Rollback + Cross-Tenant Isolation
        bp = client.post(
            "/api/v1/enterprise/platform/blueprints",
            headers=super_h,
            json={"name": f"exam-rush-{uuid.uuid4().hex[:5]}", "version": "1.0.0", "config": {"parcelFeePaise": 500, "prepackEnabled": True}},
        )
        assert bp.status_code == 201
        bp_id = bp.json()["data"]["id"]

        dep = client.post(f"/api/v1/enterprise/platform/blueprints/{bp_id}/apply/{tenant_id}", headers=super_h)
        assert dep.status_code == 200 and dep.json()["data"]["liveOrdersAffected"] is False
        dep_id = dep.json()["data"]["deploymentId"]

        rb = client.post(f"/api/v1/enterprise/platform/blueprints/deployments/{dep_id}/rollback", headers=super_h)
        assert rb.status_code == 200 and rb.json()["data"]["status"] == "rolled_back"
        assert rb.json()["data"]["restoredConfig"]["parcelFeePaise"] == 200

        # Non-member cannot read another tenant's sensor/infra/decision data
        iso = client.get(
            f"/api/v1/enterprise/infrastructure/overview?tenantId={tenant_id}",
            headers=other_h,
        )
        assert iso.status_code == 403
        assert iso.json()["error"]["code"] == "TENANT_ISOLATION_VIOLATION"
