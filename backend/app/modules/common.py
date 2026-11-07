"""Shared domain services, serializers, pricing/tax/discount rules, recipe stock and audit helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.envelope import ApiError
from app.models import (
    AppSetting,
    AuditEvent,
    BatchReservation,
    CanteenCounter,
    Category,
    Discount,
    Ingredient,
    InventoryMovement,
    InventoryMovementType,
    Notification,
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    Payment,
    PaymentStatus,
    Product,
    ProductBatch,
    RecipeItem,
    StockMovement,
    User,
    UserCounterAssignment,
    UserRole,
    paise_to_rupees,
    rupees_to_paise,
)
from app.realtime import hub, send_web_push_to_user, stage_outbox_event

V1_ORDER_STATUS_OUT = {
    OrderStatus.PENDING: "PENDING_PAYMENT",
    OrderStatus.CONFIRMED: "CONFIRMED",
    OrderStatus.ACCEPTED: "ACCEPTED",
    OrderStatus.PREPARING: "PREPARING",
    OrderStatus.READY: "READY",
    OrderStatus.COMPLETED: "COLLECTED",
    OrderStatus.COLLECTED: "COLLECTED",
    OrderStatus.PARTIALLY_COLLECTED: "PARTIALLY_COLLECTED",
    OrderStatus.CANCELLED: "CANCELLED",
    OrderStatus.REFUND_PENDING: "REFUND_PENDING",
}

V1_PAYMENT_STATUS_OUT = {
    PaymentStatus.PENDING: "PENDING",
    PaymentStatus.PAID: "SUCCESS",
    PaymentStatus.FAILED: "FAILED",
    PaymentStatus.EXPIRED: "EXPIRED",
    PaymentStatus.REFUND_PENDING: "REFUND_PENDING",
    PaymentStatus.PARTIALLY_REFUNDED: "PARTIALLY_REFUNDED",
    PaymentStatus.REFUNDED: "REFUNDED",
}

def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)

def parse_tags(csv_or_json: str | None, fallback_text: str | None = None) -> list[str]:
    raw = csv_or_json or fallback_text or ""
    raw = raw.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except ValueError:
            pass
    return [part.strip() for part in raw.split(",") if part.strip()]

def parse_json_safe(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default

def get_app_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(AppSetting, key)
    return row.value if row else default

def set_app_setting(db: Session, key: str, value: str) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=str(value)[:500]))
    else:
        row.value = str(value)[:500]

def get_tax_rate_bps(db: Session) -> int:
    """Return configured tax rate in basis points (e.g. 500 = 5.00%). Default 0."""
    raw = get_app_setting(db, "tax_rate_bps", "0")
    try:
        val = int(raw)
        return max(0, min(val, 2800))
    except ValueError:
        return 0

def ensure_default_counter(db: Session) -> CanteenCounter:
    """Guarantee at least one active counter exists so products and orders always resolve cleanly."""
    counter = db.scalar(select(CanteenCounter).order_by(CanteenCounter.id.asc()).limit(1))
    if counter is None:
        cfg = get_settings()
        hours = f"{cfg.canteen_open_time}-{cfg.canteen_close_time}" if cfg.canteen_open_time and cfg.canteen_close_time else "08:30-17:30"
        counter = CanteenCounter(
            name="Main Canteen Counter",
            location="Ground Floor, Central Block",
            is_active=True,
            capacity_per_slot=max(10, cfg.pickup_slot_capacity),
            opening_hours=hours,
        )
        db.add(counter)
        db.flush()
    return counter

def record_audit(
    db: Session,
    action: str,
    entity_type: str,
    entity_id: int | str | None,
    actor_user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    safe_details = {}
    for k, v in (details or {}).items():
        if any(secret_word in k.lower() for secret_word in ("password", "secret", "signature", "pin", "token")):
            continue
        safe_details[k] = v
    db.add(
        AuditEvent(
            actor_user_id=actor_user_id,
            action=action[:80],
            entity_type=entity_type[:80],
            entity_id=str(entity_id)[:80] if entity_id is not None else None,
            details_json=json.dumps(safe_details, separators=(",", ":"), sort_keys=True, default=str),
        )
    )

def record_order_status_change(
    db: Session,
    order: Order,
    previous_status: str,
    new_status: str,
    actor_user_id: int | None = None,
    reason: str | None = None,
) -> None:
    db.add(
        OrderStatusHistory(
            order_id=order.id,
            previous_status=previous_status[:40],
            new_status=new_status[:40],
            actor_user_id=actor_user_id,
            reason=reason[:255] if reason else None,
        )
    )

def notify_user(
    db: Session,
    user_id: int,
    notification_type: str,
    title: str,
    message: str,
    *,
    entity_type: str | None = None,
    entity_id: int | str | None = None,
    push: bool = True,
) -> Notification:
    notif = Notification(
        user_id=user_id,
        notification_type=notification_type[:40],
        title=title[:120],
        message=message[:500],
        entity_type=entity_type[:40] if entity_type else None,
        entity_id=str(entity_id)[:80] if entity_id is not None else None,
    )
    db.add(notif)
    stage_outbox_event(
        db,
        "notification.created",
        {"type": notification_type, "title": title, "message": message, "entityType": entity_type, "entityId": str(entity_id) if entity_id is not None else None},
        target_user_id=user_id,
    )
    if push:
        send_web_push_to_user(db, user_id, title, message, {"entityType": entity_type, "entityId": str(entity_id) if entity_id else None})
    return notif

def notify_counter_staff(
    db: Session,
    counter_id: int | None,
    notification_type: str,
    title: str,
    message: str,
    *,
    entity_type: str | None = None,
    entity_id: int | str | None = None,
) -> None:
    admins = db.scalars(select(User).where(User.is_active.is_(True), User.role == UserRole.ADMIN)).all()
    staff_users = db.scalars(select(User).where(User.is_active.is_(True), User.role == UserRole.STAFF)).all()
    recipients: dict[int, User] = {u.id: u for u in admins}
    for st in staff_users:
        assigned = db.scalars(select(UserCounterAssignment.counter_id).where(UserCounterAssignment.user_id == st.id)).all()
        if not assigned or counter_id is None or counter_id in assigned:
            recipients[st.id] = st
    for u in recipients.values():
        notify_user(db, u.id, notification_type, title, message, entity_type=entity_type, entity_id=entity_id, push=False)

def record_product_stock_movement(
    db: Session,
    product: Product,
    delta: int,
    movement_type: InventoryMovementType,
    reason: str,
    *,
    actor_user_id: int | None = None,
    order_id: int | None = None,
    before: int | None = None,
) -> None:
    if delta == 0:
        return
    old_qty = product.stock_quantity if before is None else before
    new_qty = old_qty + delta
    if new_qty < 0:
        raise ApiError(409, "INSUFFICIENT_STOCK", f"Insufficient stock for {product.name}.")
    db.add(
        InventoryMovement(
            product_id=product.id,
            actor_user_id=actor_user_id,
            order_id=order_id,
            movement_type=movement_type,
            quantity_delta=delta,
            quantity_before=old_qty,
            quantity_after=new_qty,
            reason=reason[:255],
        )
    )
    if new_qty <= product.low_stock_threshold and old_qty > product.low_stock_threshold:
        notify_counter_staff(
            db,
            product.counter_id,
            "low_stock",
            "Low stock alert",
            f"{product.name}: {new_qty} unit(s) remain (threshold {product.low_stock_threshold}).",
            entity_type="product",
            entity_id=product.id,
        )
        stage_outbox_event(
            db,
            "inventory.low_stock",
            {"productId": product.id, "name": product.name, "remaining": new_qty},
            counter_id=product.counter_id,
        )

def reserve_batch_stock_fefo(db: Session, product_id: int, quantity: int, order_id: int) -> None:
    today = datetime.now(ZoneInfo(get_settings().canteen_timezone)).date()
    batches = db.scalars(
        select(ProductBatch)
        .where(
            ProductBatch.product_id == product_id,
            ProductBatch.quantity_remaining > 0,
            (ProductBatch.expiry_date.is_(None)) | (ProductBatch.expiry_date >= today),
        )
        .order_by(ProductBatch.expiry_date.is_(None), ProductBatch.expiry_date.asc(), ProductBatch.received_date.asc(), ProductBatch.id.asc())
        .with_for_update()
    ).all()
    rem = quantity
    for b in batches:
        if rem <= 0:
            break
        take = min(rem, b.quantity_remaining)
        if take > 0:
            b.quantity_remaining -= take
            db.add(BatchReservation(order_id=order_id, batch_id=b.id, quantity=take))
            rem -= take

def release_batch_stock_once(db: Session, order_id: int) -> int:
    rows = db.scalars(
        select(BatchReservation)
        .where(BatchReservation.order_id == order_id, BatchReservation.released_at.is_(None))
        .with_for_update()
    ).all()
    restored = 0
    now = datetime.now(timezone.utc)
    for alloc in rows:
        batch = db.get(ProductBatch, alloc.batch_id)
        if batch is not None:
            batch.quantity_remaining += alloc.quantity
            restored += alloc.quantity
        alloc.released_at = now
    return restored

def consume_recipe_ingredients(db: Session, product_id: int, servings: int, order_id: int, actor_user_id: int | None = None) -> None:
    """Deduct linked recipe ingredients atomically when an order reserves stock."""
    recipe_rows = db.scalars(select(RecipeItem).where(RecipeItem.product_id == product_id)).all()
    for r in recipe_rows:
        ing = db.scalar(select(Ingredient).where(Ingredient.id == r.ingredient_id, Ingredient.is_active.is_(True)).with_for_update())
        if ing is None:
            continue
        needed = (Decimal(r.quantity_per_serving) * Decimal(servings)).quantize(Decimal("0.001"))
        if needed <= 0:
            continue
        before = Decimal(ing.current_quantity)
        if before < needed:
            raise ApiError(409, "INGREDIENT_SHORTAGE", f"Ingredient '{ing.name}' is insufficient to prepare {servings} serving(s).")
        after = (before - needed).quantize(Decimal("0.01"))
        ing.current_quantity = after
        db.add(
            StockMovement(
                ingredient_id=ing.id,
                movement_type="consumption",
                quantity_delta=-needed,
                quantity_before=before,
                quantity_after=after,
                reference_type="order",
                reference_id=str(order_id),
                reason=f"Recipe consumption for order #{order_id}",
                actor_user_id=actor_user_id,
            )
        )
        if after <= Decimal(ing.reorder_level) and before > Decimal(ing.reorder_level):
            notify_counter_staff(
                db,
                None,
                "ingredient_low",
                "Ingredient reorder alert",
                f"{ing.name} is down to {after} {ing.unit} (reorder level {ing.reorder_level} {ing.unit}).",
                entity_type="ingredient",
                entity_id=ing.id,
            )

def restore_recipe_ingredients(db: Session, order_id: int, actor_user_id: int | None = None) -> None:
    """Reverse recipe ingredient deductions once if an unfulfilled order is cancelled."""
    already_released = db.scalar(
        select(StockMovement.id).where(
            StockMovement.reference_type == "order",
            StockMovement.reference_id == str(order_id),
            StockMovement.movement_type == "release",
        )
    )
    if already_released is not None:
        return
    consumed = db.scalars(
        select(StockMovement).where(
            StockMovement.reference_type == "order",
            StockMovement.reference_id == str(order_id),
            StockMovement.movement_type == "consumption",
        )
    ).all()
    for mv in consumed:
        ing = db.scalar(select(Ingredient).where(Ingredient.id == mv.ingredient_id).with_for_update())
        if ing is None:
            continue
        restore_qty = abs(Decimal(mv.quantity_delta))
        before = Decimal(ing.current_quantity)
        after = (before + restore_qty).quantize(Decimal("0.01"))
        ing.current_quantity = after
        db.add(
            StockMovement(
                ingredient_id=ing.id,
                movement_type="release",
                quantity_delta=restore_qty,
                quantity_before=before,
                quantity_after=after,
                reference_type="order",
                reference_id=str(order_id),
                reason=f"Restored ingredients for cancelled order #{order_id}",
                actor_user_id=actor_user_id,
            )
        )

def compute_customization_extra_paise(product: Product, chosen_customizations: dict[str, Any] | list[Any] | None) -> tuple[int, dict[str, Any]]:
    """Validate selected customizations against the item's declared options and sum extra paise."""
    if not chosen_customizations:
        return 0, {}
    allowed_defs = parse_json_safe(product.customizations_json, [])
    if not isinstance(chosen_customizations, dict):
        return 0, {}
    extra_paise = 0
    cleaned: dict[str, Any] = {}
    defs_by_name = {str(d.get("name", "")).strip().lower(): d for d in allowed_defs if isinstance(d, dict) and d.get("name")}
    for key, val in chosen_customizations.items():
        k_clean = str(key).strip()[:60]
        v_clean = str(val).strip()[:80]
        if not k_clean or not v_clean:
            continue
        cleaned[k_clean] = v_clean
        defn = defs_by_name.get(k_clean.lower())
        if defn and isinstance(defn.get("options"), list):
            opt = next((o for o in defn["options"] if isinstance(o, dict) and str(o.get("label", "")).lower() == v_clean.lower()), None)
            if opt is None:
                raise ApiError(422, "INVALID_CUSTOMIZATION", f"Option '{v_clean}' is not valid for '{k_clean}' on {product.name}.")
            extra_paise += max(0, int(opt.get("price_paise") or opt.get("priceDeltaPaise") or 0))
        elif defn is not None:
            is_truthy = val is True or v_clean.lower() in {"true", "1", "yes", "selected", "on"}
            if is_truthy:
                delta = defn.get("priceDeltaPaise")
                if delta is None:
                    delta = defn.get("price_delta_paise", defn.get("price_paise", 0))
                extra_paise += max(0, int(delta or 0))
            else:
                cleaned.pop(k_clean, None)
    return extra_paise, cleaned

