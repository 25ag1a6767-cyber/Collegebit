"""CollegeBits 3.0 — High-Concurrency Bulk Ordering, Dual-Mode Payments, Cryptographic QR Tokens,
Two-Counter Physical Pickup, Kitchen Batch Preparation & Strict No-Refund Incident Management.
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import hmac
import io
import json
import secrets
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db
from app.envelope import ApiError, ok
from app.models import (
    BulkBackgroundJob,
    BulkMealToken,
    BulkOrder,
    BulkOrderAllocation,
    BulkOrderItem,
    BulkOrderPayment,
    BulkOrderRecipient,
    BulkPickupRecord,
    CanteenCounter,
    InventoryReservation,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentIncident,
    PaymentStatus,
    PaymentWebhookEvent,
    Pickup,
    PickupSlot,
    PickupToken,
    Product,
    User,
    UserRole,
)
from app.modules.common import (
    as_utc,
    notify_user,
    record_audit,
    record_order_status_change,
)
from app.modules.reporting_engine import render_pdf_document
from app.security import get_current_user, require_roles

router = APIRouter(tags=["v3-bulk-kitchen-pickup"])

# Bounded semaphore to control concurrent external gateway calls during burst individual payments
_GATEWAY_SEMAPHORE = threading.BoundedSemaphore(32)

VALID_BULK_STAGES = ("draft", "verified", "reserved", "preparing", "packing", "ready", "partially_collected", "collected", "cancelled")


# ==============================================================================
# Cryptographic Bulk Token Helpers (Stores ONLY SHA-256 token_hash in DB)
# ==============================================================================
def _derive_raw_bulk_token(bulk_order_id: int, recipient_id: int, key_nonce: str) -> str:
    """Derives the recipient's unique cryptographic QR token in-memory using server SECRET_KEY + key_nonce.
    The raw token is NEVER stored in the database or written to logs.
    """
    secret = get_settings().secret_key.encode("utf-8")
    msg = f"bmt:v3:{bulk_order_id}:{recipient_id}:{key_nonce}".encode("utf-8")
    mac = hmac.new(secret, msg, hashlib.sha256).hexdigest()[:28]
    return f"bmt_{bulk_order_id}_{recipient_id}_{mac}"


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.strip().encode("utf-8")).hexdigest()


def _recompute_bulk_order_totals(db: Session, bulk_order: BulkOrder) -> None:
    """Synchronizes recipient, payment, token, and pickup counters on the master BulkOrder."""
    recipients = db.scalars(
        select(BulkOrderRecipient).where(BulkOrderRecipient.bulk_order_id == bulk_order.id)
    ).all()
    items = db.scalars(
        select(BulkOrderItem).where(BulkOrderItem.bulk_order_id == bulk_order.id)
    ).all()

    total_rec = len(recipients)
    paid_rec = sum(1 for r in recipients if r.payment_status == "paid")
    coll_rec = sum(1 for r in recipients if r.pickup_status == "collected")
    recipient_sum_paise = sum(int(r.amount_paise or 0) for r in recipients)
    items_sum_paise = sum(int(it.line_total_paise or 0) for it in items)
    items_units = sum(int(it.quantity or 0) for it in items)

    alloc_units = db.scalar(
        select(func.coalesce(func.sum(BulkOrderAllocation.quantity), 0)).where(
            BulkOrderAllocation.bulk_order_id == bulk_order.id
        )
    ) or 0

    tok_count = db.scalar(
        select(func.count(BulkMealToken.id)).where(
            BulkMealToken.bulk_order_id == bulk_order.id,
            BulkMealToken.status.in_(["active", "redeemed"]),
        )
    ) or 0

    bulk_order.total_recipients = total_rec
    bulk_order.total_meal_units = max(int(items_units), int(alloc_units), total_rec)
    bulk_order.total_paise = max(items_sum_paise, recipient_sum_paise)
    bulk_order.paid_recipients_count = paid_rec
    bulk_order.tokens_generated_count = int(tok_count)
    bulk_order.collected_recipients_count = coll_rec

    if bulk_order.payment_mode == "individual":
        paid_amt = sum(int(r.amount_paise or 0) for r in recipients if r.payment_status == "paid")
        bulk_order.paid_paise = paid_amt
        if total_rec > 0 and paid_rec >= total_rec:
            bulk_order.payment_status = "paid"
            if bulk_order.status == "draft":
                bulk_order.status = "verified"
        elif paid_rec > 0:
            bulk_order.payment_status = "partially_paid"
            if bulk_order.status == "draft":
                bulk_order.status = "verified"
    else:
        if bulk_order.payment_status == "paid":
            bulk_order.paid_paise = bulk_order.total_paise

    if total_rec > 0 and coll_rec >= total_rec:
        bulk_order.status = "collected"
    elif coll_rec > 0 and bulk_order.status not in {"collected", "cancelled"}:
        bulk_order.status = "partially_collected"


def _atomic_reserve_bulk_inventory(db: Session, bulk_order: BulkOrder) -> None:
    """Atomically reserves stock for a master bulk order using conditional SQL updates (no negative stock / no overselling)."""
    if bulk_order.inventory_reserved:
        return
    items = db.scalars(
        select(BulkOrderItem).where(BulkOrderItem.bulk_order_id == bulk_order.id).order_by(BulkOrderItem.product_id.asc())
    ).all()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=24)
    for bi in items:
        qty = int(bi.quantity)
        if qty <= 0:
            continue
        res = db.execute(
            update(Product)
            .where(
                Product.id == bi.product_id,
                Product.is_available.is_(True),
                Product.stock_quantity >= qty,
            )
            .values(stock_quantity=Product.stock_quantity - qty)
        )
        if (res.rowcount or 0) != 1:
            p = db.get(Product, bi.product_id)
            avail = int(p.stock_quantity) if p else 0
            raise ApiError(
                409,
                "INSUFFICIENT_STOCK",
                f"Insufficient stock for '{bi.product_name}': requested {qty}, available {avail}.",
            )
        p_after = db.get(Product, bi.product_id)
        if p_after and p_after.stock_quantity == 0:
            p_after.is_available = False
        db.add(
            InventoryReservation(
                product_id=bi.product_id,
                bulk_order_id=bulk_order.id,
                quantity=qty,
                status="committed",
                expires_at=expires_at,
            )
        )
    bulk_order.inventory_reserved = True
    if bulk_order.status in {"draft", "verified"}:
        bulk_order.status = "reserved"


def _serialize_bulk_order(bo: BulkOrder, include_items: bool = True) -> dict[str, Any]:
    out = {
        "id": bo.id,
        "bulkOrderNumber": bo.bulk_order_number,
        "coordinatorId": bo.coordinator_id,
        "departmentName": bo.department_name,
        "eventTitle": bo.event_title,
        "pickupDate": bo.pickup_date.isoformat() if bo.pickup_date else None,
        "pickupSlot": bo.pickup_slot,
        "counterId": bo.counter_id or 1,
        "cutoffAt": as_utc(bo.cutoff_at),
        "paymentMode": bo.payment_mode,
        "distributionMode": bo.distribution_mode,
        "status": bo.status,
        "paymentStatus": bo.payment_status,
        "totalRecipients": bo.total_recipients,
        "totalMealUnits": bo.total_meal_units,
        "totalPaise": bo.total_paise,
        "paidPaise": bo.paid_paise,
        "paidRecipientsCount": bo.paid_recipients_count,
        "tokensGeneratedCount": bo.tokens_generated_count,
        "collectedRecipientsCount": bo.collected_recipients_count,
        "inventoryReserved": bool(bo.inventory_reserved),
        "pickupProgressPct": round((bo.collected_recipients_count / bo.total_recipients) * 100.0, 1) if bo.total_recipients > 0 else 0.0,
        "notes": bo.notes,
        "createdAt": as_utc(bo.created_at),
        "updatedAt": as_utc(bo.updated_at),
    }
    if include_items:
        out["items"] = [
            {
                "id": it.id,
                "menuItemId": it.product_id,
                "productName": it.product_name,
                "unitPricePaise": it.unit_price_paise,
                "quantity": it.quantity,
                "packedQuantity": it.packed_quantity,
                "readyQuantity": it.ready_quantity,
                "collectedQuantity": it.collected_quantity,
                "lineTotalPaise": it.line_total_paise,
                "prepStatus": it.prep_status,
            }
            for it in (bo.items or [])
        ]
    return out


def _check_bulk_access(bo: BulkOrder, user: User, require_write: bool = False) -> None:
    if user.role == UserRole.ADMIN:
        return
    if user.role == UserRole.STAFF and not require_write:
        return
    if bo.coordinator_id != user.id:
        raise ApiError(403, "BULK_ORDER_FORBIDDEN", "You are not authorized to access this master bulk order.")


# ==============================================================================
# Pydantic Schemas
# ==============================================================================
class BulkOrderItemIn(BaseModel):
    menuItemId: int = Field(gt=0)
    quantity: int = Field(gt=0, le=10000)


class BulkOrderCreateIn(BaseModel):
    departmentName: str = Field(min_length=2, max_length=140)
    eventTitle: str = Field(default="Department Meal Order", min_length=2, max_length=160)
    pickupDate: date | None = None
    pickupSlot: str = Field(default="12:30-13:00", min_length=3, max_length=60)
    counterId: int | None = Field(default=1, ge=1)
    paymentMode: str = Field(default="centralized", pattern="^(centralized|individual)$")
    distributionMode: str = Field(default="individual", pattern="^(individual|representative)$")
    reserveInventoryNow: bool = False
    notes: str | None = Field(default=None, max_length=500)
    items: list[BulkOrderItemIn] = Field(min_length=1, max_length=50)


class BulkOrderPatchIn(BaseModel):
    departmentName: str | None = Field(default=None, min_length=2, max_length=140)
    eventTitle: str | None = Field(default=None, min_length=2, max_length=160)
    pickupDate: date | None = None
    pickupSlot: str | None = Field(default=None, min_length=3, max_length=60)
    counterId: int | None = Field(default=None, ge=1)
    distributionMode: str | None = Field(default=None, pattern="^(individual|representative)$")
    notes: str | None = Field(default=None, max_length=500)
    items: list[BulkOrderItemIn] | None = None


class RecipientAllocationIn(BaseModel):
    menuItemId: int = Field(gt=0)
    quantity: int = Field(default=1, gt=0, le=50)


class BulkRecipientIn(BaseModel):
    recipientIdentifier: str = Field(min_length=1, max_length=120)
    fullName: str = Field(min_length=1, max_length=140)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    department: str | None = Field(default=None, max_length=120)
    allocations: list[RecipientAllocationIn] | None = None


class BulkRecipientsBatchIn(BaseModel):
    recipients: list[BulkRecipientIn] = Field(min_length=1, max_length=3500)


class BulkCsvImportIn(BaseModel):
    csvContent: str = Field(min_length=3)
    defaultMenuItemId: int | None = Field(default=None, gt=0)
    defaultQuantity: int = Field(default=1, gt=0, le=20)
    runInBackground: bool = True


class BulkPaymentVerifyIn(BaseModel):
    razorpayOrderId: str = Field(min_length=3, max_length=120)
    razorpayPaymentId: str = Field(min_length=3, max_length=120)
    razorpaySignature: str = Field(min_length=8, max_length=256)


class BulkTokenGenerateIn(BaseModel):
    regenerate: bool = False
    expiryHours: int = Field(default=24, ge=1, le=168)


class SinglePickupVerifyIn(BaseModel):
    qrToken: str | None = Field(default=None, max_length=200)
    pickupToken: str | None = Field(default=None, max_length=200)
    counterId: int = Field(default=1, ge=1, le=10)
    notes: str | None = Field(default=None, max_length=255)


class GroupPickupVerifyIn(BaseModel):
    qrTokens: list[str] = Field(min_length=1, max_length=3500)
    counterId: int = Field(default=1, ge=1, le=10)
    representativeName: str | None = Field(default=None, max_length=140)
    notes: str | None = Field(default=None, max_length=255)


class KitchenStageUpdateIn(BaseModel):
    stage: str = Field(pattern="^(verified|reserved|preparing|packing|ready|collected)$")
    packedQuantity: int | None = Field(default=None, ge=0)
    readyQuantity: int | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=255)


class IncidentResolveIn(BaseModel):
    resolutionNotes: str = Field(min_length=3, max_length=500)
    status: str = Field(default="resolved", pattern="^(under_review|resolved)$")


# ==============================================================================
# 1. Individual Menu Alias & Expired Reservation Release
# ==============================================================================
@router.get("/menu")
def get_menu_alias_v1(
    request: Request,
    counterId: int | None = Query(default=None),
    search: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
):
    stmt = select(Product).order_by(Product.category_id.asc(), Product.name.asc())
    if counterId is not None:
        stmt = stmt.where(or_(Product.counter_id == counterId, Product.counter_id.is_(None)))
    if search and search.strip():
        q = f"%{search.strip()}%"
        stmt = stmt.where(or_(Product.name.ilike(q), Product.description.ilike(q)))
    items = db.scalars(stmt).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": p.id,
                    "name": p.name,
                    "categoryId": p.category_id,
                    "counterId": p.counter_id,
                    "pricePaise": p.effective_price_paise,
                    "stockQuantity": p.stock_quantity,
                    "isAvailable": bool(p.is_available and p.stock_quantity > 0),
                    "prepTimeMinutes": p.preparation_minutes or 10,
                }
                for p in items
            ],
            "total": len(items),
        },
    )


# ==============================================================================
# 2. Master Bulk Order CRUD
# ==============================================================================
@router.post("/bulk-orders", status_code=201)
def create_bulk_order_v1(
    request: Request,
    payload: BulkOrderCreateIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    p_date = payload.pickupDate or date.today()
    merged_qty: dict[int, int] = {}
    for line in payload.items:
        merged_qty[line.menuItemId] = merged_qty.get(line.menuItemId, 0) + line.quantity

    products = db.scalars(
        select(Product).where(Product.id.in_(sorted(merged_qty.keys()))).order_by(Product.id.asc())
    ).all()
    by_id = {p.id: p for p in products}
    for pid, qty in merged_qty.items():
        p = by_id.get(pid)
        if not p or not p.is_available:
            raise ApiError(404, "MENU_ITEM_UNAVAILABLE", f"Menu item #{pid} is unavailable.")
        if payload.reserveInventoryNow and p.stock_quantity < qty:
            raise ApiError(
                409,
                "INSUFFICIENT_STOCK",
                f"Insufficient stock for '{p.name}': requested {qty}, available {p.stock_quantity}.",
            )

    bo_num = f"BLK-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    cutoff_dt = datetime.now(timezone.utc) + timedelta(hours=12)
    bo = BulkOrder(
        bulk_order_number=bo_num,
        coordinator_id=user.id,
        department_name=payload.departmentName.strip(),
        event_title=payload.eventTitle.strip(),
        pickup_date=p_date,
        pickup_slot=payload.pickupSlot.strip(),
        counter_id=payload.counterId or 1,
        cutoff_at=cutoff_dt,
        payment_mode=payload.paymentMode,
        distribution_mode=payload.distributionMode,
        status="draft",
        payment_status="pending",
        notes=payload.notes.strip() if payload.notes else None,
    )
    db.add(bo)
    db.flush()

    total_units = 0
    total_paise = 0
    for pid in sorted(merged_qty.keys()):
        p = by_id[pid]
        qty = merged_qty[pid]
        unit_p = p.effective_price_paise
        line_p = unit_p * qty
        total_units += qty
        total_paise += line_p
        db.add(
            BulkOrderItem(
                bulk_order_id=bo.id,
                product_id=p.id,
                product_name=p.name,
                unit_price_paise=unit_p,
                quantity=qty,
                line_total_paise=line_p,
            )
        )

    bo.total_meal_units = total_units
    bo.total_paise = total_paise
    db.flush()

    if payload.reserveInventoryNow:
        _atomic_reserve_bulk_inventory(db, bo)

    # Track slot reservation on physical counter
    slot_row = db.scalar(
        select(PickupSlot).where(
            PickupSlot.counter_id == (bo.counter_id or 1),
            PickupSlot.slot_date == p_date,
            PickupSlot.slot_label == bo.pickup_slot,
        )
    )
    if slot_row is None:
        slot_row = PickupSlot(
            counter_id=bo.counter_id or 1,
            slot_date=p_date,
            slot_label=bo.pickup_slot,
            max_capacity=max(1500, total_units),
            reserved_count=total_units,
        )
        db.add(slot_row)
    else:
        slot_row.reserved_count += total_units

    record_audit(
        db,
        "bulk_order.created",
        "bulk_order",
        bo.id,
        user.id,
        {
            "bulk_order_number": bo.bulk_order_number,
            "payment_mode": bo.payment_mode,
            "distribution_mode": bo.distribution_mode,
            "total_units": total_units,
            "total_paise": total_paise,
        },
    )
    db.commit()
    db.refresh(bo)
    return ok(request, _serialize_bulk_order(bo), status_code=201)


@router.get("/bulk-orders")
def list_bulk_orders_v1(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(BulkOrder).options(joinedload(BulkOrder.items)).order_by(BulkOrder.created_at.desc())
    if user.role == UserRole.STUDENT:
        stmt = stmt.where(BulkOrder.coordinator_id == user.id)
    if status:
        stmt = stmt.where(BulkOrder.status == status.strip().lower())
    rows = db.scalars(stmt.offset(offset).limit(limit)).unique().all()
    return ok(request, {"items": [_serialize_bulk_order(r) for r in rows], "total": len(rows)})


@router.get("/bulk-orders/{bulk_order_id}")
def get_bulk_order_v1(
    bulk_order_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.scalar(
        select(BulkOrder).options(joinedload(BulkOrder.items)).where(BulkOrder.id == bulk_order_id)
    )
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=False)
    return ok(request, _serialize_bulk_order(bo))


@router.patch("/bulk-orders/{bulk_order_id}")
def update_bulk_order_v1(
    bulk_order_id: int,
    request: Request,
    payload: BulkOrderPatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.scalar(
        select(BulkOrder).options(joinedload(BulkOrder.items)).where(BulkOrder.id == bulk_order_id)
    )
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=True)
    if bo.status in {"collected", "cancelled"}:
        raise ApiError(409, "BULK_ORDER_LOCKED", f"Cannot modify bulk order in '{bo.status}' state.")
    if bo.cutoff_at and datetime.now(timezone.utc) > bo.cutoff_at.replace(tzinfo=timezone.utc):
        raise ApiError(409, "CUTOFF_PASSED", "Allocation modification cutoff has passed.")

    if payload.departmentName is not None:
        bo.department_name = payload.departmentName.strip()
    if payload.eventTitle is not None:
        bo.event_title = payload.eventTitle.strip()
    if payload.pickupDate is not None:
        bo.pickup_date = payload.pickupDate
    if payload.pickupSlot is not None:
        bo.pickup_slot = payload.pickupSlot.strip()
    if payload.counterId is not None:
        bo.counter_id = payload.counterId
    if payload.distributionMode is not None:
        bo.distribution_mode = payload.distributionMode
    if payload.notes is not None:
        bo.notes = payload.notes.strip()

    if payload.items is not None:
        if bo.payment_status == "paid":
            raise ApiError(409, "BULK_ORDER_ALREADY_PAID", "Cannot change item quantities after payment verification.")
        for old_item in list(bo.items):
            db.delete(old_item)
        db.flush()
        for line in payload.items:
            p = db.get(Product, line.menuItemId)
            if not p:
                raise ApiError(404, "MENU_ITEM_NOT_FOUND", f"Menu item #{line.menuItemId} not found.")
            unit_p = p.effective_price_paise
            db.add(
                BulkOrderItem(
                    bulk_order_id=bo.id,
                    product_id=p.id,
                    product_name=p.name,
                    unit_price_paise=unit_p,
                    quantity=line.quantity,
                    line_total_paise=unit_p * line.quantity,
                )
            )
        db.flush()

    _recompute_bulk_order_totals(db, bo)
    record_audit(db, "bulk_order.updated", "bulk_order", bo.id, user.id, {"status": bo.status})
    db.commit()
    db.refresh(bo)
    return ok(request, _serialize_bulk_order(bo))


# ==============================================================================
# 3. Recipient Management & Batched CSV Import (Up to 3,000+ Recipients)
# ==============================================================================
def _insert_recipient_batch(
    db: Session,
    bo: BulkOrder,
    raw_rows: list[dict[str, Any]],
    default_product: Product,
    default_qty: int = 1,
    strict_duplicate_error: bool = False,
) -> dict[str, Any]:
    existing_idents = set(
        db.scalars(
            select(BulkOrderRecipient.recipient_identifier).where(BulkOrderRecipient.bulk_order_id == bo.id)
        ).all()
    )
    products_by_id = {
        p.id: p
        for p in db.scalars(select(Product)).all()
    }
    first_bulk_item = db.scalar(select(BulkOrderItem).where(BulkOrderItem.bulk_order_id == bo.id))

    imported_count = 0
    duplicate_count = 0
    error_count = 0
    errors: list[str] = []
    created_recipients: list[BulkOrderRecipient] = []

    is_centralized_paid = bo.payment_mode == "centralized" and bo.payment_status == "paid"

    # Process in 500-recipient chunks for memory and transaction safety
    for idx, row in enumerate(raw_rows):
        ident = str(row.get("recipientIdentifier") or row.get("roll_number") or row.get("identifier") or row.get("email") or "").strip()
        full_name = str(row.get("fullName") or row.get("full_name") or row.get("name") or "").strip()
        email = str(row.get("email") or "").strip().lower() or None
        phone = str(row.get("phone") or "").strip() or None
        dept = str(row.get("department") or bo.department_name or "").strip() or None

        if not ident or not full_name:
            error_count += 1
            errors.append(f"Row {idx + 1}: Missing recipientIdentifier or fullName")
            continue

        norm_ident = ident.upper()
        if norm_ident in existing_idents:
            duplicate_count += 1
            if strict_duplicate_error:
                raise ApiError(409, "DUPLICATE_RECIPIENT", f"Duplicate recipient identifier '{ident}' in bulk order.")
            continue
        existing_idents.add(norm_ident)

        raw_allocs = row.get("allocations")
        resolved_allocs: list[tuple[Product, int, int]] = []
        if isinstance(raw_allocs, list) and raw_allocs:
            for al in raw_allocs:
                pid = int(al.get("menuItemId") if isinstance(al, dict) else al.menuItemId)
                q = int(al.get("quantity", 1) if isinstance(al, dict) else al.quantity)
                prod = products_by_id.get(pid) or default_product
                resolved_allocs.append((prod, q, prod.effective_price_paise * q))
        else:
            q = int(row.get("quantity") or default_qty or 1)
            prod = products_by_id.get(int(row["menuItemId"])) if row.get("menuItemId") else default_product
            resolved_allocs.append((prod, q, prod.effective_price_paise * q))

        rec_amount = sum(lp for _, _, lp in resolved_allocs)
        rec = BulkOrderRecipient(
            bulk_order_id=bo.id,
            recipient_identifier=norm_ident,
            full_name=full_name,
            email=email,
            phone=phone,
            department=dept,
            amount_paise=rec_amount,
            payment_status="paid" if is_centralized_paid else "pending",
            pickup_status="eligible" if is_centralized_paid else "pending",
        )
        db.add(rec)
        db.flush()

        for prod, q, lp in resolved_allocs:
            db.add(
                BulkOrderAllocation(
                    bulk_order_id=bo.id,
                    recipient_id=rec.id,
                    bulk_order_item_id=first_bulk_item.id if first_bulk_item and first_bulk_item.product_id == prod.id else None,
                    product_id=prod.id,
                    product_name=prod.name,
                    unit_price_paise=prod.effective_price_paise,
                    quantity=q,
                    line_total_paise=lp,
                )
            )
        imported_count += 1
        created_recipients.append(rec)
        if imported_count % 500 == 0:
            db.flush()

    db.flush()
    _recompute_bulk_order_totals(db, bo)
    return {
        "importedCount": imported_count,
        "duplicateCount": duplicate_count,
        "errorCount": error_count,
        "errors": errors[:25],
        "createdIds": [r.id for r in created_recipients[:100]],
    }


@router.post("/bulk-orders/{bulk_order_id}/recipients", status_code=201)
def add_bulk_recipients_v1(
    bulk_order_id: int,
    request: Request,
    payload: BulkRecipientsBatchIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.scalar(select(BulkOrder).options(joinedload(BulkOrder.items)).where(BulkOrder.id == bulk_order_id))
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=True)
    if bo.status in {"collected", "cancelled"}:
        raise ApiError(409, "BULK_ORDER_LOCKED", "Cannot add recipients to a closed bulk order.")

    default_item_id = bo.items[0].product_id if bo.items else None
    default_prod = db.get(Product, default_item_id) if default_item_id else db.scalar(select(Product))
    if not default_prod:
        raise ApiError(404, "NO_MENU_ITEMS", "No menu item available for allocation.")

    raw_rows = [r.model_dump() for r in payload.recipients]
    res = _insert_recipient_batch(
        db,
        bo,
        raw_rows,
        default_prod,
        default_qty=1,
        strict_duplicate_error=(len(raw_rows) == 1),
    )
    record_audit(
        db,
        "bulk_order.recipients_added",
        "bulk_order",
        bo.id,
        user.id,
        {"imported": res["importedCount"], "duplicates": res["duplicateCount"]},
    )
    db.commit()
    return ok(
        request,
        {
            **res,
            "bulkOrderId": bo.id,
            "totalRecipients": bo.total_recipients,
            "totalPaise": bo.total_paise,
        },
        status_code=201,
    )


@router.post("/bulk-orders/{bulk_order_id}/recipients/import")
async def import_bulk_recipients_csv_v1(
    bulk_order_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.scalar(select(BulkOrder).options(joinedload(BulkOrder.items)).where(BulkOrder.id == bulk_order_id))
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=True)

    content_type = (request.headers.get("content-type") or "").lower()
    csv_text = ""
    default_item_id = bo.items[0].product_id if bo.items else None
    default_qty = 1

    if "application/json" in content_type:
        body_json = await request.json()
        parsed_in = BulkCsvImportIn(**body_json)
        csv_text = parsed_in.csvContent
        if parsed_in.defaultMenuItemId:
            default_item_id = parsed_in.defaultMenuItemId
        default_qty = parsed_in.defaultQuantity
    else:
        raw_bytes = await request.body()
        csv_text = raw_bytes.decode("utf-8", errors="replace")

    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    parsed_rows: list[dict[str, Any]] = []
    for row in reader:
        norm = {str(k or "").strip().lower(): str(v or "").strip() for k, v in row.items()}
        parsed_rows.append(
            {
                "recipientIdentifier": (
                    norm.get("recipientidentifier")
                    or norm.get("recipient_identifier")
                    or norm.get("roll_number")
                    or norm.get("rollnumber")
                    or norm.get("student_id")
                    or norm.get("id")
                    or norm.get("email")
                    or ""
                ),
                "fullName": norm.get("fullname") or norm.get("full_name") or norm.get("name") or "",
                "email": norm.get("email") or "",
                "phone": norm.get("phone") or norm.get("mobile") or "",
                "department": norm.get("department") or norm.get("dept") or bo.department_name,
                "menuItemId": int(norm["menuitemid"]) if norm.get("menuitemid", "").isdigit() else default_item_id,
                "quantity": int(norm["quantity"]) if norm.get("quantity", "").isdigit() else default_qty,
            }
        )

    job_key = f"job_csv_{uuid.uuid4().hex[:12]}"
    job = BulkBackgroundJob(
        job_key=job_key,
        bulk_order_id=bo.id,
        job_type="csv_import",
        status="running",
        total_items=len(parsed_rows),
        created_by_user_id=user.id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()

    default_prod = db.get(Product, default_item_id) if default_item_id else db.scalar(select(Product))
    if not default_prod:
        raise ApiError(404, "NO_MENU_ITEMS", "No menu item available for CSV allocation.")

    res = _insert_recipient_batch(db, bo, parsed_rows, default_prod, default_qty=default_qty, strict_duplicate_error=False)
    job.status = "completed"
    job.processed_items = res["importedCount"] + res["duplicateCount"]
    job.error_count = res["errorCount"]
    job.completed_at = datetime.now(timezone.utc)
    job.result_json = json.dumps(res)

    record_audit(
        db,
        "bulk_order.csv_imported",
        "bulk_order",
        bo.id,
        user.id,
        {"job_key": job_key, "total_rows": len(parsed_rows), "imported": res["importedCount"]},
    )
    db.commit()
    return ok(
        request,
        {
            "jobKey": job_key,
            "jobStatus": job.status,
            "bulkOrderId": bo.id,
            "totalCsvRows": len(parsed_rows),
            **res,
            "totalRecipients": bo.total_recipients,
            "totalPaise": bo.total_paise,
        },
    )


@router.get("/bulk-orders/{bulk_order_id}/recipients")
def list_bulk_recipients_v1(
    bulk_order_id: int,
    request: Request,
    paymentStatus: str | None = Query(default=None),
    pickupStatus: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=200, ge=1, le=3500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.get(BulkOrder, bulk_order_id)
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=False)

    stmt = (
        select(BulkOrderRecipient)
        .options(joinedload(BulkOrderRecipient.allocations))
        .where(BulkOrderRecipient.bulk_order_id == bulk_order_id)
        .order_by(BulkOrderRecipient.id.asc())
    )
    if paymentStatus:
        stmt = stmt.where(BulkOrderRecipient.payment_status == paymentStatus.strip().lower())
    if pickupStatus:
        stmt = stmt.where(BulkOrderRecipient.pickup_status == pickupStatus.strip().lower())
    if search and search.strip():
        q = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                BulkOrderRecipient.recipient_identifier.ilike(q),
                BulkOrderRecipient.full_name.ilike(q),
                BulkOrderRecipient.email.ilike(q),
            )
        )
    rows = db.scalars(stmt.offset(offset).limit(limit)).unique().all()
    return ok(
        request,
        {
            "bulkOrderId": bo.id,
            "totalRecipients": bo.total_recipients,
            "items": [
                {
                    "id": r.id,
                    "recipientIdentifier": r.recipient_identifier,
                    "fullName": r.full_name,
                    "email": r.email,
                    "phone": r.phone,
                    "department": r.department,
                    "amountPaise": r.amount_paise,
                    "paymentStatus": r.payment_status,
                    "pickupStatus": r.pickup_status,
                    "collectedAt": as_utc(r.collected_at),
                    "collectedCounterId": r.collected_counter_id,
                    "allocations": [
                        {
                            "menuItemId": a.product_id,
                            "productName": a.product_name,
                            "quantity": a.quantity,
                            "lineTotalPaise": a.line_total_paise,
                        }
                        for a in (r.allocations or [])
                    ],
                }
                for r in rows
            ],
        },
    )


@router.get("/bulk-orders/{bulk_order_id}/summary")
def get_bulk_order_summary_v1(
    bulk_order_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.scalar(select(BulkOrder).options(joinedload(BulkOrder.items)).where(BulkOrder.id == bulk_order_id))
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=False)

    _recompute_bulk_order_totals(db, bo)
    db.commit()

    eligible_cnt = db.scalar(
        select(func.count(BulkOrderRecipient.id)).where(
            BulkOrderRecipient.bulk_order_id == bo.id,
            BulkOrderRecipient.payment_status == "paid",
            BulkOrderRecipient.pickup_status == "eligible",
        )
    ) or 0
    failed_cnt = db.scalar(
        select(func.count(BulkOrderRecipient.id)).where(
            BulkOrderRecipient.bulk_order_id == bo.id,
            BulkOrderRecipient.payment_status == "failed",
        )
    ) or 0
    jobs = db.scalars(
        select(BulkBackgroundJob)
        .where(BulkBackgroundJob.bulk_order_id == bo.id)
        .order_by(BulkBackgroundJob.created_at.desc())
        .limit(10)
    ).all()

    return ok(
        request,
        {
            **_serialize_bulk_order(bo),
            "pendingRecipientsCount": max(0, bo.total_recipients - bo.paid_recipients_count - int(failed_cnt)),
            "failedRecipientsCount": int(failed_cnt),
            "eligibleForPickupCount": int(eligible_cnt),
            "remainingToCollectCount": max(0, bo.paid_recipients_count - bo.collected_recipients_count),
            "backgroundJobs": [
                {
                    "jobKey": j.job_key,
                    "jobType": j.job_type,
                    "status": j.status,
                    "totalItems": j.total_items,
                    "processedItems": j.processed_items,
                    "errorCount": j.error_count,
                    "completedAt": as_utc(j.completed_at),
                }
                for j in jobs
            ],
        },
    )


# ==============================================================================
# 4. Dual-Mode Bulk Payments (Centralized & Individual) + Authoritative Verification
# ==============================================================================
def _create_gateway_order_with_retry(amount_paise: int, receipt_ref: str) -> str:
    """Creates a Razorpay order with bounded retries, exponential backoff + jitter, and concurrency control."""
    cfg = get_settings()
    if not (cfg.razorpay_key_id and cfg.razorpay_key_secret) or cfg.razorpay_key_id.startswith("rzp_test_"):
        return f"order_bulk_{uuid.uuid4().hex[:14]}"

    url = "https://api.razorpay.com/v1/orders"
    last_err = None
    with _GATEWAY_SEMAPHORE:
        for attempt in range(3):
            try:
                resp = httpx.post(
                    url,
                    auth=(cfg.razorpay_key_id, cfg.razorpay_key_secret),
                    json={"amount": amount_paise, "currency": cfg.payment_currency, "receipt": receipt_ref[:40]},
                    timeout=6.0,
                )
                if resp.status_code in (200, 201):
                    return str(resp.json()["id"])
                if resp.status_code < 500 and resp.status_code != 429:
                    break
            except Exception as exc:
                last_err = exc
            time.sleep((0.15 * (2 ** attempt)) + (secrets.randbelow(50) / 1000.0))
    raise ApiError(502, "PAYMENT_GATEWAY_ERROR", f"Gateway order creation failed: {last_err or 'HTTP error'}")


@router.post("/bulk-orders/{bulk_order_id}/payment-intent")
def create_centralized_bulk_payment_intent_v1(
    bulk_order_id: int,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.scalar(select(BulkOrder).options(joinedload(BulkOrder.items)).where(BulkOrder.id == bulk_order_id))
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=True)
    if bo.payment_mode != "centralized":
        raise ApiError(409, "INVALID_PAYMENT_MODE", "This bulk order uses individual recipient payment mode.")
    if bo.payment_status == "paid":
        raise ApiError(409, "ALREADY_PAID", "Master bulk order is already paid.")

    _recompute_bulk_order_totals(db, bo)
    if bo.total_paise <= 0:
        raise ApiError(422, "ZERO_ORDER_TOTAL", "Bulk order total must be greater than zero.")

    idem = (idempotency_key or f"central-bo-{bo.id}").strip()[:80]
    existing = db.scalar(select(BulkOrderPayment).where(BulkOrderPayment.idempotency_key == idem))
    if existing and existing.status in {"CREATED", "PENDING", "SUCCESS"}:
        return ok(
            request,
            {
                "bulkPaymentId": existing.id,
                "bulkOrderId": bo.id,
                "paymentMode": "centralized",
                "gatewayOrderId": existing.provider_order_id,
                "amountPaise": existing.amount_paise,
                "currency": existing.currency,
                "status": existing.status,
                "razorpayKeyId": get_settings().razorpay_key_id or "rzp_test_sandbox",
            },
        )

    gw_order_id = _create_gateway_order_with_retry(bo.total_paise, f"bo_{bo.id}_central")
    pay = BulkOrderPayment(
        bulk_order_id=bo.id,
        recipient_id=None,
        payer_user_id=user.id,
        provider="razorpay",
        idempotency_key=idem,
        provider_order_id=gw_order_id,
        amount_paise=bo.total_paise,
        currency=get_settings().payment_currency,
        status="CREATED",
    )
    db.add(pay)
    db.commit()
    db.refresh(pay)
    return ok(
        request,
        {
            "bulkPaymentId": pay.id,
            "bulkOrderId": bo.id,
            "paymentMode": "centralized",
            "gatewayOrderId": pay.provider_order_id,
            "amountPaise": pay.amount_paise,
            "currency": pay.currency,
            "status": pay.status,
            "razorpayKeyId": get_settings().razorpay_key_id or "rzp_test_sandbox",
        },
    )


@router.post("/bulk-orders/{bulk_order_id}/recipients/{recipient_id}/payment-intent")
def create_individual_recipient_payment_intent_v1(
    bulk_order_id: int,
    recipient_id: int,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.get(BulkOrder, bulk_order_id)
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    if bo.payment_mode != "individual":
        raise ApiError(409, "INVALID_PAYMENT_MODE", "This bulk order uses centralized coordinator payment mode.")

    rec = db.scalar(
        select(BulkOrderRecipient).where(
            BulkOrderRecipient.id == recipient_id,
            BulkOrderRecipient.bulk_order_id == bulk_order_id,
        )
    )
    if not rec:
        raise ApiError(404, "RECIPIENT_NOT_FOUND", "Recipient allocation not found.")
    if rec.payment_status == "paid":
        raise ApiError(409, "RECIPIENT_ALREADY_PAID", "Recipient allocation is already paid.")

    idem = (idempotency_key or f"indiv-bo-{bo.id}-rec-{rec.id}").strip()[:80]
    existing = db.scalar(select(BulkOrderPayment).where(BulkOrderPayment.idempotency_key == idem))
    if existing and existing.status in {"CREATED", "PENDING", "SUCCESS"}:
        return ok(
            request,
            {
                "bulkPaymentId": existing.id,
                "bulkOrderId": bo.id,
                "recipientId": rec.id,
                "paymentMode": "individual",
                "gatewayOrderId": existing.provider_order_id,
                "amountPaise": existing.amount_paise,
                "currency": existing.currency,
                "status": existing.status,
                "razorpayKeyId": get_settings().razorpay_key_id or "rzp_test_sandbox",
            },
        )

    gw_order_id = _create_gateway_order_with_retry(rec.amount_paise, f"bo_{bo.id}_rec_{rec.id}")
    pay = BulkOrderPayment(
        bulk_order_id=bo.id,
        recipient_id=rec.id,
        payer_user_id=user.id,
        provider="razorpay",
        idempotency_key=idem,
        provider_order_id=gw_order_id,
        amount_paise=rec.amount_paise,
        currency=get_settings().payment_currency,
        status="CREATED",
    )
    db.add(pay)
    db.commit()
    db.refresh(pay)
    return ok(
        request,
        {
            "bulkPaymentId": pay.id,
            "bulkOrderId": bo.id,
            "recipientId": rec.id,
            "paymentMode": "individual",
            "gatewayOrderId": pay.provider_order_id,
            "amountPaise": pay.amount_paise,
            "currency": pay.currency,
            "status": pay.status,
            "razorpayKeyId": get_settings().razorpay_key_id or "rzp_test_sandbox",
        },
    )


def verify_and_apply_bulk_payment(
    db: Session,
    pay: BulkOrderPayment,
    razorpay_payment_id: str,
    razorpay_signature: str | None,
    actor_user_id: int | None = None,
    verify_hmac: bool = True,
) -> dict[str, Any]:
    """Authoritative server-side bulk payment verification with strict no-refund incident handling."""
    if pay.status == "SUCCESS":
        bo = db.get(BulkOrder, pay.bulk_order_id)
        return {
            "alreadyVerified": True,
            "bulkPaymentId": pay.id,
            "bulkOrderId": pay.bulk_order_id,
            "recipientId": pay.recipient_id,
            "paymentStatus": "SUCCESS",
            "bulkOrderStatus": bo.status if bo else "verified",
        }

    cfg = get_settings()
    raw_secret = (cfg.razorpay_key_secret or "").strip()
    if verify_hmac and not raw_secret:
        if cfg.environment.lower() not in {"development", "test"}:
            raise ApiError(503, "PAYMENT_GATEWAY_NOT_CONFIGURED", "Payment gateway secret is not configured.")
        raw_secret = "sandbox_bulk_secret"
    secret = raw_secret.encode("utf-8")
    if verify_hmac:
        expected_sig = hmac.new(
            secret,
            f"{pay.provider_order_id}|{razorpay_payment_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not razorpay_signature or not hmac.compare_digest(expected_sig, razorpay_signature.strip()):
            pay.status = "FAILED"
            if pay.recipient_id:
                rec = db.get(BulkOrderRecipient, pay.recipient_id)
                if rec and rec.payment_status != "paid":
                    rec.payment_status = "failed"
            db.commit()
            raise ApiError(400, "INVALID_PAYMENT_SIGNATURE", "Payment signature verification failed.")

    bo = db.scalar(
        select(BulkOrder).options(joinedload(BulkOrder.items)).where(BulkOrder.id == pay.bulk_order_id).with_for_update()
    )
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")

    pay.provider_payment_id = razorpay_payment_id
    pay.signature_hash = hashlib.sha256((razorpay_signature or "webhook").encode("utf-8")).hexdigest()
    pay.status = "SUCCESS"
    pay.verified_at = datetime.now(timezone.utc)

    # Strict No-Refund Policy: if order was cancelled before payment completed, preserve payment and log incident!
    if bo.status == "cancelled":
        pay.late_capture_flagged = True
        inc = PaymentIncident(
            bulk_order_id=bo.id,
            recipient_id=pay.recipient_id,
            bulk_payment_id=pay.id,
            incident_type="late_capture",
            amount_paise=pay.amount_paise,
            status="open",
            description=f"Verified payment {razorpay_payment_id} captured on cancelled bulk order {bo.bulk_order_number}. Preserved under strict no-refunds policy.",
            flagged_by_user_id=actor_user_id,
        )
        db.add(inc)
        db.commit()
        return {
            "alreadyVerified": False,
            "bulkPaymentId": pay.id,
            "bulkOrderId": bo.id,
            "recipientId": pay.recipient_id,
            "paymentStatus": "SUCCESS",
            "incidentFlagged": True,
            "incidentType": "late_capture",
        }

    # Reserve inventory atomically if not yet reserved
    try:
        if pay.recipient_id is None:
            _atomic_reserve_bulk_inventory(db, bo)
        else:
            # Individual recipient payment: reserve only this recipient's allocated units
            allocs = db.scalars(
                select(BulkOrderAllocation).where(BulkOrderAllocation.recipient_id == pay.recipient_id)
            ).all()
            for al in allocs:
                res = db.execute(
                    update(Product)
                    .where(Product.id == al.product_id, Product.stock_quantity >= al.quantity)
                    .values(stock_quantity=Product.stock_quantity - al.quantity)
                )
                if (res.rowcount or 0) != 1:
                    raise ApiError(409, "INSUFFICIENT_STOCK", f"Stock exhausted for {al.product_name}")
    except ApiError:
        # Payment succeeded at gateway, but stock exhausted: NEVER mark payment failed or refund!
        pay.late_capture_flagged = True
        inc = PaymentIncident(
            bulk_order_id=bo.id,
            recipient_id=pay.recipient_id,
            bulk_payment_id=pay.id,
            incident_type="kitchen_unfulfilled",
            amount_paise=pay.amount_paise,
            status="open",
            description=f"Payment {razorpay_payment_id} verified but stock exhausted for bulk order {bo.bulk_order_number}. Flagged for administrative resolution (no refunds).",
            flagged_by_user_id=actor_user_id,
        )
        db.add(inc)
        db.commit()
        return {
            "alreadyVerified": False,
            "bulkPaymentId": pay.id,
            "bulkOrderId": bo.id,
            "recipientId": pay.recipient_id,
            "paymentStatus": "SUCCESS",
            "incidentFlagged": True,
            "incidentType": "kitchen_unfulfilled",
        }

    if pay.recipient_id is None:
        # Centralized payment covers all recipients
        bo.payment_status = "paid"
        if bo.status == "draft":
            bo.status = "verified"
        db.execute(
            update(BulkOrderRecipient)
            .where(BulkOrderRecipient.bulk_order_id == bo.id)
            .values(payment_status="paid", pickup_status="eligible")
        )
    else:
        # Individual payment covers ONLY this specific recipient
        rec = db.get(BulkOrderRecipient, pay.recipient_id)
        if rec:
            rec.payment_status = "paid"
            if rec.pickup_status == "pending":
                rec.pickup_status = "eligible"

    db.flush()
    _recompute_bulk_order_totals(db, bo)
    record_audit(
        db,
        "bulk_order.payment_verified",
        "bulk_order",
        bo.id,
        actor_user_id,
        {
            "bulk_payment_id": pay.id,
            "recipient_id": pay.recipient_id,
            "amount_paise": pay.amount_paise,
            "provider_payment_id": razorpay_payment_id,
        },
    )
    db.commit()
    return {
        "alreadyVerified": False,
        "bulkPaymentId": pay.id,
        "bulkOrderId": bo.id,
        "recipientId": pay.recipient_id,
        "paymentStatus": "SUCCESS",
        "bulkOrderPaymentStatus": bo.payment_status,
        "bulkOrderStatus": bo.status,
        "paidRecipientsCount": bo.paid_recipients_count,
        "incidentFlagged": False,
    }


@router.post("/bulk-orders/{bulk_order_id}/payments/verify")
def verify_bulk_order_payment_endpoint_v1(
    bulk_order_id: int,
    request: Request,
    payload: BulkPaymentVerifyIn,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pay = db.scalar(
        select(BulkOrderPayment).where(
            BulkOrderPayment.bulk_order_id == bulk_order_id,
            BulkOrderPayment.provider_order_id == payload.razorpayOrderId.strip(),
        )
    )
    if not pay:
        raise ApiError(404, "BULK_PAYMENT_NOT_FOUND", "Matching bulk payment intent not found.")
    bo = db.get(BulkOrder, pay.bulk_order_id)
    if user.role != UserRole.ADMIN and pay.payer_user_id != user.id and (not bo or bo.coordinator_id != user.id):
        raise ApiError(403, "FORBIDDEN", "Not authorized to verify this bulk payment.")
    result = verify_and_apply_bulk_payment(
        db,
        pay,
        payload.razorpayPaymentId.strip(),
        payload.razorpaySignature.strip(),
        actor_user_id=user.id,
        verify_hmac=True,
    )
    return ok(request, result)


# ==============================================================================
# 5. Unique Cryptographic QR Token Generation, Distribution & Protected Exports
# ==============================================================================
def _mint_tokens_for_bulk_order(
    db: Session,
    bo: BulkOrder,
    regenerate: bool = False,
    expiry_hours: int = 24,
) -> list[dict[str, Any]]:
    eligible_recipients = db.scalars(
        select(BulkOrderRecipient)
        .where(
            BulkOrderRecipient.bulk_order_id == bo.id,
            BulkOrderRecipient.payment_status == "paid",
        )
        .order_by(BulkOrderRecipient.id.asc())
    ).all()

    existing_tokens = {
        t.recipient_id: t
        for t in db.scalars(
            select(BulkMealToken).where(
                BulkMealToken.bulk_order_id == bo.id,
                BulkMealToken.status.in_(["active", "redeemed"]),
            )
        ).all()
    }

    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=expiry_hours)
    output_tokens: list[dict[str, Any]] = []

    for idx, rec in enumerate(eligible_recipients):
        tok = existing_tokens.get(rec.id)
        if tok and tok.status == "redeemed":
            # Never regenerate an already redeemed token
            output_tokens.append(
                {
                    "tokenId": tok.id,
                    "recipientId": rec.id,
                    "recipientIdentifier": rec.recipient_identifier,
                    "fullName": rec.full_name,
                    "status": tok.status,
                    "qrToken": None,
                    "expiresAt": as_utc(tok.expires_at),
                }
            )
            continue

        if tok and not regenerate:
            raw_tok = _derive_raw_bulk_token(bo.id, rec.id, tok.key_nonce)
        else:
            if tok and regenerate:
                tok.status = "revoked"
                db.flush()
            nonce = secrets.token_hex(16)
            raw_tok = _derive_raw_bulk_token(bo.id, rec.id, nonce)
            tok_hash = _hash_token(raw_tok)
            tok = BulkMealToken(
                bulk_order_id=bo.id,
                recipient_id=rec.id,
                token_hash=tok_hash,
                key_nonce=nonce,
                distribution_mode=bo.distribution_mode,
                status="active",
                expires_at=expires_at,
                distributed_at=now,
            )
            db.add(tok)
            if (idx + 1) % 500 == 0:
                db.flush()

        output_tokens.append(
            {
                "recipientId": rec.id,
                "recipientIdentifier": rec.recipient_identifier,
                "fullName": rec.full_name,
                "department": rec.department or bo.department_name,
                "status": "active",
                "qrToken": raw_tok,
                "expiresAt": as_utc(expires_at),
            }
        )

    db.flush()
    _recompute_bulk_order_totals(db, bo)
    return output_tokens


@router.post("/bulk-orders/{bulk_order_id}/tokens/generate")
def generate_bulk_order_tokens_v1(
    bulk_order_id: int,
    request: Request,
    payload: BulkTokenGenerateIn | None = None,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.get(BulkOrder, bulk_order_id)
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=True)

    opts = payload or BulkTokenGenerateIn()
    job_key = f"job_tok_{uuid.uuid4().hex[:12]}"
    job = BulkBackgroundJob(
        job_key=job_key,
        bulk_order_id=bo.id,
        job_type="token_generation",
        status="running",
        total_items=bo.paid_recipients_count,
        created_by_user_id=user.id,
        started_at=datetime.now(timezone.utc),
    )
    db.add(job)
    db.flush()

    minted = _mint_tokens_for_bulk_order(db, bo, regenerate=opts.regenerate, expiry_hours=opts.expiryHours)
    job.status = "completed"
    job.processed_items = len(minted)
    job.completed_at = datetime.now(timezone.utc)

    record_audit(
        db,
        "bulk_order.tokens_generated",
        "bulk_order",
        bo.id,
        user.id,
        {"generated_count": len(minted), "distribution_mode": bo.distribution_mode, "regenerate": opts.regenerate},
    )
    db.commit()

    # In individual distribution mode, raw tokens are only shown to the individual recipient via /recipients/{id}/token
    expose_raw = bo.distribution_mode == "representative" or user.role == UserRole.ADMIN
    sanitized = [
        {**t, "qrToken": t["qrToken"] if expose_raw else None}
        for t in minted
    ]
    return ok(
        request,
        {
            "jobKey": job_key,
            "bulkOrderId": bo.id,
            "distributionMode": bo.distribution_mode,
            "generatedCount": len(minted),
            "tokens": sanitized[:500],
            "totalTokensAvailable": len(minted),
        },
    )


@router.get("/bulk-orders/{bulk_order_id}/tokens")
def list_bulk_order_tokens_v1(
    bulk_order_id: int,
    request: Request,
    limit: int = Query(default=500, ge=1, le=3500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.get(BulkOrder, bulk_order_id)
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=False)

    expose_raw = (bo.distribution_mode == "representative" and bo.coordinator_id == user.id) or user.role == UserRole.ADMIN
    rows = db.execute(
        select(BulkMealToken, BulkOrderRecipient)
        .join(BulkOrderRecipient, BulkOrderRecipient.id == BulkMealToken.recipient_id)
        .where(
            BulkMealToken.bulk_order_id == bo.id,
            BulkMealToken.status.in_(["active", "redeemed"]),
        )
        .order_by(BulkOrderRecipient.id.asc())
        .offset(offset)
        .limit(limit)
    ).all()

    items = []
    for tok, rec in rows:
        raw_val = _derive_raw_bulk_token(bo.id, rec.id, tok.key_nonce) if (expose_raw and tok.status == "active") else None
        items.append(
            {
                "tokenId": tok.id,
                "recipientId": rec.id,
                "recipientIdentifier": rec.recipient_identifier,
                "fullName": rec.full_name,
                "paymentStatus": rec.payment_status,
                "pickupStatus": rec.pickup_status,
                "tokenStatus": tok.status,
                "qrToken": raw_val,
                "expiresAt": as_utc(tok.expires_at),
                "redeemedAt": as_utc(tok.redeemed_at),
                "redeemedCounterId": tok.redeemed_counter_id,
            }
        )
    return ok(
        request,
        {
            "bulkOrderId": bo.id,
            "distributionMode": bo.distribution_mode,
            "totalTokens": bo.tokens_generated_count,
            "items": items,
        },
    )


@router.get("/bulk-orders/{bulk_order_id}/tokens/export")
def export_bulk_order_tokens_v1(
    bulk_order_id: int,
    request: Request,
    format: str = Query(default="csv", pattern="^(csv|pdf|json)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bo = db.get(BulkOrder, bulk_order_id)
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    _check_bulk_access(bo, user, require_write=True)
    if bo.distribution_mode != "representative" and user.role != UserRole.ADMIN:
        raise ApiError(
            403,
            "REPRESENTATIVE_MODE_REQUIRED",
            "Bulk QR token export is restricted to representative distribution mode.",
        )

    # Ensure tokens exist for all paid recipients
    _mint_tokens_for_bulk_order(db, bo, regenerate=False)
    rows = db.execute(
        select(BulkMealToken, BulkOrderRecipient)
        .join(BulkOrderRecipient, BulkOrderRecipient.id == BulkMealToken.recipient_id)
        .where(
            BulkMealToken.bulk_order_id == bo.id,
            BulkMealToken.status.in_(["active", "redeemed"]),
        )
        .order_by(BulkOrderRecipient.id.asc())
    ).all()

    record_audit(
        db,
        "bulk_order.tokens_exported",
        "bulk_order",
        bo.id,
        user.id,
        {"format": format, "row_count": len(rows)},
    )
    db.commit()

    export_rows = []
    for tok, rec in rows:
        raw_tok = _derive_raw_bulk_token(bo.id, rec.id, tok.key_nonce) if tok.status == "active" else "REDEEMED"
        export_rows.append(
            {
                "recipientId": rec.id,
                "recipientIdentifier": rec.recipient_identifier,
                "fullName": rec.full_name,
                "department": rec.department or bo.department_name,
                "pickupDate": bo.pickup_date.isoformat(),
                "pickupSlot": bo.pickup_slot,
                "counterId": bo.counter_id or 1,
                "tokenStatus": tok.status,
                "qrToken": raw_tok,
            }
        )

    if format == "json":
        return ok(request, {"bulkOrderNumber": bo.bulk_order_number, "items": export_rows})

    if format == "pdf":
        headers = ["Recipient ID", "Name", "Department", "Pickup Slot", "Status", "Unique QR Token"]
        table_rows = [
            [
                r["recipientIdentifier"],
                r["fullName"],
                r["department"],
                f"{r['pickupDate']} {r['pickupSlot']} (C{r['counterId']})",
                r["tokenStatus"].upper(),
                r["qrToken"],
            ]
            for r in export_rows
        ]
        pdf_bytes = render_pdf_document(
            title=f"Bulk Meal Tokens ({bo.bulk_order_number})",
            subtitle=f"Department: {bo.department_name} | Event: {bo.event_title} | Recipients: {len(export_rows)}",
            summary_pairs=[
                ("Bulk Order", bo.bulk_order_number),
                ("Distribution", bo.distribution_mode.upper()),
                ("Counter", f"Counter {bo.counter_id or 1}"),
            ],
            columns=headers,
            rows=table_rows,
        )
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{bo.bulk_order_number}-tokens.pdf"'},
        )

    out_csv = io.StringIO()
    writer = csv.writer(out_csv)
    writer.writerow(["recipient_identifier", "full_name", "department", "pickup_date", "pickup_slot", "counter_id", "token_status", "qr_token"])
    for r in export_rows:
        writer.writerow([
            ("'" + v if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@", "\t", "\r") else v)
            for v in (
                r["recipientIdentifier"],
                r["fullName"],
                r["department"],
                r["pickupDate"],
                r["pickupSlot"],
                r["counterId"],
                r["tokenStatus"],
                r["qrToken"],
            )
        ])
    return Response(
        content=out_csv.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{bo.bulk_order_number}-tokens.csv"'},
    )


@router.get("/recipients/{recipient_id}/token")
def get_individual_recipient_token_v1(
    recipient_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rec = db.scalar(
        select(BulkOrderRecipient)
        .options(joinedload(BulkOrderRecipient.allocations))
        .where(BulkOrderRecipient.id == recipient_id)
    )
    if not rec:
        raise ApiError(404, "RECIPIENT_NOT_FOUND", "Bulk order recipient not found.")
    bo = db.get(BulkOrder, rec.bulk_order_id)
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")

    # Verify strict recipient ownership or authorized representative/admin access
    is_owner = (
        (rec.user_id is not None and rec.user_id == user.id)
        or (rec.email and user.email and rec.email.lower() == user.email.lower())
        or (getattr(user, "college_id", None) and rec.recipient_identifier.upper() == str(getattr(user, "college_id")).upper())
    )
    is_rep = bo.coordinator_id == user.id and bo.distribution_mode == "representative"
    if not (is_owner or is_rep or user.role == UserRole.ADMIN):
        raise ApiError(403, "TOKEN_ACCESS_FORBIDDEN", "You are not authorized to view another recipient's QR token.")

    if rec.payment_status != "paid":
        raise ApiError(409, "PAYMENT_PENDING", "Recipient meal allocation is not yet paid.")

    tok = db.scalar(
        select(BulkMealToken).where(
            BulkMealToken.bulk_order_id == bo.id,
            BulkMealToken.recipient_id == rec.id,
            BulkMealToken.status.in_(["active", "redeemed"]),
        )
    )
    if tok is None:
        nonce = secrets.token_hex(16)
        raw_tok = _derive_raw_bulk_token(bo.id, rec.id, nonce)
        tok = BulkMealToken(
            bulk_order_id=bo.id,
            recipient_id=rec.id,
            token_hash=_hash_token(raw_tok),
            key_nonce=nonce,
            distribution_mode=bo.distribution_mode,
            status="active",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            distributed_at=datetime.now(timezone.utc),
        )
        db.add(tok)
        db.flush()
        _recompute_bulk_order_totals(db, bo)
        db.commit()
    else:
        raw_tok = _derive_raw_bulk_token(bo.id, rec.id, tok.key_nonce) if tok.status == "active" else None

    return ok(
        request,
        {
            "recipientId": rec.id,
            "bulkOrderId": bo.id,
            "bulkOrderNumber": bo.bulk_order_number,
            "recipientIdentifier": rec.recipient_identifier,
            "fullName": rec.full_name,
            "pickupDate": bo.pickup_date.isoformat(),
            "pickupSlot": bo.pickup_slot,
            "counterId": bo.counter_id or 1,
            "paymentStatus": rec.payment_status,
            "pickupStatus": rec.pickup_status,
            "tokenStatus": tok.status,
            "qrToken": raw_tok,
            "expiresAt": as_utc(tok.expires_at),
            "allocations": [
                {"menuItemId": a.product_id, "productName": a.product_name, "quantity": a.quantity}
                for a in (rec.allocations or [])
            ],
        },
    )


# ==============================================================================
# 6. Two-Counter Physical Pickup Verification (Atomic Single & Group/Representative)
# ==============================================================================
def _redeem_single_bulk_token_atomic(
    db: Session,
    raw_token: str,
    staff_id: int,
    counter_id: int,
    collected_via: str = "individual_qr",
    notes: str | None = None,
) -> dict[str, Any]:
    tok_hash = _hash_token(raw_token)
    tok = db.scalar(
        select(BulkMealToken).where(BulkMealToken.token_hash == tok_hash).with_for_update()
    )
    if not tok:
        raise ApiError(404, "INVALID_QR_TOKEN", "QR token not recognized.")
    if tok.status == "redeemed":
        raise ApiError(409, "TOKEN_ALREADY_REDEEMED", "This QR token has already been collected.")
    if tok.status == "revoked":
        raise ApiError(409, "TOKEN_REVOKED", "This QR token was revoked and replaced.")

    now = datetime.now(timezone.utc)
    if tok.status == "expired":
        raise ApiError(410, "TOKEN_EXPIRED", "This QR token has expired.")
    exp = tok.expires_at if tok.expires_at.tzinfo else tok.expires_at.replace(tzinfo=timezone.utc)
    if exp < now:
        tok.status = "expired"
        if db.in_nested_transaction():
            db.flush()
        else:
            db.commit()
        raise ApiError(410, "TOKEN_EXPIRED", "This QR token has expired.")

    bo = db.get(BulkOrder, tok.bulk_order_id)
    if not bo or bo.status == "cancelled":
        raise ApiError(409, "BULK_ORDER_CANCELLED", "Cancelled bulk orders cannot be collected.")

    rec = db.scalar(
        select(BulkOrderRecipient)
        .options(joinedload(BulkOrderRecipient.allocations))
        .where(BulkOrderRecipient.id == tok.recipient_id)
        .with_for_update()
    )
    if not rec or rec.payment_status != "paid":
        raise ApiError(409, "PAYMENT_NOT_VERIFIED", "Recipient allocation payment is not verified.")
    if rec.pickup_status == "collected":
        raise ApiError(409, "TOKEN_ALREADY_REDEEMED", "Recipient meal has already been collected.")

    # Atomic conditional update to guarantee exactly one concurrent scan succeeds even on SQLite
    upd = db.execute(
        update(BulkMealToken)
        .where(BulkMealToken.id == tok.id, BulkMealToken.status == "active")
        .values(
            status="redeemed",
            redeemed_at=now,
            redeemed_by_staff_id=staff_id,
            redeemed_counter_id=counter_id,
        )
    )
    if (upd.rowcount or 0) != 1:
        raise ApiError(409, "TOKEN_ALREADY_REDEEMED", "Concurrent scan detected: token already redeemed.")

    rec.pickup_status = "collected"
    rec.collected_at = now
    rec.collected_counter_id = counter_id

    pickup_rec = BulkPickupRecord(
        bulk_order_id=tok.bulk_order_id,
        recipient_id=rec.id,
        token_id=tok.id,
        counter_id=counter_id,
        staff_user_id=staff_id,
        collected_via=collected_via,
        notes=notes,
        picked_up_at=now,
    )
    db.add(pickup_rec)

    # Update item collected quantities on master BulkOrderItem
    for al in (rec.allocations or []):
        if al.bulk_order_item_id:
            db.execute(
                update(BulkOrderItem)
                .where(BulkOrderItem.id == al.bulk_order_item_id)
                .values(collected_quantity=BulkOrderItem.collected_quantity + al.quantity)
            )

    return {
        "pickupRecordId": pickup_rec.id,
        "bulkOrderId": tok.bulk_order_id,
        "recipientId": rec.id,
        "recipientIdentifier": rec.recipient_identifier,
        "fullName": rec.full_name,
        "counterId": counter_id,
        "collectedVia": collected_via,
        "pickedUpAt": as_utc(now),
    }


@router.post("/pickup/verify")
def verify_single_pickup_v1(
    request: Request,
    payload: SinglePickupVerifyIn,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    raw = (payload.qrToken or payload.pickupToken or "").strip()
    if not raw:
        raise ApiError(422, "MISSING_QR_TOKEN", "Provide qrToken or pickupToken.")

    if raw.startswith("bmt_"):
        res = _redeem_single_bulk_token_atomic(
            db,
            raw,
            staff_id=staff.id,
            counter_id=payload.counterId,
            collected_via="individual_qr",
            notes=payload.notes,
        )
        bo = db.get(BulkOrder, res["bulkOrderId"])
        if bo:
            _recompute_bulk_order_totals(db, bo)
        record_audit(
            db,
            "bulk_pickup.verified",
            "bulk_order",
            res["bulkOrderId"],
            staff.id,
            {"recipient_id": res["recipientId"], "counter_id": payload.counterId},
        )
        db.commit()
        return ok(request, res)

    # Fallback for individual order pk_... token
    from app.modules.orders_payments import VerifyPickupIn, verify_and_collect_pickup_v1
    return verify_and_collect_pickup_v1(
        payload=VerifyPickupIn(pickupToken=raw),
        request=request,
        staff=staff,
        db=db,
    )


@router.post("/pickup/group-verify")
def verify_group_representative_pickup_v1(
    request: Request,
    payload: GroupPickupVerifyIn,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    """Validates an array of individual recipient QR tokens presented by an authorized representative.
    A representative cannot collect meals with a single unrestricted QR code; every recipient token is validated
    and recorded individually.
    """
    succeeded: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    affected_bulk_ids: set[int] = set()

    for raw_tok in payload.qrTokens:
        clean_tok = raw_tok.strip()
        if not clean_tok:
            continue
        try:
            with db.begin_nested():
                item_res = _redeem_single_bulk_token_atomic(
                    db,
                    clean_tok,
                    staff_id=staff.id,
                    counter_id=payload.counterId,
                    collected_via="representative_batch",
                    notes=payload.notes or (f"Rep: {payload.representativeName}" if payload.representativeName else None),
                )
                succeeded.append(item_res)
                affected_bulk_ids.add(item_res["bulkOrderId"])
        except ApiError as err:
            rejected.append({"tokenPrefix": clean_tok[:16] + "...", "code": err.code, "message": err.message})

    for bid in affected_bulk_ids:
        bo = db.get(BulkOrder, bid)
        if bo:
            _recompute_bulk_order_totals(db, bo)
            record_audit(
                db,
                "bulk_pickup.group_verified",
                "bulk_order",
                bid,
                staff.id,
                {
                    "counter_id": payload.counterId,
                    "collected_count": len(succeeded),
                    "rejected_count": len(rejected),
                },
            )
    db.commit()
    return ok(
        request,
        {
            "counterId": payload.counterId,
            "collectedCount": len(succeeded),
            "rejectedCount": len(rejected),
            "succeeded": succeeded[:250],
            "rejected": rejected[:100],
        },
    )


@router.get("/pickup/progress")
def get_two_counter_pickup_progress_v1(
    request: Request,
    bulkOrderId: int | None = Query(default=None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    c1_bulk = db.scalar(select(func.count(BulkPickupRecord.id)).where(BulkPickupRecord.counter_id == 1)) or 0
    c2_bulk = db.scalar(select(func.count(BulkPickupRecord.id)).where(BulkPickupRecord.counter_id == 2)) or 0
    indiv_pickups = db.scalar(select(func.count(Pickup.id))) or 0

    active_bulk = db.scalars(
        select(BulkOrder).where(BulkOrder.status.not_in(["cancelled"])).order_by(BulkOrder.created_at.desc()).limit(20)
    ).all()
    if bulkOrderId:
        active_bulk = [b for b in active_bulk if b.id == bulkOrderId]

    return ok(
        request,
        {
            "physicalCounters": [
                {
                    "counterId": 1,
                    "name": "Counter 1 — Express & Pre-Packed Pickup",
                    "bulkMealsCollected": int(c1_bulk),
                    "individualOrdersCollected": int(indiv_pickups),
                    "avgScanServiceSeconds": 4.5,
                    "recommendedThroughputPerMinute": 13,
                },
                {
                    "counterId": 2,
                    "name": "Counter 2 — Bulk & Representative Dispatch",
                    "bulkMealsCollected": int(c2_bulk),
                    "individualOrdersCollected": 0,
                    "avgScanServiceSeconds": 3.5,
                    "recommendedThroughputPerMinute": 17,
                },
            ],
            "bulkOrdersProgress": [
                {
                    "bulkOrderId": b.id,
                    "bulkOrderNumber": b.bulk_order_number,
                    "departmentName": b.department_name,
                    "pickupSlot": b.pickup_slot,
                    "counterId": b.counter_id or 1,
                    "totalRecipients": b.total_recipients,
                    "paidRecipientsCount": b.paid_recipients_count,
                    "collectedRecipientsCount": b.collected_recipients_count,
                    "remainingCount": max(0, b.paid_recipients_count - b.collected_recipients_count),
                    "progressPct": round((b.collected_recipients_count / b.total_recipients) * 100.0, 1) if b.total_recipients > 0 else 0.0,
                }
                for b in active_bulk
            ],
        },
    )


@router.get("/pickup/history")
def get_pickup_history_v1(
    request: Request,
    counterId: int | None = Query(default=None),
    bulkOrderId: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    stmt = (
        select(BulkPickupRecord, BulkOrderRecipient, BulkOrder)
        .join(BulkOrderRecipient, BulkOrderRecipient.id == BulkPickupRecord.recipient_id)
        .join(BulkOrder, BulkOrder.id == BulkPickupRecord.bulk_order_id)
        .order_by(BulkPickupRecord.picked_up_at.desc())
    )
    if counterId is not None:
        stmt = stmt.where(BulkPickupRecord.counter_id == counterId)
    if bulkOrderId is not None:
        stmt = stmt.where(BulkPickupRecord.bulk_order_id == bulkOrderId)
    rows = db.execute(stmt.limit(limit)).all()
    return ok(
        request,
        {
            "items": [
                {
                    "pickupRecordId": rec_p.id,
                    "bulkOrderId": bo.id,
                    "bulkOrderNumber": bo.bulk_order_number,
                    "recipientId": rec.id,
                    "recipientIdentifier": rec.recipient_identifier,
                    "fullName": rec.full_name,
                    "counterId": rec_p.counter_id,
                    "collectedVia": rec_p.collected_via,
                    "staffUserId": rec_p.staff_user_id,
                    "pickedUpAt": as_utc(rec_p.picked_up_at),
                }
                for rec_p, rec, bo in rows
            ]
        },
    )


# ==============================================================================
# 7. Kitchen Batch Preparation & 6-Stage Workflow Dashboard
# ==============================================================================
@router.get("/kitchen/dashboard")
def get_kitchen_batch_dashboard_v1(
    request: Request,
    lookback_days: int = Query(default=14, ge=1, le=365, alias="lookbackDays"),
    user: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    """Aggregates ONLY verified/paid demand across active or recent individual and bulk orders."""
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    products = db.scalars(select(Product).order_by(Product.name.asc())).all()
    by_prod: dict[int, dict[str, Any]] = {
        p.id: {
            "menuItemId": p.id,
            "productName": p.name,
            "remainingInventory": p.stock_quantity,
            "individualConfirmedQty": 0,
            "bulkConfirmedQty": 0,
            "totalConfirmedMeals": 0,
            "packedQty": 0,
            "readyForPickupQty": 0,
            "collectedQty": 0,
        }
        for p in products
    }

    # 1. Confirmed/paid individual orders (all active orders + recent completed within lookback window)
    indiv_rows = db.execute(
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            Order.payment_status == PaymentStatus.PAID,
            Order.status.not_in([OrderStatus.CANCELLED]),
            or_(
                Order.status.not_in([OrderStatus.COMPLETED, OrderStatus.COLLECTED]),
                Order.created_at >= cutoff_dt,
            ),
        )
    ).all()
    for oi, ord_row in indiv_rows:
        entry = by_prod.get(oi.product_id)
        if not entry:
            continue
        entry["individualConfirmedQty"] += int(oi.quantity)
        entry["totalConfirmedMeals"] += int(oi.quantity)
        if ord_row.status == OrderStatus.READY:
            entry["packedQty"] += int(oi.quantity)
            entry["readyForPickupQty"] += int(oi.quantity)
        elif ord_row.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED}:
            entry["packedQty"] += int(oi.quantity)
            entry["collectedQty"] += int(oi.quantity)

    # 2. Paid/confirmed bulk allocations ONLY (never unpaid draft allocations; active or recent window)
    bulk_alloc_rows = db.execute(
        select(BulkOrderAllocation, BulkOrderRecipient, BulkOrder)
        .join(BulkOrderRecipient, BulkOrderRecipient.id == BulkOrderAllocation.recipient_id)
        .join(BulkOrder, BulkOrder.id == BulkOrderAllocation.bulk_order_id)
        .where(
            BulkOrderRecipient.payment_status == "paid",
            BulkOrder.status.not_in(["cancelled"]),
            or_(
                BulkOrder.status != "collected",
                BulkOrder.created_at >= cutoff_dt,
            ),
        )
    ).all()
    for al, rec, bo in bulk_alloc_rows:
        entry = by_prod.get(al.product_id)
        if not entry:
            continue
        q = int(al.quantity)
        entry["bulkConfirmedQty"] += q
        entry["totalConfirmedMeals"] += q
        if rec.pickup_status == "collected":
            entry["packedQty"] += q
            entry["collectedQty"] += q
        elif bo.status == "ready":
            entry["packedQty"] += q
            entry["readyForPickupQty"] += q
        elif bo.status == "packing":
            entry["packedQty"] += q

    bulk_orders = db.scalars(
        select(BulkOrder)
        .options(joinedload(BulkOrder.items))
        .where(BulkOrder.status.not_in(["cancelled"]))
        .order_by(BulkOrder.pickup_date.asc(), BulkOrder.created_at.desc())
        .limit(30)
    ).unique().all()

    return ok(
        request,
        {
            "workflowStages": ["verified", "reserved", "preparing", "packing", "ready", "collected"],
            "confirmedItemBatches": [v for v in by_prod.values() if v["totalConfirmedMeals"] > 0 or v["remainingInventory"] > 0],
            "bulkOrdersQueue": [_serialize_bulk_order(b) for b in bulk_orders],
        },
    )


@router.post("/kitchen/bulk-orders/{bulk_order_id}/stage")
def update_kitchen_bulk_order_stage_v1(
    bulk_order_id: int,
    request: Request,
    payload: KitchenStageUpdateIn,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    bo = db.scalar(select(BulkOrder).options(joinedload(BulkOrder.items)).where(BulkOrder.id == bulk_order_id))
    if not bo:
        raise ApiError(404, "BULK_ORDER_NOT_FOUND", "Bulk order not found.")
    if bo.paid_recipients_count == 0 and bo.payment_status != "paid":
        raise ApiError(409, "UNPAID_BULK_ORDER", "Cannot advance kitchen preparation for an unpaid bulk order.")

    prev_stage = bo.status
    bo.status = payload.stage
    if payload.stage in {"reserved", "preparing", "packing", "ready"} and not bo.inventory_reserved and bo.payment_mode == "centralized":
        _atomic_reserve_bulk_inventory(db, bo)

    for bi in (bo.items or []):
        bi.prep_status = payload.stage
        if payload.stage == "packing":
            bi.packed_quantity = payload.packedQuantity if payload.packedQuantity is not None else bi.quantity
        elif payload.stage == "ready":
            bi.packed_quantity = bi.quantity
            bi.ready_quantity = payload.readyQuantity if payload.readyQuantity is not None else bi.quantity
        elif payload.stage == "collected":
            bi.collected_quantity = bi.quantity

    record_audit(
        db,
        "kitchen.bulk_stage_updated",
        "bulk_order",
        bo.id,
        staff.id,
        {"from_stage": prev_stage, "to_stage": payload.stage, "notes": payload.notes},
    )
    db.commit()
    db.refresh(bo)
    return ok(request, _serialize_bulk_order(bo))


# ==============================================================================
# 8. Strict No-Refund Payment Incident Ledger & Administrative Resolution
# ==============================================================================
@router.get("/admin/payment-incidents")
def list_payment_incidents_v1(
    request: Request,
    status: str | None = Query(default=None),
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    stmt = select(PaymentIncident).order_by(PaymentIncident.created_at.desc())
    if status:
        stmt = stmt.where(PaymentIncident.status == status.strip().lower())
    rows = db.scalars(stmt.limit(200)).all()
    return ok(
        request,
        {
            "strictNoRefundPolicy": True,
            "items": [
                {
                    "id": inc.id,
                    "orderId": inc.order_id,
                    "bulkOrderId": inc.bulk_order_id,
                    "recipientId": inc.recipient_id,
                    "paymentId": inc.payment_id,
                    "bulkPaymentId": inc.bulk_payment_id,
                    "incidentType": inc.incident_type,
                    "amountPaise": inc.amount_paise,
                    "status": inc.status,
                    "description": inc.description,
                    "resolutionNotes": inc.resolution_notes,
                    "createdAt": as_utc(inc.created_at),
                    "resolvedAt": as_utc(inc.resolved_at),
                }
                for inc in rows
            ],
        },
    )


@router.post("/admin/payment-incidents/{incident_id}/resolve")
def resolve_payment_incident_v1(
    incident_id: int,
    request: Request,
    payload: IncidentResolveIn,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    inc = db.get(PaymentIncident, incident_id)
    if not inc:
        raise ApiError(404, "INCIDENT_NOT_FOUND", "Payment incident not found.")
    inc.status = payload.status
    inc.resolution_notes = payload.resolutionNotes.strip()
    inc.resolved_by_user_id = admin.id
    if payload.status == "resolved":
        inc.resolved_at = datetime.now(timezone.utc)
    record_audit(
        db,
        "payment_incident.resolved",
        "payment_incident",
        inc.id,
        admin.id,
        {"status": inc.status, "resolution_notes": inc.resolution_notes},
    )
    db.commit()
    return ok(
        request,
        {
            "id": inc.id,
            "status": inc.status,
            "resolutionNotes": inc.resolution_notes,
            "resolvedAt": as_utc(inc.resolved_at),
        },
    )
