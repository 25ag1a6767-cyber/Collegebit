"""/api/v1 Counters, menu discovery, availability windows, server cart, pickup slots & favorites."""
from __future__ import annotations

from datetime import datetime, time as _time_of_day, timedelta, timezone
import json
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db
from app.envelope import ApiError, ok
from app.models import (
    CanteenCounter,
    Cart,
    CartItem,
    Category,
    Discount,
    Favorite,
    MenuItemAvailability,
    Order,
    OrderItem,
    OrderReview,
    OrderStatus,
    PaymentStatus,
    Product,
    RecipeItem,
    User,
    paise_to_rupees,
    rupees_to_paise,
)
from app.modules.common import (
    as_utc,
    compute_customization_extra_paise,
    ensure_default_counter,
    evaluate_discount_paise,
    get_app_setting,
    get_tax_rate_bps,
    parse_json_safe,
    parse_tags,
    serialize_counter_v1,
    serialize_product_v1,
)
from app.security import get_current_user, get_optional_user, require_roles

router = APIRouter(tags=["v1-menu-cart"])

def _canteen_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().canteen_timezone)

def is_item_available_in_window(db: Session, product: Product, when_local: datetime | None = None) -> tuple[bool, int | None]:
    """Check whether any configured MenuItemAvailability windows restrict this product right now."""
    if not product.is_available or product.is_archived or product.stock_quantity <= 0:
        return False, 0
    now_local = when_local or datetime.now(_canteen_tz())
    windows = db.scalars(
        select(MenuItemAvailability).where(
            MenuItemAvailability.product_id == product.id,
            or_(MenuItemAvailability.service_date.is_(None), MenuItemAvailability.service_date == now_local.date()),
        )
    ).all()
    if not windows:
        return True, product.stock_quantity
    cur_hm = now_local.strftime("%H:%M")
    for w in windows:
        if w.start_time <= cur_hm <= w.end_time:
            cap = product.stock_quantity if w.available_quantity is None else min(product.stock_quantity, w.available_quantity)
            if cap > 0:
                return True, cap
    return False, 0

def _product_ratings_map(db: Session, product_ids: list[int]) -> dict[int, tuple[float | None, int]]:
    if not product_ids:
        return {}
    rows = db.execute(
        select(
            OrderItem.product_id,
            func.avg(OrderReview.rating).label("avg_r"),
            func.count(func.distinct(OrderReview.id)).label("cnt"),
        )
        .join(OrderReview, OrderReview.order_id == OrderItem.order_id)
        .where(OrderItem.product_id.in_(product_ids))
        .group_by(OrderItem.product_id)
    ).all()
    return {int(r.product_id): (round(float(r.avg_r), 2) if r.avg_r is not None else None, int(r.cnt)) for r in rows}

def _product_popularity_map(db: Session, product_ids: list[int]) -> dict[int, int]:
    if not product_ids:
        return {}
    since = datetime.now(timezone.utc) - timedelta(days=30)
    rows = db.execute(
        select(OrderItem.product_id, func.coalesce(func.sum(OrderItem.quantity), 0).label("qty"))
        .join(Order, Order.id == OrderItem.order_id)
        .where(
            OrderItem.product_id.in_(product_ids),
            Order.payment_status == PaymentStatus.PAID,
            Order.created_at >= since,
        )
        .group_by(OrderItem.product_id)
    ).all()
    return {int(r.product_id): int(r.qty or 0) for r in rows}