def evaluate_discount_paise(db: Session, code: str | None, subtotal_paise: int, *, lock_for_update: bool = False) -> tuple[int, Discount | None]:
    if not code or not code.strip():
        return 0, None
    normalized = code.strip().upper()
    stmt = select(Discount).where(func.upper(Discount.code) == normalized)
    if lock_for_update:
        stmt = stmt.with_for_update()
    offer = db.scalar(stmt)
    if offer is None or not offer.is_active:
        raise ApiError(409, "INVALID_COUPON", f"Coupon '{normalized}' is not valid or has expired.")
    now = datetime.now(timezone.utc)
    if offer.starts_at and as_utc(offer.starts_at) > now:
        raise ApiError(409, "COUPON_NOT_STARTED", f"Coupon '{normalized}' is not active yet.")
    if offer.ends_at and as_utc(offer.ends_at) < now:
        raise ApiError(409, "COUPON_EXPIRED", f"Coupon '{normalized}' has expired.")
    if offer.usage_limit is not None and offer.usage_count >= offer.usage_limit:
        raise ApiError(409, "COUPON_USAGE_EXCEEDED", f"Coupon '{normalized}' has reached its redemption limit.")
    min_paise = rupees_to_paise(offer.min_order_amount)
    if subtotal_paise < min_paise:
        raise ApiError(409, "COUPON_MINIMUM_NOT_MET", f"Minimum order of ₹{paise_to_rupees(min_paise)} required for '{normalized}'.")
    pct = Decimal(offer.discount_percent or 0)
    flat_paise = rupees_to_paise(offer.discount_amount)
    discount_paise = flat_paise
    if pct > 0:
        discount_paise += int((Decimal(subtotal_paise) * pct / Decimal(100)).quantize(Decimal("1")))
    if offer.max_discount_amount is not None and Decimal(offer.max_discount_amount) > 0:
        discount_paise = min(discount_paise, rupees_to_paise(offer.max_discount_amount))
    discount_paise = max(0, min(discount_paise, subtotal_paise))
    return discount_paise, offer

