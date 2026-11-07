"""/api/v1 Production AI Services: Recommendations, Grounded Menu Assistant (Gemini + Fallback), Demand Forecasting, Waste Insights & ETA."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import logging
import math
import re
from threading import Lock
import time
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
import httpx
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import get_db
from app.envelope import ApiError, ok
from app.models import (
    AIInteraction,
    Favorite,
    ForecastItem,
    ForecastRun,
    Ingredient,
    InventoryMovement,
    InventoryMovementType,
    Order,
    OrderItem,
    OrderReview,
    PaymentStatus,
    Product,
    ProductBatch,
    RecipeItem,
    StockMovement,
    User,
    UserRole,
    paise_to_rupees,
    rupees_to_paise,
)
from app.modules.common import (
    as_utc,
    estimate_counter_wait_minutes,
    parse_json_safe,
    parse_tags,
    record_audit,
    serialize_product_v1,
)
from app.modules.menu_cart import _product_popularity_map, _product_ratings_map, is_item_available_in_window
from app.security import get_optional_user, require_roles

logger = logging.getLogger("ace_canteen.ai")
router = APIRouter(prefix="/ai", tags=["v1-ai"])

_AI_LIMIT = 25
_AI_WINDOW_SECONDS = 300
_ai_buckets: dict[str, deque[float]] = defaultdict(deque)
_ai_lock = Lock()

def _enforce_ai_rate_limit(request: Request, user: User | None) -> None:
    key = f"user:{user.id}" if user is not None else f"ip:{request.client.host if request.client else 'anon'}"
    now = time.monotonic()
    with _ai_lock:
        dq = _ai_buckets[key]
        while dq and now - dq[0] >= _AI_WINDOW_SECONDS:
            dq.popleft()
        if len(dq) >= _AI_LIMIT:
            raise ApiError(429, "AI_RATE_LIMITED", "Too many AI assistant requests. Please wait a moment and try again.")
        dq.append(now)

def _record_ai_telemetry(
    db: Session,
    *,
    user_id: int | None,
    feature: str,
    model_version: str,
    raw_input_for_hash: str,
    metadata: dict[str, Any],
) -> None:
    """Record privacy-safe AI telemetry: stores SHA-256 input digest and non-PII metadata only."""
    digest = hashlib.sha256(raw_input_for_hash.encode("utf-8")).hexdigest()
    db.add(
        AIInteraction(
            user_id=user_id,
            feature=feature[:50],
            model_version=model_version[:60],
            input_hash=digest,
            prompt=None,
            response_json=None,
            output_metadata_json=json.dumps(metadata, separators=(",", ":"), default=str),
        )
    )

@router.get("/recommendations")
def get_ai_recommendations_v1(
    request: Request,
    counterId: int | None = Query(default=None, gt=0),
    maxBudgetPaise: int | None = Query(default=None, gt=0),
    dietaryPreference: str | None = Query(default=None, max_length=40),
    excludeAllergens: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=6, ge=1, le=20),
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
):
    _enforce_ai_rate_limit(request, user)

    stmt = (
        select(Product)
        .join(Product.category)
        .options(joinedload(Product.category))
        .where(Product.is_archived.is_(False), Product.is_available.is_(True), Product.stock_quantity > 0)
    )
    if counterId is not None:
        stmt = stmt.where(or_(Product.counter_id == counterId, Product.counter_id.is_(None)))
    candidates = [p for p in db.scalars(stmt).all() if is_item_available_in_window(db, p)[0]]

    eff_diet = (dietaryPreference or (user.dietary_preference if user else None) or "").strip().lower()
    excl_allergens = {a.strip().lower() for a in (excludeAllergens or "").split(",") if a.strip()}

    filtered: list[Product] = []
    for p in candidates:
        if maxBudgetPaise is not None and p.effective_price_paise > maxBudgetPaise:
            continue
        a_tags = {t.lower() for t in parse_tags(p.allergen_tags, p.allergens)}
        if excl_allergens and (excl_allergens & a_tags):
            continue
        if dietaryPreference:

            d_tags = {t.lower() for t in parse_tags(p.dietary_tags, p.dietary_info)}
            if eff_diet and eff_diet not in d_tags:
                continue
        filtered.append(p)

    pids = [p.id for p in filtered]
    ratings = _product_ratings_map(db, pids)
    popularity = _product_popularity_map(db, pids)
    max_pop = max(popularity.values(), default=1) or 1

    fav_ids: set[int] = set()
    own_counts: dict[int, int] = {}
    co_occur_scores: dict[int, int] = defaultdict(int)

    if user is not None and user.role == UserRole.STUDENT:
        fav_ids = set(db.scalars(select(Favorite.product_id).where(Favorite.user_id == user.id)).all())
        past_rows = db.execute(
            select(OrderItem.product_id, func.sum(OrderItem.quantity))
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.user_id == user.id, Order.payment_status == PaymentStatus.PAID)
            .group_by(OrderItem.product_id)
        ).all()
        own_counts = {int(r[0]): int(r[1] or 0) for r in past_rows}

        if own_counts:
            seed_pids = list(own_counts.keys())[:10]
            matching_order_ids = select(OrderItem.order_id).join(Order, Order.id == OrderItem.order_id).where(
                Order.payment_status == PaymentStatus.PAID,
                OrderItem.product_id.in_(seed_pids),
            )
            co_rows = db.execute(
                select(OrderItem.product_id, func.count(OrderItem.id))
                .where(OrderItem.order_id.in_(matching_order_ids), OrderItem.product_id.notin_(seed_pids))
                .group_by(OrderItem.product_id)
            ).all()
            for c_pid, cnt in co_rows:
                co_occur_scores[int(c_pid)] = int(cnt or 0)

    eta_info = estimate_counter_wait_minutes(db, counterId, 8)
    busy_queue = eta_info["queuedOrders"] >= 4

    scored: list[tuple[float, Product, list[str]]] = []
    for p in filtered:
        score = 10.0
        reasons: list[str] = []

        if p.id in fav_ids:
            score += 30.0
            reasons.append("Saved in your favorites")
        if p.id in own_counts:
            boost = min(25.0, 12.0 + own_counts[p.id] * 3.0)
            score += boost
            reasons.append(f"Ordered {own_counts[p.id]}x by you previously")
        elif co_occur_scores.get(p.id, 0) > 0:
            score += min(18.0, 6.0 + co_occur_scores[p.id] * 3.0)
            reasons.append("Popular pairing with meals you order")

        d_tags = {t.lower() for t in parse_tags(p.dietary_tags, p.dietary_info)}
        if eff_diet and eff_diet in d_tags:
            score += 15.0
            reasons.append(f"Matches '{eff_diet}' preference")

        pop = popularity.get(p.id, 0)
        if pop > 0:
            score += round((pop / max_pop) * 20.0, 2)
            if pop >= 3:
                reasons.append(f"Popular on campus ({pop} sold recently)")

        r_avg, r_cnt = ratings.get(p.id, (None, 0))
        if r_avg is not None and r_cnt >= 1:
            score += round((r_avg / 5.0) * 15.0, 2)
            if r_avg >= 4.0:
                reasons.append(f"Rated {r_avg:.1f}★ ({r_cnt})")

        prep = p.preparation_minutes or 10
        if prep <= 8:
            score += 10.0 if busy_queue else 5.0
            reasons.append(f"Fast ~{prep} min prep")

        if not reasons:
            reasons.append("Freshly available at counter right now")

        scored.append((round(score, 2), p, reasons))

    scored.sort(key=lambda item: (-item[0], item[1].effective_price_paise, item[1].name.lower()))
    top = scored[:limit]

    recommendations = []
    for sc, p, reasons in top:
        r_avg, r_cnt = ratings.get(p.id, (None, 0))
        prod_dict = serialize_product_v1(p, rating_avg=r_avg, rating_count=r_cnt)
        recommendations.append({
            "score": sc,
            "reasons": reasons[:3],
            "requiresConfirmation": True,
            "item": prod_dict,
        })

    _record_ai_telemetry(
        db,
        user_id=user.id if user else None,
        feature="recommendations",
        model_version="hybrid-cooccurrence-v1",
        raw_input_for_hash=f"{counterId}:{maxBudgetPaise}:{eff_diet}:{excludeAllergens}",
        metadata={"returned_count": len(recommendations), "personalized": bool(fav_ids or own_counts)},
    )
    db.commit()

    return ok(
        request,
        {
            "modelVersion": "hybrid-cooccurrence-v1",
            "isEstimate": True,
            "items": recommendations,
        },
    )

class AssistantQueryIn(BaseModel):
    message: str = Field(min_length=2, max_length=500)
    counterId: int | None = Field(default=None, gt=0)
    maxBudgetPaise: int | None = Field(default=None, gt=0)

def _extract_query_constraints(message: str, explicit_budget_paise: int | None) -> dict[str, Any]:
    msg_low = message.lower()
    budget_paise = explicit_budget_paise

    m = re.search(r"(?:under|below|within|less than|max|<=?)\s*(?:₹|rs\.?|inr)?\s*(\d{1,4})", msg_low)
    if m and budget_paise is None:
        budget_paise = int(m.group(1)) * 100

    diet_terms = []
    for tag in ("vegan", "jain", "high-protein", "gluten-free", "veg", "vegetarian"):
        if re.search(rf"\b{re.escape(tag)}\b", msg_low):
            diet_terms.append("veg" if tag == "vegetarian" else tag)

    avoid_allergens: set[str] = set()
    allergen_patterns = {
        "nuts": r"(?:no|without|avoid|free of|allergic to)\s+(?:tree\s+)?nuts?|nut[-\s]?free",
        "peanuts": r"(?:no|without|avoid|free of|allergic to)\s+peanuts?|peanut[-\s]?free",
        "dairy": r"(?:no|without|avoid|free of|allergic to)\s+(?:dairy|milk|lactose)|dairy[-\s]?free|lactose[-\s]?free",
        "gluten": r"(?:no|without|avoid|free of|allergic to)\s+(?:gluten|wheat)|gluten[-\s]?free",
        "soy": r"(?:no|without|avoid|free of|allergic to)\s+soy|soy[-\s]?free",
        "egg": r"(?:no|without|avoid|free of|allergic to)\s+eggs?|egg[-\s]?free|eggless",
    }
    for allergen_key, pat in allergen_patterns.items():
        if re.search(pat, msg_low):
            avoid_allergens.add(allergen_key)

    wants_fast = bool(re.search(r"\b(quick|fast|hurry|short break|instant|ready soon)\b", msg_low))
    return {
        "budgetPaise": budget_paise,
        "dietTerms": diet_terms,
        "avoidAllergens": avoid_allergens,
        "wantsFast": wants_fast,
    }

def _call_gemini_grounded(menu_catalog: list[dict[str, Any]], user_question: str, constraints: dict[str, Any]) -> dict[str, Any] | None:
    cfg = get_settings()
    if not cfg.gemini_api_key:
        return None

    system_instruction = (
        "You are the CampusBite Canteen Menu Concierge. "
        "STRICT SAFETY & GROUNDING RULES:\n"
        "1. Recommend ONLY items present in the VERIFIED_MENU_JSON below. Never invent food names, prices, ingredients, or dietary claims.\n"
        "2. Always quote exact prices in ₹ from priceRupees.\n"
        "3. If an item's allergenTags list is empty, state clearly that allergen details are not verified on record and advise checking with counter staff.\n"
        "4. You cannot place orders or modify the cart directly; students confirm before adding items.\n"
        "5. Respond strictly as JSON matching: "
        '{"answer": "concise helpful reply", "suggestedItemIds": [1, 2], "dietaryWarnings": ["optional warning"]}'
    )
    prompt_payload = json.dumps(
        {
            "parsedConstraints": {
                "maxBudgetRupees": str(paise_to_rupees(constraints["budgetPaise"])) if constraints["budgetPaise"] else None,
                "dietTerms": constraints["dietTerms"],
                "avoidAllergens": sorted(constraints["avoidAllergens"]),
            },
            "VERIFIED_MENU_JSON": menu_catalog,
            "studentQuestion": user_question,
        },
        ensure_ascii=False,
    )

    try:
        resp = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.gemini_model}:generateContent",
            headers={"x-goog-api-key": cfg.gemini_api_key, "Content-Type": "application/json"},
            json={
                "system_instruction": {"parts": [{"text": system_instruction}]},
                "contents": [{"role": "user", "parts": [{"text": prompt_payload}]}],
                "generationConfig": {
                    "temperature": 0.15,
                    "maxOutputTokens": 600,
                    "responseMimeType": "application/json",
                },
            },
            timeout=12.0,
        )
        resp.raise_for_status()
        body = resp.json()
        text_out = (
            body.get("candidates", [{}])[0]
            .get("content", {})
            .get("parts", [{}])[0]
            .get("text", "")
        )
        parsed = json.loads(text_out)
        if isinstance(parsed, dict) and isinstance(parsed.get("answer"), str):
            return parsed
    except Exception as exc:
        logger.warning("Gemini assistant call failed (%s); using deterministic verified-menu engine", type(exc).__name__)
    return None

def _deterministic_menu_concierge(
    candidates: list[dict[str, Any]],
    question: str,
    constraints: dict[str, Any],
) -> dict[str, Any]:
    budget_p = constraints["budgetPaise"]
    diet_terms = constraints["dietTerms"]
    avoid_allergens: set[str] = constraints["avoidAllergens"]
    wants_fast = constraints["wantsFast"]

    q_words = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if len(w) >= 3 and w not in {"what", "can", "get", "under", "below", "have", "show", "menu", "food", "with", "for", "the", "and"}}

    ranked: list[tuple[float, dict[str, Any]]] = []
    warnings: list[str] = []

    for item in candidates:
        if budget_p is not None and item["pricePaise"] > budget_p:
            continue
        item_allergens = {a.lower() for a in item["allergenTags"]}
        if avoid_allergens:
            if item_allergens & avoid_allergens:
                continue
            if not item_allergens:

                continue
        item_diets = {d.lower() for d in item["dietaryTags"]}
        if diet_terms and not any(dt in item_diets for dt in diet_terms):
            continue

        sc = 10.0
        haystack = f"{item['name']} {item['categoryName']} {' '.join(item['dietaryTags'])} {item['ingredientsSummary'] or ''}".lower()
        for w in q_words:
            if w in haystack:
                sc += 12.0
        if wants_fast:
            sc += max(0.0, 20.0 - float(item["preparationMinutes"]))
        else:
            sc += max(0.0, 10.0 - float(item["pricePaise"]) / 2000.0)
        ranked.append((sc, item))

    ranked.sort(key=lambda pair: (-pair[0], pair[1]["pricePaise"], pair[1]["name"]))
    picked = [it for _, it in ranked[:4]]

    if avoid_allergens:
        warnings.append(
            f"Filtered out items listing {', '.join(sorted(avoid_allergens))} and items without verified allergen declarations. Confirm severe allergies with counter staff."
        )

    if not picked:
        budget_note = f" within ₹{paise_to_rupees(budget_p)}" if budget_p else ""
        diet_note = f" matching {', '.join(diet_terms)}" if diet_terms else ""
        return {
            "answer": f"No currently available menu items matched all constraints{budget_note}{diet_note} with verified dietary/allergen records.",
            "suggestedItemIds": [],
            "dietaryWarnings": warnings,
        }

    bullets = [
        f"{it['name']} (₹{it['priceRupees']}, ~{it['preparationMinutes']} min"
        + (f", {', '.join(it['dietaryTags'])}" if it["dietaryTags"] else "")
        + ")"
        for it in picked
    ]
    prefix = f"Here are available options under ₹{paise_to_rupees(budget_p)}: " if budget_p else "Based on the live verified menu, here are the best matches: "
    for it in picked:
        if not it["allergenTags"]:
            warnings.append(f"{it['name']}: allergen tags are not verified on file — check with kitchen staff if you have allergies.")

    return {
        "answer": prefix + "; ".join(bullets) + ". Tap Confirm to add any item to your cart.",
        "suggestedItemIds": [it["id"] for it in picked],
        "dietaryWarnings": warnings[:3],
    }

@router.post("/assistant")
def query_ai_menu_assistant_v1(
    payload: AssistantQueryIn,
    request: Request,
    user: User | None = Depends(get_optional_user),
    db: Session = Depends(get_db),
):
    _enforce_ai_rate_limit(request, user)

    stmt = (
        select(Product)
        .join(Product.category)
        .options(joinedload(Product.category), joinedload(Product.recipe_items).joinedload(RecipeItem.ingredient))
        .where(Product.is_archived.is_(False), Product.is_available.is_(True), Product.stock_quantity > 0)
    )
    if payload.counterId is not None:
        stmt = stmt.where(or_(Product.counter_id == payload.counterId, Product.counter_id.is_(None)))
    live_products = [p for p in db.scalars(stmt).unique().all() if is_item_available_in_window(db, p)[0]]

    verified_catalog: list[dict[str, Any]] = []
    by_id: dict[int, Product] = {}
    for p in live_products:
        by_id[p.id] = p
        ing_names = [ri.ingredient.name for ri in p.recipe_items if ri.ingredient]
        verified_catalog.append({
            "id": p.id,
            "name": p.name,
            "categoryName": p.category_name or "General",
            "pricePaise": p.effective_price_paise,
            "priceRupees": str(paise_to_rupees(p.effective_price_paise)),
            "preparationMinutes": p.preparation_minutes or 10,
            "stockQuantity": p.stock_quantity,
            "dietaryTags": parse_tags(p.dietary_tags, p.dietary_info),
            "allergenTags": parse_tags(p.allergen_tags, p.allergens),
            "verifiedIngredients": ing_names,
            "ingredientsSummary": p.ingredients_list,
        })

    constraints = _extract_query_constraints(payload.message, payload.maxBudgetPaise)
    cfg = get_settings()
    raw_result = _call_gemini_grounded(verified_catalog, payload.message, constraints)
    used_model = cfg.gemini_model if raw_result is not None else "deterministic-grounded-concierge-v1"
    if raw_result is None:
        raw_result = _deterministic_menu_concierge(verified_catalog, payload.message, constraints)

    valid_suggestions = []
    seen_ids: set[int] = set()
    budget_cap = constraints["budgetPaise"]
    avoid_allergens: set[str] = constraints["avoidAllergens"]

    for raw_id in (raw_result.get("suggestedItemIds") or []):
        try:
            pid = int(raw_id)
        except (TypeError, ValueError):
            continue
        if pid in seen_ids or pid not in by_id:
            continue
        p = by_id[pid]
        if budget_cap is not None and p.effective_price_paise > budget_cap:
            continue
        a_tags = {a.lower() for a in parse_tags(p.allergen_tags, p.allergens)}
        if avoid_allergens and ((a_tags & avoid_allergens) or not a_tags):
            continue
        seen_ids.add(pid)
        item_dict = serialize_product_v1(p)
        item_dict["allergenVerified"] = bool(item_dict["allergenTags"])
        item_dict["allergenNotice"] = (
            f"Contains: {', '.join(item_dict['allergenTags'])}"
            if item_dict["allergenTags"]
            else "Allergen information not verified on record — confirm with staff."
        )
        valid_suggestions.append(item_dict)

    warnings = [str(w) for w in (raw_result.get("dietaryWarnings") or []) if str(w).strip()]
    _record_ai_telemetry(
        db,
        user_id=user.id if user else None,
        feature="menu_assistant",
        model_version=used_model,
        raw_input_for_hash=payload.message.strip(),
        metadata={
            "suggested_count": len(valid_suggestions),
            "budget_paise": budget_cap,
            "grounded_menu_size": len(verified_catalog),
        },
    )
    db.commit()

    return ok(
        request,
        {
            "answer": str(raw_result.get("answer") or ""),
            "suggestedItems": valid_suggestions,
            "dietaryWarnings": warnings,
            "requiresConfirmation": True,
            "autoAddedToCart": False,
            "modelVersion": used_model,
            "groundedMenuCount": len(verified_catalog),
        },
    )

SERVICE_SLOTS = (
    ("Breakfast (08:00-11:00)", 0, 11),
    ("Lunch (11:00-14:30)", 11, 15),
    ("Snacks (14:30-18:00)", 15, 24),
)

def _slot_label_for_hour(hour: int) -> str:
    if hour < 11:
        return SERVICE_SLOTS[0][0]
    if hour < 15:
        return SERVICE_SLOTS[1][0]
    return SERVICE_SLOTS[2][0]

class ForecastRunIn(BaseModel):
    forecastDate: date | None = None

class ForecastOverrideIn(BaseModel):
    approvedQuantity: int = Field(ge=0, le=10_000)
    reason: str = Field(min_length=3, max_length=255)

def generate_demand_forecast_run(db: Session, target_date: date, actor_user_id: int | None) -> ForecastRun:
    tz = ZoneInfo(get_settings().canteen_timezone)
    lookback_days = 56
    since_utc = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    products = db.scalars(
        select(Product)
        .where(Product.is_archived.is_(False), Product.item_type == "food")
        .order_by(Product.id.asc())
    ).all()

    paid_lines = db.execute(
        select(OrderItem.product_id, OrderItem.quantity, Order.created_at)
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.payment_status == PaymentStatus.PAID, Order.created_at >= since_utc)
    ).all()

    history: dict[tuple[int, str], dict[date, int]] = defaultdict(lambda: defaultdict(int))
    observed_dates: set[date] = set()
    for pid, qty, created_at in paid_lines:
        dt_utc = as_utc(created_at)
        if dt_utc is None:
            continue
        local_dt = dt_utc.astimezone(tz)
        d_local = local_dt.date()
        if d_local >= target_date:
            continue
        observed_dates.add(d_local)
        slot_lbl = _slot_label_for_hour(local_dt.hour)
        history[(int(pid), slot_lbl)][d_local] += int(qty or 0)

    sorted_dates = sorted(observed_dates)
    active_days_count = len(sorted_dates)
    target_weekday = target_date.weekday()

    abs_errors: list[float] = []
    actual_sum = 0.0
    if len(sorted_dates) >= 2:
        for idx in range(1, len(sorted_dates)):
            eval_day = sorted_dates[idx]
            prior_days = sorted_dates[:idx]
            same_wd_days = [d for d in prior_days if d.weekday() == eval_day.weekday()]
            ref_days = same_wd_days if len(same_wd_days) >= 2 else prior_days[-7:]
            for p in products:
                for slot_lbl, _, _ in SERVICE_SLOTS:
                    series = history.get((p.id, slot_lbl), {})
                    if not series:
                        continue
                    pred_bt = sum(series.get(d, 0) for d in ref_days) / max(1, len(ref_days))
                    act_bt = float(series.get(eval_day, 0))
                    abs_errors.append(abs(act_bt - pred_bt))
                    actual_sum += act_bt

    mae = round(sum(abs_errors) / len(abs_errors), 3) if abs_errors else None
    wape_pct = round((sum(abs_errors) / actual_sum) * 100.0, 2) if abs_errors and actual_sum > 0 else None

    metrics = {
        "method": "weekday_slot_weighted_moving_average_v1",
        "lookbackDays": lookback_days,
        "observedActiveDays": active_days_count,
        "mae": mae,
        "wapePct": wape_pct,
        "backtestStatus": "measured" if mae is not None else "insufficient_history_for_backtest",
        "note": "Forecasts are statistical estimates with empirical intervals, not guarantees.",
    }

    run = ForecastRun(
        forecast_date=target_date,
        model_version="weekday-slot-wma-v1",
        metrics_json=json.dumps(metrics, separators=(",", ":")),
        actor_user_id=actor_user_id,
    )
    db.add(run)
    db.flush()

    same_weekday_dates = [d for d in sorted_dates if d.weekday() == target_weekday]

    for p in products:
        for slot_lbl, _, _ in SERVICE_SLOTS:
            series = history.get((p.id, slot_lbl), {})
            ref_dates = same_weekday_dates if len(same_weekday_dates) >= 2 else sorted_dates[-14:]
            samples = [float(series.get(d, 0)) for d in ref_dates]

            if samples:

                weights = [float(i + 1) for i in range(len(samples))]
                w_mean = sum(s * w for s, w in zip(samples, weights)) / sum(weights)
                variance = sum((s - w_mean) ** 2 for s in samples) / max(1, len(samples))
                std_dev = math.sqrt(variance)
            else:

                w_mean = 3.0 if slot_lbl.startswith("Lunch") else 2.0
                std_dev = 2.0

            pred_qty = max(0, int(round(w_mean)))
            margin = max(1, int(round(1.28 * std_dev)))
            lower = max(0, pred_qty - margin)
            upper = pred_qty + margin
            low_conf = active_days_count < 7 or len(samples) < 3
            conf_score = Decimal(str(round(min(0.90, 0.35 + min(active_days_count, 28) * 0.02), 2)))
            rec_prep = pred_qty if low_conf else min(upper, int(round(pred_qty + 0.35 * std_dev)))

            db.add(
                ForecastItem(
                    forecast_run_id=run.id,
                    product_id=p.id,
                    time_slot=slot_lbl,
                    predicted_quantity=pred_qty,
                    lower_bound=lower,
                    upper_bound=upper,
                    recommended_prep=rec_prep,
                    confidence_score=conf_score,
                    low_confidence_warning=low_conf,
                )
            )

    _record_ai_telemetry(
        db,
        user_id=actor_user_id,
        feature="demand_forecast",
        model_version=run.model_version,
        raw_input_for_hash=target_date.isoformat(),
        metadata={"forecast_date": target_date.isoformat(), "active_days": active_days_count, "mae": mae, "wape_pct": wape_pct},
    )
    db.commit()
    db.refresh(run)
    return run

def _serialize_forecast_run(run: ForecastRun) -> dict[str, Any]:
    return {
        "runId": run.id,
        "forecastDate": run.forecast_date.isoformat(),
        "modelVersion": run.model_version,
        "isEstimate": True,
        "metrics": parse_json_safe(run.metrics_json, {}),
        "createdAt": as_utc(run.created_at),
        "items": [
            {
                "id": fi.id,
                "menuItemId": fi.product_id,
                "itemName": fi.product.name if fi.product else f"Item #{fi.product_id}",
                "timeSlot": fi.time_slot,
                "predictedQuantity": fi.predicted_quantity,
                "lowerBound": fi.lower_bound,
                "upperBound": fi.upper_bound,
                "recommendedPrep": fi.recommended_prep,
                "effectivePrepPlan": fi.staff_approved_quantity if fi.staff_approved_quantity is not None else fi.recommended_prep,
                "confidenceScore": float(fi.confidence_score),
                "lowConfidenceWarning": bool(fi.low_confidence_warning),
                "staffApprovedQuantity": fi.staff_approved_quantity,
                "overrideReason": fi.override_reason,
            }
            for fi in sorted(run.items, key=lambda x: (x.time_slot, -x.predicted_quantity, x.id))
        ],
    }

@router.get("/demand-forecasts")
def get_demand_forecasts_v1(
    request: Request,
    forecastDate: date | None = None,
    user: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    target_d = forecastDate or datetime.now(ZoneInfo(get_settings().canteen_timezone)).date()
    run = db.scalar(
        select(ForecastRun)
        .options(joinedload(ForecastRun.items).joinedload(ForecastItem.product))
        .where(ForecastRun.forecast_date == target_d)
        .order_by(ForecastRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        run = generate_demand_forecast_run(db, target_d, user.id)
        run = db.scalar(
            select(ForecastRun)
            .options(joinedload(ForecastRun.items).joinedload(ForecastItem.product))
            .where(ForecastRun.id == run.id)
        )
    return ok(request, _serialize_forecast_run(run))

@router.post("/forecasts/run", status_code=201)
def trigger_demand_forecast_run_v1(
    payload: ForecastRunIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    target_d = payload.forecastDate or datetime.now(ZoneInfo(get_settings().canteen_timezone)).date()
    run = generate_demand_forecast_run(db, target_d, admin.id)
    run = db.scalar(
        select(ForecastRun)
        .options(joinedload(ForecastRun.items).joinedload(ForecastItem.product))
        .where(ForecastRun.id == run.id)
    )
    record_audit(db, "ai.forecast_run_created", "forecast_run", run.id, admin.id, {"forecast_date": target_d.isoformat()})
    db.commit()
    return ok(request, _serialize_forecast_run(run), status_code=201)

@router.patch("/forecasts/items/{forecast_item_id}/override")
def override_forecast_item_v1(
    forecast_item_id: int,
    payload: ForecastOverrideIn,
    request: Request,
    staff: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    fi = db.scalar(select(ForecastItem).options(joinedload(ForecastItem.product)).where(ForecastItem.id == forecast_item_id))
    if fi is None:
        raise ApiError(404, "FORECAST_ITEM_NOT_FOUND", "Forecast item not found.")
    fi.staff_approved_quantity = payload.approvedQuantity
    fi.override_by_user_id = staff.id
    fi.override_reason = payload.reason.strip()
    record_audit(
        db,
        "ai.forecast_overridden",
        "forecast_item",
        fi.id,
        staff.id,
        {"predicted": fi.predicted_quantity, "approved": payload.approvedQuantity, "reason": fi.override_reason},
    )
    db.commit()
    db.refresh(fi)
    return ok(
        request,
        {
            "id": fi.id,
            "menuItemId": fi.product_id,
            "itemName": fi.product.name if fi.product else "",
            "timeSlot": fi.time_slot,
            "predictedQuantity": fi.predicted_quantity,
            "staffApprovedQuantity": fi.staff_approved_quantity,
            "overrideReason": fi.override_reason,
        },
    )

@router.get("/insights/waste")
def get_ai_waste_insights_v1(
    request: Request,
    days: int = Query(default=30, ge=7, le=180),
    user: User = Depends(require_roles("staff", "admin")),
    db: Session = Depends(get_db),
):
    tz = ZoneInfo(get_settings().canteen_timezone)
    today = datetime.now(tz).date()
    since_utc = datetime.now(timezone.utc) - timedelta(days=days)

    run = db.scalar(
        select(ForecastRun)
        .options(joinedload(ForecastRun.items))
        .where(ForecastRun.forecast_date == today)
        .order_by(ForecastRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        run = generate_demand_forecast_run(db, today, user.id)

    planned_servings_by_product: dict[int, int] = defaultdict(int)
    for fi in run.items:
        planned = fi.staff_approved_quantity if fi.staff_approved_quantity is not None else fi.recommended_prep
        planned_servings_by_product[fi.product_id] += int(planned)

    projected_ingredient_need: dict[int, Decimal] = defaultdict(lambda: Decimal("0.000"))
    recipes = db.scalars(select(RecipeItem).options(joinedload(RecipeItem.ingredient), joinedload(RecipeItem.product))).all()
    for ri in recipes:
        servings = planned_servings_by_product.get(ri.product_id, 0)
        if servings > 0:
            projected_ingredient_need[ri.ingredient_id] += Decimal(ri.quantity_per_serving) * Decimal(servings)

    ingredients = db.scalars(select(Ingredient).where(Ingredient.is_active.is_(True))).all()
    predicted_shortages = []
    for ing in ingredients:
        needed = projected_ingredient_need.get(ing.id, Decimal("0.00"))
        avail = Decimal(ing.current_quantity)
        if needed > avail:
            deficit = (needed - avail).quantize(Decimal("0.01"))
            predicted_shortages.append({
                "ingredientId": ing.id,
                "ingredientName": ing.name,
                "unit": ing.unit,
                "currentQuantity": str(avail),
                "projectedDemandToday": str(needed.quantize(Decimal("0.01"))),
                "predictedDeficit": str(deficit),
                "recommendation": f"Restock at least {deficit} {ing.unit} before peak service to avoid mid-break stockout.",
            })

    prod_waste_rows = db.execute(
        select(
            InventoryMovement.product_id,
            Product.name,
            func.sum(func.abs(InventoryMovement.quantity_delta)).label("wasted_units"),
            Product.price_paise,
            Product.price,
        )
        .join(Product, Product.id == InventoryMovement.product_id)
        .where(InventoryMovement.movement_type == InventoryMovementType.WASTE, InventoryMovement.created_at >= since_utc)
        .group_by(InventoryMovement.product_id, Product.name, Product.price_paise, Product.price)
        .order_by(func.sum(func.abs(InventoryMovement.quantity_delta)).desc())
        .limit(10)
    ).all()

    wastage_hotspots = []
    for r in prod_waste_rows:
        units = int(r.wasted_units or 0)
        unit_p = int(r.price_paise) if r.price_paise is not None else rupees_to_paise(r.price)
        wastage_hotspots.append({
            "menuItemId": int(r.product_id),
            "itemName": r.name,
            "unitsWasted": units,
            "estimatedValueLossPaise": units * unit_p,
            "actionSuggestion": "Reduce pre-break batch size by 15-20% or switch to made-to-order during slow slots.",
        })

    soon = today + timedelta(days=5)
    expiring = db.scalars(
        select(ProductBatch)
        .options(joinedload(ProductBatch.product))
        .where(ProductBatch.quantity_remaining > 0, ProductBatch.expiry_date.is_not(None), ProductBatch.expiry_date <= soon)
        .order_by(ProductBatch.expiry_date.asc())
    ).all()

    spoilage_risks = [
        {
            "batchCode": b.batch_code,
            "menuItemId": b.product_id,
            "itemName": b.product.name if b.product else "",
            "quantityRemaining": b.quantity_remaining,
            "expiryDate": b.expiry_date.isoformat() if b.expiry_date else None,
            "actionSuggestion": (
                "Remove expired stock and log wastage immediately."
                if (b.expiry_date and b.expiry_date < today)
                else "Prioritize FEFO usage today or configure an admin-approved discount."
            ),
        }
        for b in expiring
    ]

    adj_rows = db.execute(
        select(InventoryMovement.product_id, Product.name, func.count(InventoryMovement.id).label("adj_cnt"))
        .join(Product, Product.id == InventoryMovement.product_id)
        .where(InventoryMovement.movement_type == InventoryMovementType.ADJUSTMENT, InventoryMovement.created_at >= since_utc)
        .group_by(InventoryMovement.product_id, Product.name)
        .having(func.count(InventoryMovement.id) >= 3)
        .order_by(func.count(InventoryMovement.id).desc())
    ).all()

    return ok(
        request,
        {
            "windowDays": days,
            "isEstimate": True,
            "predictedIngredientShortages": predicted_shortages,
            "wastageHotspots": wastage_hotspots,
            "spoilageRiskBatches": spoilage_risks,
            "stockAdjustmentAnomalies": [
                {
                    "menuItemId": int(r.product_id),
                    "itemName": r.name,
                    "adjustmentCount": int(r.adj_cnt),
                    "note": "Repeated manual adjustments detected; verify portion sizes and receiving counts.",
                }
                for r in adj_rows
            ],
        },
    )

@router.get("/queue-eta")
def get_ai_queue_eta_v1(
    request: Request,
    counterId: int | None = Query(default=None, gt=0),
    menuItemIds: str | None = Query(default=None, max_length=200),
    db: Session = Depends(get_db),
):
    max_prep = 8
    if menuItemIds:
        ids = [int(x) for x in menuItemIds.split(",") if x.strip().isdigit()]
        if ids:
            preps = db.scalars(select(Product.preparation_minutes).where(Product.id.in_(ids[:25]))).all()
            if preps:
                max_prep = max(int(p or 8) for p in preps)
    eta = estimate_counter_wait_minutes(db, counterId, max_prep)
    return ok(request, {"counterId": counterId, "maxItemPrepMinutes": max_prep, **eta})

@router.get("/metrics")
def get_admin_ai_metrics_v1(
    request: Request,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    since = datetime.now(timezone.utc) - timedelta(days=30)
    by_feature = db.execute(
        select(AIInteraction.feature, AIInteraction.model_version, func.count(AIInteraction.id))
        .where(AIInteraction.created_at >= since)
        .group_by(AIInteraction.feature, AIInteraction.model_version)
    ).all()

    latest_run = db.scalar(select(ForecastRun).order_by(ForecastRun.created_at.desc()).limit(1))
    overrides_count = db.scalar(select(func.count(ForecastItem.id)).where(ForecastItem.staff_approved_quantity.is_not(None))) or 0

    return ok(
        request,
        {
            "windowDays": 30,
            "interactionsByFeatureAndModel": [
                {"feature": r[0], "modelVersion": r[1], "count": int(r[2])}
                for r in by_feature
            ],
            "latestForecastRun": (
                {
                    "runId": latest_run.id,
                    "forecastDate": latest_run.forecast_date.isoformat(),
                    "modelVersion": latest_run.model_version,
                    "metrics": parse_json_safe(latest_run.metrics_json, {}),
                    "createdAt": as_utc(latest_run.created_at),
                }
                if latest_run is not None
                else None
            ),
            "totalStaffForecastOverrides": int(overrides_count),
            "privacyGuarantee": "Only SHA-256 input hashes and aggregate metadata are stored; student PII and secrets are never logged or sent to external models.",
        },
    )

def _generate_executive_summary_narrative(actuals_payload: dict[str, Any], predictions_payload: dict[str, Any]) -> tuple[str, str]:
    cfg = get_settings()
    if cfg.gemini_api_key:
        try:
            prompt = json.dumps(
                {
                    "instruction": (
                        "Write a concise 4-sentence executive summary for the Canteen Administrator. "
                        "Strictly separate observed ACTUAL performance from AI PREDICTIONS. "
                        "Use ONLY numbers present in VERIFIED_DATA."
                    ),
                    "VERIFIED_DATA": {"actuals": actuals_payload, "predictions": predictions_payload},
                },
                default=str,
            )
            resp = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.gemini_model}:generateContent",
                headers={"x-goog-api-key": cfg.gemini_api_key, "Content-Type": "application/json"},
                json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 280},
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            txt = (
                resp.json()
                .get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
                .strip()
            )
            if txt:
                return txt, cfg.gemini_model
        except Exception:
            pass

    rev_str = actuals_payload["windowRevenueRupees"]
    ord_cnt = actuals_payload["windowPaidOrders"]
    top_food = actuals_payload["topItemName"] or "N/A"
    proj_rev = predictions_payload["next7DaysProjectedRevenueRupees"]
    reorder_cnt = predictions_payload["reorderAlertsCount"]
    narrative = (
        f"[ACTUALS] Over the observed {actuals_payload['windowDays']}-day window, the canteen processed {ord_cnt} paid order(s) "
        f"generating ₹{rev_str} in verified revenue, led by '{top_food}'. "
        f"[PREDICTIONS] Based on weekday moving-average patterns, projected revenue for the next 7 days is ~₹{proj_rev} (statistical estimate). "
        f"Inventory intelligence flags {reorder_cnt} item/ingredient reorder action(s) and {actuals_payload['anomalyCount']} daily sales anomaly event(s) for review."
    )
    return narrative, "deterministic-grounded-bi-v2"

@router.get("/bi/dashboard")
def get_ai_business_intelligence_dashboard_v1(
    request: Request,
    days: int = Query(default=30, ge=7, le=180),
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    from app.modules.analytics_bi import compute_unit_cost_map

    cfg = get_settings()
    tz = ZoneInfo(cfg.canteen_timezone)
    today_local = datetime.now(tz).date()
    since_utc = datetime.now(timezone.utc) - timedelta(days=days)

    run = db.scalar(
        select(ForecastRun)
        .options(joinedload(ForecastRun.items).joinedload(ForecastItem.product))
        .where(ForecastRun.forecast_date == today_local)
        .order_by(ForecastRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        run = generate_demand_forecast_run(db, today_local, admin.id)
        run = db.scalar(
            select(ForecastRun)
            .options(joinedload(ForecastRun.items).joinedload(ForecastItem.product))
            .where(ForecastRun.id == run.id)
        )

    products = db.scalars(select(Product).options(joinedload(Product.category)).where(Product.is_archived.is_(False))).all()
    prod_by_id = {p.id: p for p in products}
    unit_costs = compute_unit_cost_map(db)

    paid_orders = db.scalars(
        select(Order)
        .options(joinedload(Order.items), joinedload(Order.user))
        .where(Order.payment_status == PaymentStatus.PAID, Order.created_at >= since_utc)
        .order_by(Order.created_at.asc())
    ).unique().all()

    demand_by_item: dict[int, dict[str, Any]] = defaultdict(lambda: {"predictedUnits": 0, "recommendedPrep": 0, "lower": 0, "upper": 0})
    for fi in run.items:
        b = demand_by_item[fi.product_id]
        b["predictedUnits"] += int(fi.predicted_quantity)
        b["recommendedPrep"] += int(fi.staff_approved_quantity if fi.staff_approved_quantity is not None else fi.recommended_prep)
        b["lower"] += int(fi.lower_bound)
        b["upper"] += int(fi.upper_bound)
        b["itemName"] = fi.product.name if fi.product else f"Item #{fi.product_id}"
        b["lowConfidence"] = bool(fi.low_confidence_warning)

    food_demand_predictions = [
        {"menuItemId": pid, "dataKind": "PREDICTION", **vals}
        for pid, vals in sorted(demand_by_item.items(), key=lambda kv: -kv[1]["predictedUnits"])
    ]

    daily_rev_actual: dict[date, int] = defaultdict(int)
    daily_orders_actual: dict[date, int] = defaultdict(int)
    weekday_revs: dict[int, list[int]] = defaultdict(list)
    weekday_orders: dict[int, list[int]] = defaultdict(list)
    weekday_hour_orders: dict[tuple[int, int], int] = defaultdict(int)
    observed_weekday_counts: dict[int, set[date]] = defaultdict(set)

    item_units_sold: dict[int, int] = defaultdict(int)
    item_rev_paise: dict[int, int] = defaultdict(int)
    customer_orders: dict[int, int] = defaultdict(int)
    pair_counts: dict[tuple[str, str], int] = defaultdict(int)

    for o in paid_orders:
        u_dt = as_utc(o.created_at)
        if not u_dt:
            continue
        loc = u_dt.astimezone(tz)
        d_loc = loc.date()
        daily_rev_actual[d_loc] += o.effective_total_paise
        daily_orders_actual[d_loc] += 1
        weekday_hour_orders[(d_loc.weekday(), loc.hour)] += 1
        observed_weekday_counts[d_loc.weekday()].add(d_loc)
        customer_orders[o.user_id] += 1

        names_in_order = sorted({it.product_name for it in o.items})
        for i in range(len(names_in_order)):
            for j in range(i + 1, len(names_in_order)):
                pair_counts[(names_in_order[i], names_in_order[j])] += 1

        for it in o.items:
            lp = it.line_total_paise if it.line_total_paise is not None else rupees_to_paise(it.line_total)
            item_units_sold[it.product_id] += int(it.quantity)
            item_rev_paise[it.product_id] += int(lp)

    for d_obs, r_p in daily_rev_actual.items():
        weekday_revs[d_obs.weekday()].append(r_p)
        weekday_orders[d_obs.weekday()].append(daily_orders_actual[d_obs])

    all_daily_revs = list(daily_rev_actual.values())
    overall_daily_mean = (sum(all_daily_revs) / len(all_daily_revs)) if all_daily_revs else 0.0
    overall_daily_std = (
        math.sqrt(sum((x - overall_daily_mean) ** 2 for x in all_daily_revs) / max(1, len(all_daily_revs)))
        if len(all_daily_revs) >= 2
        else overall_daily_mean * 0.25
    )

    sales_forecast_7d = []
    proj_7d_total_paise = 0
    for offset in range(1, 8):
        f_date = today_local + timedelta(days=offset)
        wd = f_date.weekday()
        wd_samples = weekday_revs.get(wd, [])
        proj_rev = int(round(sum(wd_samples) / len(wd_samples))) if wd_samples else int(round(overall_daily_mean))
        ord_samples = weekday_orders.get(wd, [])
        proj_ord = int(round(sum(ord_samples) / len(ord_samples))) if ord_samples else (
            int(round(sum(daily_orders_actual.values()) / max(1, len(daily_orders_actual)))) if daily_orders_actual else 0
        )
        band = int(round(1.28 * overall_daily_std))
        proj_7d_total_paise += proj_rev
        sales_forecast_7d.append({
            "date": f_date.isoformat(),
            "weekday": f_date.strftime("%A"),
            "dataKind": "PREDICTION",
            "projectedRevenuePaise": max(0, proj_rev),
            "lowerBoundPaise": max(0, proj_rev - band),
            "upperBoundPaise": max(0, proj_rev + band),
            "projectedOrders": max(0, proj_ord),
        })

    catalog_perf = [
        {
            "menuItemId": p.id,
            "name": p.name,
            "categoryName": p.category_name or "General",
            "dataKind": "ACTUAL",
            "unitsSold": item_units_sold.get(p.id, 0),
            "revenuePaise": item_rev_paise.get(p.id, 0),
            "stockQuantity": p.stock_quantity,
        }
        for p in products
    ]
    popular_foods = sorted(catalog_perf, key=lambda x: (-x["unitsSold"], -x["revenuePaise"], x["name"]))[:8]
    slow_selling_foods = [
        {
            **item,
            "recommendation": (
                "Zero sales in window — consider featuring in a student combo, repricing, or archiving."
                if item["unitsSold"] == 0
                else "Low velocity — reduce pre-break batch prep to prevent end-of-day wastage."
            ),
        }
        for item in sorted(catalog_perf, key=lambda x: (x["unitsSold"], x["revenuePaise"], x["name"]))[:8]
    ]

    today_wd = today_local.weekday()
    wd_day_count = max(1, len(observed_weekday_counts.get(today_wd, set())))
    peak_hour_predictions = []
    for hr in range(8, 19):
        hist_cnt = weekday_hour_orders.get((today_wd, hr), 0)
        expected_orders = round(hist_cnt / wd_day_count, 1)
        load_level = "HIGH" if expected_orders >= 8 else ("MODERATE" if expected_orders >= 3 else "LOW")
        peak_hour_predictions.append({
            "hour": f"{hr:02d}:00",
            "dataKind": "PREDICTION",
            "predictedOrders": expected_orders,
            "congestionLevel": load_level,
            "staffingSuggestion": "Open express QR pickup lane + pre-portion sides" if load_level == "HIGH" else "Standard counter staffing",
        })

    recipes = db.scalars(select(RecipeItem).options(joinedload(RecipeItem.ingredient))).all()
    ing_projected: dict[int, Decimal] = defaultdict(lambda: Decimal("0.00"))
    for ri in recipes:
        pred_servings = demand_by_item.get(ri.product_id, {}).get("recommendedPrep", 0)
        if pred_servings > 0:
            ing_projected[ri.ingredient_id] += Decimal(ri.quantity_per_serving) * Decimal(pred_servings)

    ingredients = db.scalars(select(Ingredient).where(Ingredient.is_active.is_(True)).order_by(Ingredient.name.asc())).all()
    ingredient_estimates = []
    reorder_recommendations = []

    for ing in ingredients:
        est_today = ing_projected.get(ing.id, Decimal("0.00")).quantize(Decimal("0.01"))
        avail = Decimal(ing.current_quantity)
        reorder_lvl = Decimal(ing.reorder_level)
        three_day_need = (est_today * Decimal("3")).quantize(Decimal("0.01"))
        ingredient_estimates.append({
            "ingredientId": ing.id,
            "name": ing.name,
            "unit": ing.unit,
            "dataKind": "PREDICTION",
            "currentQuantity": str(avail),
            "estimatedNeedToday": str(est_today),
            "estimatedNeed3Days": str(three_day_need),
        })
        if avail <= reorder_lvl or avail < three_day_need:
            target_qty = max(reorder_lvl * Decimal("2"), three_day_need)
            suggested_order_qty = max(Decimal("1.00"), (target_qty - avail).quantize(Decimal("0.01")))
            reorder_recommendations.append({
                "kind": "ingredient",
                "id": ing.id,
                "name": ing.name,
                "dataKind": "PREDICTION",
                "currentStock": f"{avail} {ing.unit}",
                "suggestedReorderQty": f"{suggested_order_qty} {ing.unit}",
                "urgency": "CRITICAL" if avail <= reorder_lvl else "UPCOMING_3D",
            })

    obs_days_cnt = max(1, days)
    for p in products:
        daily_vel = item_units_sold.get(p.id, 0) / obs_days_cnt
        three_day_units = int(math.ceil(daily_vel * 3))
        if p.stock_quantity <= p.low_stock_threshold or (three_day_units > p.stock_quantity):
            target_stock = max(p.low_stock_threshold * 3, three_day_units + p.low_stock_threshold)
            reorder_recommendations.append({
                "kind": "menuItem",
                "id": p.id,
                "name": p.name,
                "dataKind": "PREDICTION",
                "currentStock": f"{p.stock_quantity} units",
                "suggestedReorderQty": f"{max(5, target_stock - p.stock_quantity)} units",
                "urgency": "CRITICAL" if p.stock_quantity <= p.low_stock_threshold else "UPCOMING_3D",
            })

    waste_rows = db.scalars(
        select(InventoryMovement)
        .options(joinedload(InventoryMovement.product))
        .where(InventoryMovement.movement_type == InventoryMovementType.WASTE, InventoryMovement.created_at >= since_utc)
    ).all()
    waste_by_pid: dict[int, int] = defaultdict(int)
    for w in waste_rows:
        waste_by_pid[w.product_id] += abs(int(w.quantity_delta))

    food_wastage_analysis = []
    for pid, w_units in sorted(waste_by_pid.items(), key=lambda kv: -kv[1])[:10]:
        pr = prod_by_id.get(pid)
        u_cost = unit_costs.get(pid, 0) or (pr.effective_price_paise if pr else 0)
        sold_u = item_units_sold.get(pid, 0)
        waste_ratio = round(w_units * 100.0 / max(1, sold_u + w_units), 1)
        food_wastage_analysis.append({
            "menuItemId": pid,
            "name": pr.name if pr else f"Item #{pid}",
            "dataKind": "ACTUAL",
            "unitsWasted": w_units,
            "wasteRatioPct": waste_ratio,
            "lossPaise": w_units * u_cost,
            "recommendation": "Cut batch pre-prep by 20% and cook last-mile batches on demand." if waste_ratio >= 15 else "Monitor holding temperatures and FEFO rotation.",
        })

    sales_anomalies = []
    if len(daily_rev_actual) >= 3 and overall_daily_std > 0:
        for d_obs, r_p in sorted(daily_rev_actual.items(), reverse=True):
            z = (r_p - overall_daily_mean) / overall_daily_std
            if abs(z) >= 1.75:
                sales_anomalies.append({
                    "date": d_obs.isoformat(),
                    "dataKind": "ACTUAL",
                    "revenuePaise": r_p,
                    "orders": daily_orders_actual[d_obs],
                    "zScore": round(z, 2),
                    "direction": "SPIKE" if z > 0 else "DROP",
                    "explanation": (
                        "Unusually high campus demand spike above historical mean."
                        if z > 0
                        else "Significant revenue dip below normal weekday baseline."
                    ),
                })

    total_buyers = len(customer_orders)
    repeat_buyers = sum(1 for c in customer_orders.values() if c >= 2)
    diet_counts = db.execute(
        select(User.dietary_preference, func.count(User.id))
        .where(User.role == UserRole.STUDENT, User.dietary_preference.is_not(None))
        .group_by(User.dietary_preference)
    ).all()
    customer_trends = {
        "dataKind": "ACTUAL",
        "activeBuyers": total_buyers,
        "repeatBuyers": repeat_buyers,
        "repeatRatePct": round(repeat_buyers * 100.0 / total_buyers, 1) if total_buyers else 0.0,
        "topComboPairs": [
            {"pair": f"{a} + {b}", "coOccurrences": cnt}
            for (a, b), cnt in sorted(pair_counts.items(), key=lambda kv: -kv[1])[:6]
        ],
        "dietaryPreferences": [{"preference": str(r[0]), "students": int(r[1])} for r in diet_counts if r[0]],
    }

    food_prods = [p for p in products if p.item_type == "food"] or products
    avg_units_benchmark = (sum(item_units_sold.get(p.id, 0) for p in food_prods) / max(1, len(food_prods)))
    unit_margins = {p.id: (p.effective_price_paise - unit_costs.get(p.id, 0)) for p in food_prods}
    avg_margin_benchmark = sum(unit_margins.values()) / max(1, len(unit_margins))

    menu_engineering = []
    quadrant_Guidance = {
        "STAR": "High popularity & high margin — keep prominent on home banner and protect recipe consistency.",
        "PLOWHORSE": "High popularity & lower margin — bundle with high-margin drinks/sides or review portion cost.",
        "PUZZLE": "High margin & lower volume — highlight in AI recommendations or run a limited-time student promo.",
        "DOG": "Low volume & low margin — candidate for recipe revamp, price adjustment, or seasonal replacement.",
    }
    for p in food_prods:
        u_sold = item_units_sold.get(p.id, 0)
        u_margin = unit_margins[p.id]
        high_pop = u_sold >= avg_units_benchmark and u_sold > 0
        high_mar = u_margin >= avg_margin_benchmark
        if high_pop and high_mar:
            quad = "STAR"
        elif high_pop and not high_mar:
            quad = "PLOWHORSE"
        elif not high_pop and high_mar:
            quad = "PUZZLE"
        else:
            quad = "DOG"
        menu_engineering.append({
            "menuItemId": p.id,
            "name": p.name,
            "quadrant": quad,
            "dataKind": "ACTUAL",
            "unitsSold": u_sold,
            "pricePaise": p.effective_price_paise,
            "unitCostPaise": unit_costs.get(p.id, 0),
            "unitMarginPaise": u_margin,
            "action": quadrant_Guidance[quad],
        })

    window_rev_paise = sum(o.effective_total_paise for o in paid_orders)
    actuals_meta = {
        "windowDays": days,
        "windowPaidOrders": len(paid_orders),
        "windowRevenueRupees": str(paise_to_rupees(window_rev_paise)),
        "topItemName": popular_foods[0]["name"] if popular_foods and popular_foods[0]["unitsSold"] > 0 else None,
        "anomalyCount": len(sales_anomalies),
    }
    preds_meta = {
        "next7DaysProjectedRevenueRupees": str(paise_to_rupees(proj_7d_total_paise)),
        "reorderAlertsCount": len(reorder_recommendations),
    }
    summary_text, summary_model = _generate_executive_summary_narrative(actuals_meta, preds_meta)

    _record_ai_telemetry(
        db,
        user_id=admin.id,
        feature="admin_bi_suite",
        model_version=summary_model,
        raw_input_for_hash=f"{today_local.isoformat()}:{days}",
        metadata={"orders_analyzed": len(paid_orders), "reorders": len(reorder_recommendations)},
    )
    db.commit()

    return ok(
        request,
        {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "windowDays": days,
            "transparencyNotice": "Items marked dataKind='ACTUAL' are exact PostgreSQL/SQLAlchemy query results; items marked dataKind='PREDICTION' are statistical projections.",
            "dailyBusinessSummary": {
                "narrative": summary_text,
                "modelVersion": summary_model,
                "actualsSnapshot": actuals_meta,
                "predictionsSnapshot": preds_meta,
            },
            "foodDemandPredictions": food_demand_predictions[:15],
            "salesForecast7Day": sales_forecast_7d,
            "popularFoods": popular_foods,
            "slowSellingFoods": slow_selling_foods,
            "peakHourPredictions": peak_hour_predictions,
            "ingredientDemandEstimates": ingredient_estimates,
            "inventoryReorderRecommendations": reorder_recommendations[:20],
            "foodWastageAnalysis": food_wastage_analysis,
            "salesAnomalies": sales_anomalies[:10],
            "customerPurchasingTrends": customer_trends,
            "menuEngineeringMatrix": menu_engineering,
        },
    )
