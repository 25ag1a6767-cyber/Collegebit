"""CampusBite AI v2.0 — Advanced Admin Business Intelligence & Real-Time Analytics Engine.

Implements:
- 10 KPI Dashboard Cards (Today's revenue, Today's total orders, Successful payments,
  Failed payments, Pending orders, Completed orders, Cancelled orders, Average order value,
  Active customers, Estimated profit).
- 16 Interactive Graph Series (Daily, Weekly, Monthly, Yearly sales, Top-selling, Least-selling,
  Category pie, Peak hours, Customer purchase trends, Payment success/failure, Inventory consumption,
  Food wastage, Staff performance, Counter-wise sales, Revenue vs expenses, Profitability).
- 11 Filters (Today, Yesterday, Last 7 days, Last 30 days, This month, Last month, This year,
  Custom date range, Food category, Individual product, Counter).
- 4 Period Comparisons (Today vs Yesterday, This week vs Last week, This month vs Last month,
  This year vs Last year).

100% computed from real relational records; zero fabricated statistics.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db
from app.envelope import ApiError, ok
from app.models import (
    CanteenCounter,
    Category,
    Ingredient,
    InventoryMovement,
    InventoryMovementType,
    OperationalExpense,
    Order,
    OrderItem,
    OrderStatus,
    OrderStatusHistory,
    Payment,
    PaymentStatus,
    Pickup,
    Product,
    RecipeItem,
    Refund,
    StockMovement,
    User,
    UserRole,
    paise_to_rupees,
    rupees_to_paise,
)
from app.modules.common import as_utc
from app.security import require_roles

router = APIRouter(prefix="/admin/analytics", tags=["v1-admin-analytics-bi"])

VALID_PRESETS = {
    "today",
    "yesterday",
    "last_7_days",
    "last_30_days",
    "this_month",
    "last_month",
    "this_year",
    "custom",
}

VALID_COMPARISONS = {
    "today_vs_yesterday",
    "this_week_vs_last_week",
    "this_month_vs_last_month",
    "this_year_vs_last_year",
}

def resolve_filter_dates(
    preset: str,
    start_date: date | None,
    end_date: date | None,
    today_local: date,
) -> tuple[date, date]:
    p = (preset or "last_30_days").strip().lower()
    if p not in VALID_PRESETS:
        raise ApiError(422, "INVALID_PRESET", f"Unsupported date preset '{preset}'.")
    if p == "today":
        return today_local, today_local
    if p == "yesterday":
        yd = today_local - timedelta(days=1)
        return yd, yd
    if p == "last_7_days":
        return today_local - timedelta(days=6), today_local
    if p == "last_30_days":
        return today_local - timedelta(days=29), today_local
    if p == "this_month":
        return date(today_local.year, today_local.month, 1), today_local
    if p == "last_month":
        first_this = date(today_local.year, today_local.month, 1)
        last_prev = first_this - timedelta(days=1)
        return date(last_prev.year, last_prev.month, 1), last_prev
    if p == "this_year":
        return date(today_local.year, 1, 1), today_local

    s = start_date or (today_local - timedelta(days=29))
    e = end_date or today_local
    if s > e:
        raise ApiError(422, "INVALID_DATE_RANGE", "startDate cannot be after endDate.")
    return s, e

def compute_unit_cost_map(db: Session) -> dict[int, int]:
    """Compute verified unit cost in paise per product from product.cost_paise or recipe ingredients."""
    products = db.scalars(
        select(Product).options(joinedload(Product.recipe_items).joinedload(RecipeItem.ingredient))
    ).unique().all()
    cost_map: dict[int, int] = {}
    for p in products:
        if p.cost_paise is not None and int(p.cost_paise) > 0:
            cost_map[p.id] = int(p.cost_paise)
        else:
            recipe_paise = 0
            for ri in p.recipe_items:
                if ri.ingredient and ri.ingredient.cost_per_unit is not None:
                    recipe_paise += rupees_to_paise(Decimal(ri.quantity_per_serving) * Decimal(ri.ingredient.cost_per_unit))
            cost_map[p.id] = recipe_paise
    return cost_map

def _local_date_of(dt: datetime | None, tz: ZoneInfo, fallback: date) -> date:
    u = as_utc(dt)
    if u is None:
        return fallback
    return u.astimezone(tz).date()

def _order_matches_item_filter(
    order: Order,
    product_category_map: dict[int, int],
    category_id: int | None,
    product_id: int | None,
) -> tuple[bool, int, int]:
    """Return (matches, matched_revenue_paise, matched_cogs_paise_placeholder) for an order under item/category filter."""
    if category_id is None and product_id is None:
        return True, order.effective_total_paise, 0
    matched_paise = 0
    matched_any = False
    for it in order.items:
        if product_id is not None and it.product_id != product_id:
            continue
        if category_id is not None and product_category_map.get(it.product_id) != category_id:
            continue
        matched_any = True
        lp = it.line_total_paise if it.line_total_paise is not None else rupees_to_paise(it.line_total)
        matched_paise += int(lp)
    return matched_any, matched_paise, 0

def _pct_delta(current: int | float, previous: int | float) -> float | None:
    if previous == 0:
        return 100.0 if current > 0 else 0.0
    return round(((float(current) - float(previous)) / abs(float(previous))) * 100.0, 2)

def _summarize_window(
    orders: list[Order],
    expenses: list[OperationalExpense],
    prod_waste: list[InventoryMovement],
    ing_waste: list[StockMovement],
    refunds: list[Refund],
    unit_cost_map: dict[int, int],
    product_category_map: dict[int, int],
    product_price_map: dict[int, int],
    tz: ZoneInfo,
    start_d: date,
    end_d: date,
    category_id: int | None = None,
    product_id: int | None = None,
    counter_id: int | None = None,
) -> dict[str, Any]:
    rev_paise = 0
    total_orders = 0
    paid_orders = 0
    failed_orders = 0
    pending_orders = 0
    completed_orders = 0
    cancelled_orders = 0
    cogs_paise = 0
    customers: set[int] = set()

    for o in orders:
        if counter_id is not None and o.counter_id != counter_id:
            continue
        od = _local_date_of(o.created_at, tz, start_d)
        if od < start_d or od > end_d:
            continue
        matches, matched_rev, _ = _order_matches_item_filter(o, product_category_map, category_id, product_id)
        if not matches:
            continue

        total_orders += 1
        customers.add(o.user_id)
        if o.payment_status == PaymentStatus.PAID:
            paid_orders += 1
            rev_paise += matched_rev
            for it in o.items:
                if product_id is not None and it.product_id != product_id:
                    continue
                if category_id is not None and product_category_map.get(it.product_id) != category_id:
                    continue
                cogs_paise += int(it.quantity) * unit_cost_map.get(it.product_id, 0)
        elif o.payment_status in {PaymentStatus.FAILED, PaymentStatus.EXPIRED}:
            failed_orders += 1

        if o.status in {OrderStatus.PENDING, OrderStatus.CONFIRMED, OrderStatus.ACCEPTED, OrderStatus.PREPARING}:
            pending_orders += 1
        elif o.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED}:
            completed_orders += 1
        elif o.status in {OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}:
            cancelled_orders += 1

    refund_paise = 0
    if category_id is None and product_id is None:
        for rf in refunds:
            rd = _local_date_of(rf.created_at, tz, start_d)
            if start_d <= rd <= end_d and rf.status in {"PROCESSED", "SUCCESS"}:
                if counter_id is not None and rf.order and rf.order.counter_id != counter_id:
                    continue
                refund_paise += int(rf.amount_paise or rupees_to_paise(rf.amount))

    op_exp_paise = 0
    if category_id is None and product_id is None:
        for ex in expenses:
            if start_d <= ex.expense_date <= end_d:
                if counter_id is not None and ex.counter_id is not None and ex.counter_id != counter_id:
                    continue
                op_exp_paise += int(ex.amount_paise)

    waste_paise = 0
    for mv in prod_waste:
        md = _local_date_of(mv.created_at, tz, start_d)
        if not (start_d <= md <= end_d):
            continue
        if product_id is not None and mv.product_id != product_id:
            continue
        if category_id is not None and product_category_map.get(mv.product_id) != category_id:
            continue
        unit_loss = unit_cost_map.get(mv.product_id, 0) or product_price_map.get(mv.product_id, 0)
        waste_paise += abs(int(mv.quantity_delta)) * unit_loss

    if category_id is None and product_id is None:
        for sm in ing_waste:
            sd = _local_date_of(sm.created_at, tz, start_d)
            if start_d <= sd <= end_d and sm.ingredient and sm.ingredient.cost_per_unit is not None:
                waste_paise += rupees_to_paise(abs(Decimal(sm.quantity_delta)) * Decimal(sm.ingredient.cost_per_unit))

    total_expenses_paise = cogs_paise + op_exp_paise + waste_paise + refund_paise
    profit_paise = rev_paise - total_expenses_paise
    aov_paise = int(round(rev_paise / paid_orders)) if paid_orders > 0 else 0

    return {
        "startDate": start_d.isoformat(),
        "endDate": end_d.isoformat(),
        "revenuePaise": rev_paise,
        "revenueRupees": str(paise_to_rupees(rev_paise)),
        "totalOrders": total_orders,
        "paidOrders": paid_orders,
        "failedPayments": failed_orders,
        "pendingOrders": pending_orders,
        "completedOrders": completed_orders,
        "cancelledOrders": cancelled_orders,
        "averageOrderValuePaise": aov_paise,
        "activeCustomers": len(customers),
        "cogsPaise": cogs_paise,
        "operatingExpensePaise": op_exp_paise,
        "wastageLossPaise": waste_paise,
        "refundPaise": refund_paise,
        "totalExpensesPaise": total_expenses_paise,
        "estimatedProfitPaise": profit_paise,
    }

def _build_comparison(cur: dict[str, Any], prev: dict[str, Any], label: str) -> dict[str, Any]:
    return {
        "label": label,
        "current": cur,
        "previous": prev,
        "deltas": {
            "revenuePct": _pct_delta(cur["revenuePaise"], prev["revenuePaise"]),
            "ordersPct": _pct_delta(cur["totalOrders"], prev["totalOrders"]),
            "paidOrdersPct": _pct_delta(cur["paidOrders"], prev["paidOrders"]),
            "profitPct": _pct_delta(cur["estimatedProfitPaise"], prev["estimatedProfitPaise"]),
            "aovPct": _pct_delta(cur["averageOrderValuePaise"], prev["averageOrderValuePaise"]),
            "activeCustomersPct": _pct_delta(cur["activeCustomers"], prev["activeCustomers"]),
        },
    }

@router.get("/dashboard")
def get_admin_bi_dashboard_v1(
    request: Request,
    preset: str = Query(default="last_30_days"),
    startDate: date | None = None,
    endDate: date | None = None,
    categoryId: int | None = Query(default=None, gt=0),
    productId: int | None = Query(default=None, gt=0),
    counterId: int | None = Query(default=None, gt=0),
    compare: str = Query(default="today_vs_yesterday"),
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    tz = ZoneInfo(cfg.canteen_timezone)
    today_local = datetime.now(tz).date()

    if compare not in VALID_COMPARISONS:
        raise ApiError(422, "INVALID_COMPARISON", f"Unsupported comparison '{compare}'.")

    start_d, end_d = resolve_filter_dates(preset, startDate, endDate, today_local)

    categories = db.scalars(select(Category).order_by(Category.name.asc())).all()
    cat_name_map = {c.id: c.name for c in categories}
    counters = db.scalars(select(CanteenCounter).order_by(CanteenCounter.id.asc())).all()
    counter_name_map = {c.id: c.name for c in counters}
    products = db.scalars(select(Product).options(joinedload(Product.category)).where(Product.is_archived.is_(False))).all()
    product_category_map = {p.id: p.category_id for p in products}
    product_name_map = {p.id: p.name for p in products}
    product_price_map = {p.id: p.effective_price_paise for p in products}
    unit_cost_map = compute_unit_cost_map(db)

    staff_users = db.scalars(select(User).where(User.role.in_([UserRole.STAFF, UserRole.ADMIN]))).all()
    staff_name_map = {u.id: u.full_name for u in staff_users}

    earliest_needed = min(start_d, date(today_local.year - 1, 1, 1))
    earliest_utc = datetime(earliest_needed.year, earliest_needed.month, earliest_needed.day, tzinfo=timezone.utc) - timedelta(days=1)

    all_orders = db.scalars(
        select(Order)
        .options(joinedload(Order.items), joinedload(Order.payments))
        .where(Order.created_at >= earliest_utc)
        .order_by(Order.created_at.asc())
    ).unique().all()

    all_payments = db.scalars(
        select(Payment).options(joinedload(Payment.order)).where(Payment.created_at >= earliest_utc)
    ).unique().all()

    all_refunds = db.scalars(
        select(Refund).options(joinedload(Refund.order)).where(Refund.created_at >= earliest_utc)
    ).all()

    all_expenses = db.scalars(
        select(OperationalExpense).where(OperationalExpense.expense_date >= earliest_needed)
    ).all()

    all_prod_movements = db.scalars(
        select(InventoryMovement)
        .options(joinedload(InventoryMovement.product))
        .where(InventoryMovement.created_at >= earliest_utc)
    ).all()
    prod_waste = [m for m in all_prod_movements if m.movement_type == InventoryMovementType.WASTE]

    all_ing_movements = db.scalars(
        select(StockMovement)
        .options(joinedload(StockMovement.ingredient))
        .where(StockMovement.created_at >= earliest_utc)
    ).all()
    ing_waste = [m for m in all_ing_movements if m.movement_type == "wastage"]

    today_summary = _summarize_window(
        all_orders, all_expenses, prod_waste, ing_waste, all_refunds,
        unit_cost_map, product_category_map, product_price_map,
        tz, today_local, today_local, categoryId, productId, counterId,
    )
    window_summary = _summarize_window(
        all_orders, all_expenses, prod_waste, ing_waste, all_refunds,
        unit_cost_map, product_category_map, product_price_map,
        tz, start_d, end_d, categoryId, productId, counterId,
    )

    succ_payments_cnt = 0
    fail_payments_cnt = 0
    pend_payments_cnt = 0
    refund_payments_cnt = 0
    payment_daily: dict[str, dict[str, int]] = defaultdict(lambda: {"success": 0, "failed": 0, "pending": 0, "refunded": 0})

    for pay in all_payments:
        pd = _local_date_of(pay.created_at, tz, start_d)
        if not (start_d <= pd <= end_d):
            continue
        if counterId is not None and pay.order and pay.order.counter_id != counterId:
            continue
        if pay.order is not None and (categoryId is not None or productId is not None):
            ok_match, _, _ = _order_matches_item_filter(pay.order, product_category_map, categoryId, productId)
            if not ok_match:
                continue
        dk = pd.isoformat()
        st = (pay.status or "").upper()
        if st == "SUCCESS":
            succ_payments_cnt += 1
            payment_daily[dk]["success"] += 1
        elif st in {"FAILED", "EXPIRED", "CANCELLED"}:
            fail_payments_cnt += 1
            payment_daily[dk]["failed"] += 1
        elif st in {"REFUNDED", "PARTIALLY_REFUNDED", "REFUND_PENDING"}:
            refund_payments_cnt += 1
            payment_daily[dk]["refunded"] += 1
        else:
            pend_payments_cnt += 1
            payment_daily[dk]["pending"] += 1

    if succ_payments_cnt == 0 and window_summary["paidOrders"] > 0:
        succ_payments_cnt = window_summary["paidOrders"]
    if fail_payments_cnt == 0 and window_summary["failedPayments"] > 0:
        fail_payments_cnt = window_summary["failedPayments"]

    registered_active_students = len(
        db.scalars(select(User.id).where(User.role == UserRole.STUDENT, User.is_active.is_(True))).all()
    )

    cards = {
        "todayRevenuePaise": today_summary["revenuePaise"],
        "todayRevenueRupees": today_summary["revenueRupees"],
        "todayTotalOrders": today_summary["totalOrders"],
        "filteredRevenuePaise": window_summary["revenuePaise"],
        "filteredRevenueRupees": window_summary["revenueRupees"],
        "filteredTotalOrders": window_summary["totalOrders"],
        "successfulPayments": succ_payments_cnt,
        "failedPayments": fail_payments_cnt,
        "pendingOrders": window_summary["pendingOrders"],
        "completedOrders": window_summary["completedOrders"],
        "cancelledOrders": window_summary["cancelledOrders"],
        "averageOrderValuePaise": window_summary["averageOrderValuePaise"],
        "averageOrderValueRupees": str(paise_to_rupees(window_summary["averageOrderValuePaise"])),
        "activeCustomers": window_summary["activeCustomers"],
        "registeredActiveStudents": registered_active_students,
        "estimatedProfitPaise": window_summary["estimatedProfitPaise"],
        "estimatedProfitRupees": str(paise_to_rupees(window_summary["estimatedProfitPaise"])),
        "totalExpensesPaise": window_summary["totalExpensesPaise"],
        "cogsPaise": window_summary["cogsPaise"],
        "wastageLossPaise": window_summary["wastageLossPaise"],
        "operatingExpensePaise": window_summary["operatingExpensePaise"],
        "refundPaise": window_summary["refundPaise"],
    }

    yesterday_d = today_local - timedelta(days=1)
    week_start = today_local - timedelta(days=today_local.weekday())
    last_week_start = week_start - timedelta(days=7)
    last_week_end = week_start - timedelta(days=1)
    month_start = date(today_local.year, today_local.month, 1)
    last_month_end = month_start - timedelta(days=1)
    last_month_start = date(last_month_end.year, last_month_end.month, 1)
    year_start = date(today_local.year, 1, 1)
    last_year_start = date(today_local.year - 1, 1, 1)
    last_year_end = date(today_local.year - 1, 12, 31)

    def _sum_range(s: date, e: date) -> dict[str, Any]:
        return _summarize_window(
            all_orders, all_expenses, prod_waste, ing_waste, all_refunds,
            unit_cost_map, product_category_map, product_price_map,
            tz, s, e, categoryId, productId, counterId,
        )

    comparisons = {
        "today_vs_yesterday": _build_comparison(today_summary, _sum_range(yesterday_d, yesterday_d), "Today vs Yesterday"),
        "this_week_vs_last_week": _build_comparison(_sum_range(week_start, today_local), _sum_range(last_week_start, last_week_end), "This Week vs Last Week"),
        "this_month_vs_last_month": _build_comparison(_sum_range(month_start, today_local), _sum_range(last_month_start, last_month_end), "This Month vs Last Month"),
        "this_year_vs_last_year": _build_comparison(_sum_range(year_start, today_local), _sum_range(last_year_start, last_year_end), "This Year vs Last Year"),
    }

    daily_rev: dict[str, int] = defaultdict(int)
    daily_orders_cnt: dict[str, int] = defaultdict(int)
    daily_cogs: dict[str, int] = defaultdict(int)
    daily_buyers: dict[str, set[int]] = defaultdict(set)
    daily_items_qty: dict[str, int] = defaultdict(int)

    weekly_rev: dict[str, int] = defaultdict(int)
    weekly_orders: dict[str, int] = defaultdict(int)
    monthly_rev: dict[str, int] = defaultdict(int)
    monthly_orders: dict[str, int] = defaultdict(int)
    yearly_rev: dict[str, int] = defaultdict(int)
    yearly_orders: dict[str, int] = defaultdict(int)

    item_units: dict[int, int] = defaultdict(int)
    item_rev: dict[int, int] = defaultdict(int)
    cat_units: dict[int, int] = defaultdict(int)
    cat_rev: dict[int, int] = defaultdict(int)
    hour_orders: dict[int, int] = {h: 0 for h in range(24)}
    hour_rev: dict[int, int] = {h: 0 for h in range(24)}
    counter_orders: dict[int, int] = defaultdict(int)
    counter_rev: dict[int, int] = defaultdict(int)
    customer_order_counts: dict[int, int] = defaultdict(int)
    staff_orders_handled: dict[int, int] = defaultdict(int)
    staff_prep_minutes: dict[int, list[float]] = defaultdict(list)

    if (end_d - start_d).days <= 366:
        cursor = start_d
        while cursor <= end_d:
            dk = cursor.isoformat()
            daily_rev[dk] = 0
            daily_orders_cnt[dk] = 0
            cursor += timedelta(days=1)

    for o in all_orders:
        if counterId is not None and o.counter_id != counterId:
            continue
        u_dt = as_utc(o.created_at)
        local_dt = u_dt.astimezone(tz) if u_dt else datetime(today_local.year, today_local.month, today_local.day, tzinfo=tz)
        od = local_dt.date()

        matches, matched_rev, _ = _order_matches_item_filter(o, product_category_map, categoryId, productId)
        if not matches:
            continue

        if o.payment_status == PaymentStatus.PAID:
            iso_yr, iso_wk, _ = od.isocalendar()
            wk_key = f"{iso_yr}-W{iso_wk:02d}"
            mo_key = f"{od.year:04d}-{od.month:02d}"
            yr_key = f"{od.year:04d}"
            if od >= today_local - timedelta(days=84):
                weekly_rev[wk_key] += matched_rev
                weekly_orders[wk_key] += 1
            if od >= date(today_local.year - 1, 1, 1):
                monthly_rev[mo_key] += matched_rev
                monthly_orders[mo_key] += 1
                yearly_rev[yr_key] += matched_rev
                yearly_orders[yr_key] += 1

        if not (start_d <= od <= end_d):
            continue

        dk = od.isoformat()
        if o.payment_status == PaymentStatus.PAID:
            daily_rev[dk] += matched_rev
            daily_orders_cnt[dk] += 1
            daily_buyers[dk].add(o.user_id)
            customer_order_counts[o.user_id] += 1
            hour_orders[local_dt.hour] += 1
            hour_rev[local_dt.hour] += matched_rev
            cid = o.counter_id or 0
            counter_orders[cid] += 1
            counter_rev[cid] += matched_rev

            if o.handled_by_staff_id:
                staff_orders_handled[o.handled_by_staff_id] += 1
                if o.ready_at and o.created_at:
                    r_utc = as_utc(o.ready_at)
                    c_utc = as_utc(o.created_at)
                    if r_utc and c_utc and r_utc > c_utc:
                        mins = (r_utc - c_utc).total_seconds() / 60.0
                        if 0.5 <= mins <= 180:
                            staff_prep_minutes[o.handled_by_staff_id].append(mins)

            for it in o.items:
                if productId is not None and it.product_id != productId:
                    continue
                p_cat = product_category_map.get(it.product_id, 0)
                if categoryId is not None and p_cat != categoryId:
                    continue
                lp = it.line_total_paise if it.line_total_paise is not None else rupees_to_paise(it.line_total)
                item_units[it.product_id] += int(it.quantity)
                item_rev[it.product_id] += int(lp)
                cat_units[p_cat] += int(it.quantity)
                cat_rev[p_cat] += int(lp)
                daily_items_qty[dk] += int(it.quantity)
                daily_cogs[dk] += int(it.quantity) * unit_cost_map.get(it.product_id, 0)

    hist_rows = db.scalars(
        select(OrderStatusHistory).where(
            OrderStatusHistory.actor_user_id.is_not(None),
            OrderStatusHistory.created_at >= datetime(start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc) - timedelta(days=1),
        )
    ).all()
    seen_staff_order_pairs: set[tuple[int, int]] = set()
    for h in hist_rows:
        hd = _local_date_of(h.created_at, tz, start_d)
        if start_d <= hd <= end_d and h.actor_user_id in staff_name_map:
            if h.new_status.upper() in {"ACCEPTED", "PREPARING", "READY", "COLLECTED"}:
                pair = (int(h.actor_user_id), int(h.order_id))
                if pair not in seen_staff_order_pairs:
                    seen_staff_order_pairs.add(pair)
                    if staff_orders_handled.get(int(h.actor_user_id), 0) == 0:
                        staff_orders_handled[int(h.actor_user_id)] += 1

    pickup_rows = db.scalars(
        select(Pickup).where(
            Pickup.collected_by.is_not(None),
            Pickup.collected_at >= datetime(start_d.year, start_d.month, start_d.day, tzinfo=timezone.utc) - timedelta(days=1),
        )
    ).all()
    staff_collections: dict[int, int] = defaultdict(int)
    for pk in pickup_rows:
        pkd = _local_date_of(pk.collected_at, tz, start_d)
        if start_d <= pkd <= end_d and pk.collected_by:
            staff_collections[int(pk.collected_by)] += 1

    daily_op_exp: dict[str, int] = defaultdict(int)
    if categoryId is None and productId is None:
        for ex in all_expenses:
            if start_d <= ex.expense_date <= end_d:
                if counterId is not None and ex.counter_id is not None and ex.counter_id != counterId:
                    continue
                daily_op_exp[ex.expense_date.isoformat()] += int(ex.amount_paise)

    daily_waste_paise: dict[str, int] = defaultdict(int)
    waste_by_item: dict[str, dict[str, Any]] = defaultdict(lambda: {"units": 0.0, "lossPaise": 0, "unit": "units"})
    for mv in prod_waste:
        md = _local_date_of(mv.created_at, tz, start_d)
        if not (start_d <= md <= end_d):
            continue
        if productId is not None and mv.product_id != productId:
            continue
        if categoryId is not None and product_category_map.get(mv.product_id) != categoryId:
            continue
        u_loss = unit_cost_map.get(mv.product_id, 0) or product_price_map.get(mv.product_id, 0)
        loss_p = abs(int(mv.quantity_delta)) * u_loss
        daily_waste_paise[md.isoformat()] += loss_p
        nm = mv.product.name if mv.product else f"Item #{mv.product_id}"
        waste_by_item[nm]["units"] += abs(int(mv.quantity_delta))
        waste_by_item[nm]["lossPaise"] += loss_p

    if categoryId is None and productId is None:
        for sm in ing_waste:
            sd = _local_date_of(sm.created_at, tz, start_d)
            if start_d <= sd <= end_d:
                loss_p = 0
                if sm.ingredient and sm.ingredient.cost_per_unit is not None:
                    loss_p = rupees_to_paise(abs(Decimal(sm.quantity_delta)) * Decimal(sm.ingredient.cost_per_unit))
                daily_waste_paise[sd.isoformat()] += loss_p
                nm = f"{sm.ingredient.name} ({sm.ingredient.unit})" if sm.ingredient else f"Ingredient #{sm.ingredient_id}"
                waste_by_item[nm]["units"] += float(abs(Decimal(sm.quantity_delta)))
                waste_by_item[nm]["lossPaise"] += loss_p
                waste_by_item[nm]["unit"] = sm.ingredient.unit if sm.ingredient else ""

    consumption_by_item: dict[str, dict[str, Any]] = defaultdict(lambda: {"consumed": 0.0, "unit": "units", "kind": "product"})
    for mv in all_prod_movements:
        if mv.movement_type != InventoryMovementType.RESERVATION:
            continue
        md = _local_date_of(mv.created_at, tz, start_d)
        if not (start_d <= md <= end_d):
            continue
        if productId is not None and mv.product_id != productId:
            continue
        if categoryId is not None and product_category_map.get(mv.product_id) != categoryId:
            continue
        nm = mv.product.name if mv.product else product_name_map.get(mv.product_id, f"Item #{mv.product_id}")
        consumption_by_item[nm]["consumed"] += abs(int(mv.quantity_delta))

    if categoryId is None and productId is None:
        for sm in all_ing_movements:
            if sm.movement_type != "consumption":
                continue
            sd = _local_date_of(sm.created_at, tz, start_d)
            if start_d <= sd <= end_d and sm.ingredient:
                key = f"{sm.ingredient.name} ({sm.ingredient.unit})"
                consumption_by_item[key]["consumed"] += float(abs(Decimal(sm.quantity_delta)))
                consumption_by_item[key]["unit"] = sm.ingredient.unit
                consumption_by_item[key]["kind"] = "ingredient"

    catalog_candidates = [
        p for p in products
        if (categoryId is None or p.category_id == categoryId)
        and (productId is None or p.id == productId)
        and (counterId is None or p.counter_id is None or p.counter_id == counterId)
    ]
    ranked_products = [
        {
            "productId": p.id,
            "name": p.name,
            "categoryName": cat_name_map.get(p.category_id, "General"),
            "unitsSold": item_units.get(p.id, 0),
            "revenuePaise": item_rev.get(p.id, 0),
        }
        for p in catalog_candidates
    ]
    top_selling = sorted(ranked_products, key=lambda x: (-x["unitsSold"], -x["revenuePaise"], x["name"]))[:10]
    least_selling = sorted(ranked_products, key=lambda x: (x["unitsSold"], x["revenuePaise"], x["name"]))[:10]

    total_cat_rev = sum(cat_rev.values()) or 1
    category_pie = [
        {
            "categoryId": cid,
            "categoryName": cat_name_map.get(cid, "Uncategorized"),
            "unitsSold": cat_units[cid],
            "revenuePaise": rev,
            "sharePct": round(rev * 100.0 / total_cat_rev, 2),
        }
        for cid, rev in sorted(cat_rev.items(), key=lambda kv: -kv[1])
    ]

    sorted_days = sorted(daily_rev.keys())
    rev_vs_exp_series = []
    profitability_series = []
    for dk in sorted_days:
        r_p = daily_rev.get(dk, 0)
        exp_p = daily_cogs.get(dk, 0) + daily_op_exp.get(dk, 0) + daily_waste_paise.get(dk, 0)
        prof_p = r_p - exp_p
        margin_pct = round((prof_p * 100.0 / r_p), 2) if r_p > 0 else 0.0
        rev_vs_exp_series.append({
            "date": dk,
            "revenuePaise": r_p,
            "expensesPaise": exp_p,
            "cogsPaise": daily_cogs.get(dk, 0),
            "operatingExpensePaise": daily_op_exp.get(dk, 0),
            "wastagePaise": daily_waste_paise.get(dk, 0),
        })
        profitability_series.append({
            "date": dk,
            "profitPaise": prof_p,
            "revenuePaise": r_p,
            "marginPct": margin_pct,
        })

    repeat_customers = sum(1 for cnt in customer_order_counts.values() if cnt >= 2)
    one_time_customers = sum(1 for cnt in customer_order_counts.values() if cnt == 1)

    staff_perf_series = []
    for uid, s_name in sorted(staff_name_map.items(), key=lambda kv: kv[1]):
        handled = staff_orders_handled.get(uid, 0)
        colls = staff_collections.get(uid, 0)
        p_mins = staff_prep_minutes.get(uid, [])
        avg_prep = round(sum(p_mins) / len(p_mins), 2) if p_mins else None
        if handled > 0 or colls > 0 or len(staff_name_map) <= 10:
            staff_perf_series.append({
                "staffId": uid,
                "staffName": s_name,
                "ordersHandled": handled,
                "ordersCollected": colls,
                "avgPrepMinutes": avg_prep,
            })
    staff_perf_series.sort(key=lambda x: (-x["ordersHandled"], -x["ordersCollected"], x["staffName"]))

    counter_sales_series = []
    seen_cids = set(counter_name_map.keys()) | set(counter_rev.keys())
    for cid in sorted(seen_cids):
        if cid == 0 and counter_orders.get(0, 0) == 0:
            continue
        counter_sales_series.append({
            "counterId": cid,
            "counterName": counter_name_map.get(cid, "Unassigned Counter"),
            "orders": counter_orders.get(cid, 0),
            "revenuePaise": counter_rev.get(cid, 0),
        })

    graphs = {
        "dailySales": [
            {"date": dk, "revenuePaise": daily_rev[dk], "orders": daily_orders_cnt[dk]}
            for dk in sorted_days
        ],
        "weeklySales": [
            {"week": wk, "revenuePaise": weekly_rev[wk], "orders": weekly_orders[wk]}
            for wk in sorted(weekly_rev.keys())
        ],
        "monthlySales": [
            {"month": mk, "revenuePaise": monthly_rev[mk], "orders": monthly_orders[mk]}
            for mk in sorted(monthly_rev.keys())
        ],
        "yearlySales": [
            {"year": yk, "revenuePaise": yearly_rev[yk], "orders": yearly_orders[yk]}
            for yk in sorted(yearly_rev.keys())
        ],
        "topSellingFoods": top_selling,
        "leastSellingFoods": least_selling,
        "categorySalesPie": category_pie,
        "peakOrderingHours": [
            {"hour": f"{h:02d}:00", "orders": hour_orders[h], "revenuePaise": hour_rev[h]}
            for h in range(24)
        ],
        "customerPurchaseTrends": {
            "dailySeries": [
                {
                    "date": dk,
                    "uniqueBuyers": len(daily_buyers.get(dk, set())),
                    "orders": daily_orders_cnt.get(dk, 0),
                    "itemsPurchased": daily_items_qty.get(dk, 0),
                }
                for dk in sorted_days
            ],
            "repeatBuyers": repeat_customers,
            "firstTimeOrSingleBuyers": one_time_customers,
        },
        "paymentSuccessFailure": {
            "totals": [
                {"status": "SUCCESS", "count": succ_payments_cnt},
                {"status": "FAILED", "count": fail_payments_cnt},
                {"status": "PENDING", "count": pend_payments_cnt},
                {"status": "REFUNDED", "count": refund_payments_cnt},
            ],
            "dailySeries": [
                {"date": dk, **payment_daily[dk]}
                for dk in sorted(payment_daily.keys())
            ],
        },
        "inventoryConsumption": sorted(
            [
                {"name": k, "consumed": round(v["consumed"], 2), "unit": v["unit"], "kind": v["kind"]}
                for k, v in consumption_by_item.items()
            ],
            key=lambda x: -x["consumed"],
        )[:15],
        "foodWastage": {
            "dailySeries": [{"date": dk, "lossPaise": daily_waste_paise.get(dk, 0)} for dk in sorted_days],
            "byItem": sorted(
                [{"name": k, "units": round(v["units"], 2), "unit": v["unit"], "lossPaise": v["lossPaise"]} for k, v in waste_by_item.items()],
                key=lambda x: -x["lossPaise"],
            )[:12],
        },
        "staffPerformance": staff_perf_series,
        "counterWiseSales": counter_sales_series,
        "revenueVsExpenses": rev_vs_exp_series,
        "profitability": profitability_series,
    }

    return ok(
        request,
        {
            "source": "POSTGRESQL_RELATIONAL_ACTUALS",
            "dataKind": "ACTUAL",
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "filtersApplied": {
                "preset": preset,
                "startDate": start_d.isoformat(),
                "endDate": end_d.isoformat(),
                "categoryId": categoryId,
                "productId": productId,
                "counterId": counterId,
                "compare": compare,
            },
            "filterOptions": {
                "presets": sorted(VALID_PRESETS),
                "comparisons": sorted(VALID_COMPARISONS),
                "categories": [{"id": c.id, "name": c.name} for c in categories],
                "products": [{"id": p.id, "name": p.name, "categoryId": p.category_id} for p in products],
                "counters": [{"id": c.id, "name": c.name} for c in counters],
            },
            "cards": cards,
            "activeComparison": comparisons[compare],
            "comparisons": comparisons,
            "graphs": graphs,
        },
    )