def compute_counter_pickup_slots(db: Session, counter_id: int | None = None) -> dict[str, Any]:
    """Compute available pickup slots for today per counter (or global canteen hours)."""
    cfg = get_settings()
    tz = _canteen_tz()
    now = datetime.now(tz)
    counter = db.get(CanteenCounter, counter_id) if counter_id else ensure_default_counter(db)
    if counter and not counter.is_active:
        return {"counterId": counter.id, "slots": [], "slotMinutes": cfg.pickup_slot_minutes, "capacityPerSlot": 0}

    open_str = cfg.canteen_open_time
    close_str = cfg.canteen_close_time
    if not (open_str and close_str) and counter and counter.opening_hours and "-" in counter.opening_hours and not counter.opening_hours.strip().startswith("{"):
        parts = [p.strip() for p in counter.opening_hours.split("-", 1)]
        if len(parts) == 2 and len(parts[0]) == 5 and len(parts[1]) == 5:
            open_str, close_str = parts[0], parts[1]

    capacity = counter.capacity_per_slot if counter and counter.capacity_per_slot > 0 else cfg.pickup_slot_capacity
    if not (open_str and close_str):
        return {"counterId": counter.id if counter else None, "slots": [], "slotMinutes": cfg.pickup_slot_minutes, "capacityPerSlot": capacity, "alwaysOpen": True}

    open_days = {int(x) for x in cfg.canteen_open_days.split(",") if x.strip()}
    if now.weekday() not in open_days:
        return {"counterId": counter.id if counter else None, "slots": [], "slotMinutes": cfg.pickup_slot_minutes, "capacityPerSlot": capacity, "alwaysOpen": False}

    try:
        open_dt = datetime.combine(now.date(), _time_of_day.fromisoformat(open_str), tzinfo=tz)
        close_dt = datetime.combine(now.date(), _time_of_day.fromisoformat(close_str), tzinfo=tz)
    except ValueError:
        return {"counterId": counter.id if counter else None, "slots": [], "slotMinutes": cfg.pickup_slot_minutes, "capacityPerSlot": capacity}

    step = timedelta(minutes=cfg.pickup_slot_minutes)
    earliest = max(open_dt, now + timedelta(minutes=cfg.pickup_min_lead_minutes))
    steps_needed = max(0, -(-int((earliest - open_dt).total_seconds()) // int(step.total_seconds())))
    cur = open_dt + step * steps_needed
    slot_starts: list[datetime] = []
    while cur < close_dt:
        slot_starts.append(cur)
        cur += step
    if not slot_starts:
        return {"counterId": counter.id if counter else None, "slots": [], "slotMinutes": cfg.pickup_slot_minutes, "capacityPerSlot": capacity}

    stmt = (
        select(Order.pickup_slot, func.count(Order.id))
        .where(
            Order.pickup_slot.is_not(None),
            Order.pickup_slot >= slot_starts[0].astimezone(timezone.utc),
            Order.pickup_slot <= slot_starts[-1].astimezone(timezone.utc),
            Order.status != OrderStatus.CANCELLED,
            Order.payment_status.notin_([PaymentStatus.FAILED, PaymentStatus.EXPIRED]),
        )
    )
    if counter is not None:
        stmt = stmt.where(or_(Order.counter_id == counter.id, Order.counter_id.is_(None)))
    rows = db.execute(stmt.group_by(Order.pickup_slot)).all()
    used = {as_utc(r[0]): int(r[1]) for r in rows}

    slot_list = []
    for s in slot_starts:
        s_utc = s.astimezone(timezone.utc)
        end_local = s + step
        rem = max(0, capacity - used.get(s_utc, 0))
        slot_list.append({
            "start": s.isoformat(),
            "end": end_local.isoformat(),
            "startUtc": s_utc.isoformat(),
            "label": f"{s.strftime('%H:%M')} – {end_local.strftime('%H:%M')}",
            "remaining": rem,
            "isFull": rem <= 0,
        })
    return {
        "counterId": counter.id if counter else None,
        "slots": slot_list,
        "slotMinutes": cfg.pickup_slot_minutes,
        "capacityPerSlot": capacity,
        "alwaysOpen": False,
    }

@router.get("/counters")
def list_active_counters_v1(request: Request, db: Session = Depends(get_db)):
    ensure_default_counter(db)
    db.commit()
    counters = db.scalars(select(CanteenCounter).where(CanteenCounter.is_active.is_(True)).order_by(CanteenCounter.id.asc())).all()
    paused = get_app_setting(db, "ordering_paused", "0") == "1"
    pause_message = get_app_setting(db, "pause_message", "")
    return ok(
        request,
        {
            "acceptingOrders": not paused,
            "pauseMessage": pause_message if paused else None,
            "items": [serialize_counter_v1(db, c) for c in counters],
        },
    )

@router.get("/menu/categories")
def list_menu_categories_v1(
    request: Request,
    itemType: str | None = Query(default=None, pattern="^(food|stationery)$"),
    db: Session = Depends(get_db),
):
    stmt = select(Category).where(Category.is_active.is_(True)).order_by(Category.sort_order.asc(), Category.name.asc())
    if itemType:
        stmt = stmt.where(Category.item_type == itemType)
    cats = db.scalars(stmt).all()
    return ok(
        request,
        {
            "items": [
                {
                    "id": c.id,
                    "name": c.name,
                    "slug": c.slug,
                    "itemType": c.item_type,
                    "description": c.description,
                    "sortOrder": c.sort_order,
                }
                for c in cats
            ]
        },
    )

@router.get("/menu/items")
def list_menu_items_v1(
    request: Request,
    search: str | None = Query(default=None, max_length=100),
    categoryId: int | None = Query(default=None, gt=0),
    counterId: int | None = Query(default=None, gt=0),
    itemType: str | None = Query(default=None, pattern="^(food|stationery)$"),
    maxPricePaise: int | None = Query(default=None, gt=0),
    dietaryTag: str | None = Query(default=None, max_length=40),
    excludeAllergen: str | None = Query(default=None, max_length=40),
    availableOnly: bool = True,
    sortBy: str = Query(default="popularity", pattern="^(popularity|price_asc|price_desc|prep_time|rating|name)$"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
):
    stmt = (
        select(Product)
        .join(Product.category)
        .options(joinedload(Product.category))
        .where(Category.is_active.is_(True), Product.is_archived.is_(False))
    )
    if availableOnly:
        stmt = stmt.where(Product.is_available.is_(True), Product.stock_quantity > 0)
    elif user is None or user.role.value not in {"staff", "admin"}:

        stmt = stmt.where(Product.is_available.is_(True), Product.stock_quantity > 0)

    if categoryId is not None:
        stmt = stmt.where(Product.category_id == categoryId)
    if counterId is not None:
        stmt = stmt.where(or_(Product.counter_id == counterId, Product.counter_id.is_(None)))
    if itemType:
        stmt = stmt.where(Product.item_type == itemType)
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.where(or_(Product.name.ilike(term), Product.description.ilike(term), Category.name.ilike(term)))

    all_matching = db.scalars(stmt).all()

    filtered: list[Product] = []
    for p in all_matching:
        if availableOnly:
            in_win, _ = is_item_available_in_window(db, p)
            if not in_win:
                continue
        if maxPricePaise is not None and p.effective_price_paise > maxPricePaise:
            continue
        if dietaryTag:
            d_tags = {t.lower() for t in parse_tags(p.dietary_tags, p.dietary_info)}
            if dietaryTag.strip().lower() not in d_tags:
                continue
        if excludeAllergen:
            excl = {a.strip().lower() for a in excludeAllergen.split(",") if a.strip()}
            a_tags = {t.lower() for t in parse_tags(p.allergen_tags, p.allergens)}
            if excl & a_tags:
                continue
        filtered.append(p)

    pids = [p.id for p in filtered]
    ratings = _product_ratings_map(db, pids)
    popularity = _product_popularity_map(db, pids)

    if sortBy == "price_asc":
        filtered.sort(key=lambda p: (p.effective_price_paise, p.name.lower()))
    elif sortBy == "price_desc":
        filtered.sort(key=lambda p: (-p.effective_price_paise, p.name.lower()))
    elif sortBy == "prep_time":
        filtered.sort(key=lambda p: (p.preparation_minutes or 10, p.effective_price_paise))
    elif sortBy == "rating":
        filtered.sort(key=lambda p: (-(ratings.get(p.id, (0.0, 0))[0] or 0.0), -popularity.get(p.id, 0), p.name.lower()))
    elif sortBy == "popularity":
        filtered.sort(key=lambda p: (-popularity.get(p.id, 0), -(ratings.get(p.id, (0.0, 0))[0] or 0.0), p.name.lower()))
    else:
        filtered.sort(key=lambda p: p.name.lower())

    total = len(filtered)
    page = filtered[offset: offset + limit]
    items_out = []
    for p in page:
        r_avg, r_cnt = ratings.get(p.id, (None, 0))
        d = serialize_product_v1(p, rating_avg=r_avg, rating_count=r_cnt)
        d["soldLast30Days"] = popularity.get(p.id, 0)
        items_out.append(d)

    return ok(request, {"items": items_out, "total": total, "limit": limit, "offset": offset})

@router.get("/menu/items/{item_id}")
def get_menu_item_details_v1(item_id: int, request: Request, db: Session = Depends(get_db)):
    product = db.scalar(
        select(Product)
        .options(joinedload(Product.category), joinedload(Product.recipe_items).joinedload(RecipeItem.ingredient))
        .where(Product.id == item_id, Product.is_archived.is_(False))
    )
    if product is None:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")
    ratings = _product_ratings_map(db, [product.id])
    r_avg, r_cnt = ratings.get(product.id, (None, 0))
    in_win, win_qty = is_item_available_in_window(db, product)
    out = serialize_product_v1(product, rating_avg=r_avg, rating_count=r_cnt)
    out["isAvailable"] = bool(in_win)
    out["availableQuantity"] = win_qty
    out["verifiedIngredients"] = [
        {"name": ri.ingredient.name, "quantityPerServing": str(ri.quantity_per_serving), "unit": ri.ingredient.unit}
        for ri in product.recipe_items
        if ri.ingredient is not None
    ]
    recent_reviews = db.execute(
        select(OrderReview.rating, OrderReview.comment, OrderReview.created_at, User.full_name)
        .join(OrderItem, OrderItem.order_id == OrderReview.order_id)
        .join(User, User.id == OrderReview.user_id)
        .where(OrderItem.product_id == product.id)
        .order_by(OrderReview.created_at.desc())
        .limit(10)
    ).all()
    out["recentReviews"] = [
        {"rating": int(r.rating), "comment": r.comment, "author": r.full_name.split()[0], "createdAt": r.created_at}
        for r in recent_reviews
    ]
    return ok(request, out)

@router.get("/menu/availability")
def check_menu_availability_v1(
    request: Request,
    counterId: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
):
    stmt = select(Product).join(Product.category).where(Product.is_archived.is_(False), Category.is_active.is_(True))
    if counterId is not None:
        stmt = stmt.where(or_(Product.counter_id == counterId, Product.counter_id.is_(None)))
    products = db.scalars(stmt.order_by(Product.id.asc())).all()
    now_local = datetime.now(_canteen_tz())
    out = []
    for p in products:
        avail, qty = is_item_available_in_window(db, p, now_local)
        out.append({
            "menuItemId": p.id,
            "name": p.name,
            "counterId": p.counter_id,
            "isAvailable": avail,
            "availableQuantity": qty,
            "pricePaise": p.effective_price_paise,
        })
    return ok(request, {"checkedAt": datetime.now(timezone.utc), "items": out})

class CartItemUpsertIn(BaseModel):
    quantity: int = Field(ge=0, le=50)
    customizations: dict[str, Any] = Field(default_factory=dict)

class CartMetaPatchIn(BaseModel):
    counterId: int | None = Field(default=None, gt=0)
    couponCode: str | None = Field(default=None, max_length=50)
    pickupSlot: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=500)

def _get_or_create_cart(db: Session, user_id: int) -> Cart:
    cart = db.scalar(select(Cart).options(joinedload(Cart.items).joinedload(CartItem.product).joinedload(Product.category)).where(Cart.user_id == user_id))
    if cart is None:
        cart = Cart(user_id=user_id)
        db.add(cart)
        db.flush()
    return cart

def serialize_cart_v1(db: Session, cart: Cart) -> dict[str, Any]:
    lines = []
    subtotal_paise = 0
    for ci in cart.items:
        p = ci.product
        if p is None or p.is_archived:
            continue
        cust = parse_json_safe(ci.customizations_json, {})
        try:
            extra_paise, cleaned_cust = compute_customization_extra_paise(p, cust)
        except ApiError:
            extra_paise, cleaned_cust = 0, {}
        unit_p = p.effective_price_paise + extra_paise
        line_p = unit_p * ci.quantity
        avail, avail_qty = is_item_available_in_window(db, p)
        if avail and (avail_qty or 0) >= ci.quantity:
            subtotal_paise += line_p
        lines.append({
            "menuItemId": p.id,
            "name": p.name,
            "imageUrl": p.image_url,
            "quantity": ci.quantity,
            "customizations": cleaned_cust,
            "unitPricePaise": unit_p,
            "lineTotalPaise": line_p,
            "isAvailable": bool(avail and (avail_qty or 0) >= ci.quantity),
            "stockAvailable": avail_qty or 0,
            "preparationMinutes": p.preparation_minutes or 10,
        })
    discount_paise = 0
    coupon_error = None
    if cart.coupon_code and subtotal_paise > 0:
        try:
            discount_paise, _ = evaluate_discount_paise(db, cart.coupon_code, subtotal_paise)
        except ApiError as exc:
            coupon_error = exc.message
    taxable = max(0, subtotal_paise - discount_paise)
    tax_bps = get_tax_rate_bps(db)
    tax_paise = (taxable * tax_bps + 5000) // 10000
    total_paise = taxable + tax_paise
    return {
        "id": cart.id,
        "counterId": cart.counter_id,
        "couponCode": cart.coupon_code,
        "couponError": coupon_error,
        "pickupSlot": as_utc(cart.pickup_slot),
        "notes": cart.notes,
        "items": lines,
        "subtotalPaise": subtotal_paise,
        "discountPaise": discount_paise,
        "taxRateBps": tax_bps,
        "taxPaise": tax_paise,
        "totalPaise": total_paise,
        "updatedAt": as_utc(cart.updated_at),
    }

@router.get("/cart")
def get_cart_v1(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cart = _get_or_create_cart(db, user.id)
    db.commit()
    return ok(request, serialize_cart_v1(db, cart))

@router.patch("/cart")
def patch_cart_meta_v1(
    payload: CartMetaPatchIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cart = _get_or_create_cart(db, user.id)
    updates = payload.model_dump(exclude_unset=True)
    if "counterId" in updates:
        if updates["counterId"] is not None:
            counter = db.get(CanteenCounter, updates["counterId"])
            if counter is None or not counter.is_active:
                raise ApiError(404, "COUNTER_NOT_FOUND", "Active counter not found.")
        cart.counter_id = updates["counterId"]
    if "couponCode" in updates:
        cart.coupon_code = (updates["couponCode"] or "").strip().upper() or None
    if "notes" in updates:
        cart.notes = (updates["notes"] or "").strip()[:500] or None
    if "pickupSlot" in updates:
        raw_slot = updates["pickupSlot"]
        if not raw_slot:
            cart.pickup_slot = None
        else:
            try:
                dt = datetime.fromisoformat(raw_slot)
                if dt.tzinfo is None:
                    raise ValueError
                cart.pickup_slot = dt.astimezone(timezone.utc)
            except ValueError:
                raise ApiError(422, "INVALID_PICKUP_SLOT", "Pickup slot must be an ISO-8601 timestamp with timezone.")
    cart.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cart)
    return ok(request, serialize_cart_v1(db, _get_or_create_cart(db, user.id)))

@router.put("/cart/items/{item_id}")
def put_cart_item_v1(
    item_id: int,
    payload: CartItemUpsertIn,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cart = _get_or_create_cart(db, user.id)
    existing = next((ci for ci in cart.items if ci.product_id == item_id), None)
    if payload.quantity == 0:
        if existing is not None:
            db.delete(existing)
        cart.updated_at = datetime.now(timezone.utc)
        db.commit()
        return ok(request, serialize_cart_v1(db, _get_or_create_cart(db, user.id)))

    product = db.scalar(select(Product).options(joinedload(Product.category)).where(Product.id == item_id, Product.is_archived.is_(False)))
    if product is None or not product.category.is_active:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")
    avail, avail_qty = is_item_available_in_window(db, product)
    if not avail or (avail_qty or 0) < payload.quantity:
        raise ApiError(409, "ITEM_UNAVAILABLE", f"{product.name} is unavailable or has insufficient stock ({avail_qty or 0} available).")
    _, cleaned_cust = compute_customization_extra_paise(product, payload.customizations)

    if existing is None:
        db.add(CartItem(cart_id=cart.id, product_id=product.id, quantity=payload.quantity, customizations_json=json.dumps(cleaned_cust, separators=(",", ":"))))
    else:
        existing.quantity = payload.quantity
        existing.customizations_json = json.dumps(cleaned_cust, separators=(",", ":"))
    if cart.counter_id is None and product.counter_id is not None:
        cart.counter_id = product.counter_id
    cart.updated_at = datetime.now(timezone.utc)
    db.commit()
    return ok(request, serialize_cart_v1(db, _get_or_create_cart(db, user.id)))

@router.delete("/cart/items/{item_id}")
def delete_cart_item_v1(
    item_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cart = _get_or_create_cart(db, user.id)
    existing = next((ci for ci in cart.items if ci.product_id == item_id), None)
    if existing is not None:
        db.delete(existing)
        cart.updated_at = datetime.now(timezone.utc)
        db.commit()
    return ok(request, serialize_cart_v1(db, _get_or_create_cart(db, user.id)))

@router.delete("/cart")
def clear_cart_v1(request: Request, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cart = _get_or_create_cart(db, user.id)
    for ci in list(cart.items):
        db.delete(ci)
    cart.coupon_code = None
    cart.pickup_slot = None
    cart.notes = None
    cart.updated_at = datetime.now(timezone.utc)
    db.commit()
    return ok(request, serialize_cart_v1(db, _get_or_create_cart(db, user.id)))

@router.get("/pickup-slots")
def get_pickup_slots_v1(
    request: Request,
    counterId: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
):
    return ok(request, compute_counter_pickup_slots(db, counterId))

@router.get("/discounts/active")
def list_active_discounts_v1(request: Request, db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    rows = db.scalars(select(Discount).where(Discount.is_active.is_(True)).order_by(Discount.created_at.desc())).all()
    active = []
    for d in rows:
        if d.starts_at and as_utc(d.starts_at) > now:
            continue
        if d.ends_at and as_utc(d.ends_at) < now:
            continue
        if d.usage_limit is not None and d.usage_count >= d.usage_limit:
            continue
        active.append({
            "code": d.code,
            "description": d.description,
            "discountPercent": str(d.discount_percent),
            "discountPaise": rupees_to_paise(d.discount_amount),
            "minOrderPaise": rupees_to_paise(d.min_order_amount),
            "maxDiscountPaise": rupees_to_paise(d.max_discount_amount) if d.max_discount_amount else None,
        })
    return ok(request, {"items": active})

@router.get("/favorites")
def list_favorites_v1(request: Request, user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Favorite)
        .where(Favorite.user_id == user.id)
        .options(joinedload(Favorite.product).joinedload(Product.category))
        .order_by(Favorite.created_at.desc())
    ).unique().all()
    pids = [r.product_id for r in rows if r.product]
    ratings = _product_ratings_map(db, pids)
    return ok(
        request,
        {
            "items": [
                serialize_product_v1(r.product, rating_avg=ratings.get(r.product.id, (None, 0))[0], rating_count=ratings.get(r.product.id, (None, 0))[1])
                for r in rows
                if r.product and not r.product.is_archived
            ]
        },
    )

@router.post("/favorites/{item_id}", status_code=201)
def add_favorite_v1(item_id: int, request: Request, user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    product = db.scalar(select(Product).where(Product.id == item_id, Product.is_archived.is_(False)))
    if product is None:
        raise ApiError(404, "ITEM_NOT_FOUND", "Menu item not found.")
    existing = db.scalar(select(Favorite).where(Favorite.user_id == user.id, Favorite.product_id == item_id))
    if existing is None:
        db.add(Favorite(user_id=user.id, product_id=item_id))
        db.commit()
    return ok(request, {"saved": True, "menuItemId": item_id}, status_code=201)

@router.delete("/favorites/{item_id}")
def remove_favorite_v1(item_id: int, request: Request, user: User = Depends(require_roles("student")), db: Session = Depends(get_db)):
    fav = db.scalar(select(Favorite).where(Favorite.user_id == user.id, Favorite.product_id == item_id))
    if fav is not None:
        db.delete(fav)
        db.commit()
    return ok(request, {"saved": False, "menuItemId": item_id})
