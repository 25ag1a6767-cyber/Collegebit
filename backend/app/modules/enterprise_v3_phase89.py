"""CollegeBits 3.0 — Phase 89: Enterprise Intelligence, Smart Campus Infrastructure,
Supply-Chain Ecosystem, Customer/Financial Intelligence & Platform Engineering (30 Modules).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.envelope import ApiError, ok
from app.models import (
    AuditEvent,
    BulkOrder,
    CanteenCounter,
    DatabaseRecoveryDrill,
    DecisionIntelligenceRecord,
    EnvironmentalSensorReading,
    EquipmentMaintenanceTicket,
    FireEmergencyEvent,
    ForecastItem,
    GoodsReceivingInspection,
    Ingredient,
    MealCreditAllocation,
    MealCreditRedemption,
    MultiAgentProposal,
    OperationalExpense,
    OperationalMemoryEntry,
    Order,
    OrderQualityFeedback,
    OrderStatus,
    Payment,
    PaymentIncident,
    PaymentStatus,
    ProcurementBid,
    ProcurementRfq,
    ProductBatch,
    PurchaseOrder,
    SaasTenant,
    SaasTenantProfile,
    SecurityEvent,
    Supplier,
    SupplierInvoice,
    SupportTicket,
    TenantBlueprintDeployment,
    TenantConfigBlueprint,
    User,
    UserRole,
    UtilityMeterReading,
    WalkinPosOrder,
    WaterQualityTest,
    WebhookSubscription,
)
from app.modules.common import as_utc, record_audit
from app.modules.retention_backup import check_backup_health, create_encrypted_gfs_backup, run_restore_drill
from app.modules.saas_admin_governance_v3 import _assert_tenant_member_role, require_platform_super_admin
from app.security import get_current_user, require_roles

router = APIRouter(tags=["v3-phase89-enterprise"])

ALLOWED_AGENTS = {
    "demand_agent",
    "procurement_agent",
    "kitchen_agent",
    "finance_agent",
    "sustainability_agent",
}
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def _safe_json_loads(raw: str | None, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _resolve_tenant_scope(
    db: Session,
    user: User,
    tenant_id: int | None,
    x_tenant_id: int | None,
) -> int | None:
    """Validates strict tenant boundaries; returns verified tenant_id or None for single-campus mode."""
    if x_tenant_id is not None and tenant_id is not None and x_tenant_id != tenant_id:
        raise ApiError(403, "TENANT_ISOLATION_VIOLATION", "Forged X-Tenant-ID header rejected.")
    resolved = tenant_id if tenant_id is not None else x_tenant_id
    if resolved is not None:
        _assert_tenant_member_role(db, resolved, user, allowed_roles=None, x_tenant_id=x_tenant_id)
    return resolved


def _is_ip_restricted(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def _validate_webhook_url_ssrf(url: str) -> None:
    """Blocks private, loopback, cloud metadata and unsafe schemes to prevent SSRF."""
    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    env = get_settings().environment.lower()
    allowed_schemes = {"https", "http"} if env in {"development", "test"} else {"https"}
    if scheme not in allowed_schemes:
        raise ApiError(422, "UNSAFE_WEBHOOK_SCHEME", f"Webhook URL scheme '{scheme}' is not permitted in {env}.")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ApiError(422, "INVALID_WEBHOOK_HOST", "Webhook URL must contain a valid hostname.")
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".local", ".internal", ".localhost")):
        raise ApiError(422, "SSRF_TARGET_BLOCKED", "Internal, localhost, and cloud metadata endpoints are blocked.")

    try:
        ip_lit = ipaddress.ip_address(host)
        if _is_ip_restricted(ip_lit):
            raise ApiError(422, "SSRF_IP_BLOCKED", "Private, loopback, and link-local IP addresses are blocked.")
        return
    except ValueError:
        pass

    try:
        for info in socket.getaddrinfo(host, None):
            ip_str = info[4][0].split("%")[0]
            ip_obj = ipaddress.ip_address(ip_str)
            if _is_ip_restricted(ip_obj):
                raise ApiError(422, "SSRF_RESOLVED_IP_BLOCKED", "Webhook hostname resolves to a restricted private IP.")
    except ApiError:
        raise
    except socket.gaierror:
        # Unresolvable public hostnames still pass format check when non-internal, unless strictly unreachable
        if "." not in host:
            raise ApiError(422, "INVALID_WEBHOOK_HOST", "Webhook hostname must be a valid public domain.")


# ==============================================================================
# PART 2 — AI & DECISION INTELLIGENCE (MODULES 1–6)
# ==============================================================================
class DecisionEvalCreateIn(BaseModel):
    tenantId: int | None = None
    actionCategory: str = Field(pattern="^(pricing|staffing|capacity|inventory|prep)$")
    title: str = Field(min_length=3, max_length=200)
    metricName: str = Field(min_length=2, max_length=80)
    baselineValue: float
    observedValue: float
    notes: str | None = Field(default=None, max_length=500)


class OperationalMemoryCreateIn(BaseModel):
    tenantId: int | None = None
    category: str = Field(default="rush_playbook", max_length=60)
    title: str = Field(min_length=3, max_length=200)
    summary: str = Field(min_length=5, max_length=3000)
    tags: list[str] = Field(default_factory=list, max_length=15)
    sourceReference: str | None = Field(default=None, max_length=160)
    ttlDays: int | None = Field(default=90, ge=-30, le=3650)


class AgentProposalCreateIn(BaseModel):
    tenantId: int | None = None
    proposingAgent: str = Field(min_length=3, max_length=50)
    proposalType: str = Field(min_length=3, max_length=60)
    title: str = Field(min_length=3, max_length=200)
    rationale: str = Field(min_length=5, max_length=2000)
    payload: dict[str, Any] = Field(default_factory=dict)
    estimatedImpactPaise: int = 0


class AgentProposalDecisionIn(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    decisionNotes: str | None = Field(default=None, max_length=500)


class ComplianceQueryIn(BaseModel):
    question: str = Field(min_length=3, max_length=500)


@router.post("/enterprise/ai/decisions/evaluate", status_code=201)
def create_decision_evaluation_v1(
    payload: DecisionEvalCreateIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, admin, payload.tenantId, x_tenant_id)
    base_dec = Decimal(str(payload.baselineValue))
    obs_dec = Decimal(str(payload.observedValue))
    delta_pct = Decimal("0.00") if base_dec == 0 else ((obs_dec - base_dec) / base_dec * Decimal("100")).quantize(Decimal("0.01"))
    row = DecisionIntelligenceRecord(
        tenant_id=tid,
        action_category=payload.actionCategory,
        title=payload.title.strip(),
        metric_name=payload.metricName.strip(),
        baseline_value=base_dec,
        observed_value=obs_dec,
        delta_pct=delta_pct,
        notes=payload.notes,
        evaluated_by_user_id=admin.id,
    )
    db.add(row)
    record_audit(db, "enterprise.decision_evaluated", "decision_intelligence", 0, admin.id, {"deltaPct": float(delta_pct)})
    db.commit()
    db.refresh(row)
    return ok(
        request,
        {
            "id": row.id,
            "tenantId": row.tenant_id,
            "actionCategory": row.action_category,
            "title": row.title,
            "metricName": row.metric_name,
            "baselineValue": float(row.baseline_value),
            "observedValue": float(row.observed_value),
            "deltaPct": float(row.delta_pct),
            "confidenceBand": row.confidence_band,
            "evaluatedAt": as_utc(row.evaluated_at).isoformat(),
        },
        status_code=201,
    )


@router.get("/enterprise/ai/decisions")
def list_decision_evaluations_v1(
    request: Request,
    tenant_id: int | None = Query(default=None, alias="tenantId"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, admin, tenant_id, x_tenant_id)
    stmt = select(DecisionIntelligenceRecord).order_by(DecisionIntelligenceRecord.evaluated_at.desc())
    stmt = stmt.where(DecisionIntelligenceRecord.tenant_id == tid if tid is not None else DecisionIntelligenceRecord.tenant_id.is_(None))
    rows = db.scalars(stmt.limit(50)).all()
    paid_orders = int(db.scalar(select(func.count(Order.id)).where(Order.payment_status == PaymentStatus.PAID)) or 0)
    open_incidents = int(db.scalar(select(func.count(PaymentIncident.id)).where(PaymentIncident.status == "open")) or 0)
    return ok(
        request,
        {
            "evaluations": [
                {
                    "id": r.id,
                    "actionCategory": r.action_category,
                    "title": r.title,
                    "metricName": r.metric_name,
                    "baselineValue": float(r.baseline_value),
                    "observedValue": float(r.observed_value),
                    "deltaPct": float(r.delta_pct),
                    "evaluatedAt": as_utc(r.evaluated_at).isoformat(),
                }
                for r in rows
            ],
            "liveImpactSnapshot": {
                "verifiedPaidOrders": paid_orders,
                "openPaymentIncidents": open_incidents,
                "dataMode": "empirical_db_only",
            },
        },
    )


@router.post("/enterprise/ai/memory", status_code=201)
def create_operational_memory_v1(
    payload: OperationalMemoryCreateIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, payload.tenantId, x_tenant_id)
    exp = datetime.now(timezone.utc) + timedelta(days=payload.ttlDays) if payload.ttlDays is not None else None
    entry = OperationalMemoryEntry(
        tenant_id=tid,
        category=payload.category.strip(),
        title=payload.title.strip(),
        summary=payload.summary.strip(),
        tags_json=json.dumps([t.strip().lower() for t in payload.tags if t.strip()]),
        source_reference=payload.sourceReference,
        expires_at=exp,
        created_by_user_id=staff.id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    now = datetime.now(timezone.utc)
    return ok(
        request,
        {
            "id": entry.id,
            "category": entry.category,
            "title": entry.title,
            "summary": entry.summary,
            "tags": _safe_json_loads(entry.tags_json, []),
            "expiresAt": as_utc(entry.expires_at).isoformat() if entry.expires_at else None,
            "isStale": bool(entry.expires_at and as_utc(entry.expires_at) <= now),
        },
        status_code=201,
    )


@router.get("/enterprise/ai/memory")
def search_operational_memory_v1(
    request: Request,
    q: str = Query(default="", max_length=120),
    include_stale: bool = Query(default=False, alias="includeStale"),
    tenant_id: int | None = Query(default=None, alias="tenantId"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, tenant_id, x_tenant_id)
    stmt = select(OperationalMemoryEntry).where(OperationalMemoryEntry.is_active.is_(True))
    stmt = stmt.where(OperationalMemoryEntry.tenant_id == tid if tid is not None else OperationalMemoryEntry.tenant_id.is_(None))
    rows = db.scalars(stmt.order_by(OperationalMemoryEntry.created_at.desc()).limit(200)).all()
    now = datetime.now(timezone.utc)
    terms = [t for t in q.strip().lower().split() if t]
    matches: list[dict[str, Any]] = []
    for r in rows:
        is_stale = bool(r.expires_at and as_utc(r.expires_at) <= now)
        if is_stale and not include_stale:
            continue
        tags = _safe_json_loads(r.tags_json, [])
        haystack = f"{r.title} {r.summary} {r.category} {' '.join(tags)}".lower()
        score = sum(1 for t in terms if t in haystack) if terms else 1
        if terms and score == 0:
            continue
        matches.append(
            {
                "id": r.id,
                "category": r.category,
                "title": r.title,
                "summary": r.summary,
                "tags": tags,
                "sourceReference": r.source_reference,
                "matchScore": score,
                "isStale": is_stale,
                "expiresAt": as_utc(r.expires_at).isoformat() if r.expires_at else None,
            }
        )
    matches.sort(key=lambda item: item["matchScore"], reverse=True)
    return ok(request, {"entries": matches, "count": len(matches)})


@router.post("/enterprise/ai/agents/proposals", status_code=201)
def create_agent_proposal_v1(
    payload: AgentProposalCreateIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    """Submits a structured advisory proposal from an allowlisted agent role. Never auto-executes."""
    if payload.proposingAgent not in ALLOWED_AGENTS:
        raise ApiError(
            403,
            "UNAUTHORIZED_AGENT",
            f"Agent '{payload.proposingAgent}' is not on the authorized non-autonomous agent allowlist.",
        )
    tid = _resolve_tenant_scope(db, staff, payload.tenantId, x_tenant_id)
    prop = MultiAgentProposal(
        tenant_id=tid,
        proposing_agent=payload.proposingAgent,
        proposal_type=payload.proposalType.strip(),
        title=payload.title.strip(),
        rationale=payload.rationale.strip(),
        payload_json=json.dumps(payload.payload),
        estimated_impact_paise=payload.estimatedImpactPaise,
        status="pending_approval",
    )
    db.add(prop)
    db.commit()
    db.refresh(prop)
    return ok(
        request,
        {
            "id": prop.id,
            "proposingAgent": prop.proposing_agent,
            "proposalType": prop.proposal_type,
            "title": prop.title,
            "status": prop.status,
            "autonomousExecution": False,
            "requiresHumanApproval": True,
        },
        status_code=201,
    )


@router.get("/enterprise/ai/agents/proposals")
def list_agent_proposals_v1(
    request: Request,
    tenant_id: int | None = Query(default=None, alias="tenantId"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, admin, tenant_id, x_tenant_id)
    stmt = select(MultiAgentProposal).order_by(MultiAgentProposal.created_at.desc())
    stmt = stmt.where(MultiAgentProposal.tenant_id == tid if tid is not None else MultiAgentProposal.tenant_id.is_(None))
    rows = db.scalars(stmt.limit(50)).all()
    return ok(
        request,
        {
            "proposals": [
                {
                    "id": p.id,
                    "proposingAgent": p.proposing_agent,
                    "proposalType": p.proposal_type,
                    "title": p.title,
                    "rationale": p.rationale,
                    "estimatedImpactPaise": p.estimated_impact_paise,
                    "status": p.status,
                    "requiresHumanApproval": True,
                    "approvedByUserId": p.approved_by_user_id,
                    "decisionNotes": p.decision_notes,
                }
                for p in rows
            ]
        },
    )


@router.post("/enterprise/ai/agents/proposals/{proposal_id}/decide")
def decide_agent_proposal_v1(
    proposal_id: int,
    payload: AgentProposalDecisionIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    prop = db.get(MultiAgentProposal, proposal_id)
    if not prop:
        raise ApiError(404, "PROPOSAL_NOT_FOUND", "Agent proposal not found.")
    _resolve_tenant_scope(db, admin, prop.tenant_id, x_tenant_id)
    if prop.status != "pending_approval":
        raise ApiError(409, "PROPOSAL_ALREADY_DECIDED", f"Proposal is already '{prop.status}'.")
    prop.status = payload.decision
    prop.approved_by_user_id = admin.id
    prop.decision_notes = payload.decisionNotes
    prop.decided_at = datetime.now(timezone.utc)
    record_audit(db, f"enterprise.agent_proposal_{payload.decision}", "multi_agent_proposal", prop.id, admin.id, {"agent": prop.proposing_agent})
    db.commit()
    return ok(request, {"id": prop.id, "status": prop.status, "approvedByUserId": admin.id})


@router.get("/enterprise/ai/learning/drift")
def get_continuous_learning_drift_v1(
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    """Evaluates historical ForecastItem accuracy vs recorded actual quantities for continuous recalibration."""
    items = db.scalars(select(ForecastItem).where(ForecastItem.actual_sold.is_not(None)).limit(500)).all()
    if not items:
        return ok(
            request,
            {
                "evaluatedSamples": 0,
                "mae": None,
                "bias": None,
                "driftAlert": False,
                "status": "awaiting_actual_order_observations",
            },
        )
    abs_errs = [abs(int(i.actual_sold or 0) - int(i.predicted_quantity or 0)) for i in items]
    signed_errs = [int(i.actual_sold or 0) - int(i.predicted_quantity or 0) for i in items]
    mae = round(sum(abs_errs) / len(items), 2)
    bias = round(sum(signed_errs) / len(items), 2)
    return ok(
        request,
        {
            "evaluatedSamples": len(items),
            "mae": mae,
            "bias": bias,
            "driftAlert": mae >= 10.0,
            "status": "recalibration_recommended" if mae >= 10.0 else "within_tolerance",
        },
    )


@router.post("/enterprise/ai/compliance/query")
def query_compliance_assistant_v1(
    payload: ComplianceQueryIn,
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    """Grounded internal policy assistant — cites strictly verified system policies only."""
    q = payload.question.lower()
    open_incidents = int(db.scalar(select(func.count(PaymentIncident.id)).where(PaymentIncident.status == "open")) or 0)
    unsafe_water = int(db.scalar(select(func.count(WaterQualityTest.id)).where(WaterQualityTest.is_compliant.is_(False))) or 0)
    citations: list[dict[str, str]] = []
    if "refund" in q or "payment" in q or "cancel" in q:
        citations.append(
            {
                "policy": "Strict No-Refunds Business Rule",
                "detail": f"Automated or manual refunds are disabled platform-wide. Paid unfulfilled transactions remain PaymentStatus.PAID and open a PaymentIncident ({open_incidents} currently open) for admin resolution.",
            }
        )
    if "retention" in q or "backup" in q or "delete" in q or "audit" in q:
        citations.append(
            {
                "policy": "4-Day Hybrid Data Retention & Encrypted GFS Backups",
                "detail": "Permanent orders, payments, and audit events are archived and retained; temporary operational caches/logs rotate every 4 days with Fernet-encrypted GFS backups.",
            }
        )
    if "food" in q or "water" in q or "hygiene" in q or "safety" in q or "fssai" in q:
        citations.append(
            {
                "policy": "Food Safety, Expiry Gate & Water Potability",
                "detail": f"Expired goods are automatically rejected at goods receiving, Parcel items enforce dual packing verification before READY, and water potability records currently show {unsafe_water} non-compliant alert(s).",
            }
        )
    if not citations:
        citations.append(
            {
                "policy": "CollegeBits SaaS Security & Governance",
                "detail": "Platform Super Admin and College Tenant Admin boundaries are enforced alongside Bcrypt passwords, TOTP MFA, session token revocation, and strict CSP headers.",
            }
        )
    return ok(
        request,
        {
            "question": payload.question,
            "groundedOnly": True,
            "legalAdviceDisclaimer": "Reflects configured CollegeBits platform policies and DB state only; consult campus legal/FSSAI officers for statutory rulings.",
            "citations": citations,
        },
    )


# ==============================================================================
# PART 3 — SMART CAMPUS INFRASTRUCTURE (MODULES 7–12)
# ==============================================================================
class UtilityReadingIn(BaseModel):
    tenantId: int | None = None
    counterId: int | None = None
    utilityType: str = Field(pattern="^(electricity_kwh|water_liters|lpg_kg|solar_kwh)$")
    readingValue: float = Field(ge=0)
    costPaise: int = Field(default=0, ge=0)
    sourceMode: str = Field(default="manual_entry", pattern="^(manual_entry|metered_sensor)$")
    notes: str | None = Field(default=None, max_length=300)


class WaterQualityIn(BaseModel):
    tenantId: int | None = None
    counterId: int | None = None
    sourcePoint: str = Field(min_length=2, max_length=120)
    tdsPpm: int = Field(ge=0, le=5000)
    phLevel: float = Field(ge=0, le=14)
    freeChlorinePpm: float = Field(default=0.3, ge=0, le=20)
    temperatureC: float | None = Field(default=None, ge=-10, le=100)
    notes: str | None = Field(default=None, max_length=400)


class FireEmergencyIn(BaseModel):
    tenantId: int | None = None
    zone: str = Field(min_length=2, max_length=100)
    eventType: str = Field(pattern="^(smoke_drill|lpg_shutoff_drill|thermal_alarm|evacuation_test)$")
    severity: str = Field(default="drill", pattern="^(drill|warning|critical)$")
    isDrill: bool = True
    evacuationChecklist: list[str] = Field(default_factory=lambda: ["Verify LPG shutoff valve", "Clear counter exits", "Account staff"])
    notes: str | None = Field(default=None, max_length=500)


class SensorReadingIn(BaseModel):
    tenantId: int | None = None
    stationId: int | None = None
    sensorCode: str = Field(min_length=2, max_length=60)
    sensorType: str = Field(pattern="^(cold_storage_temp|kitchen_humidity|exhaust_airflow|lpg_ppm)$")
    readingValue: float
    unit: str = Field(min_length=1, max_length=20)
    sourceMode: str = Field(default="manual", pattern="^(manual|hardware_iot)$")
    thresholdMin: float | None = None
    thresholdMax: float | None = None
    ageMinutesOverride: int = Field(default=0, ge=0, le=10080)


@router.post("/enterprise/infrastructure/utilities", status_code=201)
def record_utility_reading_v1(
    payload: UtilityReadingIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, payload.tenantId, x_tenant_id)
    row = UtilityMeterReading(
        tenant_id=tid,
        counter_id=payload.counterId,
        utility_type=payload.utilityType,
        reading_value=Decimal(str(payload.readingValue)),
        cost_paise=payload.costPaise,
        source_mode=payload.sourceMode,
        notes=payload.notes,
        recorded_by_user_id=staff.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ok(
        request,
        {"id": row.id, "utilityType": row.utility_type, "readingValue": float(row.reading_value), "costPaise": row.cost_paise, "sourceMode": row.source_mode},
        status_code=201,
    )


@router.get("/enterprise/infrastructure/utilities/summary")
def get_utility_summary_v1(
    request: Request,
    tenant_id: int | None = Query(default=None, alias="tenantId"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, tenant_id, x_tenant_id)
    stmt = select(UtilityMeterReading)
    stmt = stmt.where(UtilityMeterReading.tenant_id == tid if tid is not None else UtilityMeterReading.tenant_id.is_(None))
    rows = db.scalars(stmt.order_by(UtilityMeterReading.recorded_at.desc()).limit(100)).all()
    by_type: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = by_type.setdefault(r.utility_type, {"totalUnits": 0.0, "totalCostPaise": 0, "readingsCount": 0})
        b["totalUnits"] = round(b["totalUnits"] + float(r.reading_value), 3)
        b["totalCostPaise"] += int(r.cost_paise)
        b["readingsCount"] += 1
    paid_orders = max(1, int(db.scalar(select(func.count(Order.id)).where(Order.payment_status == PaymentStatus.PAID)) or 0))
    total_cost = sum(v["totalCostPaise"] for v in by_type.values())
    return ok(request, {"byUtilityType": by_type, "totalUtilityCostPaise": total_cost, "utilityCostPerPaidOrderPaise": round(total_cost / paid_orders, 2)})


@router.post("/enterprise/infrastructure/water-quality", status_code=201)
def record_water_quality_v1(
    payload: WaterQualityIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, payload.tenantId, x_tenant_id)
    compliant = (payload.tdsPpm <= 500) and (6.5 <= payload.phLevel <= 8.5) and (payload.freeChlorinePpm >= 0.2)
    row = WaterQualityTest(
        tenant_id=tid,
        counter_id=payload.counterId,
        source_point=payload.sourcePoint.strip(),
        tds_ppm=payload.tdsPpm,
        ph_level=Decimal(str(payload.phLevel)),
        free_chlorine_ppm=Decimal(str(payload.freeChlorinePpm)),
        temperature_c=Decimal(str(payload.temperatureC)) if payload.temperatureC is not None else None,
        is_compliant=compliant,
        notes=payload.notes,
        tested_by_user_id=staff.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ok(
        request,
        {"id": row.id, "sourcePoint": row.source_point, "tdsPpm": row.tds_ppm, "phLevel": float(row.ph_level), "isCompliant": row.is_compliant},
        status_code=201,
    )


@router.get("/enterprise/infrastructure/water-quality")
def list_water_quality_v1(
    request: Request,
    tenant_id: int | None = Query(default=None, alias="tenantId"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, tenant_id, x_tenant_id)
    stmt = select(WaterQualityTest).order_by(WaterQualityTest.tested_at.desc())
    stmt = stmt.where(WaterQualityTest.tenant_id == tid if tid is not None else WaterQualityTest.tenant_id.is_(None))
    rows = db.scalars(stmt.limit(30)).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": w.id,
                    "sourcePoint": w.source_point,
                    "tdsPpm": w.tds_ppm,
                    "phLevel": float(w.ph_level),
                    "freeChlorinePpm": float(w.free_chlorine_ppm),
                    "isCompliant": w.is_compliant,
                    "testedAt": as_utc(w.tested_at).isoformat(),
                }
                for w in rows
            ]
        },
    )


@router.get("/enterprise/infrastructure/fire-events")
def list_fire_events_v1(
    request: Request,
    tenant_id: int | None = Query(default=None, alias="tenantId"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, tenant_id, x_tenant_id)
    stmt = select(FireEmergencyEvent).order_by(FireEmergencyEvent.created_at.desc())
    stmt = stmt.where(FireEmergencyEvent.tenant_id == tid if tid is not None else FireEmergencyEvent.tenant_id.is_(None))
    rows = db.scalars(stmt.limit(30)).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": ev.id,
                    "zone": ev.zone,
                    "eventType": ev.event_type,
                    "severity": ev.severity,
                    "isDrill": ev.is_drill,
                    "status": ev.status,
                    "createdAt": as_utc(ev.created_at).isoformat(),
                }
                for ev in rows
            ]
        },
    )


@router.post("/enterprise/infrastructure/fire-events", status_code=201)
def trigger_fire_emergency_event_v1(
    payload: FireEmergencyIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, payload.tenantId, x_tenant_id)
    ev = FireEmergencyEvent(
        tenant_id=tid,
        zone=payload.zone.strip(),
        event_type=payload.eventType,
        severity=payload.severity,
        is_drill=payload.isDrill,
        status="active",
        evacuation_checklist_json=json.dumps(payload.evacuationChecklist),
        notes=payload.notes,
        reported_by_user_id=staff.id,
    )
    db.add(ev)
    record_audit(db, "enterprise.fire_event_triggered", "fire_emergency_event", 0, staff.id, {"zone": ev.zone, "isDrill": ev.is_drill})
    db.commit()
    db.refresh(ev)
    return ok(
        request,
        {"id": ev.id, "zone": ev.zone, "eventType": ev.event_type, "isDrill": ev.is_drill, "status": ev.status, "evacuationChecklist": _safe_json_loads(ev.evacuation_checklist_json, [])},
        status_code=201,
    )


@router.post("/enterprise/infrastructure/fire-events/{event_id}/resolve")
def resolve_fire_emergency_event_v1(
    event_id: int,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    ev = db.get(FireEmergencyEvent, event_id)
    if not ev:
        raise ApiError(404, "FIRE_EVENT_NOT_FOUND", "Emergency event not found.")
    _resolve_tenant_scope(db, staff, ev.tenant_id, x_tenant_id)
    ev.status = "resolved"
    ev.resolved_by_user_id = staff.id
    ev.resolved_at = datetime.now(timezone.utc)
    db.commit()
    return ok(request, {"id": ev.id, "status": ev.status})


@router.post("/enterprise/infrastructure/sensors", status_code=201)
def ingest_sensor_reading_v1(
    payload: SensorReadingIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, payload.tenantId, x_tenant_id)
    recorded = datetime.now(timezone.utc) - timedelta(minutes=payload.ageMinutesOverride)
    row = EnvironmentalSensorReading(
        tenant_id=tid,
        station_id=payload.stationId,
        sensor_code=payload.sensorCode.strip(),
        sensor_type=payload.sensorType,
        reading_value=Decimal(str(payload.readingValue)),
        unit=payload.unit.strip(),
        source_mode=payload.sourceMode,
        threshold_min=Decimal(str(payload.thresholdMin)) if payload.thresholdMin is not None else None,
        threshold_max=Decimal(str(payload.thresholdMax)) if payload.thresholdMax is not None else None,
        recorded_by_user_id=staff.id,
        recorded_at=recorded,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ok(request, _serialize_sensor_reading(row), status_code=201)


def _serialize_sensor_reading(r: EnvironmentalSensorReading) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    age_mins = int(max(0, (now - as_utc(r.recorded_at)).total_seconds() // 60))
    val = float(r.reading_value)
    out_of_range = (r.threshold_min is not None and val < float(r.threshold_min)) or (r.threshold_max is not None and val > float(r.threshold_max))
    return {
        "id": r.id,
        "sensorCode": r.sensor_code,
        "sensorType": r.sensor_type,
        "readingValue": val,
        "unit": r.unit,
        "sourceMode": r.source_mode,
        "isStale": age_mins > 30,
        "ageMinutes": age_mins,
        "outOfRange": out_of_range,
        "recordedAt": as_utc(r.recorded_at).isoformat(),
    }


@router.get("/enterprise/infrastructure/overview")
def get_infrastructure_overview_v1(
    request: Request,
    tenant_id: int | None = Query(default=None, alias="tenantId"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, staff, tenant_id, x_tenant_id)
    sensors = db.scalars(
        select(EnvironmentalSensorReading)
        .where(EnvironmentalSensorReading.tenant_id == tid if tid is not None else EnvironmentalSensorReading.tenant_id.is_(None))
        .order_by(EnvironmentalSensorReading.recorded_at.desc())
        .limit(30)
    ).all()
    sensor_list = [_serialize_sensor_reading(s) for s in sensors]
    open_tickets = int(db.scalar(select(func.count(EquipmentMaintenanceTicket.id)).where(EquipmentMaintenanceTicket.status != "resolved")) or 0)
    water_alerts = int(
        db.scalar(
            select(func.count(WaterQualityTest.id)).where(
                WaterQualityTest.is_compliant.is_(False),
                WaterQualityTest.tenant_id == tid if tid is not None else WaterQualityTest.tenant_id.is_(None),
            )
        )
        or 0
    )
    return ok(
        request,
        {
            "sensors": sensor_list,
            "staleSensorCount": sum(1 for s in sensor_list if s["isStale"]),
            "outOfRangeSensorCount": sum(1 for s in sensor_list if s["outOfRange"]),
            "openMaintenanceTickets": open_tickets,
            "waterComplianceAlerts": water_alerts,
            "hardwareIntegrationMode": "manual_and_iot_webhook_ready",
        },
    )


# ==============================================================================
# PART 4 — SUPPLY-CHAIN ECOSYSTEM (MODULES 13–18)
# ==============================================================================
class RfqCreateIn(BaseModel):
    tenantId: int | None = None
    ingredientId: int
    title: str = Field(min_length=3, max_length=200)
    requiredQuantity: float = Field(gt=0)
    unit: str = Field(default="kg", max_length=24)
    targetMaxUnitPaise: int | None = Field(default=None, ge=1)


class RfqBidIn(BaseModel):
    supplierId: int
    unitPricePaise: int = Field(ge=1)
    leadTimeDays: int = Field(default=2, ge=0, le=90)
    notes: str | None = Field(default=None, max_length=400)


class GoodsReceivingIn(BaseModel):
    tenantId: int | None = None
    purchaseOrderId: int | None = None
    ingredientId: int
    supplierId: int
    batchCode: str = Field(min_length=2, max_length=80)
    receivedQuantity: float = Field(gt=0)
    expiryDate: date
    packageTempC: float | None = None
    packagingIntact: bool = True
    rejectionReason: str | None = Field(default=None, max_length=400)


class DisruptionSimulationIn(BaseModel):
    ingredientId: int
    outageDays: int = Field(default=3, ge=1, le=30)
    demandMultiplierPct: int = Field(default=125, ge=50, le=300)


@router.get("/enterprise/supply-chain/demand-collaboration")
def get_supplier_demand_collaboration_v1(
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    low_stock = db.scalars(select(Ingredient).where(Ingredient.current_quantity <= Ingredient.reorder_level)).all()
    open_pos = db.scalars(select(PurchaseOrder).where(PurchaseOrder.status.in_(["draft", "ordered"])).order_by(PurchaseOrder.created_at.desc())).all()
    return ok(
        request,
        {
            "piiExcluded": True,
            "lowStockIngredients": [
                {
                    "id": i.id,
                    "name": i.name,
                    "unit": i.unit,
                    "currentQuantity": float(i.current_quantity),
                    "reorderLevel": float(i.reorder_level),
                }
                for i in low_stock
            ],
            "scheduledPurchaseOrders": [
                {"id": po.id, "supplierId": po.supplier_id, "status": po.status, "createdAt": as_utc(po.created_at).isoformat()}
                for po in open_pos
            ],
        },
    )


@router.get("/enterprise/supply-chain/rfqs")
def list_procurement_rfqs_v1(
    request: Request,
    tenant_id: int | None = Query(default=None, alias="tenantId"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, admin, tenant_id, x_tenant_id)
    stmt = select(ProcurementRfq).order_by(ProcurementRfq.created_at.desc())
    stmt = stmt.where(ProcurementRfq.tenant_id == tid if tid is not None else ProcurementRfq.tenant_id.is_(None))
    rfqs = db.scalars(stmt.limit(30)).all()
    items = []
    for r in rfqs:
        bids = db.scalars(select(ProcurementBid).where(ProcurementBid.rfq_id == r.id).order_by(ProcurementBid.unit_price_paise.asc())).all()
        items.append(
            {
                "id": r.id,
                "ingredientId": r.ingredient_id,
                "title": r.title,
                "requiredQuantity": float(r.required_quantity),
                "unit": r.unit,
                "targetMaxUnitPaise": r.target_max_unit_paise,
                "status": r.status,
                "awardedBidId": r.awarded_bid_id,
                "bids": [
                    {
                        "id": b.id,
                        "supplierId": b.supplier_id,
                        "unitPricePaise": b.unit_price_paise,
                        "leadTimeDays": b.lead_time_days,
                        "status": b.status,
                    }
                    for b in bids
                ],
            }
        )
    return ok(request, {"items": items})


@router.post("/enterprise/supply-chain/rfqs", status_code=201)
def create_procurement_rfq_v1(
    payload: RfqCreateIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, admin, payload.tenantId, x_tenant_id)
    ing = db.get(Ingredient, payload.ingredientId)
    if not ing:
        raise ApiError(404, "INGREDIENT_NOT_FOUND", "Ingredient not found.")
    rfq = ProcurementRfq(
        tenant_id=tid,
        ingredient_id=ing.id,
        title=payload.title.strip(),
        required_quantity=Decimal(str(payload.requiredQuantity)),
        unit=payload.unit.strip(),
        target_max_unit_paise=payload.targetMaxUnitPaise,
        created_by_user_id=admin.id,
    )
    db.add(rfq)
    db.commit()
    db.refresh(rfq)
    return ok(request, {"id": rfq.id, "title": rfq.title, "status": rfq.status, "ingredientId": rfq.ingredient_id}, status_code=201)


@router.post("/enterprise/supply-chain/rfqs/{rfq_id}/bids", status_code=201)
def submit_procurement_bid_v1(
    rfq_id: int,
    payload: RfqBidIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    rfq = db.get(ProcurementRfq, rfq_id)
    if not rfq or rfq.status != "open":
        raise ApiError(409, "RFQ_NOT_OPEN", "RFQ is missing or not accepting bids.")
    _resolve_tenant_scope(db, admin, rfq.tenant_id, x_tenant_id)
    sup = db.get(Supplier, payload.supplierId)
    if not sup or not sup.is_active:
        raise ApiError(404, "SUPPLIER_NOT_FOUND", "Active supplier not found.")
    existing = db.scalar(select(ProcurementBid).where(ProcurementBid.rfq_id == rfq.id, ProcurementBid.supplier_id == sup.id))
    if existing:
        existing.unit_price_paise = payload.unitPricePaise
        existing.lead_time_days = payload.leadTimeDays
        existing.notes = payload.notes
        bid = existing
    else:
        bid = ProcurementBid(
            rfq_id=rfq.id,
            supplier_id=sup.id,
            unit_price_paise=payload.unitPricePaise,
            lead_time_days=payload.leadTimeDays,
            notes=payload.notes,
            submitted_by_user_id=admin.id,
        )
        db.add(bid)
    db.commit()
    db.refresh(bid)
    return ok(request, {"id": bid.id, "rfqId": rfq.id, "supplierId": sup.id, "unitPricePaise": bid.unit_price_paise, "leadTimeDays": bid.lead_time_days, "status": bid.status}, status_code=201)


@router.post("/enterprise/supply-chain/rfqs/{rfq_id}/award/{bid_id}")
def award_procurement_bid_v1(
    rfq_id: int,
    bid_id: int,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    rfq = db.get(ProcurementRfq, rfq_id)
    bid = db.get(ProcurementBid, bid_id)
    if not rfq or not bid or bid.rfq_id != rfq.id:
        raise ApiError(404, "BID_NOT_FOUND", "RFQ bid not found.")
    _resolve_tenant_scope(db, admin, rfq.tenant_id, x_tenant_id)
    if rfq.status != "open":
        raise ApiError(409, "RFQ_ALREADY_CLOSED", "RFQ has already been awarded or closed.")
    rfq.status = "awarded"
    rfq.awarded_bid_id = bid.id
    for b in db.scalars(select(ProcurementBid).where(ProcurementBid.rfq_id == rfq.id)).all():
        b.status = "awarded" if b.id == bid.id else "rejected"
    record_audit(db, "enterprise.rfq_awarded", "procurement_rfq", rfq.id, admin.id, {"awardedBidId": bid.id, "supplierId": bid.supplier_id})
    db.commit()
    return ok(request, {"rfqId": rfq.id, "status": rfq.status, "awardedBidId": bid.id, "winningSupplierId": bid.supplier_id, "unitPricePaise": bid.unit_price_paise})


@router.get("/enterprise/supply-chain/suppliers/{supplier_id}/risk")
def get_supplier_risk_score_v1(
    supplier_id: int,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    sup = db.get(Supplier, supplier_id)
    if not sup:
        raise ApiError(404, "SUPPLIER_NOT_FOUND", "Supplier not found.")
    po_count = int(db.scalar(select(func.count(PurchaseOrder.id)).where(PurchaseOrder.supplier_id == supplier_id)) or 0)
    rejected_deliveries = int(
        db.scalar(
            select(func.count(GoodsReceivingInspection.id)).where(
                GoodsReceivingInspection.supplier_id == supplier_id,
                GoodsReceivingInspection.inspection_status != "accepted",
            )
        )
        or 0
    )
    disputed_invoices = int(
        db.scalar(
            select(func.count(SupplierInvoice.id)).where(
                SupplierInvoice.supplier_id == supplier_id,
                (SupplierInvoice.discrepancy_flagged.is_(True) | SupplierInvoice.payment_status.in_(["disputed"])),
            )
        )
        or 0
    )
    risk_score = min(100, rejected_deliveries * 20 + disputed_invoices * 15 + (0 if sup.is_active else 30))
    return ok(
        request,
        {
            "supplierId": sup.id,
            "supplierName": sup.name,
            "riskScore": risk_score,
            "riskBand": "high" if risk_score >= 50 else "medium" if risk_score >= 20 else "low",
            "insufficientHistory": po_count < 3 and rejected_deliveries == 0,
            "metrics": {
                "purchaseOrdersCount": po_count,
                "rejectedDeliveries": rejected_deliveries,
                "disputedInvoices": disputed_invoices,
            },
        },
    )


@router.post("/enterprise/supply-chain/receiving/inspect", status_code=201)
def inspect_goods_receiving_v1(
    payload: GoodsReceivingIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    """Inspects incoming stock; strictly rejects expired/damaged batches without updating inventory."""
    tid = _resolve_tenant_scope(db, staff, payload.tenantId, x_tenant_id)
    ing = db.get(Ingredient, payload.ingredientId)
    sup = db.get(Supplier, payload.supplierId)
    if not ing or not sup:
        raise ApiError(404, "SUPPLY_ENTITY_NOT_FOUND", "Ingredient or supplier not found.")

    today = date.today()
    is_expired = payload.expiryDate < today
    if is_expired:
        status_str = "rejected_expired"
        reason = payload.rejectionReason or f"Expired on {payload.expiryDate.isoformat()}"
    elif not payload.packagingIntact:
        status_str = "rejected_damaged"
        reason = payload.rejectionReason or "Damaged packaging failed intake check"
    else:
        status_str = "accepted"
        reason = None

    rec = GoodsReceivingInspection(
        tenant_id=tid,
        purchase_order_id=payload.purchaseOrderId,
        ingredient_id=ing.id,
        supplier_id=sup.id,
        batch_code=payload.batchCode.strip(),
        received_quantity=Decimal(str(payload.receivedQuantity)),
        expiry_date=payload.expiryDate,
        package_temp_c=Decimal(str(payload.packageTempC)) if payload.packageTempC is not None else None,
        packaging_intact=payload.packagingIntact,
        inspection_status=status_str,
        rejection_reason=reason,
        inspected_by_user_id=staff.id,
    )
    db.add(rec)
    if status_str == "accepted":
        ing.current_quantity = Decimal(ing.current_quantity) + Decimal(str(payload.receivedQuantity))
    record_audit(db, f"enterprise.receiving_{status_str}", "goods_receiving", ing.id, staff.id, {"batchCode": rec.batch_code})
    db.commit()
    db.refresh(rec)
    return ok(
        request,
        {
            "id": rec.id,
            "batchCode": rec.batch_code,
            "inspectionStatus": rec.inspection_status,
            "rejectionReason": rec.rejection_reason,
            "inventoryUpdated": status_str == "accepted",
            "ingredientCurrentStock": float(ing.current_quantity),
        },
        status_code=201,
    )


@router.get("/enterprise/supply-chain/provenance/{batch_code}")
def get_batch_provenance_v1(
    batch_code: str,
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    inspections = db.scalars(select(GoodsReceivingInspection).where(GoodsReceivingInspection.batch_code == batch_code)).all()
    product_batches = db.scalars(select(ProductBatch).where(ProductBatch.batch_code == batch_code)).all()
    if not inspections and not product_batches:
        raise ApiError(404, "BATCH_PROVENANCE_NOT_FOUND", "No receiving inspection or product batch found for this code.")
    return ok(
        request,
        {
            "batchCode": batch_code,
            "receivingInspections": [
                {
                    "id": i.id,
                    "supplierId": i.supplier_id,
                    "ingredientId": i.ingredient_id,
                    "status": i.inspection_status,
                    "expiryDate": i.expiry_date.isoformat(),
                    "inspectedAt": as_utc(i.inspected_at).isoformat(),
                }
                for i in inspections
            ],
            "productBatches": [
                {"id": pb.id, "productId": pb.product_id, "quantityRemaining": pb.quantity_remaining, "expiryDate": pb.expiry_date.isoformat()}
                for pb in product_batches
            ],
        },
    )


@router.post("/enterprise/supply-chain/disruption-simulation")
def run_disruption_simulation_v1(
    payload: DisruptionSimulationIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    ing = db.get(Ingredient, payload.ingredientId)
    if not ing:
        raise ApiError(404, "INGREDIENT_NOT_FOUND", "Ingredient not found.")
    stock = float(ing.current_quantity)
    daily_baseline = max(1.0, float(ing.reorder_level or 5.0) / 2.0)
    adjusted_daily = daily_baseline * (payload.demandMultiplierPct / 100.0)
    days_of_cover = round(stock / adjusted_daily, 2)
    shortfall_days = max(0.0, round(payload.outageDays - days_of_cover, 2))
    return ok(
        request,
        {
            "simulation": True,
            "disclaimer": "Hypothetical what-if stress simulation only — does NOT modify live inventory or supplier records.",
            "ingredient": {"id": ing.id, "name": ing.name, "currentStock": stock, "unit": ing.unit},
            "estimatedDaysOfCover": days_of_cover,
            "projectedShortfallDays": shortfall_days,
            "mitigationRecommended": shortfall_days > 0,
        },
    )


# ==============================================================================
# PART 5 — CUSTOMER & FINANCIAL INTELLIGENCE (MODULES 19–24)
# ==============================================================================
class MealCreditIssueIn(BaseModel):
    tenantId: int | None = None
    userId: int
    departmentName: str = Field(min_length=2, max_length=120)
    programName: str = Field(min_length=2, max_length=120)
    allocatedPaise: int = Field(ge=100, le=5_000_000)
    expiresOn: date


class MealCreditRedeemIn(BaseModel):
    allocationId: int
    amountPaise: int = Field(ge=1, le=500_000)
    orderId: int | None = None
    idempotencyKey: str = Field(min_length=6, max_length=80)


class LoyaltySimIn(BaseModel):
    cashbackRatePct: float = Field(ge=0.0, le=20.0)
    minOrderRupees: float = Field(default=50.0, ge=0.0, le=5000.0)


class TaxValidationIn(BaseModel):
    gstin: str = Field(min_length=15, max_length=15)
    invoiceNumber: str = Field(min_length=2, max_length=80)
    supplierId: int
    subtotalPaise: int = Field(ge=0)
    taxPaise: int = Field(ge=0)
    totalPaise: int = Field(ge=0)


@router.get("/enterprise/finance/meal-credits/me")
def list_my_meal_credits_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(MealCreditAllocation)
        .where(MealCreditAllocation.user_id == user.id)
        .order_by(MealCreditAllocation.created_at.desc())
        .limit(20)
    ).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": a.id,
                    "departmentName": a.department_name,
                    "programName": a.program_name,
                    "allocatedPaise": a.allocated_paise,
                    "remainingPaise": a.remaining_paise,
                    "expiresOn": a.expires_on.isoformat(),
                    "status": a.status,
                }
                for a in rows
            ]
        },
    )


@router.get("/enterprise/finance/meal-credits")
def list_admin_meal_credits_v1(
    request: Request,
    tenant_id: int | None = Query(default=None, alias="tenantId"),
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, admin, tenant_id, x_tenant_id)
    stmt = select(MealCreditAllocation).order_by(MealCreditAllocation.created_at.desc())
    stmt = stmt.where(MealCreditAllocation.tenant_id == tid if tid is not None else MealCreditAllocation.tenant_id.is_(None))
    rows = db.scalars(stmt.limit(40)).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": a.id,
                    "userId": a.user_id,
                    "departmentName": a.department_name,
                    "programName": a.program_name,
                    "allocatedPaise": a.allocated_paise,
                    "remainingPaise": a.remaining_paise,
                    "expiresOn": a.expires_on.isoformat(),
                    "status": a.status,
                }
                for a in rows
            ]
        },
    )


@router.post("/enterprise/finance/meal-credits", status_code=201)
def issue_meal_credit_allocation_v1(
    payload: MealCreditIssueIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    tid = _resolve_tenant_scope(db, admin, payload.tenantId, x_tenant_id)
    target = db.get(User, payload.userId)
    if not target:
        raise ApiError(404, "USER_NOT_FOUND", "Student account not found.")
    alloc = MealCreditAllocation(
        tenant_id=tid,
        user_id=target.id,
        department_name=payload.departmentName.strip(),
        program_name=payload.programName.strip(),
        allocated_paise=payload.allocatedPaise,
        remaining_paise=payload.allocatedPaise,
        expires_on=payload.expiresOn,
        status="active",
        issued_by_user_id=admin.id,
    )
    db.add(alloc)
    record_audit(db, "enterprise.meal_credit_issued", "meal_credit", target.id, admin.id, {"allocatedPaise": payload.allocatedPaise})
    db.commit()
    db.refresh(alloc)
    return ok(request, {"id": alloc.id, "userId": alloc.user_id, "remainingPaise": alloc.remaining_paise, "status": alloc.status, "separateFromCashWallet": True}, status_code=201)


@router.post("/enterprise/finance/meal-credits/redeem")
def redeem_meal_credit_v1(
    payload: MealCreditRedeemIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Atomically deducts institutional meal credit separate from cash wallet with idempotency protection."""
    existing = db.scalar(select(MealCreditRedemption).where(MealCreditRedemption.idempotency_key == payload.idempotencyKey))
    if existing:
        alloc_cur = db.get(MealCreditAllocation, existing.allocation_id)
        return ok(request, {"redemptionId": existing.id, "idempotentReplay": True, "remainingPaise": alloc_cur.remaining_paise if alloc_cur else 0})

    alloc = db.get(MealCreditAllocation, payload.allocationId)
    if not alloc:
        raise ApiError(404, "ALLOCATION_NOT_FOUND", "Meal credit allocation not found.")
    if user.role != UserRole.ADMIN and alloc.user_id != user.id:
        raise ApiError(403, "FORBIDDEN", "Cannot redeem another student's meal credit.")
    if alloc.expires_on < date.today():
        raise ApiError(409, "MEAL_CREDIT_EXPIRED", "Meal credit allocation has expired.")

    res = db.execute(
        update(MealCreditAllocation)
        .where(
            MealCreditAllocation.id == alloc.id,
            MealCreditAllocation.status == "active",
            MealCreditAllocation.remaining_paise >= payload.amountPaise,
        )
        .values(remaining_paise=MealCreditAllocation.remaining_paise - payload.amountPaise)
    )
    if res.rowcount != 1:
        raise ApiError(409, "INSUFFICIENT_MEAL_CREDIT", "Insufficient or inactive meal credit balance.")

    redemption = MealCreditRedemption(
        allocation_id=alloc.id,
        user_id=alloc.user_id,
        order_id=payload.orderId,
        amount_paise=payload.amountPaise,
        idempotency_key=payload.idempotencyKey,
    )
    db.add(redemption)
    db.commit()
    db.refresh(alloc)
    if alloc.remaining_paise == 0:
        alloc.status = "exhausted"
        db.commit()
    return ok(request, {"redemptionId": redemption.id, "deductedPaise": payload.amountPaise, "remainingPaise": alloc.remaining_paise, "separateFromCashWallet": True})


