"""/api/v1 Administrator Portal (A01-A08): Overview, Users/RBAC, Counters, Menu/Recipes, Discounts, Payments, Inventory, Reports & Audit."""
from __future__ import annotations

import base64
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.parser import BytesParser
from email.policy import default as email_default_policy
import io
import json
from pathlib import Path
import re
from typing import Any
import uuid
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db
from app.envelope import ApiError, ok
from app.models import (
    AuditEvent,
    CanteenCounter,
    Category,
    Discount,
    Ingredient,
    InventoryMovement,
    InventoryMovementType,
    MenuItemAvailability,
    Order,
    OrderItem,
    OrderStatus,
    Payment,
    PaymentStatus,
    PaymentWebhookEvent,
    Product,
    RecipeItem,
    Refund,
    StockMovement,
    User,
    UserCounterAssignment,
    UserRole,
    paise_to_rupees,
    rupees_to_paise,
)
from app.modules.common import (
    as_utc,
    commit_and_flush_outbox,
    ensure_default_counter,
    get_app_setting,
    get_tax_rate_bps,
    parse_json_safe,
    record_audit,
    record_product_stock_movement,
    serialize_counter_v1,
    serialize_product_v1,
    serialize_user_v1,
    set_app_setting,
)
from app.security import hash_password, require_roles, revoke_refresh_sessions

router = APIRouter(prefix="/admin", tags=["v1-admin"])

PROJECT_DIR = Path(__file__).resolve().parents[3]
UPLOADS_DIR = PROJECT_DIR / "frontend" / "assets" / "uploads"