def estimate_counter_wait_minutes(db: Session, counter_id: int | None, extra_prep_minutes: int = 8) -> dict[str, Any]:
    """Transparent queue + preparation duration + recent fulfillment speed formula."""
    active_statuses = [OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.ACCEPTED, OrderStatus.PREPARING]
    stmt = select(func.count(Order.id)).where(Order.payment_status == PaymentStatus.PAID, Order.status.in_(active_statuses))
    if counter_id is not None:
        stmt = stmt.where((Order.counter_id == counter_id) | (Order.counter_id.is_(None)))
    queued_count = db.scalar(stmt) or 0

    counter = db.get(CanteenCounter, counter_id) if counter_id else None
    parallel_capacity = max(2, min(8, (counter.capacity_per_slot // 4) if counter and counter.capacity_per_slot else 3))

    today = datetime.now(ZoneInfo(get_settings().canteen_timezone)).date().isoformat()
    recent_orders = db.scalars(
        select(Order)
        .where(
            Order.token_date == today,
            Order.payment_status == PaymentStatus.PAID,
            Order.ready_at.is_not(None),
        )
        .order_by(Order.ready_at.desc())
        .limit(20)
    ).all()
    measured_durations = []
    for ro in recent_orders:
        c_at = as_utc(ro.created_at)
        r_at = as_utc(ro.ready_at)
        if c_at and r_at and r_at > c_at:
            mins = (r_at - c_at).total_seconds() / 60.0
            if 1 <= mins <= 90:
                measured_durations.append(mins)
    avg_ticket_mins = (sum(measured_durations) / len(measured_durations)) if measured_durations else float(max(4, extra_prep_minutes))
    queue_backlog_mins = round((queued_count / parallel_capacity) * avg_ticket_mins)
    estimated_minutes = max(3, int(round(extra_prep_minutes + queue_backlog_mins * 0.6)))
    return {
        "queuedOrders": queued_count,
        "parallelCapacity": parallel_capacity,
        "estimatedMinutes": min(estimated_minutes, 90),
        "basis": "measured_today_and_queue" if measured_durations else "formula_queue_and_prep_time",
        "sampleSize": len(measured_durations),
    }

def generate_manual_pickup_code() -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(6))

DEFAULT_STAFF_PERMISSIONS: dict[str, bool] = {
    "canAcceptOrders": True,
    "canRejectOrders": True,
    "canUpdatePrep": True,
    "canVerifyPickup": True,
    "canManageAvailability": True,
    "canManageInventory": True,
}

def get_effective_staff_permissions(user: User) -> dict[str, bool]:
    if user.role == UserRole.ADMIN:
        return {k: True for k in DEFAULT_STAFF_PERMISSIONS}
    if user.role != UserRole.STAFF:
        return {k: False for k in DEFAULT_STAFF_PERMISSIONS}
    merged = dict(DEFAULT_STAFF_PERMISSIONS)
    raw = parse_json_safe(user.staff_permissions_json, {})
    if isinstance(raw, dict):
        for k in DEFAULT_STAFF_PERMISSIONS:
            if k in raw:
                merged[k] = bool(raw[k])
    return merged

def assert_staff_permission(user: User, perm_key: str) -> None:
    perms = get_effective_staff_permissions(user)
    if not perms.get(perm_key, False):
        raise ApiError(403, "PERMISSION_DENIED", f"Your staff account is not granted '{perm_key}' permission.")

def serialize_user_v1(db: Session, user: User) -> dict[str, Any]:
    counter_ids = db.scalars(select(UserCounterAssignment.counter_id).where(UserCounterAssignment.user_id == user.id)).all()
    return {
        "id": user.id,
        "publicUuid": user.public_uuid,
        "fullName": user.full_name,
        "email": user.email,
        "phone": user.phone,
        "role": user.role.value.upper(),
        "status": "ACTIVE" if user.is_active else "SUSPENDED",
        "isActive": user.is_active,
        "emailVerified": user.email_verified_at is not None,
        "mfaEnabled": bool(user.mfa_enabled),
        "mustChangePassword": bool(user.must_change_password),
        "dietaryPreference": user.dietary_preference,
        "assignedCounterIds": [int(c) for c in counter_ids],
        "staffPermissions": get_effective_staff_permissions(user),
        "createdAt": as_utc(user.created_at),
    }

def serialize_counter_v1(db: Session, counter: CanteenCounter) -> dict[str, Any]:
    eta = estimate_counter_wait_minutes(db, counter.id, 6)
    return {
        "id": counter.id,
        "name": counter.name,
        "location": counter.location,
        "isActive": counter.is_active,
        "capacityPerSlot": counter.capacity_per_slot,
        "openingHours": parse_json_safe(counter.opening_hours, counter.opening_hours),
        "activeQueueOrders": eta["queuedOrders"],
        "approximateWaitMinutes": eta["estimatedMinutes"],
        "createdAt": as_utc(counter.created_at),
    }

def serialize_product_v1(product: Product, *, rating_avg: float | None = None, rating_count: int = 0) -> dict[str, Any]:
    price_paise = product.effective_price_paise
    return {
        "id": product.id,
        "counterId": product.counter_id,
        "categoryId": product.category_id,
        "categoryName": product.category_name,
        "name": product.name,
        "description": product.description,
        "itemType": product.item_type,
        "pricePaise": price_paise,
        "costPaise": int(product.cost_paise or 0),
        "price": str(paise_to_rupees(price_paise)),
        "stockQuantity": product.stock_quantity,
        "lowStockThreshold": product.low_stock_threshold,
        "isAvailable": bool(product.is_available and not product.is_archived and product.stock_quantity > 0),
        "isArchived": bool(product.is_archived),
        "preparationMinutes": product.preparation_minutes or 10,
        "imageUrl": product.image_url,
        "dietaryTags": parse_tags(product.dietary_tags, product.dietary_info),
        "allergenTags": parse_tags(product.allergen_tags, product.allergens),
        "ingredientsSummary": product.ingredients_list,
        "customizations": parse_json_safe(product.customizations_json, []),
        "ratingAverage": rating_avg,
        "ratingCount": rating_count,
    }

def format_token_display(
    token_number: int | None,
    *,
    counter_name: str | None = None,
    token_date: str | None = None,
) -> str:
    """Format human-friendly smart token: e.g. CB-1107-001 or B-001."""
    if not token_number or token_number <= 0:
        return "-"
    prefix = "CB"
    if counter_name:
        cn = counter_name.strip().lower()
        if "beverage" in cn or "drink" in cn or "coffee" in cn:
            prefix = "B"
        elif "snack" in cn or "fast" in cn:
            prefix = "S"
        elif "lunch" in cn or "meal" in cn:
            prefix = "L"
        elif "breakfast" in cn or "tiffin" in cn:
            prefix = "BF"
        elif "bakery" in cn:
            prefix = "BK"
        else:
            words = [w for w in counter_name.strip().split() if w]
            prefix = "".join(w[0].upper() for w in words[:2]) or "CB"

    date_tag = ""
    if token_date and len(token_date) >= 10:
        date_tag = f"-{token_date[5:7]}{token_date[8:10]}"
    return f"{prefix}{date_tag}-{token_number:03d}" if date_tag else f"{prefix}-{token_number:03d}"

def serialize_order_v1(order: Order, *, include_customer: bool = False, include_history: bool = False) -> dict[str, Any]:
    cfg = get_settings()
    now = datetime.now(timezone.utc)
    created = as_utc(order.created_at) or now
    elapsed_mins = max(0, int((now - created).total_seconds() // 60))
    is_active_prep = order.payment_status == PaymentStatus.PAID and order.status in {
        OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.ACCEPTED, OrderStatus.PREPARING
    }
    is_overdue = bool(is_active_prep and elapsed_mins >= cfg.overdue_preparation_minutes)

    subtotal_p = order.subtotal_paise if order.subtotal_paise is not None and order.subtotal_paise > 0 else rupees_to_paise(order.subtotal)
    discount_p = order.discount_paise if order.discount_paise is not None else rupees_to_paise(order.discount_amount)
    tax_p = order.tax_paise if order.tax_paise is not None else rupees_to_paise(order.tax_amount)
    total_p = order.effective_total_paise

    can_cancel = order.status in {OrderStatus.PENDING, OrderStatus.CONFIRMED} and order.collected_at is None
    c_name = order.counter.name if getattr(order, "counter", None) else None

    out: dict[str, Any] = {
        "id": order.id,
        "publicUuid": order.public_uuid,
        "orderNumber": order.order_number or f"ACE-{order.id:06d}",
        "tokenNumber": order.token_number,
        "tokenDisplay": format_token_display(order.token_number, counter_name=c_name, token_date=order.token_date),
        "tokenDate": order.token_date,
        "counterId": order.counter_id,
        "counterName": c_name,
        "handledByStaffId": order.handled_by_staff_id,
        "status": V1_ORDER_STATUS_OUT.get(order.status, order.status.value.upper()),
        "rawStatus": order.status.value,
        "paymentStatus": V1_PAYMENT_STATUS_OUT.get(order.payment_status, order.payment_status.value.upper()),
        "subtotalPaise": subtotal_p,
        "discountPaise": discount_p,
        "taxPaise": tax_p,
        "totalPaise": total_p,
        "subtotal": str(paise_to_rupees(subtotal_p)),
        "total": str(paise_to_rupees(total_p)),
        "couponCode": order.coupon_code,
        "notes": order.notes,
        "exceptionReason": order.exception_reason,
        "manualPickupCode": order.manual_pickup_code if order.payment_status == PaymentStatus.PAID else None,
        "pickupSlotStart": as_utc(order.pickup_slot),
        "pickupSlotEnd": as_utc(order.pickup_slot_end),
        "estimatedReadyAt": as_utc(order.estimated_ready_at),
        "elapsedMinutes": elapsed_mins,
        "isOverdue": is_overdue,
        "canCancel": can_cancel,
        "gatewayOrderId": order.gateway_order_id,
        "gatewayPaymentId": order.gateway_payment_id,
        "createdAt": as_utc(order.created_at),
        "acceptedAt": as_utc(order.accepted_at),
        "readyAt": as_utc(order.ready_at),
        "collectedAt": as_utc(order.collected_at),
        "cancelledAt": as_utc(order.cancelled_at),
        "items": [
            {
                "id": item.id,
                "menuItemId": item.product_id,
                "itemName": item.product_name,
                "name": item.product_name,
                "unitPricePaise": item.unit_price_paise if item.unit_price_paise is not None else rupees_to_paise(item.unit_price),
                "quantity": item.quantity,
                "customizations": parse_json_safe(item.customizations_json, {}),
                "lineTotalPaise": item.line_total_paise if item.line_total_paise is not None else rupees_to_paise(item.line_total),
                "counterId": item.product.counter_id if getattr(item, "product", None) else None,
                "counterName": item.product.counter.name if (getattr(item, "product", None) and item.product.counter) else None,
                "isCollected": bool(getattr(item, "is_collected", False)),
                "collectedQuantity": getattr(item, "collected_quantity", 0) or 0,
                "collectedAt": as_utc(getattr(item, "collected_at", None)),
            }
            for item in order.items
        ],
    }
    if include_customer and order.user is not None:
        raw_phone = order.user.phone or ""
        masked_phone = (raw_phone[:3] + "****" + raw_phone[-3:]) if len(raw_phone) >= 7 else raw_phone
        out["customer"] = {
            "id": order.user.id,
            "fullName": order.user.full_name,
            "email": order.user.email,
            "studentId": f"ACE-{order.user.id:04d}",
            "phoneMasked": masked_phone,
        }
    if include_history:
        out["statusHistory"] = [
            {
                "previousStatus": h.previous_status,
                "newStatus": h.new_status,
                "actorUserId": h.actor_user_id,
                "reason": h.reason,
                "createdAt": as_utc(h.created_at),
            }
            for h in sorted(order.status_history, key=lambda x: x.id)
        ]
    return out

def commit_and_flush_outbox(db: Session) -> None:
    db.commit()
    hub.notify_flush()