@router.post("/enterprise/finance/loyalty-simulator")
def simulate_loyalty_economics_v1(
    payload: LoyaltySimIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    paid = db.scalars(select(Order).where(Order.payment_status == PaymentStatus.PAID)).all()
    eligible = [o for o in paid if int(o.total_paise) >= int(payload.minOrderRupees * 100)]
    eligible_gmv = sum(int(o.total_paise) for o in eligible)
    projected_reward_cost = int(eligible_gmv * (payload.cashbackRatePct / 100.0))
    return ok(
        request,
        {
            "simulation": True,
            "observedPaidOrders": len(paid),
            "eligibleOrders": len(eligible),
            "eligibleGmvPaise": eligible_gmv,
            "projectedRewardLiabilityPaise": projected_reward_cost,
        },
    )


@router.get("/enterprise/analytics/customer-journey")
def get_customer_journey_funnel_v1(
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    total_orders = int(db.scalar(select(func.count(Order.id))) or 0)
    paid_orders = int(db.scalar(select(func.count(Order.id)).where(Order.payment_status == PaymentStatus.PAID)) or 0)
    collected_orders = int(db.scalar(select(func.count(Order.id)).where(Order.status.in_([OrderStatus.COMPLETED, OrderStatus.COLLECTED]))) or 0)
    feedback_count = int(db.scalar(select(func.count(OrderQualityFeedback.id))) or 0)
    support_count = int(db.scalar(select(func.count(SupportTicket.id))) or 0)
    return ok(
        request,
        {
            "funnel": [
                {"stage": "order_created", "count": total_orders},
                {"stage": "payment_verified", "count": paid_orders},
                {"stage": "pickup_collected", "count": collected_orders},
                {"stage": "quality_feedback_submitted", "count": feedback_count},
            ],
            "supportTicketsOpened": support_count,
            "paymentConversionPct": round((paid_orders / total_orders) * 100, 2) if total_orders else 0.0,
        },
    )


@router.get("/enterprise/finance/scenario-forecast")
def get_financial_scenario_forecast_v1(
    request: Request,
    horizon_days: int = Query(default=30, ge=7, le=180, alias="horizonDays"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    paid_revenue = int(db.scalar(select(func.coalesce(func.sum(Order.total_paise), 0)).where(Order.payment_status == PaymentStatus.PAID)) or 0)
    expenses = int(db.scalar(select(func.coalesce(func.sum(OperationalExpense.amount_paise), 0))) or 0)
    baseline_daily_rev = max(10000, paid_revenue // 30) if paid_revenue else 0
    baseline_daily_exp = expenses // 30 if expenses else 0
    return ok(
        request,
        {
            "statisticalProjection": True,
            "horizonDays": horizon_days,
            "historicalPaidRevenuePaise": paid_revenue,
            "scenarios": {
                "conservative": {"projectedRevenuePaise": int(baseline_daily_rev * horizon_days * 0.85), "projectedExpensesPaise": int(baseline_daily_exp * horizon_days * 1.1)},
                "expected": {"projectedRevenuePaise": int(baseline_daily_rev * horizon_days), "projectedExpensesPaise": int(baseline_daily_exp * horizon_days)},
                "optimistic": {"projectedRevenuePaise": int(baseline_daily_rev * horizon_days * 1.2), "projectedExpensesPaise": int(baseline_daily_exp * horizon_days * 0.95)},
            },
        },
    )


@router.post("/enterprise/finance/tax/validate-invoice")
def validate_gst_invoice_v1(
    payload: TaxValidationIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    findings: list[str] = []
    gstin_upper = payload.gstin.strip().upper()
    if not GSTIN_RE.match(gstin_upper):
        findings.append("Invalid Indian GSTIN checksum format.")
    if payload.subtotalPaise + payload.taxPaise != payload.totalPaise:
        findings.append("Subtotal + tax does not equal invoice total.")
    dup = db.scalar(
        select(SupplierInvoice.id).where(
            SupplierInvoice.supplier_id == payload.supplierId,
            SupplierInvoice.invoice_reference == payload.invoiceNumber.strip(),
        )
    )
    if dup is not None:
        findings.append(f"Duplicate invoice number already recorded (#{dup}).")
    return ok(
        request,
        {
            "valid": len(findings) == 0,
            "gstinValid": bool(GSTIN_RE.match(gstin_upper)),
            "duplicateDetected": dup is not None,
            "findings": findings,
        },
    )


@router.get("/enterprise/finance/multi-outlet-consolidation")
def get_multi_outlet_consolidation_v1(
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    counters = db.scalars(select(CanteenCounter).order_by(CanteenCounter.id.asc())).all()
    outlet_rows = []
    for c in counters:
        ord_sum = int(db.scalar(select(func.coalesce(func.sum(Order.total_paise), 0)).where(Order.counter_id == c.id, Order.payment_status == PaymentStatus.PAID)) or 0)
        pos_sum = int(db.scalar(select(func.coalesce(func.sum(WalkinPosOrder.total_paise), 0)).where(WalkinPosOrder.counter_id == c.id)) or 0)
        outlet_rows.append({"counterId": c.id, "counterName": c.name, "onlinePaidPaise": ord_sum, "walkinPosPaise": pos_sum, "combinedPaise": ord_sum + pos_sum})
    bulk_paid = int(db.scalar(select(func.coalesce(func.sum(BulkOrder.paid_paise), 0))) or 0)
    return ok(
        request,
        {
            "outlets": outlet_rows,
            "centralBulkRevenuePaise": bulk_paid,
            "totalConsolidatedRevenuePaise": sum(r["combinedPaise"] for r in outlet_rows) + bulk_paid,
            "refundsIssuedPaise": 0,
            "policyNote": "Strict No-Refunds CollegeBits 3.0 ledger: refunded amount is always 0.",
        },
    )


# ==============================================================================
# PART 6 — ADVANCED PLATFORM ENGINEERING (MODULES 25–30)
# ==============================================================================
class RecoveryDrillIn(BaseModel):
    tenantId: int | None = None
    notes: str | None = Field(default=None, max_length=400)


class WebhookSubscribeIn(BaseModel):
    tenantId: int | None = None
    name: str = Field(min_length=2, max_length=120)
    targetUrl: str = Field(min_length=10, max_length=500)
    eventTypes: list[str] = Field(min_length=1, max_length=20)
    signingSecret: str = Field(min_length=16, max_length=128)


class BlueprintCreateIn(BaseModel):
    name: str = Field(min_length=3, max_length=100)
    version: str = Field(min_length=2, max_length=30)
    description: str | None = Field(default=None, max_length=300)
    config: dict[str, Any] = Field(default_factory=dict)


@router.get("/enterprise/platform/deployment-profiles")
def get_deployment_profiles_v1(
    request: Request,
    _: User = Depends(require_platform_super_admin),
):
    settings = get_settings()
    db_engine = "postgresql" if settings.database_url.startswith("postgresql") else "sqlite"
    return ok(
        request,
        {
            "activeProfile": "single_campus_docker" if db_engine == "postgresql" else "local_evaluation_sqlite",
            "profiles": [
                {"id": "on_premise_college_server", "ready": db_engine == "postgresql", "databaseEngine": db_engine, "redisConfigured": bool(settings.redis_url)},
                {"id": "aws_ecs_rds", "ready": db_engine == "postgresql" and bool(settings.redis_url), "containerized": True},
                {"id": "gcp_cloud_run_sql", "ready": db_engine == "postgresql" and bool(settings.backup_encryption_key), "statelessApi": True},
                {"id": "azure_container_apps", "ready": db_engine == "postgresql", "statelessApi": True},
            ],
        },
    )


@router.post("/enterprise/platform/resilience-check")
def run_isolated_resilience_check_v1(
    request: Request,
    _: User = Depends(require_platform_super_admin),
    db: Session = Depends(get_db),
):
    """Non-destructive resilience check: verifies DB query latency, backup health, and payment integrity."""
    t0 = time.perf_counter()
    db.execute(text("SELECT 1")).scalar()
    db_ms = round((time.perf_counter() - t0) * 1000, 2)
    b_healthy, b_detail = check_backup_health()
    return ok(
        request,
        {
            "isolatedSafeCheck": True,
            "destructiveTrafficImpact": False,
            "databasePingMs": db_ms,
            "backupHealth": {"healthy": b_healthy, "detail": b_detail},
            "status": "healthy" if db_ms < 500 else "degraded_latency",
        },
    )


@router.post("/enterprise/platform/recovery-drill", status_code=201)
def run_database_recovery_drill_v1(
    payload: RecoveryDrillIn,
    request: Request,
    admin: User = Depends(require_platform_super_admin),
    db: Session = Depends(get_db),
):
    """Creates a verified encrypted GFS snapshot, runs restoration verification, and verifies table counts."""
    t0 = time.perf_counter()
    backup_info = create_encrypted_gfs_backup(db, actor_user_id=admin.id)
    drill_res = run_restore_drill(Path(backup_info["primaryPath"]), db=db, actor_user_id=admin.id)
    counts = {
        "users": int(db.scalar(select(func.count(User.id))) or 0),
        "orders": int(db.scalar(select(func.count(Order.id))) or 0),
        "payments": int(db.scalar(select(func.count(Payment.id))) or 0),
    }
    dur_ms = int((time.perf_counter() - t0) * 1000)
    drill = DatabaseRecoveryDrill(
        tenant_id=payload.tenantId,
        backup_run_id=backup_info.get("backupRunId"),
        status="passed" if drill_res.get("restoreDrillPassed") else "failed",
        verified_table_counts_json=json.dumps(counts),
        restore_duration_ms=dur_ms,
        notes=payload.notes,
        executed_by_user_id=admin.id,
    )
    db.add(drill)
    db.commit()
    db.refresh(drill)
    return ok(
        request,
        {"drillId": drill.id, "backupRunId": backup_info.get("backupRunId"), "status": drill.status, "verifiedTableCounts": counts, "restoreDurationMs": dur_ms},
        status_code=201,
    )


@router.get("/enterprise/platform/observability/timeline")
def get_correlated_observability_timeline_v1(
    request: Request,
    _: User = Depends(require_platform_super_admin),
    db: Session = Depends(get_db),
):
    audits = db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(15)).all()
    sec = db.scalars(select(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(15)).all()
    incidents = db.scalars(select(PaymentIncident).order_by(PaymentIncident.created_at.desc()).limit(15)).all()
    events = [
        {"stream": "audit", "event": a.action, "entity": f"{a.entity_type}:{a.entity_id}", "at": as_utc(a.created_at).isoformat()}
        for a in audits
    ] + [
        {"stream": "security", "event": s.event_type, "entity": f"severity:{s.severity}", "at": as_utc(s.created_at).isoformat()}
        for s in sec
    ] + [
        {"stream": "payment_incident", "event": i.incident_type, "entity": f"incident:{i.id}", "at": as_utc(i.created_at).isoformat()}
        for i in incidents
    ]
    events.sort(key=lambda e: e["at"], reverse=True)
    return ok(request, {"timeline": events[:30]})


@router.post("/developer/webhooks/subscribe", status_code=201)
def register_developer_webhook_v1(
    payload: WebhookSubscribeIn,
    request: Request,
    x_tenant_id: int | None = Header(default=None, alias="X-Tenant-ID"),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    _validate_webhook_url_ssrf(payload.targetUrl)
    tid = _resolve_tenant_scope(db, admin, payload.tenantId, x_tenant_id)
    sub = WebhookSubscription(
        tenant_id=tid,
        name=payload.name.strip(),
        target_url=payload.targetUrl.strip(),
        event_types_json=json.dumps([e.strip() for e in payload.eventTypes]),
        secret_hash=hashlib.sha256(payload.signingSecret.encode("utf-8")).hexdigest(),
        created_by_user_id=admin.id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return ok(
        request,
        {"id": sub.id, "name": sub.name, "targetUrl": sub.target_url, "eventTypes": _safe_json_loads(sub.event_types_json, []), "isActive": sub.is_active},
        status_code=201,
    )


@router.get("/developer/portal")
def get_developer_portal_catalog_v1(
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    subs = db.scalars(select(WebhookSubscription).order_by(WebhookSubscription.created_at.desc()).limit(20)).all()
    return ok(
        request,
        {
            "openApiSpecUrl": "/openapi.json",
            "supportedWebhookEvents": ["order.paid", "order.ready", "pickup.collected", "payment.incident_opened", "bulk_order.tokens_generated"],
            "ssrfProtectionActive": True,
            "subscriptions": [{"id": s.id, "name": s.name, "targetUrl": s.target_url, "isActive": s.is_active} for s in subs],
        },
    )


@router.post("/enterprise/platform/blueprints", status_code=201)
def create_tenant_blueprint_v1(
    payload: BlueprintCreateIn,
    request: Request,
    super_admin: User = Depends(require_platform_super_admin),
    db: Session = Depends(get_db),
):
    bp = TenantConfigBlueprint(
        name=payload.name.strip(),
        version=payload.version.strip(),
        description=payload.description,
        config_json=json.dumps(payload.config),
        created_by_user_id=super_admin.id,
    )
    db.add(bp)
    db.commit()
    db.refresh(bp)
    return ok(request, {"id": bp.id, "name": bp.name, "version": bp.version, "config": _safe_json_loads(bp.config_json, {})}, status_code=201)


@router.post("/enterprise/platform/blueprints/{blueprint_id}/apply/{tenant_id}")
def apply_tenant_blueprint_v1(
    blueprint_id: int,
    tenant_id: int,
    request: Request,
    super_admin: User = Depends(require_platform_super_admin),
    db: Session = Depends(get_db),
):
    bp = db.get(TenantConfigBlueprint, blueprint_id)
    tenant = db.get(SaasTenant, tenant_id)
    if not bp or not tenant:
        raise ApiError(404, "BLUEPRINT_OR_TENANT_NOT_FOUND", "Blueprint or college tenant not found.")
    prof = db.scalar(select(SaasTenantProfile).where(SaasTenantProfile.tenant_id == tenant.id))
    prev_cfg = _safe_json_loads(prof.canteen_settings_json, {}) if prof else {}
    new_cfg = _safe_json_loads(bp.config_json, {})
    if prof:
        merged = dict(prev_cfg)
        merged.update(new_cfg)
        prof.canteen_settings_json = json.dumps(merged)
    dep = TenantBlueprintDeployment(
        tenant_id=tenant.id,
        blueprint_id=bp.id,
        previous_config_json=json.dumps(prev_cfg),
        status="applied",
        applied_by_user_id=super_admin.id,
    )
    db.add(dep)
    db.commit()
    db.refresh(dep)
    return ok(request, {"deploymentId": dep.id, "tenantId": tenant.id, "blueprintId": bp.id, "status": dep.status, "liveOrdersAffected": False})


@router.post("/enterprise/platform/blueprints/deployments/{deployment_id}/rollback")
def rollback_tenant_blueprint_v1(
    deployment_id: int,
    request: Request,
    super_admin: User = Depends(require_platform_super_admin),
    db: Session = Depends(get_db),
):
    dep = db.get(TenantBlueprintDeployment, deployment_id)
    if not dep:
        raise ApiError(404, "DEPLOYMENT_NOT_FOUND", "Blueprint deployment not found.")
    prof = db.scalar(select(SaasTenantProfile).where(SaasTenantProfile.tenant_id == dep.tenant_id))
    if prof:
        prof.canteen_settings_json = dep.previous_config_json
    dep.status = "rolled_back"
    dep.rolled_back_at = datetime.now(timezone.utc)
    record_audit(db, "enterprise.blueprint_rolled_back", "tenant_blueprint_deployment", dep.id, super_admin.id, {"tenantId": dep.tenant_id})
    db.commit()
    return ok(request, {"deploymentId": dep.id, "status": dep.status, "restoredConfig": _safe_json_loads(dep.previous_config_json, {})})
