"""/api/v1 Staff Kitchen Operations, Counter-Scoped Queue, Ingredients/Wastage & Alerts."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db
from app.emailer import send_email
from app.envelope import ApiError, ok
from app.models import (
    Ingredient,
    InventoryMovement,
    InventoryMovementType,
    Order,
    OrderStatus,
    OrderStatusHistory,
    PaymentStatus,
    Pickup,
    Product,
    ProductBatch,
    RecipeItem,
    StockMovement,
    User,
)
from app.modules.common import (
    as_utc,
    assert_staff_permission,
    commit_and_flush_outbox,
    estimate_counter_wait_minutes,
    get_effective_staff_permissions,
    notify_counter_staff,
    notify_user,
    record_audit,
    record_order_status_change,
    record_product_stock_movement,
    serialize_order_v1,
    serialize_product_v1,
)
from app.modules.orders_payments import _initiate_gateway_refund, _restore_order_inventory_once
from app.realtime import stage_outbox_event
from app.security import assert_counter_access, get_staff_assigned_counter_ids, require_roles

router = APIRouter(prefix="/staff", tags=["v1-staff"])

class OrderTransitionNoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=255)

class OrderExceptionIn(BaseModel):
    reason: str = Field(min_length=3, max_length=255)
    cancelAndRefund: bool = False

class MenuAvailabilityPatchIn(BaseModel):
    isAvailable: bool
    reason: str | None = Field(default="Staff availability update", max_length=255)

class StaffStockMovementIn(BaseModel):
    ingredientId: int | None = Field(default=None, gt=0)
    menuItemId: int | None = Field(default=None, gt=0)
    movementType: str = Field(pattern="^(restock|wastage|adjustment)$")
    quantity: Decimal = Field(gt=Decimal("0"))
    reason: str = Field(min_length=3, max_length=255)

def _load_staff_order_locked(db: Session, order_id: int, staff: User) -> Order:
    order = db.scalar(
        select(Order)
        .options(joinedload(Order.items).joinedload(Order.items.property.mapper.class_.product), joinedload(Order.user), joinedload(Order.status_history), joinedload(Order.payments))
        .where(Order.id == order_id)
        .with_for_update()
    )
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")
    assert_counter_access(db, staff, order.counter_id)
    if order.payment_status != PaymentStatus.PAID:
        raise ApiError(409, "PAYMENT_PENDING", "Only paid orders can be operated in the kitchen queue.")
    return order

@router.get("/orders")
def list_staff_orders_v1(
    request: Request,
    counterId: int | None = Query(default=None, gt=0),
    status: str | None = Query(default=None, max_length=30),
    overdueOnly: bool = False,
    includeCollected: bool = False,
    limit: int = Query(default=150, ge=1, le=300),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assigned = get_staff_assigned_counter_ids(db, staff)
    if counterId is not None:
        assert_counter_access(db, staff, counterId)

    stmt = (
        select(Order)
        .options(joinedload(Order.items), joinedload(Order.user), joinedload(Order.status_history))
        .where(Order.payment_status == PaymentStatus.PAID)
    )
    if counterId is not None:
        stmt = stmt.where(Order.counter_id == counterId)
    elif assigned:
        stmt = stmt.where(Order.counter_id.in_(assigned))

    if status:
        norm = status.strip().lower()
        if norm == "new":
            stmt = stmt.where(Order.status.in_([OrderStatus.PENDING, OrderStatus.CONFIRMED]))
        else:
            try:
                stmt = stmt.where(Order.status == OrderStatus(norm))
            except ValueError:
                raise ApiError(422, "INVALID_STATUS_FILTER", f"Invalid status '{status}'.")
    elif not includeCollected:
        stmt = stmt.where(
            Order.status.in_([
                OrderStatus.PENDING,
                OrderStatus.CONFIRMED,
                OrderStatus.ACCEPTED,
                OrderStatus.PREPARING,
                OrderStatus.READY,
            ])
        )

    orders = db.scalars(
        stmt.order_by(Order.pickup_slot.is_(None), Order.pickup_slot.asc(), Order.created_at.asc()).limit(limit)
    ).unique().all()

    serialized = [serialize_order_v1(o, include_customer=True, include_history=False) for o in orders]
    if overdueOnly:
        serialized = [s for s in serialized if s["isOverdue"]]

    today_str = datetime.now(ZoneInfo(get_settings().canteen_timezone)).date().isoformat()
    collected_stmt = select(func.count(Order.id)).where(
        Order.token_date == today_str,
        Order.payment_status == PaymentStatus.PAID,
        Order.status.in_([OrderStatus.COMPLETED, OrderStatus.COLLECTED]),
    )
    if counterId is not None:
        collected_stmt = collected_stmt.where(Order.counter_id == counterId)
    elif assigned:
        collected_stmt = collected_stmt.where(Order.counter_id.in_(assigned))
    collected_today = db.scalar(collected_stmt) or 0

    summary = {
        "newCount": sum(1 for s in serialized if s["rawStatus"] in {"pending", "confirmed"}),
        "acceptedCount": sum(1 for s in serialized if s["rawStatus"] == "accepted"),
        "preparingCount": sum(1 for s in serialized if s["rawStatus"] == "preparing"),
        "readyCount": sum(1 for s in serialized if s["rawStatus"] == "ready"),
        "overdueCount": sum(1 for s in serialized if s["isOverdue"]),
        "collectedToday": int(collected_today),
    }
    return ok(request, {"summary": summary, "items": serialized})

class OrderRejectIn(BaseModel):
    reason: str = Field(min_length=3, max_length=255)

@router.post("/orders/{order_id}/accept")
def accept_staff_order_v1(
    order_id: int,
    request: Request,
    payload: OrderTransitionNoteIn | None = None,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assert_staff_permission(staff, "canAcceptOrders")
    order = _load_staff_order_locked(db, order_id, staff)
    if order.status not in {OrderStatus.PENDING, OrderStatus.CONFIRMED}:
        raise ApiError(409, "INVALID_STATE_TRANSITION", f"Cannot accept an order currently in state '{order.status.value}'.")
    now = datetime.now(timezone.utc)
    prev = order.status.value
    order.status = OrderStatus.ACCEPTED
    order.accepted_at = now
    order.handled_by_staff_id = staff.id
    max_prep = max(((it.product.preparation_minutes if it.product else 10) for it in order.items), default=8)
    eta = estimate_counter_wait_minutes(db, order.counter_id, max_prep)
    order.estimated_ready_at = now + timedelta(minutes=eta["estimatedMinutes"])

    record_order_status_change(db, order, prev, "ACCEPTED", staff.id, (payload.note if payload else None) or "Accepted by kitchen")
    notify_user(
        db,
        order.user_id,
        "order_status",
        "Order accepted by kitchen",
        f"Kitchen accepted order #{order.order_number or order.id} (Token #{order.token_number}). Estimated ready in ~{eta['estimatedMinutes']} min.",
        entity_type="order",
        entity_id=order.id,
    )
    out = serialize_order_v1(order, include_customer=True, include_history=True)
    stage_outbox_event(db, "kitchen.order.updated", out, counter_id=order.counter_id)
    stage_outbox_event(db, "order.updated", serialize_order_v1(order, include_history=True), counter_id=order.counter_id, target_user_id=order.user_id)
    stage_outbox_event(db, "queue.public", {"tokenNumber": order.token_number, "status": "accepted", "counterId": order.counter_id}, counter_id=order.counter_id)
    record_audit(db, "staff.order_accepted", "order", order.id, staff.id, {"counter_id": order.counter_id})
    commit_and_flush_outbox(db)
    return ok(request, {"order": out})

@router.post("/orders/{order_id}/reject")
def reject_staff_order_v1(
    order_id: int,
    payload: OrderRejectIn,
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assert_staff_permission(staff, "canRejectOrders")
    order = _load_staff_order_locked(db, order_id, staff)
    if order.status not in {OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.ACCEPTED}:
        raise ApiError(409, "INVALID_STATE_TRANSITION", f"Cannot reject order once in state '{order.status.value}'.")

    reason_clean = payload.reason.strip()
    _restore_order_inventory_once(db, order, f"Staff rejected order #{order.id}: {reason_clean}", actor_user_id=staff.id)

    prev = order.status.value
    order.handled_by_staff_id = staff.id
    order.exception_reason = reason_clean
    order.cancelled_at = datetime.now(timezone.utc)
    order.status = OrderStatus.REFUND_PENDING
    order.payment_status = PaymentStatus.REFUND_PENDING

    paid_rec = next((p for p in order.payments if p.status == "SUCCESS"), None)
    refund = _initiate_gateway_refund(
        db,
        order,
        paid_rec,
        amount_paise=order.effective_total_paise,
        reason=f"Rejected by kitchen: {reason_clean}",
        actor_user_id=staff.id,
    )
    refund_info = {"refundId": refund.id, "status": refund.status, "amountPaise": refund.amount_paise}

    record_order_status_change(db, order, prev, "REJECTED_REFUND_INITIATED", staff.id, reason_clean)
    notify_user(
        db,
        order.user_id,
        "order_status",
        "Order declined by kitchen — full refund initiated",
        f"Order #{order.order_number or order.id} could not be accepted ({reason_clean}). Your full UPI refund has been initiated.",
        entity_type="order",
        entity_id=order.id,
    )
    out = serialize_order_v1(order, include_customer=True, include_history=True)
    stage_outbox_event(db, "kitchen.order.rejected", out, counter_id=order.counter_id, target_user_id=order.user_id)
    stage_outbox_event(db, "order.cancelled", out, counter_id=order.counter_id, target_user_id=order.user_id)
    record_audit(db, "staff.order_rejected", "order", order.id, staff.id, {"reason": reason_clean, "refund_id": refund.id})
    commit_and_flush_outbox(db)
    return ok(request, {"order": out, "refund": refund_info})

@router.post("/orders/{order_id}/start")
def start_staff_order_v1(
    order_id: int,
    request: Request,
    payload: OrderTransitionNoteIn | None = None,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assert_staff_permission(staff, "canUpdatePrep")
    order = _load_staff_order_locked(db, order_id, staff)
    if order.status not in {OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.ACCEPTED}:
        raise ApiError(409, "INVALID_STATE_TRANSITION", f"Cannot start preparation from state '{order.status.value}'.")
    now = datetime.now(timezone.utc)
    prev = order.status.value
    order.status = OrderStatus.PREPARING
    order.handled_by_staff_id = staff.id
    if order.accepted_at is None:
        order.accepted_at = now
    record_order_status_change(db, order, prev, "PREPARING", staff.id, (payload.note if payload else None) or "Preparation started")
    notify_user(
        db,
        order.user_id,
        "order_status",
        "Preparing your meal",
        f"Your order #{order.order_number or order.id} (Token #{order.token_number}) is now being prepared.",
        entity_type="order",
        entity_id=order.id,
    )
    out = serialize_order_v1(order, include_customer=True, include_history=True)
    stage_outbox_event(db, "kitchen.order.updated", out, counter_id=order.counter_id)
    stage_outbox_event(db, "order.updated", serialize_order_v1(order, include_history=True), counter_id=order.counter_id, target_user_id=order.user_id)
    stage_outbox_event(db, "queue.public", {"tokenNumber": order.token_number, "status": "preparing", "counterId": order.counter_id}, counter_id=order.counter_id)
    record_audit(db, "staff.order_started", "order", order.id, staff.id, {"counter_id": order.counter_id})
    commit_and_flush_outbox(db)
    return ok(request, {"order": out})

@router.post("/orders/{order_id}/ready")
def mark_staff_order_ready_v1(
    order_id: int,
    background: BackgroundTasks,
    request: Request,
    payload: OrderTransitionNoteIn | None = None,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assert_staff_permission(staff, "canUpdatePrep")
    order = _load_staff_order_locked(db, order_id, staff)
    if order.status not in {OrderStatus.CONFIRMED, OrderStatus.ACCEPTED, OrderStatus.PREPARING}:
        raise ApiError(409, "INVALID_STATE_TRANSITION", f"Cannot mark READY from state '{order.status.value}'.")
    now = datetime.now(timezone.utc)
    prev = order.status.value
    order.status = OrderStatus.READY
    order.ready_at = now
    order.estimated_ready_at = now
    order.handled_by_staff_id = staff.id
    record_order_status_change(db, order, prev, "READY", staff.id, (payload.note if payload else None) or "Ready at pickup counter")
    notify_user(
        db,
        order.user_id,
        "order_status",
        "Order ready for pickup!",
        f"Order #{order.order_number or order.id} (Token #{order.token_number}) is ready! Show your pickup QR code at the counter.",
        entity_type="order",
        entity_id=order.id,
    )
    out = serialize_order_v1(order, include_customer=True, include_history=True)
    stage_outbox_event(db, "kitchen.order.updated", out, counter_id=order.counter_id)
    stage_outbox_event(db, "order.ready", serialize_order_v1(order, include_history=True), counter_id=order.counter_id, target_user_id=order.user_id)
    stage_outbox_event(db, "queue.public", {"tokenNumber": order.token_number, "status": "ready", "counterId": order.counter_id}, counter_id=order.counter_id)
    record_audit(db, "staff.order_ready", "order", order.id, staff.id, {"counter_id": order.counter_id})
    commit_and_flush_outbox(db)

    cfg = get_settings()
    if cfg.email_order_ready and order.user and order.user.email:
        background.add_task(
            send_email,
            order.user.email,
            f"Ready for pickup: {cfg.app_name} Token #{order.token_number}",
            (
                f"Hello {order.user.full_name},\n\n"
                f"Your order {order.order_number} (Token #{order.token_number}) is ready for pickup.\n"
                f"Open the app to present your short-lived pickup QR code.\n\n"
                f"{cfg.app_name}\n"
            ),
        )
    return ok(request, {"order": out})

@router.post("/orders/{order_id}/exception")
def report_staff_order_exception_v1(
    order_id: int,
    payload: OrderExceptionIn,
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    order = _load_staff_order_locked(db, order_id, staff)
    if order.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED, OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}:
        raise ApiError(409, "INVALID_STATE_TRANSITION", f"Cannot raise kitchen exception on order in state '{order.status.value}'.")

    reason_clean = payload.reason.strip()
    order.exception_reason = reason_clean
    refund_info = None

    if payload.cancelAndRefund:
        _restore_order_inventory_once(db, order, f"Kitchen exception cancellation #{order.id}: {reason_clean}", actor_user_id=staff.id)
        prev = order.status.value
        order.cancelled_at = datetime.now(timezone.utc)
        order.status = OrderStatus.REFUND_PENDING
        order.payment_status = PaymentStatus.REFUND_PENDING
        paid_rec = next((p for p in order.payments if p.status == "SUCCESS"), None)
        refund = _initiate_gateway_refund(
            db,
            order,
            paid_rec,
            amount_paise=order.effective_total_paise,
            reason=f"Kitchen exception: {reason_clean}",
            actor_user_id=staff.id,
        )
        refund_info = {"refundId": refund.id, "status": refund.status, "amountPaise": refund.amount_paise}
        record_order_status_change(db, order, prev, order.status.value.upper(), staff.id, f"Kitchen exception & refund: {reason_clean}")
        notify_user(
            db,
            order.user_id,
            "order_status",
            "Order cancelled due to kitchen issue — refund initiated",
            f"We could not fulfill order #{order.order_number or order.id} ({reason_clean}). Your full refund has been initiated.",
            entity_type="order",
            entity_id=order.id,
        )
    else:
        record_order_status_change(db, order, order.status.value, order.status.value.upper(), staff.id, f"Exception flagged: {reason_clean}")
        notify_user(
            db,
            order.user_id,
            "order_status",
            "Update on your order",
            f"Kitchen note on order #{order.order_number or order.id}: {reason_clean}",
            entity_type="order",
            entity_id=order.id,
        )

    notify_counter_staff(
        db,
        order.counter_id,
        "kitchen_exception",
        f"Kitchen exception on #{order.token_number or order.id}",
        reason_clean,
        entity_type="order",
        entity_id=order.id,
    )
    out = serialize_order_v1(order, include_customer=True, include_history=True)
    stage_outbox_event(db, "kitchen.order.exception", out, counter_id=order.counter_id, target_user_id=order.user_id)
    record_audit(db, "staff.order_exception", "order", order.id, staff.id, {"reason": reason_clean, "cancel_and_refund": payload.cancelAndRefund})
    commit_and_flush_outbox(db)
    return ok(request, {"order": out, "refund": refund_info})

@router.patch("/menu-items/{item_id}/availability")
def patch_staff_menu_availability_v1(
    item_id: int,
    payload: MenuAvailabilityPatchIn,
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assert_staff_permission(staff, "canManageAvailability")
    product = db.scalar(select(Product).options(joinedload(Product.category)).where(Product.id == item_id, Product.is_archived.is_(False)))
    if product is None:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")
    assert_counter_access(db, staff, product.counter_id)
    if payload.isAvailable and product.stock_quantity <= 0:
        raise ApiError(409, "ZERO_STOCK", "Cannot mark item available when stock quantity is 0. Record a restock first.")
    product.is_available = bool(payload.isAvailable and product.category.is_active)
    record_audit(
        db,
        "staff.menu_availability_changed",
        "product",
        product.id,
        staff.id,
        {"is_available": product.is_available, "reason": payload.reason},
    )
    stage_outbox_event(
        db,
        "inventory.availability_changed",
        {"menuItemId": product.id, "name": product.name, "isAvailable": product.is_available},
        counter_id=product.counter_id,
    )
    commit_and_flush_outbox(db)
    return ok(request, serialize_product_v1(product))

@router.get("/inventory")
def get_staff_inventory_v1(
    request: Request,
    counterId: int | None = Query(default=None, gt=0),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assigned = get_staff_assigned_counter_ids(db, staff)
    if counterId is not None:
        assert_counter_access(db, staff, counterId)

    p_stmt = select(Product).options(joinedload(Product.category), joinedload(Product.recipe_items).joinedload(RecipeItem.ingredient)).where(Product.is_archived.is_(False))
    if counterId is not None:
        p_stmt = p_stmt.where(or_(Product.counter_id == counterId, Product.counter_id.is_(None)))
    elif assigned:
        p_stmt = p_stmt.where(or_(Product.counter_id.in_(assigned), Product.counter_id.is_(None)))
    products = db.scalars(p_stmt.order_by(Product.stock_quantity.asc(), Product.name.asc())).unique().all()

    ingredients = db.scalars(select(Ingredient).where(Ingredient.is_active.is_(True)).order_by(Ingredient.name.asc())).all()
    recent_ing_movements = db.scalars(select(StockMovement).options(joinedload(StockMovement.ingredient)).order_by(StockMovement.created_at.desc()).limit(40)).all()
    recent_prod_movements = db.scalars(select(InventoryMovement).options(joinedload(InventoryMovement.product)).order_by(InventoryMovement.created_at.desc()).limit(40)).all()

    return ok(
        request,
        {
            "menuItems": [
                {
                    **serialize_product_v1(p),
                    "isLowStock": p.stock_quantity <= p.low_stock_threshold,
                    "recipe": [
                        {
                            "ingredientId": ri.ingredient_id,
                            "ingredientName": ri.ingredient.name if ri.ingredient else "",
                            "quantityPerServing": str(ri.quantity_per_serving),
                            "unit": ri.ingredient.unit if ri.ingredient else "",
                        }
                        for ri in p.recipe_items
                    ],
                }
                for p in products
            ],
            "ingredients": [
                {
                    "id": ing.id,
                    "name": ing.name,
                    "unit": ing.unit,
                    "currentQuantity": str(ing.current_quantity),
                    "reorderLevel": str(ing.reorder_level),
                    "costPerUnit": str(ing.cost_per_unit) if ing.cost_per_unit is not None else None,
                    "isLowStock": Decimal(ing.current_quantity) <= Decimal(ing.reorder_level),
                    "updatedAt": as_utc(ing.updated_at),
                }
                for ing in ingredients
            ],
            "recentIngredientMovements": [
                {
                    "id": mv.id,
                    "ingredientId": mv.ingredient_id,
                    "ingredientName": mv.ingredient.name if mv.ingredient else "",
                    "movementType": mv.movement_type,
                    "quantityDelta": str(mv.quantity_delta),
                    "quantityAfter": str(mv.quantity_after),
                    "reason": mv.reason,
                    "createdAt": as_utc(mv.created_at),
                }
                for mv in recent_ing_movements
            ],
            "recentProductMovements": [
                {
                    "id": mv.id,
                    "menuItemId": mv.product_id,
                    "itemName": mv.product.name if mv.product else "",
                    "movementType": mv.movement_type.value,
                    "quantityDelta": mv.quantity_delta,
                    "quantityAfter": mv.quantity_after,
                    "reason": mv.reason,
                    "createdAt": as_utc(mv.created_at),
                }
                for mv in recent_prod_movements
            ],
        },
    )

@router.post("/inventory/movements", status_code=201)
def record_staff_stock_movement_v1(
    payload: StaffStockMovementIn,
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assert_staff_permission(staff, "canManageInventory")
    if bool(payload.ingredientId) == bool(payload.menuItemId):
        raise ApiError(422, "TARGET_REQUIRED", "Specify exactly one of ingredientId or menuItemId.")

    if payload.ingredientId is not None:
        ing = db.scalar(select(Ingredient).where(Ingredient.id == payload.ingredientId, Ingredient.is_active.is_(True)).with_for_update())
        if ing is None:
            raise ApiError(404, "INGREDIENT_NOT_FOUND", "Ingredient not found.")
        before = Decimal(ing.current_quantity)
        qty = Decimal(payload.quantity).quantize(Decimal("0.001"))
        delta = -qty if payload.movementType == "wastage" else qty
        if payload.movementType == "adjustment":

            delta = qty
        after = (before + delta).quantize(Decimal("0.01"))
        if after < 0:
            raise ApiError(409, "INSUFFICIENT_STOCK", f"Cannot deduct {qty} {ing.unit} from {ing.name} ({before} {ing.unit} available).")
        ing.current_quantity = after
        mv = StockMovement(
            ingredient_id=ing.id,
            movement_type=payload.movementType,
            quantity_delta=delta,
            quantity_before=before,
            quantity_after=after,
            reference_type="staff_manual",
            reason=payload.reason.strip(),
            actor_user_id=staff.id,
        )
        db.add(mv)
        record_audit(
            db,
            f"ingredient.{payload.movementType}",
            "ingredient",
            ing.id,
            staff.id,
            {"delta": str(delta), "after": str(after), "reason": payload.reason.strip()},
        )
        commit_and_flush_outbox(db)
        db.refresh(mv)
        return ok(
            request,
            {
                "kind": "ingredient",
                "movementId": mv.id,
                "ingredientId": ing.id,
                "name": ing.name,
                "movementType": mv.movement_type,
                "quantityBefore": str(before),
                "quantityAfter": str(after),
                "unit": ing.unit,
            },
            status_code=201,
        )

    if payload.quantity != payload.quantity.to_integral_value():
        raise ApiError(422, "INTEGER_QUANTITY_REQUIRED", "Menu item stock movements require whole unit quantities.")
    units = int(payload.quantity)
    product = db.scalar(select(Product).options(joinedload(Product.category)).where(Product.id == payload.menuItemId, Product.is_archived.is_(False)).with_for_update())
    if product is None:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")
    assert_counter_access(db, staff, product.counter_id)

    before_units = product.stock_quantity
    delta_units = -units if payload.movementType == "wastage" else units
    mv_enum = {
        "restock": InventoryMovementType.RECEIPT,
        "wastage": InventoryMovementType.WASTE,
        "adjustment": InventoryMovementType.ADJUSTMENT,
    }[payload.movementType]
    if before_units + delta_units < 0:
        raise ApiError(409, "INSUFFICIENT_STOCK", f"Cannot record wastage of {units} units ({before_units} in stock).")

    product.stock_quantity = before_units + delta_units
    product.is_available = bool(product.category.is_active and product.stock_quantity > 0)
    record_product_stock_movement(
        db,
        product,
        delta_units,
        mv_enum,
        payload.reason.strip(),
        actor_user_id=staff.id,
        before=before_units,
    )
    record_audit(
        db,
        f"inventory.{payload.movementType}",
        "product",
        product.id,
        staff.id,
        {"delta": delta_units, "after": product.stock_quantity, "reason": payload.reason.strip()},
    )
    stage_outbox_event(
        db,
        "inventory.updated",
        {"menuItemId": product.id, "stockQuantity": product.stock_quantity, "isAvailable": product.is_available},
        counter_id=product.counter_id,
    )
    commit_and_flush_outbox(db)
    return ok(
        request,
        {
            "kind": "menuItem",
            "menuItemId": product.id,
            "name": product.name,
            "movementType": payload.movementType,
            "quantityBefore": before_units,
            "quantityAfter": product.stock_quantity,
            "isAvailable": product.is_available,
        },
        status_code=201,
    )

@router.get("/inventory/alerts")
def get_staff_inventory_alerts_v1(
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assigned = get_staff_assigned_counter_ids(db, staff)
    p_stmt = select(Product).where(Product.is_archived.is_(False), Product.stock_quantity <= Product.low_stock_threshold)
    if assigned:
        p_stmt = p_stmt.where(or_(Product.counter_id.in_(assigned), Product.counter_id.is_(None)))
    low_products = db.scalars(p_stmt.order_by(Product.stock_quantity.asc())).all()

    low_ingredients = db.scalars(
        select(Ingredient)
        .where(Ingredient.is_active.is_(True), Ingredient.current_quantity <= Ingredient.reorder_level)
        .order_by(Ingredient.name.asc())
    ).all()

    today = date.today()
    soon = today + timedelta(days=7)
    expiring_batches = db.scalars(
        select(ProductBatch)
        .options(joinedload(ProductBatch.product))
        .where(ProductBatch.quantity_remaining > 0, ProductBatch.expiry_date.is_not(None), ProductBatch.expiry_date <= soon)
        .order_by(ProductBatch.expiry_date.asc())
    ).all()

    return ok(
        request,
        {
            "lowStockMenuItems": [
                {
                    "id": p.id,
                    "name": p.name,
                    "counterId": p.counter_id,
                    "stockQuantity": p.stock_quantity,
                    "lowStockThreshold": p.low_stock_threshold,
                }
                for p in low_products
            ],
            "lowStockIngredients": [
                {
                    "id": ing.id,
                    "name": ing.name,
                    "currentQuantity": str(ing.current_quantity),
                    "reorderLevel": str(ing.reorder_level),
                    "unit": ing.unit,
                }
                for ing in low_ingredients
            ],
            "expiringBatches": [
                {
                    "batchId": b.id,
                    "batchCode": b.batch_code,
                    "menuItemId": b.product_id,
                    "itemName": b.product.name if b.product else "",
                    "quantityRemaining": b.quantity_remaining,
                    "expiryDate": b.expiry_date.isoformat() if b.expiry_date else None,
                    "isExpired": bool(b.expiry_date and b.expiry_date < today),
                }
                for b in expiring_batches
            ],
        },
    )

@router.get("/orders/history")
def get_staff_completed_order_history_v1(
    request: Request,
    counterId: int | None = Query(default=None, gt=0),
    serviceDate: date | None = None,
    limit: int = Query(default=100, ge=1, le=300),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assigned = get_staff_assigned_counter_ids(db, staff)
    if counterId is not None:
        assert_counter_access(db, staff, counterId)

    stmt = (
        select(Order)
        .options(joinedload(Order.items), joinedload(Order.user))
        .where(
            Order.status.in_([
                OrderStatus.COMPLETED,
                OrderStatus.COLLECTED,
                OrderStatus.CANCELLED,
                OrderStatus.REFUND_PENDING,
            ])
        )
    )
    if counterId is not None:
        stmt = stmt.where(Order.counter_id == counterId)
    elif assigned:
        stmt = stmt.where(Order.counter_id.in_(assigned))

    if serviceDate is not None:
        stmt = stmt.where(Order.token_date == serviceDate.isoformat())

    rows = db.scalars(stmt.order_by(Order.updated_at.desc(), Order.id.desc()).limit(limit)).unique().all()
    return ok(
        request,
        {
            "count": len(rows),
            "items": [serialize_order_v1(o, include_customer=True) for o in rows],
        },
    )

@router.get("/performance")
def get_staff_daily_performance_v1(
    request: Request,
    counterId: int | None = Query(default=None, gt=0),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    tz = ZoneInfo(cfg.canteen_timezone)
    today_local = datetime.now(tz).date()
    today_str = today_local.isoformat()
    start_today_utc = datetime(today_local.year, today_local.month, today_local.day, tzinfo=tz).astimezone(timezone.utc)

    assigned = get_staff_assigned_counter_ids(db, staff)
    if counterId is not None:
        assert_counter_access(db, staff, counterId)

    o_stmt = select(Order).where(Order.created_at >= start_today_utc - timedelta(hours=4))
    if counterId is not None:
        o_stmt = o_stmt.where(Order.counter_id == counterId)
    elif assigned:
        o_stmt = o_stmt.where(Order.counter_id.in_(assigned))
    orders_today = [
        o for o in db.scalars(o_stmt).all()
        if (o.token_date == today_str) or ((as_utc(o.created_at) or start_today_utc).astimezone(tz).date() == today_local)
    ]

    hist_today = db.scalars(
        select(OrderStatusHistory).where(
            OrderStatusHistory.actor_user_id == staff.id,
            OrderStatusHistory.created_at >= start_today_utc,
        )
    ).all()
    pickups_today = db.scalars(
        select(Pickup).where(
            Pickup.collected_by == staff.id,
            Pickup.collected_at >= start_today_utc,
        )
    ).all()

    my_accepted = sum(1 for h in hist_today if h.new_status.upper() == "ACCEPTED")
    my_ready = sum(1 for h in hist_today if h.new_status.upper() == "READY")
    my_rejected = sum(1 for h in hist_today if "REJECTED" in h.new_status.upper())
    my_collected = len(pickups_today)

    prep_durations: list[float] = []
    my_prep_durations: list[float] = []
    hourly_buckets: dict[int, int] = {h: 0 for h in range(7, 22)}
    paid_cnt = 0
    completed_cnt = 0
    rejected_cnt = 0
    active_cnt = 0
    overdue_cnt = 0
    now_utc = datetime.now(timezone.utc)

    for o in orders_today:
        u_dt = as_utc(o.created_at)
        if u_dt:
            hr = u_dt.astimezone(tz).hour
            if hr in hourly_buckets and o.payment_status in {PaymentStatus.PAID, PaymentStatus.REFUND_PENDING, PaymentStatus.REFUNDED}:
                hourly_buckets[hr] += 1
        if o.payment_status == PaymentStatus.PAID:
            paid_cnt += 1
        if o.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED}:
            completed_cnt += 1
        elif o.status in {OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}:
            rejected_cnt += 1
        elif o.payment_status == PaymentStatus.PAID and o.status in {OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.ACCEPTED, OrderStatus.PREPARING}:
            active_cnt += 1
            if u_dt and ((now_utc - u_dt).total_seconds() // 60) >= cfg.overdue_preparation_minutes:
                overdue_cnt += 1

        if o.ready_at and o.created_at:
            mins = ((as_utc(o.ready_at) - as_utc(o.created_at)).total_seconds() / 60.0)  # type: ignore[operator]
            if 0.2 <= mins <= 180:
                prep_durations.append(mins)
                if o.handled_by_staff_id == staff.id:
                    my_prep_durations.append(mins)

    prep_durations.sort()
    avg_prep = round(sum(prep_durations) / len(prep_durations), 2) if prep_durations else 0.0
    median_prep = round(prep_durations[len(prep_durations) // 2], 2) if prep_durations else 0.0
    on_time_cnt = sum(1 for m in prep_durations if m <= cfg.overdue_preparation_minutes)
    sla_pct = round(on_time_cnt * 100.0 / len(prep_durations), 1) if prep_durations else 100.0
    my_avg_prep = round(sum(my_prep_durations) / len(my_prep_durations), 2) if my_prep_durations else 0.0

    return ok(
        request,
        {
            "date": today_str,
            "staffMember": {"id": staff.id, "fullName": staff.full_name, "permissions": get_effective_staff_permissions(staff)},
            "myStatsToday": {
                "ordersAccepted": my_accepted,
                "ordersMarkedReady": my_ready,
                "ordersCollected": my_collected,
                "ordersRejected": my_rejected,
                "avgPrepMinutes": my_avg_prep,
            },
            "counterStatsToday": {
                "paidOrders": paid_cnt,
                "completedOrders": completed_cnt,
                "rejectedOrCancelledOrders": rejected_cnt,
                "activeQueueOrders": active_cnt,
                "overdueOrders": overdue_cnt,
                "avgPrepMinutes": avg_prep,
                "medianPrepMinutes": median_prep,
                "onTimeSlaPct": sla_pct,
                "hourlyThroughput": [{"hour": f"{h:02d}:00", "orders": hourly_buckets[h]} for h in sorted(hourly_buckets.keys())],
            },
        },
    )