def _detect_image_extension(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None

@router.get("/overview")
def get_admin_overview_v1(
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    tz = ZoneInfo(cfg.canteen_timezone)
    today_str = datetime.now(tz).date().isoformat()

    total_orders = db.scalar(select(func.count(Order.id))) or 0
    paid_orders = db.scalar(select(func.count(Order.id)).where(Order.payment_status == PaymentStatus.PAID)) or 0
    pending_orders = db.scalar(select(func.count(Order.id)).where(Order.payment_status == PaymentStatus.PENDING)) or 0
    failed_payments = db.scalar(select(func.count(Order.id)).where(Order.payment_status.in_([PaymentStatus.FAILED, PaymentStatus.EXPIRED]))) or 0
    refund_pending = db.scalar(select(func.count(Order.id)).where(Order.payment_status == PaymentStatus.REFUND_PENDING)) or 0
    late_captures = db.scalar(select(func.count(Payment.id)).where(Payment.late_capture_flagged.is_(True))) or 0

    paid_rows = db.scalars(select(Order).where(Order.payment_status == PaymentStatus.PAID)).all()
    total_revenue_paise = sum(o.effective_total_paise for o in paid_rows)
    today_revenue_paise = sum(o.effective_total_paise for o in paid_rows if o.token_date == today_str)

    now_utc = datetime.now(timezone.utc)
    active_prep_statuses = {OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.ACCEPTED, OrderStatus.PREPARING}
    active_queue = [o for o in paid_rows if o.status in active_prep_statuses]
    overdue_count = sum(
        1 for o in active_queue
        if ((now_utc - (as_utc(o.created_at) or now_utc)).total_seconds() // 60) >= cfg.overdue_preparation_minutes
    )

    popular_rows = db.execute(
        select(
            OrderItem.product_id,
            OrderItem.product_name,
            func.sum(OrderItem.quantity).label("units"),
            func.sum(func.coalesce(OrderItem.line_total_paise, 0)).label("rev_paise"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.payment_status == PaymentStatus.PAID)
        .group_by(OrderItem.product_id, OrderItem.product_name)
        .order_by(func.sum(OrderItem.quantity).desc())
        .limit(8)
    ).all()

    hour_buckets: dict[int, int] = {}
    for o in paid_rows:
        dt = as_utc(o.created_at)
        if dt is not None:
            h = dt.astimezone(tz).hour
            hour_buckets[h] = hour_buckets.get(h, 0) + 1
    peak_periods = [
        {"hour": f"{h:02d}:00", "orders": cnt}
        for h, cnt in sorted(hour_buckets.items(), key=lambda kv: (-kv[1], kv[0]))[:6]
    ]

    low_products = db.scalar(select(func.count(Product.id)).where(Product.is_archived.is_(False), Product.stock_quantity <= Product.low_stock_threshold)) or 0
    low_ingredients = db.scalar(select(func.count(Ingredient.id)).where(Ingredient.is_active.is_(True), Ingredient.current_quantity <= Ingredient.reorder_level)) or 0

    counters = db.scalars(select(CanteenCounter).order_by(CanteenCounter.id.asc())).all()
    return ok(
        request,
        {
            "totalOrders": int(total_orders),
            "paidOrders": int(paid_orders),
            "pendingOrders": int(pending_orders),
            "failedPayments": int(failed_payments),
            "refundPendingOrders": int(refund_pending),
            "lateCaptureAlerts": int(late_captures),
            "activeQueueOrders": len(active_queue),
            "overdueOrders": overdue_count,
            "totalRevenuePaise": total_revenue_paise,
            "todayRevenuePaise": today_revenue_paise,
            "lowStockProducts": int(low_products),
            "lowStockIngredients": int(low_ingredients),
            "popularFoods": [
                {
                    "menuItemId": int(r.product_id),
                    "name": r.product_name,
                    "unitsSold": int(r.units or 0),
                    "revenuePaise": int(r.rev_paise or 0),
                }
                for r in popular_rows
            ],
            "peakPeriods": peak_periods,
            "counters": [serialize_counter_v1(db, c) for c in counters],
        },
    )

class CounterCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    location: str = Field(default="Main Building", min_length=2, max_length=150)
    capacityPerSlot: int = Field(default=20, ge=1, le=500)
    openingHours: str = Field(default="08:30-17:30", max_length=500)
    isActive: bool = True

class CounterPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    location: str | None = Field(default=None, min_length=2, max_length=150)
    capacityPerSlot: int | None = Field(default=None, ge=1, le=500)
    openingHours: str | None = Field(default=None, max_length=500)
    isActive: bool | None = None

@router.get("/counters")
def list_admin_counters_v1(request: Request, _: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    ensure_default_counter(db)
    db.commit()
    rows = db.scalars(select(CanteenCounter).order_by(CanteenCounter.id.asc())).all()
    return ok(request, {"items": [serialize_counter_v1(db, c) for c in rows]})

@router.post("/counters", status_code=201)
def create_admin_counter_v1(payload: CounterCreateIn, request: Request, admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    exists = db.scalar(select(CanteenCounter).where(func.lower(CanteenCounter.name) == payload.name.strip().lower()))
    if exists is not None:
        raise ApiError(409, "DUPLICATE_COUNTER", "A counter with this name already exists.")
    c = CanteenCounter(
        name=payload.name.strip(),
        location=payload.location.strip(),
        capacity_per_slot=payload.capacityPerSlot,
        opening_hours=payload.openingHours.strip(),
        is_active=payload.isActive,
    )
    db.add(c)
    db.flush()
    record_audit(db, "counter.created", "counter", c.id, admin.id, {"name": c.name})
    db.commit()
    db.refresh(c)
    return ok(request, serialize_counter_v1(db, c), status_code=201)

@router.patch("/counters/{counter_id}")
def patch_admin_counter_v1(counter_id: int, payload: CounterPatchIn, request: Request, admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    c = db.get(CanteenCounter, counter_id)
    if c is None:
        raise ApiError(404, "COUNTER_NOT_FOUND", "Counter not found.")
    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"]:
        c.name = updates["name"].strip()
    if "location" in updates and updates["location"]:
        c.location = updates["location"].strip()
    if "capacityPerSlot" in updates and updates["capacityPerSlot"] is not None:
        c.capacity_per_slot = updates["capacityPerSlot"]
    if "openingHours" in updates and updates["openingHours"] is not None:
        c.opening_hours = updates["openingHours"].strip()
    if "isActive" in updates and updates["isActive"] is not None:
        c.is_active = updates["isActive"]
    record_audit(db, "counter.updated", "counter", c.id, admin.id, updates)
    db.commit()
    db.refresh(c)
    return ok(request, serialize_counter_v1(db, c))

class AdminInviteUserIn(BaseModel):
    fullName: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=30)
    temporaryPassword: str = Field(min_length=8, max_length=128)
    role: str = Field(pattern="^(student|staff|admin|STUDENT|STAFF|ADMIN)$")
    counterIds: list[int] = Field(default_factory=list)

class AdminRoleUpdateIn(BaseModel):
    role: str = Field(pattern="^(student|staff|admin|STUDENT|STAFF|ADMIN)$")

class AdminStatusUpdateIn(BaseModel):
    isActive: bool | None = None
    status: str | None = Field(default=None, pattern="^(ACTIVE|SUSPENDED)$")

class AdminCounterAssignIn(BaseModel):
    counterIds: list[int] = Field(default_factory=list)

@router.get("/users")
def list_admin_users_v1(
    request: Request,
    role: str | None = Query(default=None),
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    stmt = select(User).order_by(User.created_at.desc())
    if role:
        try:
            stmt = stmt.where(User.role == UserRole(role.strip().lower()))
        except ValueError:
            raise ApiError(422, "INVALID_ROLE", f"Unknown role '{role}'.")
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(term), User.email.ilike(term), User.phone.ilike(term)))
    users = db.scalars(stmt.offset(offset).limit(limit)).all()
    return ok(request, {"items": [serialize_user_v1(db, u) for u in users], "limit": limit, "offset": offset})

@router.post("/users", status_code=201)
def create_admin_user_v1(
    payload: AdminInviteUserIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    email_norm = payload.email.lower().strip()
    if db.scalar(select(User).where(User.email == email_norm)):
        raise ApiError(409, "EMAIL_ALREADY_REGISTERED", "An account with this email already exists.")
    phone_clean = (payload.phone or "").strip() or None
    if phone_clean and db.scalar(select(User).where(User.phone == phone_clean)):
        raise ApiError(409, "PHONE_ALREADY_REGISTERED", "An account with this phone number already exists.")
    role_enum = UserRole(payload.role.strip().lower())
    user = User(
        full_name=payload.fullName.strip(),
        email=email_norm,
        phone=phone_clean,
        password_hash=hash_password(payload.temporaryPassword),
        role=role_enum,
        is_active=True,
        must_change_password=(role_enum in {UserRole.STAFF, UserRole.ADMIN}),
        email_verified_at=datetime.now(timezone.utc),
    )
    db.add(user)
    db.flush()
    for cid in set(payload.counterIds):
        if db.get(CanteenCounter, cid):
            db.add(UserCounterAssignment(user_id=user.id, counter_id=cid))
    record_audit(db, "admin.user_created", "user", user.id, admin.id, {"role": role_enum.value, "counters": payload.counterIds})
    db.commit()
    db.refresh(user)
    return ok(request, serialize_user_v1(db, user), status_code=201)

@router.post("/users/{user_id}/role")
def update_user_role_v1(
    user_id: int,
    payload: AdminRoleUpdateIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    new_role = UserRole(payload.role.strip().lower())
    if target.id == admin.id and new_role != UserRole.ADMIN:
        raise ApiError(409, "SELF_DEMOTION_FORBIDDEN", "You cannot remove your own administrator role.")
    if target.role == UserRole.ADMIN and new_role != UserRole.ADMIN:
        other_admins = db.scalar(select(func.count(User.id)).where(User.role == UserRole.ADMIN, User.is_active.is_(True), User.id != target.id)) or 0
        if other_admins == 0:
            raise ApiError(409, "LAST_ADMIN_PROTECTED", "Cannot demote the last active administrator.")
    old_role = target.role.value
    target.role = new_role
    target.token_version += 1
    record_audit(db, "admin.user_role_changed", "user", target.id, admin.id, {"old_role": old_role, "new_role": new_role.value})
    db.commit()
    db.refresh(target)
    return ok(request, serialize_user_v1(db, target))

@router.patch("/users/{user_id}/status")
def update_user_status_v1(
    user_id: int,
    payload: AdminStatusUpdateIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    desired_active = payload.isActive if payload.isActive is not None else (payload.status == "ACTIVE" if payload.status else target.is_active)
    if target.id == admin.id and not desired_active:
        raise ApiError(409, "SELF_SUSPENSION_FORBIDDEN", "You cannot suspend your own administrator account.")
    target.is_active = bool(desired_active)
    if not target.is_active:
        target.token_version += 1
        revoke_refresh_sessions(db, user_id=target.id)
    record_audit(db, "admin.user_status_changed", "user", target.id, admin.id, {"is_active": target.is_active})
    db.commit()
    db.refresh(target)
    return ok(request, serialize_user_v1(db, target))

@router.put("/users/{user_id}/counters")
def assign_user_counters_v1(
    user_id: int,
    payload: AdminCounterAssignIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    valid_ids = []
    for cid in sorted(set(payload.counterIds)):
        if db.get(CanteenCounter, cid) is None:
            raise ApiError(404, "COUNTER_NOT_FOUND", f"Counter {cid} does not exist.")
        valid_ids.append(cid)
    existing = db.scalars(select(UserCounterAssignment).where(UserCounterAssignment.user_id == target.id)).all()
    for row in existing:
        db.delete(row)
    db.flush()
    for cid in valid_ids:
        db.add(UserCounterAssignment(user_id=target.id, counter_id=cid))
    record_audit(db, "admin.user_counters_assigned", "user", target.id, admin.id, {"counter_ids": valid_ids})
    db.commit()
    return ok(request, serialize_user_v1(db, target))

class CategoryUpsertIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    slug: str | None = Field(default=None, max_length=90)
    itemType: str = Field(default="food", pattern="^(food|stationery)$")
    description: str | None = Field(default=None, max_length=255)
    sortOrder: int = Field(default=0, ge=0, le=1000)
    isActive: bool = True

class MenuItemCreateV1In(BaseModel):
    categoryId: int = Field(gt=0)
    counterId: int | None = Field(default=None, gt=0)
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    itemType: str = Field(default="food", pattern="^(food|stationery)$")
    pricePaise: int = Field(gt=0, le=10_000_000)
    costPaise: int = Field(default=0, ge=0, le=10_000_000)
    stockQuantity: int = Field(default=25, ge=0, le=100_000)
    lowStockThreshold: int = Field(default=5, ge=0, le=10_000)
    preparationMinutes: int = Field(default=10, ge=1, le=180)
    isAvailable: bool = True
    imageUrl: str | None = Field(default=None, max_length=500)
    dietaryTags: list[str] = Field(default_factory=list)
    allergenTags: list[str] = Field(default_factory=list)
    ingredientsSummary: str | None = Field(default=None, max_length=1000)
    customizations: list[dict[str, Any]] = Field(default_factory=list)

class MenuItemPatchV1In(BaseModel):
    categoryId: int | None = Field(default=None, gt=0)
    counterId: int | None = Field(default=None, gt=0)
    name: str | None = Field(default=None, min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    pricePaise: int | None = Field(default=None, gt=0, le=10_000_000)
    costPaise: int | None = Field(default=None, ge=0, le=10_000_000)
    stockQuantity: int | None = Field(default=None, ge=0, le=100_000)
    lowStockThreshold: int | None = Field(default=None, ge=0, le=10_000)
    preparationMinutes: int | None = Field(default=None, ge=1, le=180)
    isAvailable: bool | None = None
    isArchived: bool | None = None
    imageUrl: str | None = Field(default=None, max_length=500)
    dietaryTags: list[str] | None = None
    allergenTags: list[str] | None = None
    ingredientsSummary: str | None = Field(default=None, max_length=1000)
    customizations: list[dict[str, Any]] | None = None

class RecipeLineIn(BaseModel):
    ingredientId: int = Field(gt=0)
    quantityPerServing: Decimal = Field(gt=Decimal("0"))

class RecipeReplaceIn(BaseModel):
    items: list[RecipeLineIn] = Field(default_factory=list, max_length=50)

class AvailabilityWindowIn(BaseModel):
    serviceDate: date | None = None
    startTime: str = Field(pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")
    endTime: str = Field(pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")
    availableQuantity: int | None = Field(default=None, ge=0, le=100_000)

class AvailabilityWindowsReplaceIn(BaseModel):
    windows: list[AvailabilityWindowIn] = Field(default_factory=list, max_length=20)

@router.post("/categories", status_code=201)
def create_admin_category_v1(payload: CategoryUpsertIn, request: Request, admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    slug = (payload.slug or re.sub(r"[^a-z0-9]+", "-", payload.name.strip().lower()).strip("-"))[:90]
    if db.scalar(select(Category).where(or_(func.lower(Category.name) == payload.name.strip().lower(), Category.slug == slug))):
        raise ApiError(409, "DUPLICATE_CATEGORY", "Category name or slug already exists.")
    cat = Category(
        name=payload.name.strip(),
        slug=slug,
        item_type=payload.itemType,
        description=payload.description,
        sort_order=payload.sortOrder,
        is_active=payload.isActive,
    )
    db.add(cat)
    db.flush()
    record_audit(db, "category.created", "category", cat.id, admin.id, {"name": cat.name})
    db.commit()
    db.refresh(cat)
    return ok(request, {"id": cat.id, "name": cat.name, "slug": cat.slug, "itemType": cat.item_type, "sortOrder": cat.sort_order, "isActive": cat.is_active}, status_code=201)

@router.get("/menu-items")
def list_admin_menu_items_v1(
    request: Request,
    includeArchived: bool = False,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    stmt = select(Product).options(joinedload(Product.category), joinedload(Product.recipe_items).joinedload(RecipeItem.ingredient)).order_by(Product.id.asc())
    if not includeArchived:
        stmt = stmt.where(Product.is_archived.is_(False))
    products = db.scalars(stmt).unique().all()
    items_out = []
    for p in products:
        d = serialize_product_v1(p)
        d["rawIsAvailable"] = p.is_available
        d["recipe"] = [
            {"ingredientId": ri.ingredient_id, "name": ri.ingredient.name if ri.ingredient else "", "quantityPerServing": str(ri.quantity_per_serving), "unit": ri.ingredient.unit if ri.ingredient else ""}
            for ri in p.recipe_items
        ]
        items_out.append(d)
    return ok(request, {"items": items_out})

@router.post("/menu-items", status_code=201)
def create_admin_menu_item_v1(
    payload: MenuItemCreateV1In,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    cat = db.get(Category, payload.categoryId)
    if cat is None:
        raise ApiError(404, "CATEGORY_NOT_FOUND", "Category not found.")
    counter_id = payload.counterId
    if counter_id is not None and db.get(CanteenCounter, counter_id) is None:
        raise ApiError(404, "COUNTER_NOT_FOUND", "Counter not found.")
    price_dec = paise_to_rupees(payload.pricePaise)
    d_tags = ",".join(t.strip() for t in payload.dietaryTags if t.strip())[:255] or None
    a_tags = ",".join(t.strip() for t in payload.allergenTags if t.strip())[:255] or None
    p = Product(
        category_id=cat.id,
        counter_id=counter_id,
        name=payload.name.strip(),
        description=payload.description,
        item_type=payload.itemType,
        price=price_dec,
        price_paise=payload.pricePaise,
        cost_paise=payload.costPaise,
        stock_quantity=payload.stockQuantity,
        low_stock_threshold=payload.lowStockThreshold,
        preparation_minutes=payload.preparationMinutes,
        is_available=bool(payload.isAvailable and cat.is_active and payload.stockQuantity > 0),
        image_url=payload.imageUrl,
        dietary_tags=d_tags,
        dietary_info=d_tags,
        allergen_tags=a_tags,
        allergens=a_tags,
        ingredients_list=payload.ingredientsSummary,
        customizations_json=json.dumps(payload.customizations, separators=(",", ":")),
    )
    db.add(p)
    db.flush()
    if p.stock_quantity > 0:
        record_product_stock_movement(db, p, p.stock_quantity, InventoryMovementType.INITIAL, "Initial menu item stock", actor_user_id=admin.id, before=0)
    record_audit(db, "menu_item.created", "product", p.id, admin.id, {"name": p.name, "price_paise": payload.pricePaise})
    db.commit()
    db.refresh(p)
    return ok(request, serialize_product_v1(p), status_code=201)

@router.patch("/menu-items/{item_id}")
def patch_admin_menu_item_v1(
    item_id: int,
    payload: MenuItemPatchV1In,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    p = db.scalar(select(Product).options(joinedload(Product.category)).where(Product.id == item_id).with_for_update())
    if p is None:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")
    updates = payload.model_dump(exclude_unset=True)
    if "categoryId" in updates and updates["categoryId"] is not None:
        cat = db.get(Category, updates["categoryId"])
        if cat is None:
            raise ApiError(404, "CATEGORY_NOT_FOUND", "Category not found.")
        p.category_id = cat.id
    if "counterId" in updates:
        if updates["counterId"] is not None and db.get(CanteenCounter, updates["counterId"]) is None:
            raise ApiError(404, "COUNTER_NOT_FOUND", "Counter not found.")
        p.counter_id = updates["counterId"]
    if "name" in updates and updates["name"]:
        p.name = updates["name"].strip()
    if "description" in updates:
        p.description = updates["description"]
    if "pricePaise" in updates and updates["pricePaise"] is not None:
        p.price_paise = updates["pricePaise"]
        p.price = paise_to_rupees(updates["pricePaise"])
    if "costPaise" in updates and updates["costPaise"] is not None:
        p.cost_paise = updates["costPaise"]
    if "lowStockThreshold" in updates and updates["lowStockThreshold"] is not None:
        p.low_stock_threshold = updates["lowStockThreshold"]
    if "preparationMinutes" in updates and updates["preparationMinutes"] is not None:
        p.preparation_minutes = updates["preparationMinutes"]
    if "imageUrl" in updates:
        p.image_url = updates["imageUrl"]
    if "dietaryTags" in updates and updates["dietaryTags"] is not None:
        joined = ",".join(t.strip() for t in updates["dietaryTags"] if t.strip())[:255] or None
        p.dietary_tags = joined
        p.dietary_info = joined
    if "allergenTags" in updates and updates["allergenTags"] is not None:
        joined = ",".join(t.strip() for t in updates["allergenTags"] if t.strip())[:255] or None
        p.allergen_tags = joined
        p.allergens = joined
    if "ingredientsSummary" in updates:
        p.ingredients_list = updates["ingredientsSummary"]
    if "customizations" in updates and updates["customizations"] is not None:
        p.customizations_json = json.dumps(updates["customizations"], separators=(",", ":"))
    if "stockQuantity" in updates and updates["stockQuantity"] is not None:
        new_q = updates["stockQuantity"]
        delta = new_q - p.stock_quantity
        if delta != 0:
            before_q = p.stock_quantity
            p.stock_quantity = new_q
            record_product_stock_movement(db, p, delta, InventoryMovementType.ADJUSTMENT, "Admin menu stock update", actor_user_id=admin.id, before=before_q)
    if "isArchived" in updates and updates["isArchived"] is not None:
        p.is_archived = updates["isArchived"]
        if p.is_archived:
            p.is_available = False
    if "isAvailable" in updates and updates["isAvailable"] is not None and not p.is_archived:
        p.is_available = bool(updates["isAvailable"] and p.stock_quantity > 0)

    record_audit(db, "menu_item.updated", "product", p.id, admin.id, {"fields": sorted(updates.keys())})
    commit_and_flush_outbox(db)
    db.refresh(p)
    return ok(request, serialize_product_v1(p))

@router.delete("/menu-items/{item_id}")
def delete_admin_menu_item_v1(
    item_id: int,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    p = db.get(Product, item_id)
    if p is None:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")

    item_name = p.name
    try:
        db.delete(p)
        db.flush()
        record_audit(db, "menu_item.deleted", "product", item_id, admin.id, {"name": item_name})
        commit_and_flush_outbox(db)
        return ok(request, {"deleted": True, "archived": False, "id": item_id})
    except Exception:
        db.rollback()
        p = db.get(Product, item_id)
        if p is not None:
            p.is_archived = True
            p.is_available = False
            p.stock_quantity = 0
            record_audit(db, "menu_item.archived_as_deleted", "product", item_id, admin.id, {"name": item_name})
            commit_and_flush_outbox(db)
            return ok(request, {"deleted": True, "archived": True, "id": item_id})
        raise ApiError(500, "DELETE_FAILED", "Failed to delete menu item.")

@router.post("/menu-items/{item_id}/image")
async def upload_admin_menu_item_image_v1(
    item_id: int,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    p = db.get(Product, item_id)
    if p is None:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")

    ctype = (request.headers.get("content-type") or "").lower()
    raw_body = await request.body()
    if not raw_body:
        raise ApiError(422, "EMPTY_IMAGE", "No image payload supplied.")

    img_bytes: bytes = b""
    if "application/json" in ctype:
        try:
            body_json = json.loads(raw_body.decode("utf-8"))
            b64_str = str(body_json.get("imageBase64") or "")
            if "," in b64_str and b64_str.startswith("data:"):
                b64_str = b64_str.split(",", 1)[1]
            img_bytes = base64.b64decode(b64_str, validate=True)
        except Exception:
            raise ApiError(422, "INVALID_IMAGE_BASE64", "Invalid base64 image payload.")
    elif "multipart/form-data" in ctype:
        header_bytes = f"Content-Type: {request.headers.get('content-type')}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
        msg = BytesParser(policy=email_default_policy).parsebytes(header_bytes + raw_body)
        for part in msg.iter_parts():
            if part.get_filename() or part.get_content_maintype() == "image":
                payload_part = part.get_payload(decode=True)
                if payload_part:
                    img_bytes = payload_part
                    break
    else:
        img_bytes = raw_body

    if len(img_bytes) > 900_000:
        raise ApiError(413, "IMAGE_TOO_LARGE", "Image must be 900 KB or smaller.")
    ext = _detect_image_extension(img_bytes)
    if ext is None:
        raise ApiError(422, "UNSUPPORTED_IMAGE_FORMAT", "Only verified PNG, JPEG and WEBP images are accepted.")

    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"item-{p.id}-{uuid.uuid4().hex[:12]}.{ext}"
    (UPLOADS_DIR / filename).write_bytes(img_bytes)

    cfg = get_settings()
    if cfg.object_storage_public_url:
        public_url = f"{cfg.object_storage_public_url.rstrip('/')}/{filename}"
    else:
        public_url = f"/assets/uploads/{filename}"

    p.image_url = public_url
    record_audit(db, "menu_item.image_uploaded", "product", p.id, admin.id, {"image_url": public_url, "bytes": len(img_bytes)})
    db.commit()
    return ok(request, {"menuItemId": p.id, "imageUrl": public_url})

@router.put("/menu-items/{item_id}/recipe")
def replace_menu_item_recipe_v1(
    item_id: int,
    payload: RecipeReplaceIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    p = db.get(Product, item_id)
    if p is None:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")
    seen: set[int] = set()
    for line in payload.items:
        if line.ingredientId in seen:
            raise ApiError(422, "DUPLICATE_RECIPE_INGREDIENT", "Each ingredient may appear at most once per recipe.")
        seen.add(line.ingredientId)
        if db.get(Ingredient, line.ingredientId) is None:
            raise ApiError(404, "INGREDIENT_NOT_FOUND", f"Ingredient {line.ingredientId} not found.")

    old_rows = db.scalars(select(RecipeItem).where(RecipeItem.product_id == p.id)).all()
    for r in old_rows:
        db.delete(r)
    db.flush()
    for line in payload.items:
        db.add(RecipeItem(product_id=p.id, ingredient_id=line.ingredientId, quantity_per_serving=line.quantityPerServing))
    record_audit(db, "menu_item.recipe_updated", "product", p.id, admin.id, {"ingredient_count": len(payload.items)})
    db.commit()
    return ok(request, {"menuItemId": p.id, "recipeCount": len(payload.items)})

@router.put("/menu-items/{item_id}/availability-windows")
def replace_menu_item_windows_v1(
    item_id: int,
    payload: AvailabilityWindowsReplaceIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    p = db.get(Product, item_id)
    if p is None:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")
    for w in payload.windows:
        if w.startTime >= w.endTime:
            raise ApiError(422, "INVALID_TIME_WINDOW", "startTime must be earlier than endTime.")
    old = db.scalars(select(MenuItemAvailability).where(MenuItemAvailability.product_id == p.id)).all()
    for r in old:
        db.delete(r)
    db.flush()
    for w in payload.windows:
        db.add(
            MenuItemAvailability(
                product_id=p.id,
                service_date=w.serviceDate,
                start_time=w.startTime,
                end_time=w.endTime,
                available_quantity=w.availableQuantity,
            )
        )
    record_audit(db, "menu_item.windows_updated", "product", p.id, admin.id, {"window_count": len(payload.windows)})
    db.commit()
    return ok(request, {"menuItemId": p.id, "windowCount": len(payload.windows)})

class DiscountCreateIn(BaseModel):
    code: str = Field(min_length=3, max_length=50)
    description: str | None = Field(default=None, max_length=255)
    discountPercent: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"), le=Decimal("100"))
    discountPaise: int = Field(default=0, ge=0, le=1_000_000)
    minOrderPaise: int = Field(default=0, ge=0, le=10_000_000)
    maxDiscountPaise: int | None = Field(default=None, gt=0, le=1_000_000)
    usageLimit: int | None = Field(default=None, gt=0)
    staffApprovable: bool = False
    startsAt: datetime | None = None
    endsAt: datetime | None = None
    isActive: bool = True

class DiscountPatchIn(BaseModel):
    description: str | None = Field(default=None, max_length=255)
    discountPercent: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("100"))
    discountPaise: int | None = Field(default=None, ge=0, le=1_000_000)
    minOrderPaise: int | None = Field(default=None, ge=0, le=10_000_000)
    maxDiscountPaise: int | None = Field(default=None, gt=0, le=1_000_000)
    usageLimit: int | None = Field(default=None, gt=0)
    staffApprovable: bool | None = None
    isActive: bool | None = None

def _serialize_discount(d: Discount) -> dict[str, Any]:
    return {
        "id": d.id,
        "code": d.code,
        "description": d.description,
        "discountPercent": str(d.discount_percent),
        "discountPaise": rupees_to_paise(d.discount_amount),
        "minOrderPaise": rupees_to_paise(d.min_order_amount),
        "maxDiscountPaise": rupees_to_paise(d.max_discount_amount) if d.max_discount_amount is not None else None,
        "usageLimit": d.usage_limit,
        "usageCount": d.usage_count,
        "staffApprovable": d.staff_approvable,
        "startsAt": as_utc(d.starts_at),
        "endsAt": as_utc(d.ends_at),
        "isActive": d.is_active,
    }

@router.get("/discounts")
def list_admin_discounts_v1(request: Request, _: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Discount).order_by(Discount.created_at.desc())).all()
    return ok(request, {"items": [_serialize_discount(d) for d in rows]})

@router.post("/discounts", status_code=201)
def create_admin_discount_v1(payload: DiscountCreateIn, request: Request, admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    code_norm = payload.code.strip().upper()
    if payload.discountPercent <= 0 and payload.discountPaise <= 0:
        raise ApiError(422, "INVALID_DISCOUNT", "Specify a positive discountPercent or discountPaise.")
    if db.scalar(select(Discount).where(func.upper(Discount.code) == code_norm)):
        raise ApiError(409, "DUPLICATE_DISCOUNT_CODE", f"Discount code '{code_norm}' already exists.")
    d = Discount(
        code=code_norm,
        description=payload.description,
        discount_percent=payload.discountPercent,
        discount_amount=paise_to_rupees(payload.discountPaise),
        min_order_amount=paise_to_rupees(payload.minOrderPaise),
        max_discount_amount=paise_to_rupees(payload.maxDiscountPaise) if payload.maxDiscountPaise else None,
        usage_limit=payload.usageLimit,
        staff_approvable=payload.staffApprovable,
        starts_at=payload.startsAt,
        ends_at=payload.endsAt,
        is_active=payload.isActive,
    )
    db.add(d)
    db.flush()
    record_audit(db, "discount.created", "discount", d.id, admin.id, {"code": d.code})
    db.commit()
    db.refresh(d)
    return ok(request, _serialize_discount(d), status_code=201)

@router.patch("/discounts/{discount_id}")
def patch_admin_discount_v1(discount_id: int, payload: DiscountPatchIn, request: Request, admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    d = db.get(Discount, discount_id)
    if d is None:
        raise ApiError(404, "DISCOUNT_NOT_FOUND", "Discount not found.")
    updates = payload.model_dump(exclude_unset=True)
    if "description" in updates:
        d.description = updates["description"]
    if "discountPercent" in updates and updates["discountPercent"] is not None:
        d.discount_percent = updates["discountPercent"]
    if "discountPaise" in updates and updates["discountPaise"] is not None:
        d.discount_amount = paise_to_rupees(updates["discountPaise"])
    if "minOrderPaise" in updates and updates["minOrderPaise"] is not None:
        d.min_order_amount = paise_to_rupees(updates["minOrderPaise"])
    if "maxDiscountPaise" in updates:
        d.max_discount_amount = paise_to_rupees(updates["maxDiscountPaise"]) if updates["maxDiscountPaise"] else None
    if "usageLimit" in updates:
        d.usage_limit = updates["usageLimit"]
    if "staffApprovable" in updates and updates["staffApprovable"] is not None:
        d.staff_approvable = updates["staffApprovable"]
    if "isActive" in updates and updates["isActive"] is not None:
        d.is_active = updates["isActive"]
    record_audit(db, "discount.updated", "discount", d.id, admin.id, {"code": d.code})
    db.commit()
    db.refresh(d)
    return ok(request, _serialize_discount(d))

@router.get("/payments/reconciliation")
def get_admin_payment_reconciliation_v1(
    request: Request,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    payments = db.scalars(select(Payment).options(joinedload(Payment.order), joinedload(Payment.refunds)).order_by(Payment.created_at.desc()).limit(300)).unique().all()
    refunds = db.scalars(select(Refund).order_by(Refund.created_at.desc()).limit(200)).all()
    webhooks = db.scalars(select(PaymentWebhookEvent).order_by(PaymentWebhookEvent.received_at.desc()).limit(100)).all()

    discrepancies = []
    for p in payments:
        ord_obj = p.order
        if p.late_capture_flagged:
            discrepancies.append({
                "type": "LATE_CAPTURE_AFTER_EXPIRY",
                "paymentId": p.id,
                "orderId": p.order_id,
                "providerPaymentId": p.provider_payment_id,
                "amountPaise": p.amount_paise,
                "status": p.status,
            })
        elif ord_obj is not None and p.status == "SUCCESS" and ord_obj.payment_status not in {PaymentStatus.PAID, PaymentStatus.REFUND_PENDING, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED}:
            discrepancies.append({
                "type": "ORDER_NOT_MARKED_PAID",
                "paymentId": p.id,
                "orderId": p.order_id,
                "providerPaymentId": p.provider_payment_id,
                "amountPaise": p.amount_paise,
                "orderPaymentStatus": ord_obj.payment_status.value,
            })

    return ok(
        request,
        {
            "payments": [
                {
                    "id": p.id,
                    "orderId": p.order_id,
                    "orderNumber": p.order.order_number if p.order else None,
                    "provider": p.provider,
                    "providerOrderId": p.provider_order_id,
                    "providerPaymentId": p.provider_payment_id,
                    "amountPaise": p.amount_paise,
                    "status": p.status,
                    "lateCaptureFlagged": p.late_capture_flagged,
                    "createdAt": as_utc(p.created_at),
                }
                for p in payments
            ],
            "refunds": [
                {
                    "id": r.id,
                    "orderId": r.order_id,
                    "paymentRecordId": r.payment_record_id,
                    "providerRefundId": r.provider_refund_id,
                    "amountPaise": r.amount_paise or rupees_to_paise(r.amount),
                    "status": r.status,
                    "reason": r.reason,
                    "createdAt": as_utc(r.created_at),
                }
                for r in refunds
            ],
            "discrepancies": discrepancies,
            "recentWebhookEvents": [
                {
                    "id": w.id,
                    "eventId": w.event_id,
                    "eventName": w.event_name,
                    "processingStatus": w.processing_status,
                    "receivedAt": as_utc(w.received_at),
                }
                for w in webhooks
            ],
        },
    )

class IngredientCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    unit: str = Field(default="g", min_length=1, max_length=20)
    reorderLevel: Decimal = Field(default=Decimal("100.00"), ge=Decimal("0"))
    currentQuantity: Decimal = Field(default=Decimal("0.00"), ge=Decimal("0"))
    costPerUnit: Decimal | None = Field(default=None, ge=Decimal("0"))

class IngredientPatchIn(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    unit: str | None = Field(default=None, min_length=1, max_length=20)
    reorderLevel: Decimal | None = Field(default=None, ge=Decimal("0"))
    costPerUnit: Decimal | None = Field(default=None, ge=Decimal("0"))
    isActive: bool | None = None

@router.get("/ingredients")
def list_admin_ingredients_v1(request: Request, _: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    rows = db.scalars(select(Ingredient).order_by(Ingredient.name.asc())).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": ing.id,
                    "name": ing.name,
                    "unit": ing.unit,
                    "currentQuantity": str(ing.current_quantity),
                    "reorderLevel": str(ing.reorder_level),
                    "costPerUnit": str(ing.cost_per_unit) if ing.cost_per_unit is not None else None,
                    "isActive": ing.is_active,
                    "isLowStock": Decimal(ing.current_quantity) <= Decimal(ing.reorder_level),
                }
                for ing in rows
            ]
        },
    )

@router.post("/ingredients", status_code=201)
def create_admin_ingredient_v1(payload: IngredientCreateIn, request: Request, admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    if db.scalar(select(Ingredient).where(func.lower(Ingredient.name) == payload.name.strip().lower())):
        raise ApiError(409, "DUPLICATE_INGREDIENT", "An ingredient with this name already exists.")
    ing = Ingredient(
        name=payload.name.strip(),
        unit=payload.unit.strip(),
        reorder_level=payload.reorderLevel,
        current_quantity=payload.currentQuantity,
        cost_per_unit=payload.costPerUnit,
        is_active=True,
    )
    db.add(ing)
    db.flush()
    if payload.currentQuantity > 0:
        db.add(
            StockMovement(
                ingredient_id=ing.id,
                movement_type="initial",
                quantity_delta=payload.currentQuantity,
                quantity_before=Decimal("0.00"),
                quantity_after=payload.currentQuantity,
                reference_type="admin_init",
                reason="Initial ingredient stock",
                actor_user_id=admin.id,
            )
        )
    record_audit(db, "ingredient.created", "ingredient", ing.id, admin.id, {"name": ing.name, "unit": ing.unit})
    db.commit()
    db.refresh(ing)
    return ok(request, {"id": ing.id, "name": ing.name, "unit": ing.unit, "currentQuantity": str(ing.current_quantity), "reorderLevel": str(ing.reorder_level)}, status_code=201)

@router.patch("/ingredients/{ingredient_id}")
def patch_admin_ingredient_v1(ingredient_id: int, payload: IngredientPatchIn, request: Request, admin: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    ing = db.get(Ingredient, ingredient_id)
    if ing is None:
        raise ApiError(404, "INGREDIENT_NOT_FOUND", "Ingredient not found.")
    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"]:
        ing.name = updates["name"].strip()
    if "unit" in updates and updates["unit"]:
        ing.unit = updates["unit"].strip()
    if "reorderLevel" in updates and updates["reorderLevel"] is not None:
        ing.reorder_level = updates["reorderLevel"]
    if "costPerUnit" in updates:
        ing.cost_per_unit = updates["costPerUnit"]
    if "isActive" in updates and updates["isActive"] is not None:
        ing.is_active = updates["isActive"]
    record_audit(db, "ingredient.updated", "ingredient", ing.id, admin.id, {"fields": sorted(updates.keys())})
    db.commit()
    db.refresh(ing)
    return ok(request, {"id": ing.id, "name": ing.name, "unit": ing.unit, "currentQuantity": str(ing.current_quantity), "reorderLevel": str(ing.reorder_level), "isActive": ing.is_active})

def _parse_date_window(start_date: date | None, end_date: date | None) -> tuple[datetime, datetime, date, date]:
    end_d = end_date or date.today()
    start_d = start_date or (end_d - timedelta(days=30))
    if start_d > end_d:
        raise ApiError(422, "INVALID_DATE_RANGE", "startDate must not be after endDate.")
    start_dt = datetime(start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc)
    end_dt = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=timezone.utc)
    return start_dt, end_dt, start_d, end_d

@router.get("/reports/sales")
def get_admin_sales_report_v1(
    request: Request,
    startDate: date | None = None,
    endDate: date | None = None,
    counterId: int | None = Query(default=None, gt=0),
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    start_dt, end_dt, start_d, end_d = _parse_date_window(startDate, endDate)
    stmt = select(Order).options(joinedload(Order.items)).where(
        Order.payment_status == PaymentStatus.PAID,
        Order.created_at >= start_dt,
        Order.created_at <= end_dt,
    )
    if counterId is not None:
        stmt = stmt.where(Order.counter_id == counterId)
    orders = db.scalars(stmt).unique().all()

    gross_paise = sum(o.subtotal_paise or rupees_to_paise(o.subtotal) for o in orders)
    discount_paise = sum(o.discount_paise or rupees_to_paise(o.discount_amount) for o in orders)
    tax_paise = sum(o.tax_paise or rupees_to_paise(o.tax_amount) for o in orders)
    net_paise = sum(o.effective_total_paise for o in orders)

    by_day: dict[str, dict[str, int]] = {}
    for o in orders:
        day_k = o.token_date or (as_utc(o.created_at) or start_dt).date().isoformat()
        bucket = by_day.setdefault(day_k, {"orders": 0, "netRevenuePaise": 0})
        bucket["orders"] += 1
        bucket["netRevenuePaise"] += o.effective_total_paise

    return ok(
        request,
        {
            "startDate": start_d.isoformat(),
            "endDate": end_d.isoformat(),
            "paidOrderCount": len(orders),
            "grossSubtotalPaise": gross_paise,
            "discountTotalPaise": discount_paise,
            "taxTotalPaise": tax_paise,
            "netRevenuePaise": net_paise,
            "dailyBreakdown": [{"date": k, **v} for k, v in sorted(by_day.items())],
        },
    )

@router.get("/reports/orders")
def get_admin_orders_kpi_report_v1(
    request: Request,
    startDate: date | None = None,
    endDate: date | None = None,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    start_dt, end_dt, start_d, end_d = _parse_date_window(startDate, endDate)
    orders = db.scalars(select(Order).where(Order.created_at >= start_dt, Order.created_at <= end_dt)).all()
    total = len(orders)
    collected = [o for o in orders if o.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED}]
    cancelled = [o for o in orders if o.status in {OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}]
    prep_mins = []
    for o in orders:
        c_at = as_utc(o.created_at)
        r_at = as_utc(o.ready_at)
        if c_at and r_at and r_at > c_at:
            prep_mins.append(round((r_at - c_at).total_seconds() / 60.0, 2))
    prep_mins.sort()
    median_prep = prep_mins[len(prep_mins) // 2] if prep_mins else None

    return ok(
        request,
        {
            "startDate": start_d.isoformat(),
            "endDate": end_d.isoformat(),
            "totalOrders": total,
            "collectedOrders": len(collected),
            "cancelledOrders": len(cancelled),
            "collectionRatePct": round(len(collected) * 100.0 / total, 2) if total else 0.0,
            "cancellationRatePct": round(len(cancelled) * 100.0 / total, 2) if total else 0.0,
            "medianPrepMinutes": median_prep,
        },
    )

@router.get("/reports/waste")
def get_admin_waste_report_v1(
    request: Request,
    startDate: date | None = None,
    endDate: date | None = None,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    start_dt, end_dt, start_d, end_d = _parse_date_window(startDate, endDate)
    prod_waste = db.scalars(
        select(InventoryMovement)
        .options(joinedload(InventoryMovement.product))
        .where(
            InventoryMovement.movement_type == InventoryMovementType.WASTE,
            InventoryMovement.created_at >= start_dt,
            InventoryMovement.created_at <= end_dt,
        )
        .order_by(InventoryMovement.created_at.desc())
    ).all()
    ing_waste = db.scalars(
        select(StockMovement)
        .options(joinedload(StockMovement.ingredient))
        .where(
            StockMovement.movement_type == "wastage",
            StockMovement.created_at >= start_dt,
            StockMovement.created_at <= end_dt,
        )
        .order_by(StockMovement.created_at.desc())
    ).all()

    est_prod_loss_paise = sum(abs(mv.quantity_delta) * (mv.product.effective_price_paise if mv.product else 0) for mv in prod_waste)
    est_ing_loss_paise = 0
    for mv in ing_waste:
        if mv.ingredient and mv.ingredient.cost_per_unit is not None:
            est_ing_loss_paise += rupees_to_paise(abs(Decimal(mv.quantity_delta)) * Decimal(mv.ingredient.cost_per_unit))

    return ok(
        request,
        {
            "startDate": start_d.isoformat(),
            "endDate": end_d.isoformat(),
            "estimatedProductLossPaise": est_prod_loss_paise,
            "estimatedIngredientLossPaise": est_ing_loss_paise,
            "productWasteEvents": [
                {
                    "id": mv.id,
                    "menuItemId": mv.product_id,
                    "itemName": mv.product.name if mv.product else "",
                    "unitsWasted": abs(mv.quantity_delta),
                    "reason": mv.reason,
                    "createdAt": as_utc(mv.created_at),
                }
                for mv in prod_waste
            ],
            "ingredientWasteEvents": [
                {
                    "id": mv.id,
                    "ingredientId": mv.ingredient_id,
                    "ingredientName": mv.ingredient.name if mv.ingredient else "",
                    "quantityWasted": str(abs(Decimal(mv.quantity_delta))),
                    "unit": mv.ingredient.unit if mv.ingredient else "",
                    "reason": mv.reason,
                    "createdAt": as_utc(mv.created_at),
                }
                for mv in ing_waste
            ],
        },
    )

@router.get("/reports/export.csv")
def export_admin_reports_csv_v1(
    reportType: str = Query(default="sales", pattern="^(sales|orders|waste)$"),
    startDate: date | None = None,
    endDate: date | None = None,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    start_dt, end_dt, _, _ = _parse_date_window(startDate, endDate)
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)

    if reportType == "waste":
        writer.writerow(["kind", "item_or_ingredient", "quantity_wasted", "unit", "reason", "created_at"])
        for mv in db.scalars(select(InventoryMovement).options(joinedload(InventoryMovement.product)).where(InventoryMovement.movement_type == InventoryMovementType.WASTE, InventoryMovement.created_at >= start_dt, InventoryMovement.created_at <= end_dt)).all():
            writer.writerow(["menu_item", mv.product.name if mv.product else mv.product_id, abs(mv.quantity_delta), "units", mv.reason, mv.created_at.isoformat()])
        for mv in db.scalars(select(StockMovement).options(joinedload(StockMovement.ingredient)).where(StockMovement.movement_type == "wastage", StockMovement.created_at >= start_dt, StockMovement.created_at <= end_dt)).all():
            writer.writerow(["ingredient", mv.ingredient.name if mv.ingredient else mv.ingredient_id, str(abs(Decimal(mv.quantity_delta))), mv.ingredient.unit if mv.ingredient else "", mv.reason or "", mv.created_at.isoformat()])
    else:
        writer.writerow(["order_id", "order_number", "created_at", "counter_id", "status", "payment_status", "subtotal_paise", "discount_paise", "tax_paise", "total_paise", "gateway_order_id", "gateway_payment_id"])
        stmt = select(Order).where(Order.created_at >= start_dt, Order.created_at <= end_dt).order_by(Order.created_at.desc()).limit(10_000)
        if reportType == "sales":
            stmt = stmt.where(Order.payment_status == PaymentStatus.PAID)
        for o in db.scalars(stmt).all():
            writer.writerow([
                o.id,
                o.order_number or "",
                o.created_at.isoformat() if o.created_at else "",
                o.counter_id or "",
                o.status.value,
                o.payment_status.value,
                o.subtotal_paise or rupees_to_paise(o.subtotal),
                o.discount_paise or 0,
                o.tax_paise or 0,
                o.effective_total_paise,
                o.gateway_order_id or "",
                o.gateway_payment_id or "",
            ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=ace-canteen-{reportType}.csv",
            "Cache-Control": "no-store",
        },
    )

@router.get("/audit-logs")
def list_admin_audit_logs_v1(
    request: Request,
    action: str | None = Query(default=None, max_length=80),
    entityType: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    stmt = select(AuditEvent).options(joinedload(AuditEvent.actor)).order_by(AuditEvent.created_at.desc())
    if action:
        stmt = stmt.where(AuditEvent.action.ilike(f"%{action.strip()}%"))
    if entityType:
        stmt = stmt.where(AuditEvent.entity_type == entityType.strip())
    rows = db.scalars(stmt.offset(offset).limit(limit)).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": ev.id,
                    "action": ev.action,
                    "entityType": ev.entity_type,
                    "entityId": ev.entity_id,
                    "actorUserId": ev.actor_user_id,
                    "actorEmail": ev.actor.email if ev.actor else None,
                    "details": parse_json_safe(ev.details_json, {}),
                    "createdAt": as_utc(ev.created_at),
                }
                for ev in rows
            ],
            "limit": limit,
            "offset": offset,
        },
    )

class AdminSettingsPatchIn(BaseModel):
    taxRateBps: int | None = Field(default=None, ge=0, le=2800)
    orderingPaused: bool | None = None
    pauseMessage: str | None = Field(default=None, max_length=200)
    auditRetentionDays: int | None = Field(default=None, ge=30, le=3650)
    canteenOpenTime: str | None = Field(default=None, pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")
    canteenCloseTime: str | None = Field(default=None, pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")

@router.get("/settings")
def get_admin_settings_v1(request: Request, _: User = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    cfg = get_settings()
    return ok(
        request,
        {
            "appName": cfg.app_name,
            "brandName": cfg.brand_name,
            "version": cfg.app_version,
            "environment": cfg.environment,
            "taxRateBps": get_tax_rate_bps(db),
            "orderingPaused": get_app_setting(db, "ordering_paused", "0") == "1",
            "pauseMessage": get_app_setting(db, "pause_message", ""),
            "auditRetentionDays": int(get_app_setting(db, "audit_retention_days", "365") or "365"),
            "tempRetentionDays": int(get_app_setting(db, "temp_retention_days", str(cfg.temp_retention_days)) or "4"),
            "canteenTimezone": cfg.canteen_timezone,
            "canteenOpenTime": get_app_setting(db, "canteen_open_time", cfg.canteen_open_time or "08:30"),
            "canteenCloseTime": get_app_setting(db, "canteen_close_time", cfg.canteen_close_time or "17:30"),
            "pickupSlotMinutes": cfg.pickup_slot_minutes,
            "pickupSlotCapacity": cfg.pickup_slot_capacity,
            "pickupTokenMinutes": cfg.pickup_token_minutes,
            "overduePreparationMinutes": cfg.overdue_preparation_minutes,
            "adminRequireMfa": cfg.admin_require_mfa,
            "aiProviderConfigured": bool(cfg.gemini_api_key),
            "paymentGatewayConfigured": bool(cfg.razorpay_key_id and cfg.razorpay_key_secret),
        },
    )

@router.patch("/settings")
def patch_admin_settings_v1(
    payload: AdminSettingsPatchIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    updates = payload.model_dump(exclude_unset=True)
    if "taxRateBps" in updates and updates["taxRateBps"] is not None:
        set_app_setting(db, "tax_rate_bps", str(updates["taxRateBps"]))
    if "orderingPaused" in updates and updates["orderingPaused"] is not None:
        set_app_setting(db, "ordering_paused", "1" if updates["orderingPaused"] else "0")
    if "pauseMessage" in updates:
        set_app_setting(db, "pause_message", (updates["pauseMessage"] or "").strip())
    if "auditRetentionDays" in updates and updates["auditRetentionDays"] is not None:
        set_app_setting(db, "audit_retention_days", str(updates["auditRetentionDays"]))
    if "canteenOpenTime" in updates and updates["canteenOpenTime"] is not None:
        set_app_setting(db, "canteen_open_time", updates["canteenOpenTime"])
    if "canteenCloseTime" in updates and updates["canteenCloseTime"] is not None:
        set_app_setting(db, "canteen_close_time", updates["canteenCloseTime"])
    record_audit(db, "admin.settings_updated", "settings", "global", admin.id, updates)
    db.commit()
    return get_admin_settings_v1(request, admin, db)
