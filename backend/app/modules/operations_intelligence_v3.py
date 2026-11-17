"""CollegeBits — Phase 60 to 85 Operational, Intelligence, Support, Food Safety,
Notification, Sustainability, Workforce, Procurement, Group Order, Meal Pass,
Daily Closing, Equipment, and Fulfillment (Parcel vs Ready-to-Eat) Engine.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import secrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.envelope import ApiError, ok
from app.models import (
    CanteenCounter,
    DailyCanteenClosing,
    EquipmentAsset,
    EquipmentMaintenanceTicket,
    FoodSafetyChecklist,
    GroupOrder,
    GroupOrderParticipantItem,
    Ingredient,
    KitchenStation,
    MealPassPlan,
    MealPassUsage,
    MenuPricingRule,
    NotificationDeliveryLog,
    Order,
    OrderItemFulfillment,
    OrderQualityFeedback,
    OrderStatus,
    PackagingLedger,
    PaymentStatus,
    Product,
    ProductFulfillmentConfig,
    PurchaseOrder,
    PurchaseRequisition,
    StaffShift,
    StudentMealPass,
    Supplier,
    SupplierInvoice,
    SupportTicket,
    SupportTicketMessage,
    TemperatureLog,
    User,
    UserNotificationPreference,
    UserRole,
    WasteRecord,
)
from app.modules.common import (
    as_utc,
    notify_user,
    record_audit,
    record_order_status_change,
    record_product_stock_movement,
)
from app.security import get_current_user, require_roles

router = APIRouter(tags=["v3-operations-intelligence"])


# ==============================================================================
# 1. Kitchen Stations, Capacity Auto-Pause & Parcel/Ready-to-Eat Config
# ==============================================================================
class KitchenStationIn(BaseModel):
    counterId: int = Field(default=1, ge=1)
    name: str = Field(min_length=2, max_length=120)
    stationType: str = Field(default="general", pattern="^(general|grill|tandoor|beverages|meals|packing)$")
    maxConcurrentOrders: int = Field(default=30, ge=1, le=500)


class ProductFulfillmentConfigIn(BaseModel):
    supportsParcel: bool = True
    supportsReadyToEat: bool = True
    packagingChargePaise: int = Field(default=0, ge=0, le=50000)
    packagingInstructions: str | None = Field(default=None, max_length=255)
    stationId: int | None = Field(default=None, gt=0)


@router.post("/kitchen/stations", status_code=201)
def create_kitchen_station_v1(
    request: Request,
    payload: KitchenStationIn,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    st = KitchenStation(
        counter_id=payload.counterId,
        name=payload.name.strip(),
        station_type=payload.stationType,
        max_concurrent_orders=payload.maxConcurrentOrders,
        active_order_count=0,
        is_active=True,
        auto_paused=False,
    )
    db.add(st)
    db.flush()
    record_audit(db, "kitchen_station.created", "kitchen_station", st.id, admin.id, {"name": st.name, "max": st.max_concurrent_orders})
    db.commit()
    db.refresh(st)
    return ok(
        request,
        {
            "id": st.id,
            "counterId": st.counter_id,
            "name": st.name,
            "stationType": st.station_type,
            "maxConcurrentOrders": st.max_concurrent_orders,
            "activeOrderCount": st.active_order_count,
            "autoPaused": bool(st.auto_paused),
        },
        status_code=201,
    )


@router.get("/kitchen/stations")
def list_kitchen_stations_v1(
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    stations = db.scalars(select(KitchenStation).order_by(KitchenStation.id.asc())).all()
    items = []
    for st in stations:
        pending_fulfillments = db.scalars(
            select(OrderItemFulfillment)
            .where(OrderItemFulfillment.station_id == st.id, OrderItemFulfillment.packing_status.in_(["pending", "preparing", "serving"]))
            .limit(100)
        ).all()
        items.append(
            {
                "id": st.id,
                "counterId": st.counter_id,
                "name": st.name,
                "stationType": st.station_type,
                "maxConcurrentOrders": st.max_concurrent_orders,
                "activeOrderCount": st.active_order_count,
                "utilizationPct": round((st.active_order_count / max(1, st.max_concurrent_orders)) * 100.0, 1),
                "isActive": bool(st.is_active),
                "autoPaused": bool(st.auto_paused),
                "pendingLinesCount": len(pending_fulfillments),
            }
        )
    return ok(request, {"items": items})


@router.post("/kitchen/stations/{station_id}/reopen")
def reopen_kitchen_station_v1(
    station_id: int,
    request: Request,
    newMaxConcurrentOrders: int | None = Query(default=None, ge=1, le=500),
    manager: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    st = db.get(KitchenStation, station_id)
    if not st:
        raise ApiError(404, "STATION_NOT_FOUND", "Kitchen station not found.")
    st.auto_paused = False
    st.is_active = True
    st.active_order_count = 0
    if newMaxConcurrentOrders is not None:
        st.max_concurrent_orders = newMaxConcurrentOrders
    record_audit(
        db,
        "kitchen_station.reopened",
        "kitchen_station",
        st.id,
        manager.id,
        {"max_concurrent_orders": st.max_concurrent_orders},
    )
    db.commit()
    db.refresh(st)
    return ok(
        request,
        {
            "id": st.id,
            "name": st.name,
            "autoPaused": bool(st.auto_paused),
            "activeOrderCount": st.active_order_count,
            "maxConcurrentOrders": st.max_concurrent_orders,
        },
    )


@router.put("/menu/items/{item_id}/fulfillment")
def configure_item_fulfillment_v1(
    item_id: int,
    request: Request,
    payload: ProductFulfillmentConfigIn,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    prod = db.get(Product, item_id)
    if not prod:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")
    if not payload.supportsParcel and not payload.supportsReadyToEat:
        raise ApiError(422, "INVALID_FULFILLMENT_CONFIG", "Item must support at least PARCEL or READY_TO_EAT.")

    cfg_row = db.scalar(select(ProductFulfillmentConfig).where(ProductFulfillmentConfig.product_id == prod.id))
    if cfg_row is None:
        cfg_row = ProductFulfillmentConfig(
            product_id=prod.id,
            supports_parcel=payload.supportsParcel,
            supports_ready_to_eat=payload.supportsReadyToEat,
            packaging_charge_paise=payload.packagingChargePaise,
            packaging_instructions=payload.packagingInstructions,
            station_id=payload.stationId,
        )
        db.add(cfg_row)
    else:
        cfg_row.supports_parcel = payload.supportsParcel
        cfg_row.supports_ready_to_eat = payload.supportsReadyToEat
        cfg_row.packaging_charge_paise = payload.packagingChargePaise
        cfg_row.packaging_instructions = payload.packagingInstructions
        cfg_row.station_id = payload.stationId
    record_audit(db, "menu_item.fulfillment_configured", "product", prod.id, admin.id, payload.model_dump())
    db.commit()
    return ok(
        request,
        {
            "menuItemId": prod.id,
            "supportsParcel": bool(cfg_row.supports_parcel),
            "supportsReadyToEat": bool(cfg_row.supports_ready_to_eat),
            "packagingChargePaise": cfg_row.packaging_charge_paise,
            "packagingInstructions": cfg_row.packaging_instructions,
            "stationId": cfg_row.station_id,
        },
    )


@router.post("/staff/orders/{order_id}/mark-uncollected")
def mark_overdue_order_uncollected_v1(
    order_id: int,
    request: Request,
    reason: str = Query(default="Customer no-show after holding period", min_length=3, max_length=255),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    order = db.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if not order:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")
    if order.status in {OrderStatus.COLLECTED, OrderStatus.COMPLETED} or order.collected_at is not None:
        raise ApiError(409, "ORDER_ALREADY_COLLECTED", "Collected orders cannot be marked uncollected.")
    if order.status != OrderStatus.READY:
        raise ApiError(409, "ORDER_NOT_READY", "Only READY orders can be marked uncollected.")

    prev = order.status.value
    order.status = OrderStatus.CANCELLED
    order.exception_reason = f"Uncollected no-show ({reason.strip()})"[:255]
    record_order_status_change(db, order, prev, "UNCOLLECTED_NO_SHOW", staff.id, reason.strip())
    notify_user(
        db,
        order.user_id,
        "order_status",
        "Order marked uncollected after holding period",
        f"Order #{order.order_number or order.id} was marked uncollected ({reason.strip()}). Payment records remain preserved under the strict no-refunds policy.",
        entity_type="order",
        entity_id=order.id,
    )
    record_audit(db, "staff.order_marked_uncollected", "order", order.id, staff.id, {"reason": reason.strip()})
    db.commit()
    return ok(request, {"orderId": order.id, "status": "cancelled", "exceptionReason": order.exception_reason})


# ==============================================================================
# 2. Customer Support & Complaint Management (Phase 66 / 84)
# ==============================================================================
class SupportTicketCreateIn(BaseModel):
    orderId: int | None = Field(default=None, gt=0)
    category: str = Field(pattern="^(payment|order|missing_item|incorrect_item|delayed_order|pickup|hygiene|account|other)$")
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")
    subject: str = Field(min_length=3, max_length=180)
    description: str = Field(min_length=5, max_length=2000)
    evidenceUrl: str | None = Field(default=None, max_length=500)


class SupportTicketMessageIn(BaseModel):
    message: str = Field(min_length=2, max_length=2000)
    isInternalNote: bool = False


class SupportTicketUpdateIn(BaseModel):
    status: str | None = Field(default=None, pattern="^(OPEN|ASSIGNED|IN_PROGRESS|WAITING_FOR_CUSTOMER|RESOLVED|CLOSED)$")
    assignedStaffId: int | None = Field(default=None, gt=0)
    escalatedToManager: bool | None = None
    resolutionSummary: str | None = Field(default=None, max_length=1000)


@router.post("/support/tickets", status_code=201)
def create_support_ticket_v1(
    request: Request,
    payload: SupportTicketCreateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tck_num = f"TCK-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    t = SupportTicket(
        ticket_number=tck_num,
        user_id=user.id,
        order_id=payload.orderId,
        category=payload.category,
        priority=payload.priority,
        status="OPEN",
        subject=payload.subject.strip(),
        description=payload.description.strip(),
        evidence_url=payload.evidenceUrl,
    )
    db.add(t)
    db.flush()
    db.add(SupportTicketMessage(ticket_id=t.id, sender_user_id=user.id, is_internal_note=False, message=t.description))
    record_audit(db, "support_ticket.created", "support_ticket", t.id, user.id, {"ticket_number": t.ticket_number, "category": t.category})
    db.commit()
    db.refresh(t)
    return ok(
        request,
        {
            "id": t.id,
            "ticketNumber": t.ticket_number,
            "category": t.category,
            "priority": t.priority,
            "status": t.status,
            "subject": t.subject,
            "createdAt": as_utc(t.created_at),
        },
        status_code=201,
    )


@router.get("/support/tickets")
def list_support_tickets_v1(
    request: Request,
    status: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(SupportTicket).order_by(SupportTicket.created_at.desc())
    if user.role == UserRole.STUDENT:
        stmt = stmt.where(SupportTicket.user_id == user.id)
    if status:
        stmt = stmt.where(SupportTicket.status == status.strip().upper())
    rows = db.scalars(stmt.limit(100)).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": t.id,
                    "ticketNumber": t.ticket_number,
                    "userId": t.user_id,
                    "orderId": t.order_id,
                    "category": t.category,
                    "priority": t.priority,
                    "status": t.status,
                    "subject": t.subject,
                    "assignedStaffId": t.assigned_staff_id,
                    "escalatedToManager": bool(t.escalated_to_manager),
                    "resolutionSummary": t.resolution_summary,
                    "createdAt": as_utc(t.created_at),
                    "resolvedAt": as_utc(t.resolved_at),
                }
                for t in rows
            ]
        },
    )


@router.get("/support/tickets/{ticket_id}")
def get_support_ticket_detail_v1(
    ticket_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    t = db.get(SupportTicket, ticket_id)
    if not t:
        raise ApiError(404, "TICKET_NOT_FOUND", "Support ticket not found.")
    if user.role == UserRole.STUDENT and t.user_id != user.id:
        raise ApiError(403, "TICKET_FORBIDDEN", "You can only access your own support tickets.")

    msgs = db.scalars(
        select(SupportTicketMessage).where(SupportTicketMessage.ticket_id == t.id).order_by(SupportTicketMessage.id.asc())
    ).all()
    visible_msgs = [
        {
            "id": m.id,
            "senderUserId": m.sender_user_id,
            "isInternalNote": bool(m.is_internal_note),
            "message": m.message,
            "createdAt": as_utc(m.created_at),
        }
        for m in msgs
        if not m.is_internal_note or user.role in {UserRole.STAFF, UserRole.ADMIN}
    ]
    return ok(
        request,
        {
            "id": t.id,
            "ticketNumber": t.ticket_number,
            "category": t.category,
            "priority": t.priority,
            "status": t.status,
            "subject": t.subject,
            "description": t.description,
            "escalatedToManager": bool(t.escalated_to_manager),
            "resolutionSummary": t.resolution_summary,
            "messages": visible_msgs,
        },
    )


@router.post("/support/tickets/{ticket_id}/messages", status_code=201)
def add_support_ticket_message_v1(
    ticket_id: int,
    request: Request,
    payload: SupportTicketMessageIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    t = db.get(SupportTicket, ticket_id)
    if not t:
        raise ApiError(404, "TICKET_NOT_FOUND", "Support ticket not found.")
    if user.role == UserRole.STUDENT:
        if t.user_id != user.id:
            raise ApiError(403, "TICKET_FORBIDDEN", "You can only reply to your own support tickets.")
        if payload.isInternalNote:
            raise ApiError(403, "INTERNAL_NOTE_FORBIDDEN", "Students cannot post internal staff notes.")
    elif t.first_response_at is None:
        t.first_response_at = datetime.now(timezone.utc)

    msg = SupportTicketMessage(
        ticket_id=t.id,
        sender_user_id=user.id,
        is_internal_note=payload.isInternalNote,
        message=payload.message.strip(),
    )
    db.add(msg)
    db.commit()
    return ok(request, {"id": msg.id, "ticketId": t.id, "isInternalNote": bool(msg.is_internal_note)}, status_code=201)


@router.patch("/support/tickets/{ticket_id}")
def update_support_ticket_v1(
    ticket_id: int,
    request: Request,
    payload: SupportTicketUpdateIn,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    t = db.get(SupportTicket, ticket_id)
    if not t:
        raise ApiError(404, "TICKET_NOT_FOUND", "Support ticket not found.")
    if payload.assignedStaffId is not None:
        t.assigned_staff_id = payload.assignedStaffId
        if t.status == "OPEN":
            t.status = "ASSIGNED"
    if payload.escalatedToManager is not None:
        t.escalated_to_manager = payload.escalatedToManager
    if payload.resolutionSummary is not None:
        t.resolution_summary = payload.resolutionSummary.strip()
    if payload.status is not None:
        t.status = payload.status
        if t.first_response_at is None:
            t.first_response_at = datetime.now(timezone.utc)
        if t.status in {"RESOLVED", "CLOSED"}:
            t.resolved_at = datetime.now(timezone.utc)

    notify_user(
        db,
        t.user_id,
        "support",
        f"Ticket {t.ticket_number} updated",
        f"Your support ticket status is now {t.status}." + (f" Note: {t.resolution_summary}" if t.resolution_summary else ""),
        entity_type="support_ticket",
        entity_id=t.id,
    )
    record_audit(db, "support_ticket.updated", "support_ticket", t.id, staff.id, {"status": t.status, "escalated": t.escalated_to_manager})
    db.commit()
    db.refresh(t)
    return ok(
        request,
        {
            "id": t.id,
            "ticketNumber": t.ticket_number,
            "status": t.status,
            "escalatedToManager": bool(t.escalated_to_manager),
            "resolutionSummary": t.resolution_summary,
            "resolvedAt": as_utc(t.resolved_at),
        },
    )


@router.get("/support/sla-metrics")
def get_support_sla_metrics_v1(
    request: Request,
    _: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(select(SupportTicket)).all()
    resp_secs = [
        (t.first_response_at - t.created_at).total_seconds()
        for t in rows
        if t.first_response_at and t.created_at
    ]
    res_secs = [
        (t.resolved_at - t.created_at).total_seconds()
        for t in rows
        if t.resolved_at and t.created_at
    ]
    return ok(
        request,
        {
            "totalTickets": len(rows),
            "openCount": sum(1 for t in rows if t.status in {"OPEN", "ASSIGNED", "IN_PROGRESS", "WAITING_FOR_CUSTOMER"}),
            "resolvedCount": sum(1 for t in rows if t.status in {"RESOLVED", "CLOSED"}),
            "escalatedCount": sum(1 for t in rows if t.escalated_to_manager),
            "avgFirstResponseSeconds": round(sum(resp_secs) / len(resp_secs), 1) if resp_secs else None,
            "avgResolutionSeconds": round(sum(res_secs) / len(res_secs), 1) if res_secs else None,
        },
    )


# ==============================================================================
# 3. Food Safety, Cleaning Checklists & Temperature Monitoring (Phase 67 / 84)
# ==============================================================================
class ChecklistSubmitIn(BaseModel):
    checklistType: str = Field(pattern="^(daily_cleaning|staff_hygiene|equipment_maintenance|receiving_verification)$")
    stationName: str = Field(default="Main Kitchen", min_length=2, max_length=120)
    itemsChecked: list[dict[str, Any]] = Field(min_length=1)
    passed: bool = True
    correctiveAction: str | None = Field(default=None, max_length=500)


class TemperatureLogIn(BaseModel):
    equipmentOrItem: str = Field(min_length=2, max_length=140)
    storageZone: str = Field(default="cold_storage", pattern="^(cold_storage|freezer|hot_holding)$")
    temperatureCelsius: Decimal
    sourceType: str = Field(default="manual", pattern="^(manual|sensor)$")
    correctiveAction: str | None = Field(default=None, max_length=500)


@router.post("/food-safety/checklists", status_code=201)
def submit_food_safety_checklist_v1(
    request: Request,
    payload: ChecklistSubmitIn,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    c = FoodSafetyChecklist(
        checklist_type=payload.checklistType,
        station_name=payload.stationName.strip(),
        items_checked_json=json.dumps(payload.itemsChecked),
        passed=payload.passed,
        corrective_action=payload.correctiveAction,
        submitted_by_staff_id=staff.id,
    )
    db.add(c)
    db.flush()
    record_audit(db, "food_safety.checklist_submitted", "food_safety_checklist", c.id, staff.id, {"type": c.checklist_type, "passed": c.passed})
    db.commit()
    db.refresh(c)
    return ok(
        request,
        {
            "id": c.id,
            "inspectionDate": c.inspection_date.isoformat(),
            "checklistType": c.checklist_type,
            "stationName": c.station_name,
            "passed": bool(c.passed),
            "approvedByManagerId": c.approved_by_manager_id,
        },
        status_code=201,
    )


@router.post("/food-safety/checklists/{checklist_id}/approve")
def approve_food_safety_checklist_v1(
    checklist_id: int,
    request: Request,
    manager: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    c = db.get(FoodSafetyChecklist, checklist_id)
    if not c:
        raise ApiError(404, "CHECKLIST_NOT_FOUND", "Food safety checklist not found.")
    c.approved_by_manager_id = manager.id
    c.approved_at = datetime.now(timezone.utc)
    record_audit(db, "food_safety.checklist_approved", "food_safety_checklist", c.id, manager.id, {"station": c.station_name})
    db.commit()
    return ok(request, {"id": c.id, "approvedByManagerId": manager.id, "approvedAt": as_utc(c.approved_at)})


@router.post("/food-safety/temperatures", status_code=201)
def record_temperature_log_v1(
    request: Request,
    payload: TemperatureLogIn,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    temp = float(payload.temperatureCelsius)
    if payload.storageZone == "cold_storage":
        safe = 0.0 <= temp <= 5.0
    elif payload.storageZone == "freezer":
        safe = temp <= -15.0
    else:  # hot_holding
        safe = temp >= 60.0

    row = TemperatureLog(
        equipment_or_item=f"{payload.equipmentOrItem.strip()} [{payload.sourceType}]",
        storage_Zone=payload.storageZone,
        temperature_celsius=payload.temperatureCelsius,
        within_safe_range=safe,
        corrective_action=payload.correctiveAction,
        recorded_by_staff_id=staff.id,
    )
    db.add(row)
    db.flush()
    record_audit(
        db,
        "food_safety.temperature_recorded",
        "temperature_log",
        row.id,
        staff.id,
        {"zone": payload.storageZone, "temp_c": temp, "safe": safe, "source": payload.sourceType},
    )
    db.commit()
    db.refresh(row)
    return ok(
        request,
        {
            "id": row.id,
            "equipmentOrItem": row.equipment_or_item,
            "storageZone": row.storage_Zone,
            "temperatureCelsius": float(row.temperature_celsius),
            "sourceType": payload.sourceType,
            "withinSafeRange": bool(row.within_safe_range),
            "alertTriggered": not bool(row.within_safe_range),
        },
        status_code=201,
    )


@router.get("/food-safety/summary")
def get_food_safety_summary_v1(
    request: Request,
    _: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    checks = db.scalars(select(FoodSafetyChecklist).order_by(FoodSafetyChecklist.id.desc()).limit(50)).all()
    temps = db.scalars(select(TemperatureLog).order_by(TemperatureLog.id.desc()).limit(50)).all()
    return ok(
        request,
        {
            "checklists": [
                {
                    "id": c.id,
                    "inspectionDate": c.inspection_date.isoformat(),
                    "checklistType": c.checklist_type,
                    "stationName": c.station_name,
                    "passed": bool(c.passed),
                    "approved": c.approved_by_manager_id is not None,
                }
                for c in checks
            ],
            "temperatureLogs": [
                {
                    "id": t.id,
                    "equipmentOrItem": t.equipment_or_item,
                    "storageZone": t.storage_Zone,
                    "temperatureCelsius": float(t.temperature_celsius),
                    "withinSafeRange": bool(t.within_safe_range),
                    "recordedAt": as_utc(t.recorded_at),
                }
                for t in temps
            ],
            "unsafeTempAlertsCount": sum(1 for t in temps if not t.within_safe_range),
        },
    )


# ==============================================================================
# 4. Reliable Notification Delivery Queue & User Preferences (Phase 65)
# ==============================================================================
class NotificationPrefsPatchIn(BaseModel):
    inAppEnabled: bool | None = None
    emailEnabled: bool | None = None
    webPushEnabled: bool | None = None
    smsEnabled: bool | None = None


class NotificationDispatchIn(BaseModel):
    userId: int = Field(gt=0)
    channel: str = Field(default="in_app", pattern="^(in_app|email|web_push|sms)$")
    eventType: str = Field(default="order_update", max_length=50)
    idempotencyKey: str | None = Field(default=None, max_length=100)
    subject: str = Field(min_length=2, max_length=200)
    body: str = Field(min_length=2, max_length=2000)
    simulateTransientFailure: bool = False


@router.get("/users/me/notification-preferences")
def get_my_notification_preferences_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pref = db.scalar(select(UserNotificationPreference).where(UserNotificationPreference.user_id == user.id))
    if not pref:
        pref = UserNotificationPreference(user_id=user.id)
        db.add(pref)
        db.commit()
        db.refresh(pref)
    return ok(
        request,
        {
            "inAppEnabled": bool(pref.in_app_enabled),
            "emailEnabled": bool(pref.email_enabled),
            "webPushEnabled": bool(pref.web_push_enabled),
            "smsEnabled": bool(pref.sms_enabled),
        },
    )


@router.patch("/users/me/notification-preferences")
def update_my_notification_preferences_v1(
    request: Request,
    payload: NotificationPrefsPatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pref = db.scalar(select(UserNotificationPreference).where(UserNotificationPreference.user_id == user.id))
    if not pref:
        pref = UserNotificationPreference(user_id=user.id)
        db.add(pref)
    if payload.inAppEnabled is not None:
        pref.in_app_enabled = payload.inAppEnabled
    if payload.emailEnabled is not None:
        pref.email_enabled = payload.emailEnabled
    if payload.webPushEnabled is not None:
        pref.web_push_enabled = payload.webPushEnabled
    if payload.smsEnabled is not None:
        pref.sms_enabled = payload.smsEnabled
    db.commit()
    db.refresh(pref)
    return ok(
        request,
        {
            "inAppEnabled": bool(pref.in_app_enabled),
            "emailEnabled": bool(pref.email_enabled),
            "webPushEnabled": bool(pref.web_push_enabled),
            "smsEnabled": bool(pref.sms_enabled),
        },
    )


@router.post("/notifications/dispatch", status_code=201)
def dispatch_reliable_notification_v1(
    request: Request,
    payload: NotificationDispatchIn,
    actor: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    idem = (payload.idempotencyKey or f"ntf-{uuid.uuid4().hex[:14]}").strip()
    existing = db.scalar(select(NotificationDeliveryLog).where(NotificationDeliveryLog.idempotency_key == idem))
    if existing:
        return ok(
            request,
            {"id": existing.id, "status": existing.status, "duplicateSkipped": True},
        )

    status_val = "failed" if payload.simulateTransientFailure else "delivered"
    row = NotificationDeliveryLog(
        user_id=payload.userId,
        channel=payload.channel,
        event_type=payload.eventType,
        idempotency_key=idem,
        subject=payload.subject.strip(),
        body=payload.body.strip(),
        status=status_val,
        retry_count=0,
        last_error="External provider transient timeout" if payload.simulateTransientFailure else None,
        next_retry_at=datetime.now(timezone.utc) if payload.simulateTransientFailure else None,
    )
    db.add(row)
    if status_val == "delivered" and payload.channel == "in_app":
        notify_user(db, payload.userId, payload.eventType, payload.subject, payload.body)
    db.commit()
    db.refresh(row)
    return ok(
        request,
        {"id": row.id, "status": row.status, "channel": row.channel, "retryCount": row.retry_count},
        status_code=201,
    )


@router.post("/notifications/retry-failed")
def retry_failed_notifications_v1(
    request: Request,
    actor: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    failed_rows = db.scalars(
        select(NotificationDeliveryLog).where(NotificationDeliveryLog.status == "failed").limit(100)
    ).all()
    retried = 0
    dead_lettered = 0
    for row in failed_rows:
        row.retry_count += 1
        if row.retry_count >= 5:
            row.status = "dead_letter"
            dead_lettered += 1
        elif row.channel == "in_app":
            notify_user(db, row.user_id, row.event_type, row.subject, row.body)
            row.status = "delivered"
            row.last_error = None
            retried += 1
        else:
            row.next_retry_at = datetime.now(timezone.utc) + timedelta(minutes=2 ** row.retry_count)
            retried += 1
    db.commit()
    return ok(request, {"processedCount": len(failed_rows), "retriedCount": retried, "deadLetteredCount": dead_lettered})


# ==============================================================================
# 5. Phase 81 — Sustainability & Waste, Workforce Shifts, Procurement & Smart Pricing
# ==============================================================================
class WasteRecordIn(BaseModel):
    productId: int | None = Field(default=None, gt=0)
    ingredientId: int | None = Field(default=None, gt=0)
    itemName: str = Field(min_length=2, max_length=140)
    wasteCategory: str = Field(pattern="^(prepared_food|unserved_food|returned_food|spoiled_food|expired_ingredient|prep_waste)$")
    quantity: Decimal = Field(gt=Decimal("0"))
    unit: str = Field(default="units", max_length=20)
    reason: str = Field(min_length=3, max_length=255)
    idempotencyKey: str | None = Field(default=None, max_length=80)


class StaffShiftIn(BaseModel):
    staffUserId: int = Field(gt=0)
    stationRole: str = Field(default="kitchen_prep", pattern="^(kitchen_prep|packing|pickup|inventory|counter)$")
    shiftStart: datetime
    shiftEnd: datetime
    breakMinutes: int = Field(default=30, ge=0, le=180)


class PurchaseRequisitionIn(BaseModel):
    supplierId: int | None = Field(default=None, gt=0)
    items: list[dict[str, Any]] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=255)


class SupplierInvoiceIn(BaseModel):
    invoiceReference: str = Field(min_length=2, max_length=60)
    purchaseOrderId: int = Field(gt=0)
    invoiceAmountPaise: int = Field(ge=0)
    notes: str | None = Field(default=None, max_length=500)


class PricingRuleIn(BaseModel):
    productId: int = Field(gt=0)
    ruleName: str = Field(min_length=2, max_length=120)
    ruleType: str = Field(default="scheduled_window", pattern="^(scheduled_window|outlet_override|promo|combo)$")
    mealWindow: str = Field(default="lunch", pattern="^(breakfast|lunch|evening|all_day)$")
    proposedPricePaise: int = Field(gt=0)
    minPricePaise: int = Field(gt=0)
    maxPricePaise: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=255)


@router.post("/waste/records", status_code=201)
def record_sustainability_waste_v1(
    request: Request,
    payload: WasteRecordIn,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    idem = (payload.idempotencyKey or f"wst-{uuid.uuid4().hex[:14]}").strip()
    existing = db.scalar(select(WasteRecord).where(WasteRecord.idempotency_key == idem))
    if existing:
        return ok(request, {"id": existing.id, "duplicateSkipped": True, "stockDeducted": bool(existing.stock_deducted)})

    cost_loss = 0
    deducted = False
    if payload.productId is not None:
        p = db.get(Product, payload.productId)
        if p:
            qty_int = max(1, int(payload.quantity))
            record_product_stock_movement(db, p, -qty_int, "waste", f"Waste ({payload.wasteCategory}): {payload.reason}", actor_user_id=staff.id)
            cost_loss = qty_int * int(p.cost_paise or p.effective_price_paise)
            deducted = True
    elif payload.ingredientId is not None:
        ing = db.get(Ingredient, payload.ingredientId)
        if ing:
            ing.current_quantity = max(Decimal("0.00"), Decimal(ing.current_quantity) - Decimal(payload.quantity))
            if ing.cost_per_unit is not None:
                cost_loss = int(round(float(Decimal(payload.quantity) * Decimal(ing.cost_per_unit)) * 100))
            deducted = True

    rec = WasteRecord(
        idempotency_key=idem,
        product_id=payload.productId,
        ingredient_id=payload.ingredientId,
        item_name=payload.itemName.strip(),
        waste_category=payload.wasteCategory,
        quantity=payload.quantity,
        unit=payload.unit,
        cost_loss_paise=cost_loss,
        reason=payload.reason.strip(),
        stock_deducted=deducted,
        recorded_by_staff_id=staff.id,
    )
    db.add(rec)
    db.flush()
    record_audit(db, "sustainability.waste_recorded", "waste_record", rec.id, staff.id, {"item": rec.item_name, "loss_paise": cost_loss})
    db.commit()
    db.refresh(rec)
    return ok(
        request,
        {
            "id": rec.id,
            "itemName": rec.item_name,
            "wasteCategory": rec.waste_category,
            "quantity": float(rec.quantity),
            "costLossPaise": rec.cost_loss_paise,
            "stockDeducted": bool(rec.stock_deducted),
        },
        status_code=201,
    )


@router.get("/waste/dashboard")
def get_sustainability_waste_dashboard_v1(
    request: Request,
    _: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(select(WasteRecord).order_by(WasteRecord.recorded_at.desc()).limit(200)).all()
    by_cat: dict[str, int] = {}
    for r in rows:
        by_cat[r.waste_category] = by_cat.get(r.waste_category, 0) + int(r.cost_loss_paise or 0)
    return ok(
        request,
        {
            "totalRecords": len(rows),
            "totalWasteCostPaise": sum(int(r.cost_loss_paise or 0) for r in rows),
            "wasteByCategoryPaise": by_cat,
            "recentItems": [
                {
                    "id": r.id,
                    "itemName": r.item_name,
                    "wasteCategory": r.waste_category,
                    "quantity": float(r.quantity),
                    "costLossPaise": r.cost_loss_paise,
                    "recordedAt": as_utc(r.recorded_at),
                }
                for r in rows[:20]
            ],
        },
    )


@router.get("/workforce/shifts")
def list_staff_shifts_v1(
    request: Request,
    user: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    stmt = select(StaffShift).order_by(StaffShift.shift_start.desc())
    if user.role == UserRole.STAFF:
        stmt = stmt.where(StaffShift.staff_user_id == user.id)
    rows = db.scalars(stmt.limit(40)).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": s.id,
                    "staffUserId": s.staff_user_id,
                    "stationRole": s.station_role,
                    "status": s.status,
                    "lateMinutes": s.late_minutes,
                    "shiftStart": as_utc(s.shift_start),
                    "shiftEnd": as_utc(s.shift_end),
                }
                for s in rows
            ]
        },
    )


@router.post("/workforce/shifts", status_code=201)
def create_staff_shift_v1(
    request: Request,
    payload: StaffShiftIn,
    manager: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    if payload.shiftEnd <= payload.shiftStart:
        raise ApiError(422, "INVALID_SHIFT_WINDOW", "Shift end must be after shift start.")

    overlap = db.scalar(
        select(StaffShift).where(
            StaffShift.staff_user_id == payload.staffUserId,
            StaffShift.status != "cancelled",
            StaffShift.shift_start < payload.shiftEnd,
            StaffShift.shift_end > payload.shiftStart,
        )
    )
    if overlap:
        raise ApiError(409, "SHIFT_CONFLICT", "Staff member already has an overlapping shift scheduled.")

    sh = StaffShift(
        staff_user_id=payload.staffUserId,
        station_role=payload.stationRole,
        shift_start=payload.shiftStart,
        shift_end=payload.shiftEnd,
        break_minutes=payload.breakMinutes,
        created_by_manager_id=manager.id,
    )
    db.add(sh)
    db.flush()
    record_audit(db, "workforce.shift_created", "staff_shift", sh.id, manager.id, {"staff_id": sh.staff_user_id, "role": sh.station_role})
    db.commit()
    db.refresh(sh)
    return ok(
        request,
        {
            "id": sh.id,
            "staffUserId": sh.staff_user_id,
            "stationRole": sh.station_role,
            "status": sh.status,
            "shiftStart": as_utc(sh.shift_start),
            "shiftEnd": as_utc(sh.shift_end),
        },
        status_code=201,
    )


@router.post("/workforce/shifts/{shift_id}/check-in")
def check_in_staff_shift_v1(
    shift_id: int,
    request: Request,
    user: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    sh = db.get(StaffShift, shift_id)
    if not sh:
        raise ApiError(404, "SHIFT_NOT_FOUND", "Staff shift not found.")
    if user.role == UserRole.STAFF and sh.staff_user_id != user.id:
        raise ApiError(403, "SHIFT_FORBIDDEN", "Cannot check into another staff member's shift.")
    now = datetime.now(timezone.utc)
    start = sh.shift_start if sh.shift_start.tzinfo else sh.shift_start.replace(tzinfo=timezone.utc)
    sh.checked_in_at = now
    sh.status = "active"
    sh.late_minutes = max(0, int((now - start).total_seconds() // 60))
    db.commit()
    return ok(request, {"id": sh.id, "status": sh.status, "lateMinutes": sh.late_minutes, "checkedInAt": as_utc(now)})


@router.post("/workforce/shifts/{shift_id}/check-out")
def check_out_staff_shift_v1(
    shift_id: int,
    request: Request,
    handoverNotes: str | None = Query(default=None, max_length=500),
    user: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    sh = db.get(StaffShift, shift_id)
    if not sh:
        raise ApiError(404, "SHIFT_NOT_FOUND", "Staff shift not found.")
    sh.checked_out_at = datetime.now(timezone.utc)
    sh.status = "completed"
    if handoverNotes:
        sh.handover_notes = handoverNotes.strip()
    db.commit()
    return ok(request, {"id": sh.id, "status": sh.status, "handoverNotes": sh.handover_notes})


@router.post("/procurement/requisitions", status_code=201)
def create_purchase_requisition_v1(
    request: Request,
    payload: PurchaseRequisitionIn,
    user: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    req_num = f"REQ-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    est_paise = sum(int(it.get("quantity", 1)) * int(it.get("unitCostPaise", 0)) for it in payload.items)
    row = PurchaseRequisition(
        requisition_number=req_num,
        supplier_id=payload.supplierId,
        items_json=json.dumps(payload.items),
        estimated_total_paise=est_paise,
        reason=payload.reason.strip(),
        requested_by_user_id=user.id,
    )
    db.add(row)
    db.flush()
    record_audit(db, "procurement.requisition_created", "purchase_requisition", row.id, user.id, {"req_number": req_num, "est_paise": est_paise})
    db.commit()
    db.refresh(row)
    return ok(request, {"id": row.id, "requisitionNumber": row.requisition_number, "status": row.status, "estimatedTotalPaise": est_paise}, status_code=201)


@router.post("/procurement/requisitions/{req_id}/approve")
def approve_purchase_requisition_v1(
    req_id: int,
    request: Request,
    manager: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    req_row = db.get(PurchaseRequisition, req_id)
    if not req_row:
        raise ApiError(404, "REQUISITION_NOT_FOUND", "Purchase requisition not found.")
    if req_row.status == "po_generated":
        return ok(request, {"id": req_row.id, "status": req_row.status, "purchaseOrderId": req_row.purchase_order_id})

    supp = db.get(Supplier, req_row.supplier_id) if req_row.supplier_id else db.scalar(select(Supplier))
    if not supp:
        supp = Supplier(name="Verified Campus Supplier", contact_name="Ops Desk", phone="+919000000000", is_active=True)
        db.add(supp)
        db.flush()

    po_num = f"PO-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    po = PurchaseOrder(
        supplier_id=supp.id,
        status="ordered",
        lines_json=req_row.items_json or "[]",
        total_cost_paise=req_row.estimated_total_paise,
        notes=f"{po_num} — Approved from {req_row.requisition_number}",
        created_by_user_id=manager.id,
    )
    db.add(po)
    db.flush()
    req_row.status = "po_generated"
    req_row.approved_by_user_id = manager.id
    req_row.purchase_order_id = po.id
    record_audit(db, "procurement.requisition_approved", "purchase_requisition", req_row.id, manager.id, {"po_id": po.id})
    db.commit()
    return ok(request, {"id": req_row.id, "status": req_row.status, "purchaseOrderId": po.id, "poNumber": po_num})


@router.post("/procurement/invoices", status_code=201)
def record_supplier_invoice_v1(
    request: Request,
    payload: SupplierInvoiceIn,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    po = db.get(PurchaseOrder, payload.purchaseOrderId)
    if not po:
        raise ApiError(404, "PO_NOT_FOUND", "Purchase order not found.")
    expected = int(po.total_cost_paise or 0)
    disc = payload.invoiceAmountPaise - expected
    inv = SupplierInvoice(
        invoice_reference=payload.invoiceReference.strip(),
        purchase_order_id=po.id,
        supplier_id=po.supplier_id,
        invoice_amount_paise=payload.invoiceAmountPaise,
        po_expected_paise=expected,
        discrepancy_paise=disc,
        discrepancy_flagged=(disc != 0),
        notes=payload.notes,
        recorded_by_user_id=admin.id,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return ok(
        request,
        {
            "id": inv.id,
            "invoiceReference": inv.invoice_reference,
            "discrepancyPaise": inv.discrepancy_paise,
            "discrepancyFlagged": bool(inv.discrepancy_flagged),
        },
        status_code=201,
    )


@router.post("/menu/pricing-rules", status_code=201)
def propose_smart_pricing_rule_v1(
    request: Request,
    payload: PricingRuleIn,
    user: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    if not (payload.minPricePaise <= payload.proposedPricePaise <= payload.maxPricePaise):
        raise ApiError(422, "PRICE_OUT_OF_SAFE_BOUNDS", "Proposed price must fall within configured min/max safeguards.")
    rule = MenuPricingRule(
        product_id=payload.productId,
        rule_name=payload.ruleName.strip(),
        rule_type=payload.ruleType,
        meal_window=payload.mealWindow,
        proposed_price_paise=payload.proposedPricePaise,
        min_price_paise=payload.minPricePaise,
        max_price_paise=payload.maxPricePaise,
        reason=payload.reason.strip(),
        created_by_user_id=user.id,
    )
    db.add(rule)
    db.flush()
    record_audit(db, "menu_pricing.proposed", "menu_pricing_rule", rule.id, user.id, {"proposed_paise": rule.proposed_price_paise})
    db.commit()
    db.refresh(rule)
    return ok(request, {"id": rule.id, "status": rule.status, "proposedPricePaise": rule.proposed_price_paise}, status_code=201)


@router.post("/menu/pricing-rules/{rule_id}/approve")
def approve_smart_pricing_rule_v1(
    rule_id: int,
    request: Request,
    manager: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    rule = db.get(MenuPricingRule, rule_id)
    if not rule:
        raise ApiError(404, "PRICING_RULE_NOT_FOUND", "Pricing rule not found.")
    if rule.created_by_user_id == manager.id:
        raise ApiError(403, "SEPARATION_OF_DUTIES", "Creator cannot self-approve their own price change rule.")
    prod = db.get(Product, rule.product_id)
    if not prod:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")

    old_p = prod.effective_price_paise
    prod.price_paise = rule.proposed_price_paise
    prod.price = Decimal(rule.proposed_price_paise) / Decimal("100")
    rule.status = "approved"
    rule.approved_by_user_id = manager.id
    rule.approved_at = datetime.now(timezone.utc)
    record_audit(db, "menu_pricing.approved", "product", prod.id, manager.id, {"old_paise": old_p, "new_paise": rule.proposed_price_paise, "reason": rule.reason})
    db.commit()
    return ok(request, {"id": rule.id, "status": rule.status, "newPricePaise": prod.price_paise})


# ==============================================================================
# 6. Phase 82 — Group Orders, Multi-Dim Quality Feedback, Meal Passes & Closing
# ==============================================================================
class GroupOrderCreateIn(BaseModel):
    title: str = Field(default="Friends Lunch Group Order", min_length=2, max_length=140)
    counterId: int = Field(default=1, ge=1)
    pickupSlot: str = Field(default="12:45-13:00", max_length=60)
    closesInMinutes: int = Field(default=20, ge=5, le=240)


class GroupOrderJoinAddIn(BaseModel):
    menuItemId: int = Field(gt=0)
    quantity: int = Field(default=1, ge=1, le=20)
    fulfillmentType: str = Field(default="READY_TO_EAT", pattern="^(PARCEL|READY_TO_EAT)$")


class QualityFeedbackIn(BaseModel):
    foodQualityRating: int = Field(ge=1, le=5)
    tasteRating: int = Field(ge=1, le=5)
    packagingRating: int = Field(ge=1, le=5)
    waitTimeRating: int = Field(ge=1, le=5)
    outletRating: int = Field(ge=1, le=5)
    missingOrIncorrectItems: bool = False
    hygieneConcern: bool = False
    comment: str | None = Field(default=None, max_length=500)


class MealPassPlanIn(BaseModel):
    planCode: str = Field(min_length=2, max_length=40)
    name: str = Field(min_length=2, max_length=120)
    passPeriod: str = Field(default="weekly", pattern="^(daily|weekly|monthly)$")
    totalMeals: int = Field(ge=1, le=120)
    validityDays: int = Field(ge=1, le=90)
    pricePaise: int = Field(gt=0)


class DailyClosingCreateIn(BaseModel):
    businessDate: date | None = None
    counterId: int = Field(default=1, ge=1)
    discrepancyNotes: str | None = Field(default=None, max_length=500)


class DailyClosingAdjustmentIn(BaseModel):
    reason: str = Field(min_length=3, max_length=255)
    deltaPaise: int


@router.get("/group-orders")
def list_my_group_orders_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(GroupOrder)
        .where(GroupOrder.organizer_user_id == user.id)
        .order_by(GroupOrder.created_at.desc())
        .limit(20)
    ).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": g.id,
                    "inviteCode": g.invite_code,
                    "title": g.title,
                    "pickupSlot": g.pickup_slot,
                    "status": g.status,
                    "totalPaise": g.total_paise,
                    "closesAt": as_utc(g.closes_at),
                }
                for g in rows
            ]
        },
    )


@router.post("/group-orders", status_code=201)
def create_group_order_v1(
    request: Request,
    payload: GroupOrderCreateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    code = f"GRP-{secrets.token_hex(4).upper()}"
    grp = GroupOrder(
        invite_code=code,
        organizer_user_id=user.id,
        title=payload.title.strip(),
        counter_id=payload.counterId,
        pickup_slot=payload.pickupSlot.strip(),
        closes_at=datetime.now(timezone.utc) + timedelta(minutes=payload.closesInMinutes),
    )
    db.add(grp)
    db.commit()
    db.refresh(grp)
    return ok(request, {"id": grp.id, "inviteCode": grp.invite_code, "title": grp.title, "status": grp.status}, status_code=201)


@router.post("/group-orders/{invite_code}/items", status_code=201)
def add_item_to_group_order_v1(
    invite_code: str,
    request: Request,
    payload: GroupOrderJoinAddIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    grp = db.scalar(select(GroupOrder).where(GroupOrder.invite_code == invite_code.strip().upper()))
    if not grp:
        raise ApiError(404, "GROUP_ORDER_NOT_FOUND", "Group order invite code not found.")
    if grp.status != "open":
        raise ApiError(409, "GROUP_ORDER_LOCKED", "Group order is closed for new additions.")

    p = db.get(Product, payload.menuItemId)
    if not p or not p.is_available:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item unavailable.")

    f_cfg = db.scalar(select(ProductFulfillmentConfig).where(ProductFulfillmentConfig.product_id == p.id))
    pkg_paise = (int(f_cfg.packaging_charge_paise or 0) * payload.quantity) if (f_cfg and payload.fulfillmentType == "PARCEL") else 0
    line_paise = (p.effective_price_paise * payload.quantity) + pkg_paise

    item = GroupOrderParticipantItem(
        group_order_id=grp.id,
        participant_user_id=user.id,
        participant_name=user.full_name,
        product_id=p.id,
        product_name=p.name,
        quantity=payload.quantity,
        fulfillment_type=payload.fulfillmentType,
        unit_price_paise=p.effective_price_paise,
        packaging_charge_paise=pkg_paise,
        line_total_paise=line_paise,
    )
    db.add(item)
    grp.total_paise += line_paise
    db.commit()
    return ok(
        request,
        {
            "groupOrderId": grp.id,
            "inviteCode": grp.invite_code,
            "addedItem": item.product_name,
            "fulfillmentType": item.fulfillment_type,
            "totalPaise": grp.total_paise,
        },
        status_code=201,
    )


@router.get("/group-orders/{invite_code}")
def get_group_order_v1(
    invite_code: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    grp = db.scalar(select(GroupOrder).where(GroupOrder.invite_code == invite_code.strip().upper()))
    if not grp:
        raise ApiError(404, "GROUP_ORDER_NOT_FOUND", "Group order not found.")
    lines = db.scalars(
        select(GroupOrderParticipantItem).where(GroupOrderParticipantItem.group_order_id == grp.id)
    ).all()
    return ok(
        request,
        {
            "id": grp.id,
            "inviteCode": grp.invite_code,
            "title": grp.title,
            "status": grp.status,
            "totalPaise": grp.total_paise,
            "items": [
                {
                    "id": it.id,
                    "participantName": it.participant_name,
                    "productName": it.product_name,
                    "quantity": it.quantity,
                    "fulfillmentType": it.fulfillment_type,
                    "lineTotalPaise": it.line_total_paise,
                }
                for it in lines
            ],
        },
    )


@router.post("/orders/{order_id}/quality-feedback", status_code=201)
def submit_order_quality_feedback_v1(
    order_id: int,
    request: Request,
    payload: QualityFeedbackIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = db.get(Order, order_id)
    if not order:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")
    if user.role == UserRole.STUDENT and order.user_id != user.id:
        raise ApiError(403, "FORBIDDEN", "You can only rate your own orders.")
    if order.status not in {OrderStatus.COMPLETED, OrderStatus.COLLECTED}:
        raise ApiError(409, "ORDER_NOT_COLLECTED", "Feedback can only be submitted after order collection.")

    existing = db.scalar(select(OrderQualityFeedback).where(OrderQualityFeedback.order_id == order.id))
    if existing:
        raise ApiError(409, "DUPLICATE_FEEDBACK", "Quality feedback was already submitted for this order.")

    escalated_tck_id = None
    if payload.hygieneConcern or payload.missingOrIncorrectItems:
        tck = SupportTicket(
            ticket_number=f"TCK-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}",
            user_id=user.id,
            order_id=order.id,
            category="hygiene" if payload.hygieneConcern else "missing_item",
            priority="high",
            status="OPEN",
            subject=f"Auto-escalated feedback issue on Order #{order.order_number or order.id}",
            description=payload.comment or "Customer reported hygiene/missing item concern in post-collection feedback.",
            escalated_to_manager=True,
        )
        db.add(tck)
        db.flush()
        escalated_tck_id = tck.id

    fb = OrderQualityFeedback(
        order_id=order.id,
        user_id=user.id,
        food_quality_rating=payload.foodQualityRating,
        taste_rating=payload.tasteRating,
        packaging_rating=payload.packagingRating,
        wait_time_rating=payload.waitTimeRating,
        outlet_rating=payload.outletRating,
        missing_or_incorrect_items=payload.missingOrIncorrectItems,
        hygiene_concern=payload.hygieneConcern,
        comment=payload.comment,
        escalated_ticket_id=escalated_tck_id,
    )
    db.add(fb)
    db.commit()
    db.refresh(fb)
    return ok(request, {"id": fb.id, "orderId": order.id, "escalatedTicketId": escalated_tck_id}, status_code=201)


@router.get("/meal-passes/plans")
def list_meal_pass_plans_v1(
    request: Request,
    db: Session = Depends(get_db),
):
    plans = db.scalars(
        select(MealPassPlan).where(MealPassPlan.is_active.is_(True)).order_by(MealPassPlan.price_paise.asc())
    ).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": p.id,
                    "planCode": p.plan_code,
                    "name": p.name,
                    "passPeriod": p.pass_period,
                    "totalMeals": p.total_meals,
                    "validityDays": p.validity_days,
                    "pricePaise": p.price_paise,
                }
                for p in plans
            ]
        },
    )


@router.get("/meal-passes/me")
def list_my_meal_passes_v1(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = db.execute(
        select(StudentMealPass, MealPassPlan)
        .join(MealPassPlan, MealPassPlan.id == StudentMealPass.plan_id)
        .where(StudentMealPass.user_id == user.id)
        .order_by(StudentMealPass.created_at.desc())
        .limit(20)
    ).all()
    return ok(
        request,
        {
            "items": [
                {
                    "passId": mp.id,
                    "planId": plan.id,
                    "planName": plan.name,
                    "planCode": plan.plan_code,
                    "status": mp.status,
                    "remainingMeals": mp.remaining_meals,
                    "totalMeals": plan.total_meals,
                    "expiresAt": as_utc(mp.expires_at),
                }
                for mp, plan in rows
            ]
        },
    )


@router.post("/meal-passes/plans", status_code=201)
def create_meal_pass_plan_v1(
    request: Request,
    payload: MealPassPlanIn,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    plan = MealPassPlan(
        plan_code=payload.planCode.strip().upper(),
        name=payload.name.strip(),
        pass_period=payload.passPeriod,
        total_meals=payload.totalMeals,
        validity_days=payload.validityDays,
        price_paise=payload.pricePaise,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return ok(request, {"id": plan.id, "planCode": plan.plan_code, "name": plan.name, "totalMeals": plan.total_meals, "pricePaise": plan.price_paise}, status_code=201)


@router.post("/meal-passes/{plan_id}/purchase")
def purchase_meal_pass_v1(
    plan_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    plan = db.get(MealPassPlan, plan_id)
    if not plan or not plan.is_active:
        raise ApiError(404, "MEAL_PASS_PLAN_NOT_FOUND", "Meal pass plan not found.")
    gw_ord = f"order_mp_{uuid.uuid4().hex[:12]}"
    mp = StudentMealPass(
        user_id=user.id,
        plan_id=plan.id,
        status="payment_pending",
        remaining_meals=plan.total_meals,
        gateway_order_id=gw_ord,
        expires_at=datetime.now(timezone.utc) + timedelta(days=plan.validity_days),
    )
    db.add(mp)
    db.commit()
    db.refresh(mp)
    return ok(request, {"passId": mp.id, "status": mp.status, "gatewayOrderId": gw_ord, "remainingMeals": mp.remaining_meals})


@router.post("/meal-passes/{pass_id}/activate-verified")
def activate_meal_pass_verified_v1(
    pass_id: int,
    request: Request,
    gatewayPaymentId: str = Query(min_length=4),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    mp = db.get(StudentMealPass, pass_id)
    if not mp or (user.role == UserRole.STUDENT and mp.user_id != user.id):
        raise ApiError(404, "MEAL_PASS_NOT_FOUND", "Student meal pass not found.")
    mp.gateway_payment_id = gatewayPaymentId.strip()
    mp.status = "active"
    db.commit()
    return ok(request, {"passId": mp.id, "status": mp.status, "remainingMeals": mp.remaining_meals})


@router.post("/meal-passes/{pass_id}/redeem")
def redeem_meal_pass_meal_v1(
    pass_id: int,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    idem = (idempotency_key or f"mp-use-{uuid.uuid4().hex[:12]}").strip()
    existing_use = db.scalar(select(MealPassUsage).where(MealPassUsage.idempotency_key == idem))
    if existing_use:
        mp_cur = db.get(StudentMealPass, pass_id)
        return ok(request, {"passId": pass_id, "duplicateSkipped": True, "remainingMeals": mp_cur.remaining_meals if mp_cur else 0})

    upd = db.execute(
        update(StudentMealPass)
        .where(
            StudentMealPass.id == pass_id,
            StudentMealPass.status == "active",
            StudentMealPass.remaining_meals >= 1,
        )
        .values(remaining_meals=StudentMealPass.remaining_meals - 1)
    )
    if (upd.rowcount or 0) != 1:
        raise ApiError(409, "MEAL_PASS_UNAVAILABLE", "Meal pass is inactive, expired, or has zero remaining meals.")

    mp = db.get(StudentMealPass, pass_id)
    if mp and mp.remaining_meals == 0:
        mp.status = "exhausted"
    db.add(MealPassUsage(student_pass_id=pass_id, idempotency_key=idem, meals_deducted=1))
    db.commit()
    return ok(request, {"passId": pass_id, "remainingMeals": mp.remaining_meals, "status": mp.status})


@router.post("/admin/closings", status_code=201)
def submit_daily_canteen_closing_v1(
    request: Request,
    payload: DailyClosingCreateIn,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    b_date = payload.businessDate or date.today()
    existing = db.scalar(
        select(DailyCanteenClosing).where(
            DailyCanteenClosing.business_date == b_date,
            DailyCanteenClosing.counter_id == payload.counterId,
        )
    )
    if existing:
        if existing.is_locked:
            raise ApiError(409, "CLOSING_LOCKED", "Daily closing record is already locked and approved.")
        return ok(request, {"id": existing.id, "businessDate": existing.business_date.isoformat(), "isLocked": False})

    tot_orders = db.scalar(select(func.count(Order.id))) or 0
    comp_orders = db.scalar(select(func.count(Order.id)).where(Order.status.in_([OrderStatus.COMPLETED, OrderStatus.COLLECTED]))) or 0
    canc_orders = db.scalar(select(func.count(Order.id)).where(Order.status == OrderStatus.CANCELLED)) or 0
    succ_paise = db.scalar(select(func.coalesce(func.sum(Order.total_paise), 0)).where(Order.payment_status == PaymentStatus.PAID)) or 0
    pend_paise = db.scalar(select(func.coalesce(func.sum(Order.total_paise), 0)).where(Order.payment_status == PaymentStatus.PENDING)) or 0
    waste_paise = db.scalar(select(func.coalesce(func.sum(WasteRecord.cost_loss_paise), 0))) or 0

    row = DailyCanteenClosing(
        business_date=b_date,
        counter_id=payload.counterId,
        total_orders=int(tot_orders),
        completed_orders=int(comp_orders),
        cancelled_orders=int(canc_orders),
        successful_payments_paise=int(succ_paise),
        pending_payments_paise=int(pend_paise),
        wastage_loss_paise=int(waste_paise),
        discrepancy_notes=payload.discrepancyNotes,
        submitted_by_user_id=staff.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return ok(
        request,
        {
            "id": row.id,
            "businessDate": row.business_date.isoformat(),
            "successfulPaymentsPaise": row.successful_payments_paise,
            "wastageLossPaise": row.wastage_loss_paise,
            "isLocked": bool(row.is_locked),
        },
        status_code=201,
    )


@router.post("/admin/closings/{closing_id}/approve")
def approve_and_lock_daily_closing_v1(
    closing_id: int,
    request: Request,
    manager: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    row = db.get(DailyCanteenClosing, closing_id)
    if not row:
        raise ApiError(404, "CLOSING_NOT_FOUND", "Daily closing record not found.")
    row.is_locked = True
    row.approved_by_manager_id = manager.id
    row.locked_at = datetime.now(timezone.utc)
    record_audit(db, "daily_closing.locked", "daily_canteen_closing", row.id, manager.id, {"date": row.business_date.isoformat()})
    db.commit()
    return ok(request, {"id": row.id, "isLocked": True, "lockedAt": as_utc(row.locked_at)})


@router.post("/admin/closings/{closing_id}/adjustments")
def add_daily_closing_adjustment_v1(
    closing_id: int,
    request: Request,
    payload: DailyClosingAdjustmentIn,
    manager: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    row = db.get(DailyCanteenClosing, closing_id)
    if not row:
        raise ApiError(404, "CLOSING_NOT_FOUND", "Daily closing record not found.")
    entries = json.loads(row.adjustments_json or "[]")
    entries.append(
        {
            "reason": payload.reason.strip(),
            "deltaPaise": payload.deltaPaise,
            "recordedByUserId": manager.id,
            "recordedAt": datetime.now(timezone.utc).isoformat(),
        }
    )
    row.adjustments_json = json.dumps(entries)
    record_audit(db, "daily_closing.adjustment_added", "daily_canteen_closing", row.id, manager.id, {"delta_paise": payload.deltaPaise})
    db.commit()
    return ok(request, {"id": row.id, "adjustmentsCount": len(entries), "isLocked": bool(row.is_locked)})


# ==============================================================================
# 7. Phase 83 — Equipment Maintenance Registry & Breakdown Tracking
# ==============================================================================
class EquipmentCreateIn(BaseModel):
    stationId: int | None = Field(default=None, gt=0)
    name: str = Field(min_length=2, max_length=140)
    category: str = Field(default="chiller", max_length=60)
    assetCode: str = Field(min_length=2, max_length=60)


class EquipmentBreakdownIn(BaseModel):
    priority: str = Field(default="high", pattern="^(low|normal|high|critical)$")
    issueSummary: str = Field(min_length=3, max_length=500)


@router.post("/equipment/assets", status_code=201)
def register_equipment_asset_v1(
    request: Request,
    payload: EquipmentCreateIn,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    eq = EquipmentAsset(
        station_id=payload.stationId,
        name=payload.name.strip(),
        category=payload.category.strip(),
        asset_code=payload.assetCode.strip().upper(),
    )
    db.add(eq)
    db.commit()
    db.refresh(eq)
    return ok(request, {"id": eq.id, "name": eq.name, "assetCode": eq.asset_code, "operationalStatus": eq.operational_status}, status_code=201)


@router.get("/equipment/assets")
def list_equipment_assets_v1(
    request: Request,
    _: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(select(EquipmentAsset).order_by(EquipmentAsset.id.desc()).limit(50)).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": e.id,
                    "name": e.name,
                    "category": e.category,
                    "assetCode": e.asset_code,
                    "operationalStatus": e.operational_status,
                }
                for e in rows
            ]
        },
    )


@router.get("/admin/closings")
def list_daily_closings_v1(
    request: Request,
    _: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(select(DailyCanteenClosing).order_by(DailyCanteenClosing.business_date.desc()).limit(30)).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": c.id,
                    "businessDate": c.business_date.isoformat(),
                    "counterId": c.counter_id,
                    "totalOrders": c.total_orders,
                    "completedOrders": c.completed_orders,
                    "successfulPaymentsPaise": c.successful_payments_paise,
                    "wastageLossPaise": c.wastage_loss_paise,
                    "isLocked": bool(c.is_locked),
                }
                for c in rows
            ]
        },
    )


@router.post("/equipment/{equipment_id}/report-breakdown", status_code=201)
def report_equipment_breakdown_v1(
    equipment_id: int,
    request: Request,
    payload: EquipmentBreakdownIn,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    eq = db.get(EquipmentAsset, equipment_id)
    if not eq:
        raise ApiError(404, "EQUIPMENT_NOT_FOUND", "Equipment asset not found.")
    eq.operational_status = "down"
    tck = EquipmentMaintenanceTicket(
        equipment_id=eq.id,
        priority=payload.priority,
        status="open",
        issue_summary=payload.issueSummary.strip(),
        reported_by_user_id=staff.id,
    )
    db.add(tck)
    record_audit(db, "equipment.breakdown_reported", "equipment_asset", eq.id, staff.id, {"priority": payload.priority})
    db.commit()
    db.refresh(tck)
    return ok(request, {"ticketId": tck.id, "equipmentId": eq.id, "operationalStatus": eq.operational_status}, status_code=201)
