"""/api/v1 Orders, Quotes, UPI Payments, Webhooks (with late-capture safety), Refunds & QR Pickup."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import Response
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db
from app.emailer import send_email
from app.envelope import ApiError, ok
from app.models import (
    CanteenCounter,
    Cart,
    CartItem,
    CheckoutIdempotency,
    InventoryMovementType,
    Order,
    OrderItem,
    OrderReview,
    OrderStatus,
    Payment,
    PaymentStatus,
    PaymentWebhookEvent,
    Pickup,
    PickupToken,
    Product,
    Refund,
    User,
    UserRole,
    paise_to_rupees,
    rupees_to_paise,
)
from app.modules.common import (
    as_utc,
    assert_staff_permission,
    commit_and_flush_outbox,
    compute_customization_extra_paise,
    consume_recipe_ingredients,
    ensure_default_counter,
    estimate_counter_wait_minutes,
    evaluate_discount_paise,
    format_token_display,
    generate_manual_pickup_code,
    get_app_setting,
    get_tax_rate_bps,
    notify_counter_staff,
    notify_user,
    parse_json_safe,
    record_audit,
    record_order_status_change,
    record_product_stock_movement,
    release_batch_stock_once,
    reserve_batch_stock_fefo,
    restore_recipe_ingredients,
    serialize_order_v1,
)
from app.modules.menu_cart import compute_counter_pickup_slots, is_item_available_in_window
from app.realtime import stage_outbox_event
from app.security import (
    assert_counter_access,
    get_current_user,
    get_staff_assigned_counter_ids,
    hash_opaque_token,
    require_roles,
)

logger = logging.getLogger("ace_canteen.orders_payments")
router = APIRouter(tags=["v1-orders-payments"])


def _canteen_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().canteen_timezone)


def _assert_ordering_open_v1(db: Session, counter_id: int | None) -> CanteenCounter:
    if get_app_setting(db, "ordering_paused", "0") == "1":
        msg = get_app_setting(db, "pause_message", "") or "Ordering is temporarily paused by canteen management."
        raise ApiError(409, "ORDERING_PAUSED", msg)
    counter = db.get(CanteenCounter, counter_id) if counter_id else ensure_default_counter(db)
    if counter is None or not counter.is_active:
        raise ApiError(409, "COUNTER_CLOSED", "The selected pickup counter is not currently accepting orders.")
    cfg = get_settings()
    if cfg.canteen_open_time and cfg.canteen_close_time:
        now_local = datetime.now(_canteen_tz())
        open_days = {int(x) for x in cfg.canteen_open_days.split(",") if x.strip()}
        if now_local.weekday() not in open_days:
            raise ApiError(409, "CANTEEN_CLOSED", "Canteen is closed today.")
        hm = now_local.strftime("%H:%M")
        if hm < cfg.canteen_open_time or hm >= cfg.canteen_close_time:
            raise ApiError(409, "CANTEEN_CLOSED", f"Ordering is open between {cfg.canteen_open_time} and {cfg.canteen_close_time}.")
    return counter


def _resolve_pickup_slot_v1(db: Session, counter_id: int, raw_slot: str | None) -> tuple[datetime | None, datetime | None]:
    if not raw_slot:
        return None, None
    try:
        requested = datetime.fromisoformat(raw_slot)
        if requested.tzinfo is None:
            raise ValueError
        requested_utc = requested.astimezone(timezone.utc)
    except ValueError:
        raise ApiError(422, "INVALID_PICKUP_SLOT", "Pickup slot must be a valid ISO-8601 timestamp with timezone.")

    slot_info = compute_counter_pickup_slots(db, counter_id)
    slots = slot_info.get("slots", [])
    if not slots:
        # When operating hours are open-ended (no fixed slot schedule), accept future slots within today.
        if requested_utc < datetime.now(timezone.utc) - timedelta(minutes=2):
            raise ApiError(409, "SLOT_UNAVAILABLE", "Selected pickup time is in the past.")
        slot_end = requested_utc + timedelta(minutes=get_settings().pickup_slot_minutes)
        return requested_utc, slot_end

    matched = next((s for s in slots if as_utc(datetime.fromisoformat(s["startUtc"])) == requested_utc), None)
    if matched is None:
        raise ApiError(409, "SLOT_UNAVAILABLE", "Selected pickup slot is no longer available.")
    if matched["isFull"] or matched["remaining"] <= 0:
        raise ApiError(409, "SLOT_FULL", "Selected pickup slot is full. Choose another slot.")
    return requested_utc, datetime.fromisoformat(matched["end"]).astimezone(timezone.utc)


def _restore_order_inventory_once(db: Session, order: Order, reason: str, actor_user_id: int | None = None) -> None:
    """Idempotently restore product stock, FEFO batch allocations and recipe ingredients for an unfulfilled order."""
    already_released = db.scalar(
        select( func.count() )
        .select_from(Order.__table__.metadata.tables["inventory_movements"])
        .where(
            Order.__table__.metadata.tables["inventory_movements"].c.order_id == order.id,
            Order.__table__.metadata.tables["inventory_movements"].c.movement_type == InventoryMovementType.RELEASE.value,
        )
    )
    if already_released and int(already_released) > 0:
        return
    release_batch_stock_once(db, order.id)
    restore_recipe_ingredients(db, order.id, actor_user_id=actor_user_id)
    for line in order.items:
        product = db.scalar(select(Product).options(joinedload(Product.category)).where(Product.id == line.product_id).with_for_update())
        if product is not None:
            before_qty = product.stock_quantity
            product.stock_quantity += line.quantity
            record_product_stock_movement(
                db,
                product,
                line.quantity,
                InventoryMovementType.RELEASE,
                reason,
                actor_user_id=actor_user_id,
                order_id=order.id,
                before=before_qty,
            )
            if product.category and product.category.is_active and not product.is_archived:
                product.is_available = True


def release_expired_reservations_v1(db: Session) -> int:
    """Expire unpaid reservations older than CHECKOUT_RESERVATION_MINUTES and restore stock."""
    cfg = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cfg.checkout_reservation_minutes)
    stale_orders = db.scalars(
        select(Order)
        .options(joinedload(Order.items), joinedload(Order.payments))
        .where(
            Order.payment_status == PaymentStatus.PENDING,
            Order.status == OrderStatus.PENDING,
        )
    ).unique().all()
    expired_count = 0
    now = datetime.now(timezone.utc)
    for order in stale_orders:
        created = as_utc(order.created_at)
        if created is None or created >= cutoff:
            continue
        _restore_order_inventory_once(db, order, f"Expired unpaid reservation for order #{order.id}")
        prev = order.status.value
        order.status = OrderStatus.CANCELLED
        order.payment_status = PaymentStatus.EXPIRED
        order.cancelled_at = now
        for pay in order.payments:
            if pay.status == "PENDING":
                pay.status = "EXPIRED"
        record_order_status_change(db, order, prev, "CANCELLED", None, "Unpaid reservation expired")
        record_audit(db, "order.reservation_expired", "order", order.id, None, {"stock_released": True})
        expired_count += 1
    if expired_count > 0:
        db.commit()
    return expired_count


def _initiate_gateway_refund(
    db: Session,
    order: Order,
    payment_record: Payment | None,
    *,
    amount_paise: int,
    reason: str,
    actor_user_id: int | None = None,
) -> Refund:
    """Call Razorpay refund API when credentials exist, persisting an auditable Refund row."""
    cfg = get_settings()
    provider_payment_id = (payment_record.provider_payment_id if payment_record else None) or order.gateway_payment_id
    refund = Refund(
        order_id=order.id,
        payment_record_id=payment_record.id if payment_record else None,
        payment_id=provider_payment_id,
        amount=paise_to_rupees(amount_paise),
        amount_paise=amount_paise,
        status="PENDING",
        reason=reason[:255],
        actor_user_id=actor_user_id,
    )
    db.add(refund)
    db.flush()

    if provider_payment_id and cfg.razorpay_key_id and cfg.razorpay_key_secret:
        try:
            resp = httpx.post(
                f"https://api.razorpay.com/v1/payments/{provider_payment_id}/refund",
                auth=(cfg.razorpay_key_id, cfg.razorpay_key_secret),
                json={
                    "amount": amount_paise,
                    "notes": {"order_id": str(order.id), "reason": reason[:120]},
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            gw_data = resp.json()
            refund.provider_refund_id = gw_data.get("id")
            gw_status = str(gw_data.get("status", "pending")).lower()
            refund.status = "PROCESSED" if gw_status == "processed" else "PENDING"
        except Exception as exc:
            logger.warning("Gateway refund request failed for order %s: %s", order.id, type(exc).__name__)
            refund.status = "PENDING_MANUAL_REVIEW"
    else:
        refund.status = "PENDING_MANUAL_REVIEW"

    db.flush()

    # Synchronize payment & order refund states
    total_order_paise = order.effective_total_paise
    processed_paise = sum(
        int(r.amount_paise or rupees_to_paise(r.amount))
        for r in db.scalars(select(Refund).where(Refund.order_id == order.id, Refund.status == "PROCESSED")).all()
    )
    if refund.status == "PROCESSED":
        if processed_paise >= total_order_paise:
            order.payment_status = PaymentStatus.REFUNDED
            if order.status == OrderStatus.REFUND_PENDING:
                order.status = OrderStatus.CANCELLED
            if payment_record is not None:
                payment_record.status = "REFUNDED"
        else:
            order.payment_status = PaymentStatus.PARTIALLY_REFUNDED
            if payment_record is not None:
                payment_record.status = "PARTIALLY_REFUNDED"
    else:
        order.payment_status = PaymentStatus.REFUND_PENDING
        if payment_record is not None:
            payment_record.status = "REFUND_PENDING"

    record_audit(
        db,
        "payment.refund_initiated",
        "order",
        order.id,
        actor_user_id,
        {"refund_id": refund.id, "amount_paise": amount_paise, "status": refund.status, "provider_refund_id": refund.provider_refund_id},
    )
    return refund


def handle_late_captured_payment(
    db: Session,
    order: Order,
    provider_order_id: str,
    provider_payment_id: str,
    amount_paise: int,
    currency: str,
) -> Refund:
    """Safety handler when a UPI payment is captured after reservation expiry/cancellation."""
    pay_row = db.scalar(select(Payment).where(Payment.provider_order_id == provider_order_id))
    existing_refund = db.scalar(select(Refund).where(Refund.order_id == order.id, Refund.payment_id == provider_payment_id))
    if existing_refund is not None:
        if pay_row is not None:
            pay_row.late_capture_flagged = True
        return existing_refund
    if pay_row is None:
        pay_row = Payment(
            order_id=order.id,
            provider="razorpay",
            provider_order_id=provider_order_id,
            provider_payment_id=provider_payment_id,
            amount_paise=amount_paise,
            currency=currency,
            status="REFUND_PENDING",
            late_capture_flagged=True,
        )
        db.add(pay_row)
        db.flush()
    else:
        pay_row.provider_payment_id = provider_payment_id
        pay_row.late_capture_flagged = True
        pay_row.status = "REFUND_PENDING"

    order.gateway_payment_id = provider_payment_id
    order.status = OrderStatus.REFUND_PENDING
    order.payment_status = PaymentStatus.REFUND_PENDING
    record_order_status_change(
        db,
        order,
        "CANCELLED",
        "REFUND_PENDING",
        None,
        "Late payment capture arrived after checkout expiry; automatic refund initiated",
    )
    refund = _initiate_gateway_refund(
        db,
        order,
        pay_row,
        amount_paise=amount_paise,
        reason="Automatic refund: payment captured after order reservation expired",
        actor_user_id=None,
    )
    notify_user(
        db,
        order.user_id,
        "payment",
        "Late payment detected — refund initiated",
        f"Your payment for expired order #{order.order_number or order.id} arrived after the reservation window closed. A refund of ₹{paise_to_rupees(amount_paise)} has been initiated.",
        entity_type="order",
        entity_id=order.id,
    )
    notify_counter_staff(
        db,
        order.counter_id,
        "payment_alert",
        "Late payment capture flagged",
        f"Order #{order.id} received late payment {provider_payment_id} after expiry. Refund status: {refund.status}.",
        entity_type="order",
        entity_id=order.id,
    )
    record_audit(
        db,
        "payment.late_capture_flagged",
        "order",
        order.id,
        None,
        {"provider_payment_id": provider_payment_id, "amount_paise": amount_paise, "refund_status": refund.status},
    )
    return refund


# --- Request Schemas ---
class OrderLineInputV1(BaseModel):
    menuItemId: int = Field(gt=0)
    quantity: int = Field(ge=1, le=50)
    customizations: dict[str, Any] = Field(default_factory=dict)


class OrderQuoteOrCreateIn(BaseModel):
    items: list[OrderLineInputV1] = Field(default_factory=list, max_length=50)
    fromCart: bool = False
    counterId: int | None = Field(default=None, gt=0)
    pickupSlot: str | None = Field(default=None, max_length=60)
    couponCode: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=500)
    idempotencyKey: str | None = Field(default=None, max_length=100)


class CancelOrderIn(BaseModel):
    reason: str | None = Field(default="Cancelled by user", max_length=255)


class UpiIntentIn(BaseModel):
    orderId: int = Field(gt=0)
    idempotencyKey: str | None = Field(default=None, max_length=100)


class VerifyPaymentV1In(BaseModel):
    razorpayOrderId: str | None = Field(default=None, max_length=100)
    razorpayPaymentId: str | None = Field(default=None, max_length=100)
    razorpaySignature: str | None = Field(default=None, max_length=200)
    razorpay_order_id: str | None = Field(default=None, max_length=100)
    razorpay_payment_id: str | None = Field(default=None, max_length=100)
    razorpay_signature: str | None = Field(default=None, max_length=200)


class RefundRequestIn(BaseModel):
    amountPaise: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=3, max_length=255)


class VerifyPickupIn(BaseModel):
    pickupToken: str | None = Field(default=None, max_length=400)
    orderId: int | None = Field(default=None, gt=0)
    manualCode: str | None = Field(default=None, max_length=20)
    tokenNumber: int | None = Field(default=None, gt=0)
    tokenString: str | None = Field(default=None, max_length=60)
    itemIds: list[int] | None = None


class InspectPickupIn(BaseModel):
    pickupToken: str | None = Field(default=None, max_length=400)
    orderId: int | None = Field(default=None, gt=0)
    manualCode: str | None = Field(default=None, max_length=20)
    tokenNumber: int | None = Field(default=None, gt=0)
    tokenString: str | None = Field(default=None, max_length=60)


class ReviewSubmitV1In(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str | None = Field(default=None, max_length=500)


def _extract_requested_lines(db: Session, user: User, payload: OrderQuoteOrCreateIn) -> tuple[list[OrderLineInputV1], int | None, str | None, str | None, str | None, Cart | None]:
    cart: Cart | None = None
    if payload.fromCart or not payload.items:
        cart = db.scalar(select(Cart).options(joinedload(Cart.items)).where(Cart.user_id == user.id))
        if cart is None or not cart.items:
            raise ApiError(422, "EMPTY_CART", "Your cart is empty. Add items before placing an order.")
        lines = [
            OrderLineInputV1(
                menuItemId=ci.product_id,
                quantity=ci.quantity,
                customizations=parse_json_safe(ci.customizations_json, {}),
            )
            for ci in cart.items
        ]
        counter_id = payload.counterId or cart.counter_id
        coupon = payload.couponCode if payload.couponCode is not None else cart.coupon_code
        slot = payload.pickupSlot if payload.pickupSlot is not None else (as_utc(cart.pickup_slot).isoformat() if cart.pickup_slot else None)
        notes = payload.notes if payload.notes is not None else cart.notes
        return lines, counter_id, coupon, slot, notes, cart
    return payload.items, payload.counterId, payload.couponCode, payload.pickupSlot, payload.notes, None


@router.post("/orders/quote")
def quote_order_v1(
    payload: OrderQuoteOrCreateIn,
    request: Request,
    user: User = Depends(require_roles("student", "admin")),
    db: Session = Depends(get_db),
):
    lines_in, counter_id, coupon_code, slot_raw, _, _ = _extract_requested_lines(db, user, payload)
    counter = _assert_ordering_open_v1(db, counter_id)
    slot_start, slot_end = _resolve_pickup_slot_v1(db, counter.id, slot_raw)

    product_ids = list({ln.menuItemId for ln in lines_in})
    products = db.scalars(select(Product).options(joinedload(Product.category)).where(Product.id.in_(product_ids), Product.is_archived.is_(False))).all()
    by_id = {p.id: p for p in products}
    if len(by_id) != len(product_ids):
        raise ApiError(404, "ITEM_NOT_FOUND", "One or more menu items were not found.")

    agg_qty: dict[int, int] = {}
    for ln in lines_in:
        agg_qty[ln.menuItemId] = agg_qty.get(ln.menuItemId, 0) + ln.quantity

    now_local = datetime.now(_canteen_tz())
    for pid, total_q in agg_qty.items():
        p = by_id[pid]
        if not p.category.is_active:
            raise ApiError(409, "ITEM_UNAVAILABLE", f"{p.name} is currently unavailable.")
        if p.counter_id is not None and p.counter_id != counter.id:
            raise ApiError(409, "COUNTER_MISMATCH", f"{p.name} is served at a different counter.")
        in_win, win_qty = is_item_available_in_window(db, p, now_local)
        if not in_win or (win_qty or 0) < total_q:
            raise ApiError(409, "ITEM_UNAVAILABLE", f"{p.name} is unavailable or has insufficient stock ({win_qty or 0} left).")

    quote_lines = []
    subtotal_paise = 0
    max_prep = 6
    for ln in lines_in:
        p = by_id[ln.menuItemId]
        extra_paise, cleaned_cust = compute_customization_extra_paise(p, ln.customizations)
        unit_p = p.effective_price_paise + extra_paise
        line_p = unit_p * ln.quantity
        subtotal_paise += line_p
        max_prep = max(max_prep, p.preparation_minutes or 10)
        quote_lines.append({
            "menuItemId": p.id,
            "itemName": p.name,
            "quantity": ln.quantity,
            "customizations": cleaned_cust,
            "unitPricePaise": unit_p,
            "lineTotalPaise": line_p,
            "stockAvailable": p.stock_quantity,
        })

    discount_paise, offer = evaluate_discount_paise(db, coupon_code, subtotal_paise)
    taxable_paise = max(0, subtotal_paise - discount_paise)
    tax_bps = get_tax_rate_bps(db)
    tax_paise = (taxable_paise * tax_bps + 5000) // 10000
    total_paise = taxable_paise + tax_paise
    eta = estimate_counter_wait_minutes(db, counter.id, max_prep)

    return ok(
        request,
        {
            "counterId": counter.id,
            "counterName": counter.name,
            "pickupSlotStart": slot_start,
            "pickupSlotEnd": slot_end,
            "items": quote_lines,
            "subtotalPaise": subtotal_paise,
            "discountPaise": discount_paise,
            "couponCode": offer.code if offer else None,
            "taxRateBps": tax_bps,
            "taxPaise": tax_paise,
            "totalPaise": total_paise,
            "currency": get_settings().payment_currency,
            "estimatedReadyMinutes": eta["estimatedMinutes"],
            "stockReserved": False,
        },
    )


_SQLITE_CHECKOUT_LOCK = threading.Lock()


@router.post("/orders", status_code=201)
def create_order_v1(
    payload: OrderQuoteOrCreateIn,
    request: Request,
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_roles("student", "admin")),
    db: Session = Depends(get_db),
):
    dialect_name = db.bind.dialect.name if db.bind is not None else "sqlite"
    lock_ctx = _SQLITE_CHECKOUT_LOCK if dialect_name == "sqlite" else threading.Lock()
    with lock_ctx:
        if dialect_name == "sqlite":
            db.expire_all()
        return _execute_create_order_v1(payload, request, idempotency_header, user, db, dialect_name)


def _execute_create_order_v1(
    payload: OrderQuoteOrCreateIn,
    request: Request,
    idempotency_header: str | None,
    user: User,
    db: Session,
    dialect_name: str,
):
    release_expired_reservations_v1(db)
    lines_in, counter_id, coupon_code, slot_raw, notes, cart = _extract_requested_lines(db, user, payload)
    idem_key = (idempotency_header or payload.idempotencyKey or "").strip()[:100] or None

    canonical = json.dumps(
        {
            "items": [{"id": ln.menuItemId, "q": ln.quantity, "c": ln.customizations} for ln in lines_in],
            "counter": counter_id,
            "coupon": (coupon_code or "").strip().upper(),
            "slot": slot_raw,
            "notes": (notes or "").strip(),
        },
        sort_keys=True,
        default=str,
    )
    req_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    idem_record: CheckoutIdempotency | None = None
    if idem_key:
        idem_record = db.scalar(
            select(CheckoutIdempotency).where(
                CheckoutIdempotency.user_id == user.id,
                CheckoutIdempotency.idempotency_key == idem_key,
            )
        )
        if idem_record is not None:
            if idem_record.request_hash != req_hash:
                raise ApiError(409, "IDEMPOTENCY_KEY_CONFLICT", "Idempotency key was already used with a different order payload.")
            if idem_record.state == "completed" and idem_record.response_json:
                return ok(request, json.loads(idem_record.response_json), status_code=201, headers={"Idempotency-Replayed": "true"})
            if idem_record.state == "processing":
                raise ApiError(409, "ORDER_IN_PROGRESS", "This order request is already being processed.")
        else:
            idem_record = CheckoutIdempotency(
                user_id=user.id,
                idempotency_key=idem_key,
                request_hash=req_hash,
                state="processing",
            )
            db.add(idem_record)
            db.commit()
            db.refresh(idem_record)

    try:
        counter = _assert_ordering_open_v1(db, counter_id)
        slot_start, slot_end = _resolve_pickup_slot_v1(db, counter.id, slot_raw)

        product_ids = sorted({ln.menuItemId for ln in lines_in})
        products = db.scalars(
            select(Product)
            .options(joinedload(Product.category))
            .where(Product.id.in_(product_ids), Product.is_archived.is_(False))
            .with_for_update()
        ).all()
        by_id = {p.id: p for p in products}
        if len(by_id) != len(product_ids):
            raise ApiError(404, "ITEM_NOT_FOUND", "One or more menu items no longer exist.")

        agg_qty: dict[int, int] = {}
        for ln in lines_in:
            agg_qty[ln.menuItemId] = agg_qty.get(ln.menuItemId, 0) + ln.quantity

        now_local = datetime.now(_canteen_tz())
        for pid, total_q in agg_qty.items():
            p = by_id[pid]
            if not p.category.is_active:
                raise ApiError(409, "ITEM_UNAVAILABLE", f"{p.name} is currently unavailable.")
            if p.counter_id is not None and p.counter_id != counter.id:
                raise ApiError(409, "COUNTER_MISMATCH", f"{p.name} is served at a different counter.")
            in_win, win_qty = is_item_available_in_window(db, p, now_local)
            if not in_win or (win_qty or 0) < total_q or p.stock_quantity < total_q:
                raise ApiError(409, "ITEM_UNAVAILABLE", f"{p.name} is unavailable or has insufficient stock.")

        resolved_lines: list[tuple[Product, int, int, int, dict[str, Any]]] = []
        subtotal_paise = 0
        max_prep = 6
        for ln in lines_in:
            p = by_id[ln.menuItemId]
            extra_paise, cleaned_cust = compute_customization_extra_paise(p, ln.customizations)
            unit_p = p.effective_price_paise + extra_paise
            line_p = unit_p * ln.quantity
            subtotal_paise += line_p
            max_prep = max(max_prep, p.preparation_minutes or 10)
            resolved_lines.append((p, ln.quantity, unit_p, line_p, cleaned_cust))

        discount_paise, offer = evaluate_discount_paise(db, coupon_code, subtotal_paise, lock_for_update=True)
        if offer is not None:
            offer.usage_count += 1

        taxable_paise = max(0, subtotal_paise - discount_paise)
        tax_bps = get_tax_rate_bps(db)
        tax_paise = (taxable_paise * tax_bps + 5000) // 10000
        total_paise = taxable_paise + tax_paise

        today_str = now_local.date().isoformat()
        if dialect_name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"campusbite_token:{today_str}"})
        last_token = db.scalar(select(func.max(Order.token_number)).where(Order.token_date == today_str)) or 0
        next_token = int(last_token) + 1
        eta = estimate_counter_wait_minutes(db, counter.id, max_prep)
        now_utc = datetime.now(timezone.utc)
        est_ready = max(slot_start, now_utc + timedelta(minutes=eta["estimatedMinutes"])) if slot_start else (now_utc + timedelta(minutes=eta["estimatedMinutes"]))

        order = Order(
            user_id=user.id,
            counter_id=counter.id,
            token_number=next_token,
            token_date=today_str,
            status=OrderStatus.PENDING,
            payment_status=PaymentStatus.PENDING,
            subtotal=paise_to_rupees(subtotal_paise),
            discount_amount=paise_to_rupees(discount_paise),
            tax_amount=paise_to_rupees(tax_paise),
            final_total=paise_to_rupees(total_paise),
            subtotal_paise=subtotal_paise,
            discount_paise=discount_paise,
            tax_paise=tax_paise,
            total_paise=total_paise,
            coupon_code=offer.code if offer else None,
            manual_pickup_code=generate_manual_pickup_code(),
            notes=(notes or "").strip()[:500] or None,
            idempotency_key=idem_key,
            pickup_slot=slot_start,
            pickup_slot_end=slot_end,
            estimated_ready_at=est_ready,
        )
        db.add(order)
        db.flush()
        order.order_number = f"ACE-{today_str.replace('-', '')}-{order.id:05d}"

        # Deduct stock, FEFO batches and recipe ingredients atomically
        for pid, total_q in agg_qty.items():
            p = by_id[pid]
            reserve_batch_stock_fefo(db, p.id, total_q, order.id)
            consume_recipe_ingredients(db, p.id, total_q, order.id, actor_user_id=user.id)
            before_qty = p.stock_quantity
            p.stock_quantity -= total_q
            record_product_stock_movement(
                db,
                p,
                -total_q,
                InventoryMovementType.RESERVATION,
                f"Reserved for order {order.order_number}",
                actor_user_id=user.id,
                order_id=order.id,
                before=before_qty,
            )
            if p.stock_quantity == 0:
                p.is_available = False

        for p, qty, unit_p, line_p, cleaned_cust in resolved_lines:
            order.items.append(
                OrderItem(
                    product_id=p.id,
                    product_name=p.name,
                    unit_price=paise_to_rupees(unit_p),
                    unit_price_paise=unit_p,
                    quantity=qty,
                    customizations_json=json.dumps(cleaned_cust, separators=(",", ":")),
                    line_total=paise_to_rupees(line_p),
                    line_total_paise=line_p,
                )
            )

        record_order_status_change(db, order, "NONE", "PENDING_PAYMENT", user.id, "Order placed; awaiting UPI payment")
        record_audit(
            db,
            "order.created_v1",
            "order",
            order.id,
            user.id,
            {"order_number": order.order_number, "total_paise": total_paise, "counter_id": counter.id},
        )

        if cart is not None and payload.fromCart:
            for ci in list(cart.items):
                db.delete(ci)
            cart.coupon_code = None
            cart.pickup_slot = None
            cart.notes = None

        db.commit()
        db.refresh(order)

        response_data = {"order": serialize_order_v1(order, include_history=True)}
        if idem_record is not None:
            idem_record.state = "completed"
            idem_record.response_json = json.dumps(response_data, default=str)
            db.commit()
        return ok(request, response_data, status_code=201)
    except Exception:
        db.rollback()
        if idem_record is not None and idem_record.id:
            try:
                stale = db.get(CheckoutIdempotency, idem_record.id)
                if stale and stale.state == "processing":
                    db.delete(stale)
                    db.commit()
            except Exception:
                db.rollback()
        raise


@router.get("/orders")
def list_orders_v1(
    request: Request,
    status: str | None = Query(default=None, max_length=30),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Order).options(joinedload(Order.items), joinedload(Order.user)).order_by(Order.created_at.desc())
    if user.role == UserRole.STUDENT:
        stmt = stmt.where(Order.user_id == user.id)
    elif user.role == UserRole.STAFF:
        assigned = get_staff_assigned_counter_ids(db, user)
        if assigned:
            stmt = stmt.where(Order.counter_id.in_(assigned))
    if status:
        norm = status.strip().lower()
        if norm == "pending_payment":
            norm = "pending"
        try:
            stmt = stmt.where(Order.status == OrderStatus(norm))
        except ValueError:
            raise ApiError(422, "INVALID_STATUS_FILTER", f"Unknown status filter '{status}'.")
    rows = db.scalars(stmt.offset(offset).limit(limit)).unique().all()
    include_cust = user.role in {UserRole.STAFF, UserRole.ADMIN}
    return ok(
        request,
        {
            "items": [serialize_order_v1(o, include_customer=include_cust) for o in rows],
            "limit": limit,
            "offset": offset,
        },
    )


def _load_authorized_order(db: Session, order_id: int, user: User) -> Order:
    order = db.scalar(
        select(Order)
        .options(joinedload(Order.items), joinedload(Order.user), joinedload(Order.status_history), joinedload(Order.payments))
        .where(Order.id == order_id)
    )
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")
    if user.role == UserRole.STUDENT:
        if order.user_id != user.id:
            raise ApiError(403, "FORBIDDEN", "You can only access your own orders.")
    elif user.role == UserRole.STAFF:
        assert_counter_access(db, user, order.counter_id)
    return order


@router.get("/orders/{order_id}")
def get_order_v1(
    order_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _load_authorized_order(db, order_id, user)
    data = serialize_order_v1(order, include_customer=(user.role != UserRole.STUDENT), include_history=True)
    data["payments"] = [
        {
            "id": p.id,
            "provider": p.provider,
            "providerOrderId": p.provider_order_id,
            "providerPaymentId": p.provider_payment_id,
            "amountPaise": p.amount_paise,
            "currency": p.currency,
            "status": p.status,
            "lateCaptureFlagged": p.late_capture_flagged,
            "createdAt": as_utc(p.created_at),
        }
        for p in sorted(order.payments, key=lambda x: x.id)
    ]
    refunds = db.scalars(select(Refund).where(Refund.order_id == order.id).order_by(Refund.created_at.asc())).all()
    data["refunds"] = [
        {
            "id": r.id,
            "amountPaise": r.amount_paise or rupees_to_paise(r.amount),
            "status": r.status,
            "reason": r.reason,
            "providerRefundId": r.provider_refund_id,
            "createdAt": as_utc(r.created_at),
        }
        for r in refunds
    ]
    pickup = db.scalar(select(Pickup).where(Pickup.order_id == order.id))
    data["pickup"] = (
        {"collectedAt": as_utc(pickup.collected_at), "collectedByUserId": pickup.collected_by}
        if pickup is not None
        else None
    )
    return ok(request, data)


@router.get("/orders/{order_id}/status")
def get_order_status_v1(
    order_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _load_authorized_order(db, order_id, user)
    serialized = serialize_order_v1(order, include_history=True)
    active_statuses = [OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.ACCEPTED, OrderStatus.PREPARING]
    queue_pos = None
    wait_mins = 0
    if order.payment_status == PaymentStatus.PAID and order.status in active_statuses:
        stmt = select(func.count(Order.id)).where(
            Order.token_date == order.token_date,
            Order.payment_status == PaymentStatus.PAID,
            Order.status.in_(active_statuses),
            Order.created_at < order.created_at,
        )
        if order.counter_id is not None:
            stmt = stmt.where(or_(Order.counter_id == order.counter_id, Order.counter_id.is_(None)))
        ahead = db.scalar(stmt) or 0
        queue_pos = int(ahead) + 1
        max_prep = max(( (it.product.preparation_minutes if it.product else 10) for it in order.items ), default=8)
        eta = estimate_counter_wait_minutes(db, order.counter_id, max_prep)
        wait_mins = eta["estimatedMinutes"]
    return ok(
        request,
        {
            "orderId": order.id,
            "orderNumber": serialized["orderNumber"],
            "tokenNumber": order.token_number,
            "status": serialized["status"],
            "paymentStatus": serialized["paymentStatus"],
            "queuePosition": queue_pos,
            "estimatedWaitMinutes": wait_mins,
            "estimatedReadyAt": serialized["estimatedReadyAt"],
            "canCancel": serialized["canCancel"],
            "isOverdue": serialized["isOverdue"],
            "collectedAt": serialized["collectedAt"],
            "statusHistory": serialized["statusHistory"],
        },
    )


@router.post("/orders/{order_id}/cancel")
def cancel_order_v1(
    order_id: int,
    payload: CancelOrderIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = db.scalar(
        select(Order)
        .options(joinedload(Order.items), joinedload(Order.payments), joinedload(Order.status_history))
        .where(Order.id == order_id)
        .with_for_update()
    )
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")
    if user.role == UserRole.STUDENT:
        if order.user_id != user.id:
            raise ApiError(403, "FORBIDDEN", "You can only cancel your own orders.")
        if order.status not in {OrderStatus.PENDING, OrderStatus.CONFIRMED} or order.collected_at is not None:
            raise ApiError(409, "CANCELLATION_NOT_ALLOWED", "Order preparation has already started and can no longer be cancelled by student.")
    else:
        assert_counter_access(db, user, order.counter_id)
        if order.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED, OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}:
            raise ApiError(409, "CANCELLATION_NOT_ALLOWED", f"Order in state '{order.status.value}' cannot be cancelled.")

    now = datetime.now(timezone.utc)
    reason_clean = (payload.reason or "Cancelled").strip()[:255]
    _restore_order_inventory_once(db, order, f"Cancelled order #{order.id}: {reason_clean}", actor_user_id=user.id)

    prev_status = order.status.value
    order.cancelled_at = now

    refund_info = None
    if order.payment_status == PaymentStatus.PAID:
        order.status = OrderStatus.REFUND_PENDING
        order.payment_status = PaymentStatus.REFUND_PENDING
        paid_rec = next((p for p in order.payments if p.status == "SUCCESS"), None)
        refund = _initiate_gateway_refund(
            db,
            order,
            paid_rec,
            amount_paise=order.effective_total_paise,
            reason=reason_clean,
            actor_user_id=user.id,
        )
        refund_info = {"refundId": refund.id, "status": refund.status, "amountPaise": refund.amount_paise}
        record_order_status_change(db, order, prev_status, order.status.value.upper(), user.id, f"Cancelled with refund: {reason_clean}")
    else:
        order.status = OrderStatus.CANCELLED
        for p in order.payments:
            if p.status == "PENDING":
                p.status = "CANCELLED"
        record_order_status_change(db, order, prev_status, "CANCELLED", user.id, reason_clean)

    notify_user(
        db,
        order.user_id,
        "order_status",
        "Order cancelled",
        f"Order #{order.order_number or order.id} has been cancelled." + (" Refund has been initiated." if refund_info else ""),
        entity_type="order",
        entity_id=order.id,
    )
    stage_outbox_event(
        db,
        "order.cancelled",
        {"orderId": order.id, "status": order.status.value, "counterId": order.counter_id},
        counter_id=order.counter_id,
        target_user_id=order.user_id,
    )
    record_audit(db, "order.cancelled_v1", "order", order.id, user.id, {"reason": reason_clean, "refund": refund_info})
    commit_and_flush_outbox(db)
    db.refresh(order)
    return ok(request, {"order": serialize_order_v1(order, include_history=True), "refund": refund_info})


# --- UPI Payment Intent, Verification, Lookup, Webhooks & Refunds ---
@router.post("/payments/upi/intent", status_code=201)
def create_upi_payment_intent_v1(
    payload: UpiIntentIn,
    request: Request,
    idempotency_header: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(require_roles("student", "admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    if not cfg.razorpay_key_id or not cfg.razorpay_key_secret:
        raise ApiError(503, "PAYMENT_GATEWAY_NOT_CONFIGURED", "UPI payment gateway credentials are not configured.")

    release_expired_reservations_v1(db)
    order = db.scalar(select(Order).options(joinedload(Order.payments), joinedload(Order.items)).where(Order.id == payload.orderId).with_for_update())
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")
    if user.role == UserRole.STUDENT and order.user_id != user.id:
        raise ApiError(403, "FORBIDDEN", "You can only pay for your own orders.")
    if order.payment_status == PaymentStatus.PAID:
        raise ApiError(409, "ORDER_ALREADY_PAID", "This order has already been paid.")
    if order.status != OrderStatus.PENDING or order.payment_status in {PaymentStatus.EXPIRED, PaymentStatus.REFUNDED, PaymentStatus.REFUND_PENDING}:
        raise ApiError(409, "ORDER_NOT_PAYABLE", "This order has expired or been cancelled. Place a fresh order.")

    idem_key = (idempotency_header or payload.idempotencyKey or "").strip()[:120] or None
    if idem_key:
        existing_idem = db.scalar(select(Payment).where(Payment.idempotency_key == idem_key))
        if existing_idem is not None:
            if existing_idem.order_id != order.id:
                raise ApiError(409, "IDEMPOTENCY_KEY_CONFLICT", "Idempotency key belongs to a different order.")
            return ok(
                request,
                {
                    "paymentId": existing_idem.id,
                    "orderId": order.id,
                    "provider": existing_idem.provider,
                    "keyId": cfg.razorpay_key_id,
                    "providerOrderId": existing_idem.provider_order_id,
                    "gatewayOrderId": existing_idem.provider_order_id,
                    "amountPaise": existing_idem.amount_paise,
                    "currency": existing_idem.currency,
                    "upiIntentSupported": True,
                    "prefill": {"name": user.full_name, "email": user.email, "contact": user.phone or ""},
                },
                status_code=201,
                headers={"Idempotency-Replayed": "true"},
            )

    existing_pending = next((p for p in order.payments if p.status == "PENDING" and p.provider_order_id), None)
    if existing_pending is not None:
        return ok(
            request,
            {
                "paymentId": existing_pending.id,
                "orderId": order.id,
                "provider": existing_pending.provider,
                "keyId": cfg.razorpay_key_id,
                "providerOrderId": existing_pending.provider_order_id,
                "gatewayOrderId": existing_pending.provider_order_id,
                "amountPaise": existing_pending.amount_paise,
                "currency": existing_pending.currency,
                "upiIntentSupported": True,
                "prefill": {"name": user.full_name, "email": user.email, "contact": user.phone or ""},
            },
            status_code=201,
        )

    amount_paise = order.effective_total_paise
    try:
        resp = httpx.post(
            "https://api.razorpay.com/v1/orders",
            auth=(cfg.razorpay_key_id, cfg.razorpay_key_secret),
            json={
                "amount": amount_paise,
                "currency": cfg.payment_currency,
                "receipt": order.order_number or f"ace-{order.id}-{int(time.time())}",
                "notes": {"order_id": str(order.id), "user_id": str(order.user_id)},
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        gw_order = resp.json()
    except Exception as exc:
        logger.warning("Razorpay UPI intent creation failed: %s", type(exc).__name__)
        raise ApiError(502, "PAYMENT_GATEWAY_UNAVAILABLE", "Payment gateway is temporarily unreachable. Your order remains unpaid; please retry.")

    provider_order_id = str(gw_order["id"])
    payment_rec = Payment(
        order_id=order.id,
        provider="razorpay",
        provider_order_id=provider_order_id,
        amount_paise=amount_paise,
        currency=cfg.payment_currency,
        status="PENDING",
        idempotency_key=idem_key,
    )
    db.add(payment_rec)
    order.gateway_order_id = provider_order_id
    record_audit(db, "payment.intent_created", "order", order.id, user.id, {"provider_order_id": provider_order_id, "amount_paise": amount_paise})
    db.commit()
    db.refresh(payment_rec)

    return ok(
        request,
        {
            "paymentId": payment_rec.id,
            "orderId": order.id,
            "provider": "razorpay",
            "keyId": cfg.razorpay_key_id,
            "providerOrderId": provider_order_id,
            "gatewayOrderId": provider_order_id,
            "amountPaise": amount_paise,
            "currency": cfg.payment_currency,
            "upiIntentSupported": True,
            "prefill": {"name": user.full_name, "email": user.email, "contact": user.phone or ""},
        },
        status_code=201,
    )


def _mark_order_paid_and_notify(
    db: Session,
    order: Order,
    payment_rec: Payment | None,
    provider_payment_id: str,
    signature: str | None,
    actor_user_id: int | None,
    source: str,
) -> None:
    prev_status = order.status.value
    order.payment_status = PaymentStatus.PAID
    if order.status == OrderStatus.PENDING:
        order.status = OrderStatus.CONFIRMED
    order.gateway_payment_id = provider_payment_id
    if signature:
        order.payment_signature = signature
    if payment_rec is not None:
        payment_rec.provider_payment_id = provider_payment_id
        payment_rec.status = "SUCCESS"

    record_order_status_change(db, order, prev_status, order.status.value.upper(), actor_user_id, f"Payment confirmed ({source})")
    notify_user(
        db,
        order.user_id,
        "payment",
        "UPI payment confirmed",
        f"Payment of ₹{paise_to_rupees(order.effective_total_paise)} received for order #{order.order_number or order.id} (Token #{order.token_number}).",
        entity_type="order",
        entity_id=order.id,
    )
    stage_outbox_event(
        db,
        "kitchen.order.created",
        serialize_order_v1(order, include_customer=True),
        counter_id=order.counter_id,
    )
    stage_outbox_event(
        db,
        "order.updated",
        serialize_order_v1(order),
        counter_id=order.counter_id,
        target_user_id=order.user_id,
    )
    stage_outbox_event(
        db,
        "queue.public",
        {"tokenNumber": order.token_number, "status": order.status.value, "counterId": order.counter_id},
        counter_id=order.counter_id,
    )
    record_audit(
        db,
        f"payment.confirmed_{source}",
        "order",
        order.id,
        actor_user_id,
        {"provider_payment_id": provider_payment_id, "amount_paise": order.effective_total_paise},
    )


@router.post("/payments/verify")
def verify_payment_v1(
    payload: VerifyPaymentV1In,
    background: BackgroundTasks,
    request: Request,
    user: User = Depends(require_roles("student", "admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    gw_order_id = (payload.razorpayOrderId or payload.razorpay_order_id or "").strip()
    gw_payment_id = (payload.razorpayPaymentId or payload.razorpay_payment_id or "").strip()
    gw_sig = (payload.razorpaySignature or payload.razorpay_signature or "").strip()
    if not all((gw_order_id, gw_payment_id, gw_sig)):
        raise ApiError(422, "MISSING_PAYMENT_FIELDS", "razorpayOrderId, razorpayPaymentId and razorpaySignature are required.")
    if not cfg.razorpay_key_secret:
        raise ApiError(503, "PAYMENT_GATEWAY_NOT_CONFIGURED", "Payment verification secret is not configured.")

    expected = hmac.new(cfg.razorpay_key_secret.encode(), f"{gw_order_id}|{gw_payment_id}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, gw_sig):
        raise ApiError(400, "INVALID_PAYMENT_SIGNATURE", "Payment signature verification failed.")

    pay_rec = db.scalar(select(Payment).where(Payment.provider_order_id == gw_order_id).with_for_update())
    order = db.scalar(
        select(Order)
        .options(joinedload(Order.items), joinedload(Order.user), joinedload(Order.status_history))
        .where(
            or_(
                Order.gateway_order_id == gw_order_id,
                Order.id == (pay_rec.order_id if pay_rec else -1),
            )
        )
        .with_for_update()
    )
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order matching this payment intent was not found.")
    if user.role == UserRole.STUDENT and order.user_id != user.id:
        raise ApiError(403, "FORBIDDEN", "You can only verify payments for your own orders.")

    if order.payment_status == PaymentStatus.PAID:
        if order.gateway_payment_id == gw_payment_id:
            return ok(request, {"verified": True, "order": serialize_order_v1(order, include_history=True)})
        raise ApiError(409, "DUPLICATE_PAYMENT_CONFLICT", "Order was already paid using a different payment transaction.")

    if order.status == OrderStatus.CANCELLED or order.payment_status in {PaymentStatus.EXPIRED, PaymentStatus.FAILED}:
        refund = handle_late_captured_payment(
            db,
            order,
            gw_order_id,
            gw_payment_id,
            order.effective_total_paise,
            cfg.payment_currency,
        )
        commit_and_flush_outbox(db)
        raise ApiError(
            409,
            "ORDER_EXPIRED_REFUND_INITIATED",
            "Order reservation had already expired. Your payment was flagged and an automatic refund has been initiated.",
            details={"refundId": refund.id, "refundStatus": refund.status},
        )

    _mark_order_paid_and_notify(db, order, pay_rec, gw_payment_id, gw_sig, user.id, "client_verify")
    commit_and_flush_outbox(db)
    db.refresh(order)

    if cfg.email_receipts_enabled and order.user and order.user.email:
        background.add_task(
            send_email,
            order.user.email,
            f"Receipt for {cfg.app_name} Order {order.order_number}",
            (
                f"Hello {order.user.full_name},\n\n"
                f"We received your UPI payment of INR {paise_to_rupees(order.effective_total_paise)} "
                f"for order {order.order_number} (Token #{order.token_number}).\n"
                f"Payment Reference: {gw_payment_id}\n\n"
                f"{cfg.app_name}\n"
            ),
        )
    return ok(request, {"verified": True, "order": serialize_order_v1(order, include_history=True)})


@router.get("/payments/{payment_id}")
def get_payment_status_v1(
    payment_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    pay_rec = db.scalar(select(Payment).options(joinedload(Payment.order).joinedload(Order.items), joinedload(Payment.refunds)).where(Payment.id == payment_id))
    if pay_rec is None or pay_rec.order is None:
        raise ApiError(404, "PAYMENT_NOT_FOUND", "Payment record not found.")
    order = pay_rec.order
    if user.role == UserRole.STUDENT and order.user_id != user.id:
        raise ApiError(403, "FORBIDDEN", "You can only view your own payment records.")
    if user.role == UserRole.STAFF:
        assert_counter_access(db, user, order.counter_id)

    cfg = get_settings()
    if pay_rec.status == "PENDING" and cfg.razorpay_key_id and cfg.razorpay_key_secret:
        try:
            res = httpx.get(
                f"https://api.razorpay.com/v1/orders/{pay_rec.provider_order_id}/payments",
                auth=(cfg.razorpay_key_id, cfg.razorpay_key_secret),
                timeout=10.0,
            )
            res.raise_for_status()
            for item in res.json().get("items", []):
                if item.get("status") == "captured" and int(item.get("amount", 0)) == pay_rec.amount_paise:
                    if order.status == OrderStatus.CANCELLED or order.payment_status in {PaymentStatus.EXPIRED, PaymentStatus.FAILED}:
                        handle_late_captured_payment(db, order, pay_rec.provider_order_id, str(item["id"]), pay_rec.amount_paise, cfg.payment_currency)
                    else:
                        _mark_order_paid_and_notify(db, order, pay_rec, str(item["id"]), None, user.id, "reconcile_poll")
                    commit_and_flush_outbox(db)
                    db.refresh(pay_rec)
                    break
        except Exception:
            logger.debug("Provider poll skipped/failed during payment lookup", exc_info=True)

    return ok(
        request,
        {
            "id": pay_rec.id,
            "orderId": order.id,
            "provider": pay_rec.provider,
            "providerOrderId": pay_rec.provider_order_id,
            "providerPaymentId": pay_rec.provider_payment_id,
            "amountPaise": pay_rec.amount_paise,
            "currency": pay_rec.currency,
            "status": pay_rec.status,
            "lateCaptureFlagged": pay_rec.late_capture_flagged,
            "refunds": [
                {
                    "id": r.id,
                    "amountPaise": r.amount_paise or rupees_to_paise(r.amount),
                    "status": r.status,
                    "providerRefundId": r.provider_refund_id,
                    "reason": r.reason,
                }
                for r in pay_rec.refunds
            ],
        },
    )


@router.post("/webhooks/payments/{provider}")
async def payment_webhook_v1(
    provider: str,
    request: Request,
    x_razorpay_signature: str | None = Header(default=None),
    x_razorpay_event_id: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if provider.lower() != "razorpay":
        raise ApiError(404, "UNSUPPORTED_PROVIDER", f"Webhook provider '{provider}' is not supported.")
    cfg = get_settings()
    if not cfg.razorpay_webhook_secret:
        raise ApiError(503, "WEBHOOK_NOT_CONFIGURED", "RAZORPAY_WEBHOOK_SECRET is not configured.")

    raw_body = await request.body()
    expected_sig = hmac.new(cfg.razorpay_webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not x_razorpay_signature or not hmac.compare_digest(expected_sig, x_razorpay_signature):
        raise ApiError(400, "INVALID_WEBHOOK_SIGNATURE", "Webhook HMAC signature verification failed.")

    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(400, "INVALID_WEBHOOK_JSON", "Webhook body must be valid UTF-8 JSON.")

    payload_hash = hashlib.sha256(raw_body).hexdigest()
    webhook_id = (x_razorpay_event_id or payload_hash).strip()[:160]
    event_name = str(event.get("event") or "unknown")[:100]

    prior = db.scalar(
        select(PaymentWebhookEvent).where(
            or_(PaymentWebhookEvent.event_id == webhook_id, PaymentWebhookEvent.payload_hash == payload_hash)
        )
    )
    if prior is not None:
        return ok(request, {"received": True, "duplicate": True, "eventId": prior.event_id})

    wh_row = PaymentWebhookEvent(
        event_id=webhook_id,
        event_name=event_name,
        payload_hash=payload_hash,
        processing_status="processing",
    )
    db.add(wh_row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        return ok(request, {"received": True, "duplicate": True, "eventId": webhook_id})

    payment_entity = event.get("payload", {}).get("payment", {}).get("entity", {}) if isinstance(event, dict) else {}
    refund_entity = event.get("payload", {}).get("refund", {}).get("entity", {}) if isinstance(event, dict) else {}

    if event_name in {"payment.captured", "payment.failed"} and payment_entity.get("order_id"):
        gw_order_id = str(payment_entity["order_id"])
        gw_pay_id = str(payment_entity.get("id") or "")
        amt_paise = int(payment_entity.get("amount") or 0)
        curr = str(payment_entity.get("currency") or cfg.payment_currency)

        pay_rec = db.scalar(select(Payment).where(Payment.provider_order_id == gw_order_id).with_for_update())
        order = db.scalar(
            select(Order)
            .options(joinedload(Order.items), joinedload(Order.status_history))
            .where(or_(Order.gateway_order_id == gw_order_id, Order.id == (pay_rec.order_id if pay_rec else -1)))
            .with_for_update()
        )
        if order is not None:
            if event_name == "payment.captured" and gw_pay_id:
                if (
                    order.status in {OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}
                    or order.payment_status in {PaymentStatus.EXPIRED, PaymentStatus.FAILED, PaymentStatus.REFUNDED, PaymentStatus.REFUND_PENDING, PaymentStatus.PARTIALLY_REFUNDED}
                    or (pay_rec is not None and pay_rec.late_capture_flagged)
                ):
                    handle_late_captured_payment(db, order, gw_order_id, gw_pay_id, amt_paise, curr)
                elif order.payment_status != PaymentStatus.PAID:
                    if amt_paise == order.effective_total_paise and curr == cfg.payment_currency:
                        if pay_rec is None:
                            pay_rec = Payment(
                                order_id=order.id,
                                provider="razorpay",
                                provider_order_id=gw_order_id,
                                provider_payment_id=gw_pay_id,
                                amount_paise=amt_paise,
                                currency=curr,
                                status="SUCCESS",
                            )
                            db.add(pay_rec)
                            db.flush()
                        _mark_order_paid_and_notify(db, order, pay_rec, gw_pay_id, None, None, "webhook")
                    else:
                        record_audit(
                            db,
                            "payment.webhook_amount_mismatch",
                            "order",
                            order.id,
                            None,
                            {"expected_paise": order.effective_total_paise, "received_paise": amt_paise, "currency": curr},
                        )
            elif event_name == "payment.failed":
                if order.payment_status != PaymentStatus.PAID and order.status != OrderStatus.CANCELLED:
                    _restore_order_inventory_once(db, order, f"Payment failed webhook for order #{order.id}")
                    prev = order.status.value
                    order.status = OrderStatus.CANCELLED
                    order.payment_status = PaymentStatus.FAILED
                    order.cancelled_at = datetime.now(timezone.utc)
                    if pay_rec is not None:
                        pay_rec.status = "FAILED"
                        if gw_pay_id:
                            pay_rec.provider_payment_id = gw_pay_id
                    record_order_status_change(db, order, prev, "CANCELLED", None, "UPI payment failed")
                    notify_user(
                        db,
                        order.user_id,
                        "payment",
                        "UPI payment failed",
                        f"Payment for order #{order.order_number or order.id} failed and reserved items were released.",
                        entity_type="order",
                        entity_id=order.id,
                    )

    elif event_name in {"refund.processed", "refund.failed"}:
        gw_refund_id = str(refund_entity.get("id") or "")
        gw_pay_id = str(refund_entity.get("payment_id") or "")
        ref_row = None
        if gw_refund_id:
            ref_row = db.scalar(select(Refund).where(Refund.provider_refund_id == gw_refund_id).with_for_update())
        if ref_row is None and gw_pay_id:
            ref_row = db.scalar(select(Refund).where(Refund.payment_id == gw_pay_id, Refund.status != "PROCESSED").order_by(Refund.id.desc()).with_for_update())
        if ref_row is not None:
            order = db.get(Order, ref_row.order_id)
            pay_rec = db.get(Payment, ref_row.payment_record_id) if ref_row.payment_record_id else None
            if gw_refund_id:
                ref_row.provider_refund_id = gw_refund_id
            if event_name == "refund.processed":
                ref_row.status = "PROCESSED"
                if order is not None:
                    total_refunded = sum(
                        int(r.amount_paise or rupees_to_paise(r.amount))
                        for r in db.scalars(select(Refund).where(Refund.order_id == order.id, Refund.status == "PROCESSED")).all()
                    )
                    if total_refunded >= order.effective_total_paise:
                        order.payment_status = PaymentStatus.REFUNDED
                        if order.status == OrderStatus.REFUND_PENDING:
                            order.status = OrderStatus.CANCELLED
                        if pay_rec:
                            pay_rec.status = "REFUNDED"
                    else:
                        order.payment_status = PaymentStatus.PARTIALLY_REFUNDED
                        if pay_rec:
                            pay_rec.status = "PARTIALLY_REFUNDED"
                    notify_user(
                        db,
                        order.user_id,
                        "payment",
                        "Refund processed",
                        f"Refund of ₹{paise_to_rupees(ref_row.amount_paise or rupees_to_paise(ref_row.amount))} for order #{order.order_number or order.id} has been processed.",
                        entity_type="order",
                        entity_id=order.id,
                    )
            else:
                ref_row.status = "FAILED"

    wh_row.processing_status = "processed"
    wh_row.processed_at = datetime.now(timezone.utc)
    commit_and_flush_outbox(db)
    return ok(request, {"received": True, "duplicate": False, "eventId": webhook_id})


@router.post("/payments/{payment_id}/refunds", status_code=201)
def create_refund_v1(
    payment_id: int,
    payload: RefundRequestIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    pay_rec = db.scalar(select(Payment).options(joinedload(Payment.order), joinedload(Payment.refunds)).where(Payment.id == payment_id).with_for_update())
    if pay_rec is None or pay_rec.order is None:
        raise ApiError(404, "PAYMENT_NOT_FOUND", "Payment record not found.")
    if pay_rec.status not in {"SUCCESS", "PARTIALLY_REFUNDED", "REFUND_PENDING"}:
        raise ApiError(409, "PAYMENT_NOT_REFUNDABLE", f"Payment in status '{pay_rec.status}' cannot be refunded.")

    existing_refunded = sum(
        int(r.amount_paise or rupees_to_paise(r.amount))
        for r in pay_rec.refunds
        if r.status in {"PROCESSED", "PENDING", "PENDING_MANUAL_REVIEW"}
    )
    remaining_paise = max(0, int(pay_rec.amount_paise) - existing_refunded)
    req_paise = payload.amountPaise if payload.amountPaise is not None else remaining_paise
    if req_paise <= 0 or req_paise > remaining_paise:
        raise ApiError(409, "REFUND_EXCEEDS_PAYMENT", f"Requested refund ({req_paise} paise) exceeds refundable balance ({remaining_paise} paise).")

    refund = _initiate_gateway_refund(
        db,
        pay_rec.order,
        pay_rec,
        amount_paise=req_paise,
        reason=payload.reason.strip(),
        actor_user_id=admin.id,
    )
    notify_user(
        db,
        pay_rec.order.user_id,
        "payment",
        "Refund initiated",
        f"A refund of ₹{paise_to_rupees(req_paise)} was initiated for order #{pay_rec.order.order_number or pay_rec.order.id}.",
        entity_type="order",
        entity_id=pay_rec.order.id,
    )
    commit_and_flush_outbox(db)
    db.refresh(refund)
    return ok(
        request,
        {
            "id": refund.id,
            "paymentId": pay_rec.id,
            "orderId": pay_rec.order.id,
            "amountPaise": refund.amount_paise or req_paise,
            "status": refund.status,
            "providerRefundId": refund.provider_refund_id,
            "reason": refund.reason,
        },
        status_code=201,
    )


# --- Short-lived Opaque QR Pickup Token & Single-Use Atomic Handover ---
@router.post("/orders/{order_id}/pickup-token")
def issue_pickup_token_v1(
    order_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _load_authorized_order(db, order_id, user)
    if user.role == UserRole.STAFF:
        raise ApiError(403, "FORBIDDEN", "Pickup QR tokens are issued to the ordering student.")
    if order.payment_status != PaymentStatus.PAID:
        raise ApiError(409, "PAYMENT_PENDING", "Order payment must be confirmed before generating a pickup QR code.")
    if order.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED} or order.collected_at is not None:
        raise ApiError(409, "ORDER_ALREADY_COLLECTED", "This order has already been collected.")
    if order.status in {OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}:
        raise ApiError(409, "ORDER_CANCELLED", "Cancelled orders cannot be collected.")

    if not order.manual_pickup_code:
        order.manual_pickup_code = generate_manual_pickup_code()

    cfg = get_settings()
    ttl_minutes = cfg.pickup_token_minutes
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    raw_token = f"pk_{secrets.token_urlsafe(24)}"
    token_hash = hash_opaque_token(raw_token)
    db.add(PickupToken(order_id=order.id, token_hash=token_hash, expires_at=expires_at))
    db.commit()

    qr_url = f"{cfg.public_base_url.rstrip('/')}/pickup#code={raw_token}"
    c_name = order.counter.name if getattr(order, "counter", None) else "Main Counter"
    return ok(
        request,
        {
            "orderId": order.id,
            "orderNumber": order.order_number or f"ACE-{order.id:06d}",
            "tokenNumber": order.token_number,
            "tokenDisplay": format_token_display(order.token_number, counter_name=c_name, token_date=order.token_date),
            "counterName": c_name,
            "pickupToken": raw_token,
            "qrUrl": qr_url,
            "manualPickupCode": order.manual_pickup_code,
            "expiresAt": expires_at,
            "ttlSeconds": ttl_minutes * 60,
            "orderStatus": serialize_order_v1(order)["status"],
        },
    )


def _extract_raw_pickup_token(supplied: str) -> str:
    val = supplied.strip()
    if val.startswith(("http://", "https://")):
        parsed = urlparse(val)
        val = (parse_qs(parsed.fragment).get("code") or parse_qs(parsed.query).get("code") or [""])[0].strip()
    return val


@router.post("/pickups/public-status")
def public_pickup_status_v1(payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
    """Privacy-preserving status check for a scanned short-lived QR code (never exposes student PII)."""
    raw_tok = _extract_raw_pickup_token(str(payload.get("code") or payload.get("pickupToken") or ""))
    if not raw_tok or not raw_tok.startswith("pk_"):
        raise ApiError(422, "INVALID_PICKUP_TOKEN", "Invalid pickup QR token.")
    tok_hash = hash_opaque_token(raw_tok)
    pt = db.scalar(select(PickupToken).where(PickupToken.token_hash == tok_hash))
    if pt is None:
        raise ApiError(404, "INVALID_PICKUP_TOKEN", "Pickup QR token not found.")
    now = datetime.now(timezone.utc)
    if pt.used_at is None and as_utc(pt.expires_at) <= now:
        raise ApiError(410, "PICKUP_TOKEN_EXPIRED", "This pickup QR token has expired.")
    order = db.scalar(select(Order).options(joinedload(Order.items)).where(Order.id == pt.order_id))
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")
    serialized = serialize_order_v1(order, include_customer=False, include_history=False)
    serialized.pop("manualPickupCode", None)
    return ok(request, serialized)


def _lookup_order_for_pickup(
    db: Session,
    pickup_token: str | None = None,
    order_id: int | None = None,
    manual_code: str | None = None,
    token_number: int | None = None,
    token_string: str | None = None,
    *,
    for_update: bool = False,
) -> tuple[Order | None, PickupToken | None, str]:
    now = datetime.now(timezone.utc)
    today_str = datetime.now(ZoneInfo(get_settings().canteen_timezone)).date().isoformat()
    order: Order | None = None
    pt_row: PickupToken | None = None
    used_hash: str = ""

    if token_string and token_string.strip() and not token_number:
        clean_tok = token_string.strip()
        parts = clean_tok.split("-")
        if parts[-1].isdigit():
            token_number = int(parts[-1])
        elif clean_tok.isdigit():
            token_number = int(clean_tok)

    if pickup_token and pickup_token.strip():
        raw_tok = _extract_raw_pickup_token(pickup_token)
        if not raw_tok:
            raise ApiError(422, "INVALID_PICKUP_TOKEN", "Invalid QR pickup token.")
        if raw_tok.startswith("pk_"):
            tok_hash = hash_opaque_token(raw_tok)
            q = select(PickupToken).where(PickupToken.token_hash == tok_hash)
            if for_update:
                q = q.with_for_update()
            pt_row = db.scalar(q)
            if pt_row is None:
                raise ApiError(404, "INVALID_PICKUP_TOKEN", "Pickup QR token was not recognized.")
            if pt_row.used_at is not None and for_update:
                raise ApiError(409, "ORDER_ALREADY_COLLECTED", "This pickup QR token has already been redeemed.")
            if as_utc(pt_row.expires_at) <= now and for_update:
                raise ApiError(410, "PICKUP_TOKEN_EXPIRED", "Pickup QR token has expired. Ask the student to refresh their QR or enter the manual code.")
            oq = select(Order).options(joinedload(Order.items).joinedload(OrderItem.product).joinedload(Product.counter), joinedload(Order.user), joinedload(Order.status_history), joinedload(Order.counter)).where(Order.id == pt_row.order_id)
            if for_update:
                oq = oq.with_for_update()
            order = db.scalar(oq)
            used_hash = tok_hash
        elif "." in raw_tok:
            try:
                oid_str, sig_part = raw_tok.split(".", 1)
                oid = int(oid_str)
            except ValueError:
                raise ApiError(422, "INVALID_PICKUP_TOKEN", "Malformed pickup QR code.")
            expected_sig = hmac.new(get_settings().secret_key.encode(), f"ace-pickup:{oid_str}".encode(), hashlib.sha256).hexdigest()[:32]
            if oid <= 0 or not hmac.compare_digest(expected_sig, sig_part):
                raise ApiError(422, "INVALID_PICKUP_TOKEN", "Invalid pickup QR signature.")
            oq = select(Order).options(joinedload(Order.items).joinedload(OrderItem.product).joinedload(Product.counter), joinedload(Order.user), joinedload(Order.status_history), joinedload(Order.counter)).where(Order.id == oid)
            if for_update:
                oq = oq.with_for_update()
            order = db.scalar(oq)
            used_hash = hash_opaque_token(raw_tok)
        else:
            raise ApiError(422, "INVALID_PICKUP_TOKEN", "Unrecognized pickup token format.")
    elif order_id is not None and manual_code and manual_code.strip():
        oq = select(Order).options(joinedload(Order.items).joinedload(OrderItem.product).joinedload(Product.counter), joinedload(Order.user), joinedload(Order.status_history), joinedload(Order.counter)).where(Order.id == order_id)
        if for_update:
            oq = oq.with_for_update()
        order = db.scalar(oq)
        if order is None:
            raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")
        supplied_code = manual_code.strip().upper()
        if not order.manual_pickup_code or not hmac.compare_digest(order.manual_pickup_code.upper(), supplied_code):
            raise ApiError(400, "INVALID_MANUAL_PICKUP_CODE", "Manual pickup code does not match this order.")
        used_hash = hash_opaque_token(f"manual:{order.id}:{supplied_code}")
    elif order_id is not None:
        oq = select(Order).options(joinedload(Order.items).joinedload(OrderItem.product).joinedload(Product.counter), joinedload(Order.user), joinedload(Order.status_history), joinedload(Order.counter)).where(Order.id == order_id)
        if for_update:
            oq = oq.with_for_update()
        order = db.scalar(oq)
        used_hash = hash_opaque_token(f"staff_order_id:{order_id}")
    elif token_number is not None:
        oq = select(Order).options(joinedload(Order.items).joinedload(OrderItem.product).joinedload(Product.counter), joinedload(Order.user), joinedload(Order.status_history), joinedload(Order.counter)).where(Order.token_date == today_str, Order.token_number == token_number)
        if for_update:
            oq = oq.with_for_update()
        order = db.scalar(oq)
        used_hash = hash_opaque_token(f"staff_token:{today_str}:{token_number}")

    return order, pt_row, used_hash


@router.post("/pickups/inspect")
def inspect_pickup_order_v1(
    payload: InspectPickupIn,
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    """Retrieve full order, customer, payment, preparation & collection eligibility details for staff inspection."""
    assert_staff_permission(staff, "canVerifyPickup")
    order, pt_row, _ = _lookup_order_for_pickup(
        db,
        pickup_token=payload.pickupToken,
        order_id=payload.orderId,
        manual_code=payload.manualCode,
        token_number=payload.tokenNumber,
        token_string=payload.tokenString,
        for_update=False,
    )
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "No matching order found for this token or QR.")
    assert_counter_access(db, staff, order.counter_id)

    now = datetime.now(timezone.utc)
    is_paid = (order.payment_status == PaymentStatus.PAID)
    is_ready = (order.status in {OrderStatus.READY, OrderStatus.PARTIALLY_COLLECTED})
    is_partially_collected = (order.status == OrderStatus.PARTIALLY_COLLECTED)
    is_collected = (order.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED} or (order.collected_at is not None and not is_partially_collected))
    is_cancelled = bool(order.status in {OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING} or order.payment_status in {PaymentStatus.REFUND_PENDING, PaymentStatus.REFUNDED})

    eligible = bool(is_paid and is_ready and not is_collected and not is_cancelled)
    block_reason: str | None = None
    collected_by_name: str | None = None
    collected_at_iso: str | None = None

    if is_collected:
        p_rec = db.scalar(select(Pickup).options(joinedload(Pickup.collector)).where(Pickup.order_id == order.id))
        if p_rec and p_rec.collector:
            collected_by_name = p_rec.collector.full_name
        if p_rec and p_rec.collected_at:
            collected_at_iso = as_utc(p_rec.collected_at).isoformat()
            time_str = as_utc(p_rec.collected_at).strftime("%I:%M %p")
            block_reason = f"Order was ALREADY COLLECTED at {time_str} by {collected_by_name or 'Staff'}."
        else:
            block_reason = "Order has already been collected."
    elif not is_paid:
        block_reason = f"Payment status is {order.payment_status.value.upper()} (Unpaid order — do not handover food!)."
    elif is_cancelled:
        block_reason = f"Order was cancelled ({order.exception_reason or 'Refund pending'})."
    elif not is_ready:
        block_reason = f"Order is currently '{order.status.value.upper()}' — kitchen has not marked it READY yet."

    c_name = order.counter.name if getattr(order, "counter", None) else "Main Counter"
    tok_display = format_token_display(order.token_number, counter_name=c_name, token_date=order.token_date)

    customer_info = {
        "id": order.user.id if order.user else None,
        "fullName": order.user.full_name if order.user else "Student",
        "studentId": f"ACE-{order.user.id:04d}" if order.user else "STU",
        "phoneMasked": (order.user.phone[:3] + "****" + order.user.phone[-3:]) if (order.user and order.user.phone and len(order.user.phone) >= 7) else ((order.user.phone or "") if order.user else ""),
        "email": order.user.email if order.user else "",
    }
    token_info = {
        "orderId": order.id,
        "orderNumber": order.order_number or f"ACE-{order.id:06d}",
        "tokenNumber": order.token_number,
        "tokenDisplay": tok_display,
        "counterId": order.counter_id,
        "counterName": c_name,
        "createdAt": as_utc(order.created_at),
        "estimatedReadyAt": as_utc(order.estimated_ready_at),
        "elapsedMinutes": max(0, int((now - (as_utc(order.created_at) or now)).total_seconds() // 60)),
    }
    items_out = []
    for it in order.items:
        it_counter_id = it.product.counter_id if getattr(it, "product", None) else None
        it_counter_name = it.product.counter.name if (getattr(it, "product", None) and it.product.counter) else c_name
        items_out.append({
            "id": it.id,
            "menuItemId": it.product_id,
            "name": it.product_name,
            "quantity": it.quantity,
            "unitPriceRupees": str(paise_to_rupees(it.unit_price_paise or rupees_to_paise(it.unit_price))),
            "lineTotalRupees": str(paise_to_rupees(it.line_total_paise or rupees_to_paise(it.line_total))),
            "customizations": parse_json_safe(it.customizations_json, {}),
            "counterId": it_counter_id,
            "counterName": it_counter_name,
            "isCollected": bool(getattr(it, "is_collected", False)),
            "collectedQuantity": getattr(it, "collected_quantity", 0) or 0,
            "collectedAt": as_utc(getattr(it, "collected_at", None)).isoformat() if getattr(it, "collected_at", None) else None,
        })

    gw_masked = (order.gateway_payment_id[:6] + "..." + order.gateway_payment_id[-4:]) if order.gateway_payment_id else "UPI-VERIFIED"
    payment_info = {
        "status": order.payment_status.value.upper(),
        "method": "UPI / Razorpay",
        "referenceMasked": gw_masked,
        "totalRupees": str(paise_to_rupees(order.effective_total_paise)),
        "discountRupees": str(paise_to_rupees(order.discount_paise or 0)),
        "taxRupees": str(paise_to_rupees(order.tax_paise or 0)),
    }
    prep_info = {
        "status": order.status.value.upper(),
        "acceptedAt": as_utc(order.accepted_at),
        "readyAt": as_utc(order.ready_at),
        "isOverdue": bool(order.status != OrderStatus.READY and (now - (as_utc(order.created_at) or now)).total_seconds() > 900),
    }
    col_status = "already_collected" if is_collected else ("partially_collected" if is_partially_collected else ("ready_for_pickup" if is_ready else ("cancelled" if is_cancelled else ("payment_pending" if not is_paid else order.status.value))))
    collection_info = {
        "status": col_status,
        "eligibleForCollection": eligible,
        "blockReason": block_reason,
        "collectedAt": collected_at_iso,
        "collectedByStaffName": collected_by_name,
        "manualPickupCode": order.manual_pickup_code if is_paid else None,
    }

    return ok(
        request,
        {
            "verified": True,
            "eligibleForCollection": eligible,
            "blockReason": block_reason,
            "customer": customer_info,
            "token": token_info,
            "items": items_out,
            "payment": payment_info,
            "preparation": prep_info,
            "collection": collection_info,
            "order": serialize_order_v1(order, include_customer=True, include_history=True),
        },
    )


@router.post("/pickups/verify")
def verify_and_collect_pickup_v1(
    payload: VerifyPickupIn,
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    assert_staff_permission(staff, "canVerifyPickup")
    now = datetime.now(timezone.utc)
    order, pt_row, used_hash = _lookup_order_for_pickup(
        db,
        pickup_token=payload.pickupToken,
        order_id=payload.orderId,
        manual_code=payload.manualCode,
        token_number=payload.tokenNumber,
        token_string=payload.tokenString,
        for_update=True,
    )

    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")

    assert_counter_access(db, staff, order.counter_id)

    if order.payment_status != PaymentStatus.PAID:
        raise ApiError(409, "PAYMENT_PENDING", "Order payment is not verified. Do not hand over food.")

    if order.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED} or (order.collected_at is not None and order.status != OrderStatus.PARTIALLY_COLLECTED):
        raise ApiError(409, "ORDER_ALREADY_COLLECTED", "This order has already been fully collected.")

    if order.status not in {OrderStatus.READY, OrderStatus.PARTIALLY_COLLECTED}:
        raise ApiError(409, "ORDER_NOT_READY", f"Order is currently '{order.status.value}' and must be marked READY before handover.")

    # Determine which items are being handed over
    target_items = []
    if payload.itemIds:
        selected_set = set(payload.itemIds)
        target_items = [it for it in order.items if it.id in selected_set and not getattr(it, "is_collected", False)]
        if not target_items:
            raise ApiError(400, "NO_ITEMS_TO_COLLECT", "Selected item(s) are already collected or invalid.")
    else:
        target_items = [it for it in order.items if not getattr(it, "is_collected", False)]
        if not target_items:
            raise ApiError(409, "ORDER_ALREADY_COLLECTED", "All items for this order have already been collected.")

    # Hand over the items
    for it in target_items:
        it.is_collected = True
        it.collected_quantity = it.quantity
        it.collected_at = now
        it.collected_by_staff_id = staff.id

    all_done = all(getattr(it, "is_collected", False) for it in order.items)
    prev_status = order.status.value

    if all_done:
        if pt_row is not None:
            pt_row.used_at = now

        existing_pickup = db.scalar(select(Pickup).where(Pickup.order_id == order.id))
        if existing_pickup is None:
            pickup_rec = Pickup(
                order_id=order.id,
                pickup_token_hash=used_hash or hash_opaque_token(f"pickup:{order.id}:{now.isoformat()}"),
                collected_by=staff.id,
                collected_at=now,
            )
            db.add(pickup_rec)

        order.status = OrderStatus.COLLECTED
        order.collected_at = now
        record_order_status_change(db, order, prev_status, "COLLECTED", staff.id, "All items collected via verified handover")
        notify_user(
            db,
            order.user_id,
            "order_status",
            "Order collected",
            f"All items for Order #{order.order_number or order.id} were collected. Enjoy your meal!",
            entity_type="order",
            entity_id=order.id,
        )
        stage_outbox_event(
            db,
            "order.collected",
            {"orderId": order.id, "tokenNumber": order.token_number, "collectedAt": now.isoformat(), "counterId": order.counter_id, "fullyCollected": True},
            counter_id=order.counter_id,
            target_user_id=order.user_id,
        )
        stage_outbox_event(
            db,
            "queue.public",
            {"tokenNumber": order.token_number, "status": "collected", "counterId": order.counter_id},
            counter_id=order.counter_id,
        )
    else:
        order.status = OrderStatus.PARTIALLY_COLLECTED
        record_order_status_change(db, order, prev_status, "PARTIALLY_COLLECTED", staff.id, f"Handed over {len(target_items)} of {len(order.items)} item(s)")
        notify_user(
            db,
            order.user_id,
            "order_status",
            "Order partially collected",
            f"{len(target_items)} item(s) for Order #{order.order_number or order.id} were collected. Remaining items can be picked up when ready.",
            entity_type="order",
            entity_id=order.id,
        )
        stage_outbox_event(
            db,
            "order.partially_collected",
            {"orderId": order.id, "tokenNumber": order.token_number, "collectedAt": now.isoformat(), "counterId": order.counter_id, "itemsCollected": [it.id for it in target_items]},
            counter_id=order.counter_id,
            target_user_id=order.user_id,
        )

    record_audit(db, "order.pickup_verified", "order", order.id, staff.id, {"collector_staff_id": staff.id, "all_done": all_done, "items": [it.id for it in target_items]})
    try:
        commit_and_flush_outbox(db)
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "ORDER_ALREADY_COLLECTED", "This order was just collected by another counter terminal.")

    db.refresh(order)
    return ok(
        request,
        {
            "verified": True,
            "collectedAt": now,
            "collectedByStaffId": staff.id,
            "order": serialize_order_v1(order, include_customer=True, include_history=True),
        },
    )


@router.get("/queue/display-board")
def get_display_board_tokens_v1(
    request: Request,
    counterId: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
):
    """Public, privacy-safe token board data for canteen TVs and monitors."""
    cfg = get_settings()
    tz = ZoneInfo(cfg.canteen_timezone)
    now_local = datetime.now(tz)
    today_str = now_local.date().isoformat()
    now_utc = datetime.now(timezone.utc)

    c_stmt = select(CanteenCounter).where(CanteenCounter.is_active.is_(True)).order_by(CanteenCounter.id.asc())
    counters = db.scalars(c_stmt).all()

    # Query ready paid orders
    r_stmt = (
        select(Order)
        .options(joinedload(Order.counter))
        .where(
            Order.token_date == today_str,
            Order.payment_status == PaymentStatus.PAID,
            Order.status == OrderStatus.READY,
        )
    )
    if counterId:
        r_stmt = r_stmt.where(Order.counter_id == counterId)
    ready_orders = db.scalars(r_stmt.order_by(Order.ready_at.desc(), Order.token_number.asc()).limit(50)).all()

    # Query preparing / accepted orders
    p_stmt = (
        select(Order)
        .options(joinedload(Order.counter))
        .where(
            Order.token_date == today_str,
            Order.payment_status == PaymentStatus.PAID,
            Order.status.in_([OrderStatus.ACCEPTED, OrderStatus.PREPARING]),
        )
    )
    if counterId:
        p_stmt = p_stmt.where(Order.counter_id == counterId)
    prep_orders = db.scalars(p_stmt.order_by(Order.accepted_at.asc(), Order.token_number.asc()).limit(50)).all()

    # Query last 15 collected orders
    col_stmt = (
        select(Order)
        .options(joinedload(Order.counter))
        .where(
            Order.token_date == today_str,
            Order.status.in_([OrderStatus.COMPLETED, OrderStatus.COLLECTED]),
        )
    )
    if counterId:
        col_stmt = col_stmt.where(Order.counter_id == counterId)
    collected_orders = db.scalars(col_stmt.order_by(Order.collected_at.desc()).limit(15)).all()

    def _tok(o: Order) -> dict[str, Any]:
        c_name = o.counter.name if o.counter else "Counter"
        ready_dt = as_utc(o.ready_at)
        ready_mins = max(0, int((now_utc - ready_dt).total_seconds() // 60)) if ready_dt else 0
        return {
            "orderId": o.id,
            "tokenNumber": o.token_number,
            "tokenDisplay": format_token_display(o.token_number, counter_name=c_name, token_date=o.token_date),
            "counterId": o.counter_id,
            "counterName": c_name,
            "status": o.status.value,
            "readyMinsAgo": ready_mins,
            "readyAt": ready_dt.isoformat() if ready_dt else None,
        }

    return ok(
        request,
        {
            "date": today_str,
            "serverTime": now_utc.isoformat(),
            "counters": [{"id": c.id, "name": c.name, "location": c.location} for c in counters],
            "readyTokens": [_tok(o) for o in ready_orders],
            "preparingTokens": [_tok(o) for o in prep_orders],
            "recentlyCollected": [_tok(o) for o in collected_orders],
        },
    )


@router.get("/tokens")
def list_and_search_tokens_v1(
    request: Request,
    search: str | None = Query(default=None, max_length=100),
    counterId: int | None = Query(default=None, gt=0),
    serviceDate: date | None = None,
    status: str | None = Query(default=None, max_length=30),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    """Token management search & history for staff and administrators."""
    cfg = get_settings()
    tz = ZoneInfo(cfg.canteen_timezone)
    date_str = serviceDate.isoformat() if serviceDate else datetime.now(tz).date().isoformat()

    stmt = (
        select(Order)
        .options(joinedload(Order.user), joinedload(Order.counter), joinedload(Order.items))
        .where(Order.token_date == date_str)
    )
    if counterId:
        stmt = stmt.where(Order.counter_id == counterId)

    if status and status.lower() != "all":
        st_clean = status.lower()
        if st_clean == "preparing":
            stmt = stmt.where(Order.status.in_([OrderStatus.ACCEPTED, OrderStatus.PREPARING]))
        elif st_clean == "ready":
            stmt = stmt.where(Order.status == OrderStatus.READY)
        elif st_clean == "collected":
            stmt = stmt.where(Order.status.in_([OrderStatus.COMPLETED, OrderStatus.COLLECTED]))
        elif st_clean == "cancelled":
            stmt = stmt.where(or_(Order.status.in_([OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING]), Order.payment_status.in_([PaymentStatus.REFUND_PENDING, PaymentStatus.REFUNDED])))

    if search and search.strip():
        s = search.strip()
        conds = []
        clean_s = s.split("-")[-1]
        if clean_s.isdigit():
            conds.append(Order.token_number == int(clean_s))
            conds.append(Order.id == int(clean_s))
        conds.append(Order.order_number.ilike(f"%{s}%"))
        conds.append(Order.user.has(User.full_name.ilike(f"%{s}%")))
        conds.append(Order.user.has(User.phone.ilike(f"%{s}%")))
        stmt = stmt.where(or_(*conds))

    total = db.scalar(select(func.count(Order.id)).where(stmt.whereclause)) or 0
    orders = db.scalars(stmt.order_by(Order.token_number.desc(), Order.id.desc()).offset((page - 1) * limit).limit(limit)).unique().all()

    return ok(
        request,
        {
            "date": date_str,
            "total": total,
            "page": page,
            "limit": limit,
            "items": [
                {
                    "orderId": o.id,
                    "orderNumber": o.order_number or f"ACE-{o.id:06d}",
                    "tokenNumber": o.token_number,
                    "tokenDisplay": format_token_display(o.token_number, counter_name=o.counter.name if o.counter else None, token_date=o.token_date),
                    "counterId": o.counter_id,
                    "counterName": o.counter.name if o.counter else "Counter",
                    "status": o.status.value.upper(),
                    "paymentStatus": o.payment_status.value.upper(),
                    "customerName": o.user.full_name if o.user else "Student",
                    "studentId": f"ACE-{o.user_id:04d}",
                    "itemsSummary": ", ".join(f"{it.quantity}x {it.product_name}" for it in o.items[:3]) + (f" +{len(o.items)-3} more" if len(o.items) > 3 else ""),
                    "totalRupees": str(paise_to_rupees(o.effective_total_paise)),
                    "createdAt": as_utc(o.created_at),
                    "readyAt": as_utc(o.ready_at),
                    "collectedAt": as_utc(o.collected_at),
                }
                for o in orders
            ],
        },
    )


# --- Verified Order Ratings & Reviews ---
@router.get("/orders/{order_id}/review")
def get_order_review_v1(
    order_id: int,
    request: Request,
    user: User = Depends(require_roles("student")),
    db: Session = Depends(get_db),
):
    order = _load_authorized_order(db, order_id, user)
    eligible = order.payment_status == PaymentStatus.PAID and order.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED}
    rev = db.scalar(select(OrderReview).where(OrderReview.order_id == order.id, OrderReview.user_id == user.id))
    return ok(
        request,
        {
            "eligible": eligible,
            "review": (
                {"id": rev.id, "rating": rev.rating, "comment": rev.comment, "createdAt": as_utc(rev.created_at)}
                if rev is not None
                else None
            ),
        },
    )


@router.post("/orders/{order_id}/review")
def upsert_order_review_v1(
    order_id: int,
    payload: ReviewSubmitV1In,
    request: Request,
    user: User = Depends(require_roles("student")),
    db: Session = Depends(get_db),
):
    order = _load_authorized_order(db, order_id, user)
    if order.payment_status != PaymentStatus.PAID or order.status not in {OrderStatus.COMPLETED, OrderStatus.COLLECTED}:
        raise ApiError(409, "REVIEW_NOT_ELIGIBLE", "Ratings can only be submitted after your paid order has been collected.")
    rev = db.scalar(select(OrderReview).where(OrderReview.order_id == order.id))
    comment_clean = (payload.comment or "").strip() or None
    if rev is not None:
        if rev.user_id != user.id:
            raise ApiError(403, "FORBIDDEN", "Cannot modify another user's review.")
        rev.rating = payload.rating
        rev.comment = comment_clean
        rev.updated_at = datetime.now(timezone.utc)
    else:
        rev = OrderReview(order_id=order.id, user_id=user.id, rating=payload.rating, comment=comment_clean)
        db.add(rev)
    db.commit()
    db.refresh(rev)
    return ok(request, {"id": rev.id, "orderId": order.id, "rating": rev.rating, "comment": rev.comment})


# --- Official Digital Order Receipt (JSON & PDF) ---
@router.get("/orders/{order_id}/receipt")
def get_order_digital_receipt_v1(
    order_id: int,
    request: Request,
    format: str = Query(default="json", pattern="^(json|pdf)$"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    order = _load_authorized_order(db, order_id, user)
    counter = db.get(CanteenCounter, order.counter_id) if order.counter_id else None
    cfg = get_settings()
    tz = ZoneInfo(cfg.canteen_timezone)

    sub_p = int(order.subtotal_paise or rupees_to_paise(order.subtotal))
    disc_p = int(order.discount_paise or rupees_to_paise(order.discount_amount))
    tax_p = int(order.tax_paise or rupees_to_paise(order.tax_amount))
    tot_p = order.effective_total_paise

    paid_payment = next((p for p in order.payments if p.status == "SUCCESS"), None)
    receipt_number = f"RCPT-{order.order_number or order.id}"
    issued_at = as_utc(order.created_at)

    receipt_payload = {
        "receiptNumber": receipt_number,
        "brandName": cfg.brand_name,
        "canteenName": cfg.app_name,
        "orderId": order.id,
        "orderPublicUuid": order.public_uuid,
        "orderNumber": order.order_number or f"ACE-{order.id:06d}",
        "tokenNumber": order.token_number,
        "counterName": counter.name if counter else "Main Canteen Counter",
        "customerName": order.user.full_name if order.user else "",
        "customerEmail": order.user.email if order.user else "",
        "customer": {
            "fullName": order.user.full_name if order.user else "",
            "email": order.user.email if order.user else "",
        },
        "status": order.status.value.upper(),
        "paymentStatus": order.payment_status.value.upper(),
        "paymentReference": paid_payment.provider_payment_id if paid_payment else order.gateway_payment_id,
        "payment": {
            "provider": paid_payment.provider if paid_payment else "razorpay",
            "providerPaymentId": paid_payment.provider_payment_id if paid_payment else order.gateway_payment_id,
        },
        "gatewayOrderId": paid_payment.provider_order_id if paid_payment else order.gateway_order_id,
        "couponCode": order.coupon_code,
        "subtotalPaise": sub_p,
        "discountPaise": disc_p,
        "taxPaise": tax_p,
        "totalPaise": tot_p,
        "subtotalRupees": str(paise_to_rupees(sub_p)),
        "discountRupees": str(paise_to_rupees(disc_p)),
        "taxRupees": str(paise_to_rupees(tax_p)),
        "totalRupees": str(paise_to_rupees(tot_p)),
        "issuedAt": issued_at,
        "collectedAt": as_utc(order.collected_at),
        "items": [
            {
                "name": it.product_name,
                "quantity": it.quantity,
                "unitPricePaise": int(it.unit_price_paise or rupees_to_paise(it.unit_price)),
                "lineTotalPaise": int(it.line_total_paise or rupees_to_paise(it.line_total)),
                "unitPriceRupees": str(paise_to_rupees(it.unit_price_paise or rupees_to_paise(it.unit_price))),
                "lineTotalRupees": str(paise_to_rupees(it.line_total_paise or rupees_to_paise(it.line_total))),
            }
            for it in order.items
        ],
    }

    if format.lower() == "pdf":
        from app.modules.reporting_engine import render_pdf_document

        local_ts = issued_at.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if issued_at else ""
        summary_pairs = [
            ("Receipt #", receipt_number),
            ("Token #", f"#{order.token_number}" if order.token_number else "-"),
            ("Payment", receipt_payload["paymentStatus"]),
            ("UPI Ref", receipt_payload["paymentReference"] or "-"),
            ("Total Paid", f"INR {receipt_payload['totalRupees']}"),
        ]
        cols = ["Item", "Qty", "Unit Price (INR)", "Line Total (INR)"]
        pdf_rows = [
            [it["name"], it["quantity"], it["unitPriceRupees"], it["lineTotalRupees"]]
            for it in receipt_payload["items"]
        ]
        pdf_rows.append(["Subtotal", "", "", receipt_payload["subtotalRupees"]])
        if disc_p > 0:
            pdf_rows.append([f"Discount ({order.coupon_code or 'Promo'})", "", "", f"-{receipt_payload['discountRupees']}"])
        if tax_p > 0:
            pdf_rows.append(["Tax", "", "", receipt_payload["taxRupees"]])
        pdf_rows.append(["TOTAL", "", "", receipt_payload["totalRupees"]])

        pdf_bytes = render_pdf_document(
            title=f"Digital Receipt {receipt_payload['orderNumber']}",
            subtitle=f"Customer: {receipt_payload['customerName']} | Counter: {receipt_payload['counterName']} | Issued: {local_ts}",
            summary_pairs=summary_pairs,
            columns=cols,
            rows=pdf_rows,
        )
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{receipt_number}.pdf"',
                "Cache-Control": "no-store",
            },
        )

    return ok(request, receipt_payload)
