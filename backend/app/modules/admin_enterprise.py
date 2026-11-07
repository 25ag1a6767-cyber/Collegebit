"""CampusBite AI v2.0 — Enterprise Administration Modules (/api/v1/admin/*).

Covers:
- Granular Staff Permissions (PUT /users/{user_id}/permissions)
- Supplier Management (GET/POST/PATCH /suppliers)
- Purchase Orders & Automated Batch/Stock/Expense Receiving (GET/POST /purchase-orders, POST /purchase-orders/{id}/receive)
- Operational Expenses Ledger (GET/POST/DELETE /expenses)
- Complete Admin Order Management (GET /orders, POST /orders/{id}/status)
- Customer Feedback & Ratings Console (GET /feedback)
- Database Administration & Diagnostics Console (GET /database/overview, POST /database/analyze)
- Security Monitoring & Session Revocation Console (GET /security/overview, POST /security/users/{id}/revoke-sessions)
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any
import uuid

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.database import Base, engine, get_db
from app.envelope import ApiError, ok
from app.models import (
    CanteenCounter,
    Ingredient,
    InventoryMovementType,
    OperationalExpense,
    Order,
    OrderReview,
    OrderStatus,
    PaymentStatus,
    Product,
    ProductBatch,
    PurchaseOrder,
    RefreshSession,
    SecurityEvent,
    StockMovement,
    Supplier,
    User,
    UserRole,
    paise_to_rupees,
    rupees_to_paise,
)
from app.modules.common import (
    DEFAULT_STAFF_PERMISSIONS,
    as_utc,
    commit_and_flush_outbox,
    notify_user,
    parse_json_safe,
    record_audit,
    record_order_status_change,
    record_product_stock_movement,
    serialize_order_v1,
    serialize_user_v1,
)
from app.modules.orders_payments import _initiate_gateway_refund, _restore_order_inventory_once
from app.security import require_roles, revoke_refresh_sessions

router = APIRouter(prefix="/admin", tags=["v1-admin-enterprise"])

class StaffPermissionsUpdateIn(BaseModel):
    canAcceptOrders: bool = True
    canRejectOrders: bool = True
    canUpdatePrep: bool = True
    canVerifyPickup: bool = True
    canManageAvailability: bool = True
    canManageInventory: bool = True

@router.put("/users/{user_id}/permissions")
def update_staff_permissions_v1(
    user_id: int,
    payload: StaffPermissionsUpdateIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    perms_dict = {k: bool(getattr(payload, k)) for k in DEFAULT_STAFF_PERMISSIONS}
    target.staff_permissions_json = json.dumps(perms_dict, separators=(",", ":"), sort_keys=True)
    record_audit(db, "admin.staff_permissions_updated", "user", target.id, admin.id, perms_dict)
    db.commit()
    db.refresh(target)
    return ok(request, serialize_user_v1(db, target))

class SupplierCreateV1In(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    contactName: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    notes: str | None = Field(default=None, max_length=500)
    isActive: bool = True

class SupplierPatchV1In(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    contactName: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=40)
    email: EmailStr | None = None
    notes: str | None = Field(default=None, max_length=500)
    isActive: bool | None = None

def _serialize_supplier(s: Supplier) -> dict[str, Any]:
    return {
        "id": s.id,
        "name": s.name,
        "contactName": s.contact_name,
        "phone": s.phone,
        "email": s.email,
        "notes": s.notes,
        "isActive": s.is_active,
        "createdAt": as_utc(s.created_at),
    }

@router.get("/suppliers")
def list_admin_suppliers_v1(
    request: Request,
    includeInactive: bool = True,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    stmt = select(Supplier).order_by(Supplier.name.asc())
    if not includeInactive:
        stmt = stmt.where(Supplier.is_active.is_(True))
    rows = db.scalars(stmt).all()
    return ok(request, {"items": [_serialize_supplier(s) for s in rows]})

@router.post("/suppliers", status_code=201)
def create_admin_supplier_v1(
    payload: SupplierCreateV1In,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    if db.scalar(select(Supplier).where(func.lower(Supplier.name) == payload.name.strip().lower())):
        raise ApiError(409, "DUPLICATE_SUPPLIER", "A supplier with this name already exists.")
    sup = Supplier(
        name=payload.name.strip(),
        contact_name=(payload.contactName or "").strip() or None,
        phone=(payload.phone or "").strip() or None,
        email=str(payload.email).lower().strip() if payload.email else None,
        notes=(payload.notes or "").strip() or None,
        is_active=payload.isActive,
    )
    db.add(sup)
    db.flush()
    record_audit(db, "supplier.created", "supplier", sup.id, admin.id, {"name": sup.name})
    db.commit()
    db.refresh(sup)
    return ok(request, _serialize_supplier(sup), status_code=201)

@router.patch("/suppliers/{supplier_id}")
def patch_admin_supplier_v1(
    supplier_id: int,
    payload: SupplierPatchV1In,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    sup = db.get(Supplier, supplier_id)
    if sup is None:
        raise ApiError(404, "SUPPLIER_NOT_FOUND", "Supplier not found.")
    updates = payload.model_dump(exclude_unset=True)
    if "name" in updates and updates["name"]:
        sup.name = updates["name"].strip()
    if "contactName" in updates:
        sup.contact_name = (updates["contactName"] or "").strip() or None
    if "phone" in updates:
        sup.phone = (updates["phone"] or "").strip() or None
    if "email" in updates:
        sup.email = str(updates["email"]).lower().strip() if updates["email"] else None
    if "notes" in updates:
        sup.notes = (updates["notes"] or "").strip() or None
    if "isActive" in updates and updates["isActive"] is not None:
        sup.is_active = bool(updates["isActive"])
    record_audit(db, "supplier.updated", "supplier", sup.id, admin.id, {"fields": sorted(updates.keys())})
    db.commit()
    db.refresh(sup)
    return ok(request, _serialize_supplier(sup))

class PurchaseOrderLineV1In(BaseModel):
    menuItemId: int | None = Field(default=None, gt=0)
    ingredientId: int | None = Field(default=None, gt=0)
    quantity: Decimal = Field(gt=Decimal("0"))
    unitCostPaise: int = Field(ge=0, le=10_000_000)
    expiryDate: date | None = None

class PurchaseOrderCreateV1In(BaseModel):
    supplierId: int = Field(gt=0)
    lines: list[PurchaseOrderLineV1In] = Field(min_length=1, max_length=50)
    notes: str | None = Field(default=None, max_length=500)
    markOrdered: bool = True

def _serialize_po(po: PurchaseOrder) -> dict[str, Any]:
    lines = parse_json_safe(po.lines_json, [])
    cost_p = int(po.total_cost_paise or 0)
    return {
        "id": po.id,
        "poNumber": f"PO-{po.id:04d}",
        "supplierId": po.supplier_id,
        "supplierName": po.supplier.name if po.supplier else f"Supplier #{po.supplier_id}",
        "status": po.status.upper(),
        "lines": lines,
        "totalCostPaise": cost_p,
        "totalCostRupees": str(paise_to_rupees(cost_p)),
        "notes": po.notes,
        "createdAt": as_utc(po.created_at),
        "orderedAt": as_utc(po.ordered_at),
        "receivedAt": as_utc(po.received_at),
    }

@router.get("/purchase-orders")
def list_admin_purchase_orders_v1(
    request: Request,
    status: str | None = None,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    stmt = select(PurchaseOrder).options(joinedload(PurchaseOrder.supplier)).order_by(PurchaseOrder.created_at.desc())
    if status:
        stmt = stmt.where(func.lower(PurchaseOrder.status) == status.strip().lower())
    rows = db.scalars(stmt.limit(200)).all()
    return ok(request, {"items": [_serialize_po(po) for po in rows]})

@router.post("/purchase-orders", status_code=201)
def create_admin_purchase_order_v1(
    payload: PurchaseOrderCreateV1In,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    sup = db.get(Supplier, payload.supplierId)
    if sup is None or not sup.is_active:
        raise ApiError(404, "SUPPLIER_NOT_FOUND", "Active supplier not found.")

    normalized_lines: list[dict[str, Any]] = []
    total_cost_paise = 0
    for ln in payload.lines:
        if bool(ln.menuItemId) == bool(ln.ingredientId):
            raise ApiError(422, "INVALID_PO_LINE", "Each line must specify either menuItemId or ingredientId.")
        if ln.menuItemId is not None:
            if ln.quantity != ln.quantity.to_integral_value():
                raise ApiError(422, "INTEGER_QUANTITY_REQUIRED", "Menu item purchase lines require whole unit quantities.")
            prod = db.get(Product, ln.menuItemId)
            if prod is None:
                raise ApiError(404, "ITEM_NOT_FOUND", f"Menu item {ln.menuItemId} not found.")
            qty_int = int(ln.quantity)
            line_paise = qty_int * ln.unitCostPaise
            total_cost_paise += line_paise
            normalized_lines.append({
                "kind": "menuItem",
                "menuItemId": prod.id,
                "name": prod.name,
                "quantity": qty_int,
                "unit": "units",
                "unitCostPaise": ln.unitCostPaise,
                "lineCostPaise": line_paise,
                "expiryDate": ln.expiryDate.isoformat() if ln.expiryDate else None,
            })
        else:
            ing = db.get(Ingredient, ln.ingredientId)
            if ing is None:
                raise ApiError(404, "INGREDIENT_NOT_FOUND", f"Ingredient {ln.ingredientId} not found.")
            line_paise = int((ln.quantity * Decimal(ln.unitCostPaise)).quantize(Decimal("1")))
            total_cost_paise += line_paise
            normalized_lines.append({
                "kind": "ingredient",
                "ingredientId": ing.id,
                "name": ing.name,
                "quantity": str(ln.quantity),
                "unit": ing.unit,
                "unitCostPaise": ln.unitCostPaise,
                "lineCostPaise": line_paise,
                "expiryDate": ln.expiryDate.isoformat() if ln.expiryDate else None,
            })

    now = datetime.now(timezone.utc)
    po = PurchaseOrder(
        supplier_id=sup.id,
        created_by_user_id=admin.id,
        status="ordered" if payload.markOrdered else "draft",
        lines_json=json.dumps(normalized_lines, separators=(",", ":")),
        total_cost_paise=total_cost_paise,
        notes=(payload.notes or "").strip() or None,
        ordered_at=now if payload.markOrdered else None,
    )
    db.add(po)
    db.flush()
    record_audit(db, "purchase_order.created", "purchase_order", po.id, admin.id, {"supplier_id": sup.id, "total_cost_paise": total_cost_paise})
    db.commit()
    po = db.scalar(select(PurchaseOrder).options(joinedload(PurchaseOrder.supplier)).where(PurchaseOrder.id == po.id))
    return ok(request, _serialize_po(po), status_code=201)

@router.post("/purchase-orders/{po_id}/receive")
def receive_admin_purchase_order_v1(
    po_id: int,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    po = db.scalar(select(PurchaseOrder).options(joinedload(PurchaseOrder.supplier)).where(PurchaseOrder.id == po_id).with_for_update())
    if po is None:
        raise ApiError(404, "PO_NOT_FOUND", "Purchase order not found.")
    if po.status.lower() == "received":
        raise ApiError(409, "PO_ALREADY_RECEIVED", "This purchase order has already been received into inventory.")
    if po.status.lower() == "cancelled":
        raise ApiError(409, "PO_CANCELLED", "Cannot receive a cancelled purchase order.")

    lines = parse_json_safe(po.lines_json, [])
    today_d = date.today()
    created_batches = 0
    computed_total_paise = 0

    for idx, ln in enumerate(lines, start=1):
        if not isinstance(ln, dict):
            continue
        kind = ln.get("kind") or ("menuItem" if ln.get("menuItemId") or ln.get("product_id") else "ingredient")
        unit_cost_p = int(ln.get("unitCostPaise") or rupees_to_paise(ln.get("unit_cost") or 0))
        exp_raw = ln.get("expiryDate") or ln.get("expiry_date")
        exp_date = date.fromisoformat(exp_raw) if exp_raw else None

        if kind == "menuItem":
            pid = int(ln.get("menuItemId") or ln.get("product_id"))
            qty = int(ln.get("quantity") or 0)
            if qty <= 0:
                continue
            prod = db.scalar(select(Product).options(joinedload(Product.category)).where(Product.id == pid).with_for_update())
            if prod is None:
                continue
            batch_code = f"PO{po.id:04d}-L{idx}-{uuid.uuid4().hex[:6].upper()}"
            db.add(
                ProductBatch(
                    product_id=prod.id,
                    supplier_id=po.supplier_id,
                    batch_code=batch_code,
                    quantity_received=qty,
                    quantity_remaining=qty,
                    unit_cost=paise_to_rupees(unit_cost_p) if unit_cost_p > 0 else None,
                    received_date=today_d,
                    expiry_date=exp_date,
                    notes=f"Received via PO-{po.id:04d}",
                    created_by_user_id=admin.id,
                )
            )
            created_batches += 1
            before_q = prod.stock_quantity
            prod.stock_quantity += qty
            if unit_cost_p > 0:
                prod.cost_paise = unit_cost_p
            if prod.category and prod.category.is_active and not prod.is_archived:
                prod.is_available = True
            record_product_stock_movement(
                db, prod, qty, InventoryMovementType.RECEIPT, f"PO-{po.id:04d} received", actor_user_id=admin.id, before=before_q
            )
            computed_total_paise += qty * unit_cost_p
        else:
            iid = int(ln.get("ingredientId") or ln.get("ingredient_id"))
            qty_dec = Decimal(str(ln.get("quantity") or "0")).quantize(Decimal("0.01"))
            if qty_dec <= 0:
                continue
            ing = db.scalar(select(Ingredient).where(Ingredient.id == iid).with_for_update())
            if ing is None:
                continue
            before_ing = Decimal(ing.current_quantity)
            after_ing = (before_ing + qty_dec).quantize(Decimal("0.01"))
            ing.current_quantity = after_ing
            if unit_cost_p > 0:
                ing.cost_per_unit = paise_to_rupees(unit_cost_p)
            db.add(
                StockMovement(
                    ingredient_id=ing.id,
                    movement_type="restock",
                    quantity_delta=qty_dec,
                    quantity_before=before_ing,
                    quantity_after=after_ing,
                    reference_type="purchase_order",
                    reference_id=str(po.id),
                    reason=f"PO-{po.id:04d} received",
                    actor_user_id=admin.id,
                )
            )
            computed_total_paise += int((qty_dec * Decimal(unit_cost_p)).quantize(Decimal("1")))

    now = datetime.now(timezone.utc)
    po.status = "received"
    po.received_at = now
    if not po.total_cost_paise:
        po.total_cost_paise = computed_total_paise

    final_cost_p = int(po.total_cost_paise or computed_total_paise)
    if final_cost_p > 0:
        db.add(
            OperationalExpense(
                expense_date=today_d,
                category="procurement",
                title=f"Supplier PO-{po.id:04d} ({po.supplier.name if po.supplier else 'Supplier'})",
                amount_paise=final_cost_p,
                reference_type="purchase_order",
                reference_id=str(po.id),
                notes=po.notes,
                created_by_user_id=admin.id,
            )
        )

    record_audit(
        db,
        "purchase_order.received",
        "purchase_order",
        po.id,
        admin.id,
        {"batches_created": created_batches, "total_cost_paise": final_cost_p},
    )
    commit_and_flush_outbox(db)
    db.refresh(po)
    return ok(request, {"purchaseOrder": _serialize_po(po), "batchesCreated": created_batches})

class ExpenseCreateIn(BaseModel):
    expenseDate: date | None = None
    category: str = Field(default="operations", pattern="^(ingredients|procurement|wages|utilities|packaging|maintenance|operations|other)$")
    counterId: int | None = Field(default=None, gt=0)
    title: str = Field(min_length=2, max_length=160)
    amountPaise: int = Field(gt=0, le=100_000_000)
    notes: str | None = Field(default=None, max_length=500)

@router.get("/expenses")
def list_admin_expenses_v1(
    request: Request,
    startDate: date | None = None,
    endDate: date | None = None,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    stmt = select(OperationalExpense).order_by(OperationalExpense.expense_date.desc(), OperationalExpense.id.desc())
    if startDate:
        stmt = stmt.where(OperationalExpense.expense_date >= startDate)
    if endDate:
        stmt = stmt.where(OperationalExpense.expense_date <= endDate)
    rows = db.scalars(stmt.limit(300)).all()
    return ok(
        request,
        {
            "totalPaise": sum(int(e.amount_paise) for e in rows),
            "items": [
                {
                    "id": e.id,
                    "expenseDate": e.expense_date.isoformat(),
                    "category": e.category,
                    "counterId": e.counter_id,
                    "title": e.title,
                    "amountPaise": e.amount_paise,
                    "amountRupees": str(paise_to_rupees(e.amount_paise)),
                    "referenceType": e.reference_type,
                    "referenceId": e.reference_id,
                    "notes": e.notes,
                    "createdAt": as_utc(e.created_at),
                }
                for e in rows
            ],
        },
    )

@router.post("/expenses", status_code=201)
def create_admin_expense_v1(
    payload: ExpenseCreateIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    if payload.counterId is not None and db.get(CanteenCounter, payload.counterId) is None:
        raise ApiError(404, "COUNTER_NOT_FOUND", "Counter not found.")
    exp = OperationalExpense(
        expense_date=payload.expenseDate or date.today(),
        category=payload.category,
        counter_id=payload.counterId,
        title=payload.title.strip(),
        amount_paise=payload.amountPaise,
        notes=(payload.notes or "").strip() or None,
        created_by_user_id=admin.id,
    )
    db.add(exp)
    db.flush()
    record_audit(db, "expense.created", "operational_expense", exp.id, admin.id, {"category": exp.category, "amount_paise": exp.amount_paise})
    db.commit()
    db.refresh(exp)
    return ok(
        request,
        {
            "id": exp.id,
            "expenseDate": exp.expense_date.isoformat(),
            "category": exp.category,
            "title": exp.title,
            "amountPaise": exp.amount_paise,
            "amountRupees": str(paise_to_rupees(exp.amount_paise)),
        },
        status_code=201,
    )

@router.delete("/expenses/{expense_id}")
def delete_admin_expense_v1(
    expense_id: int,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    exp = db.get(OperationalExpense, expense_id)
    if exp is None:
        raise ApiError(404, "EXPENSE_NOT_FOUND", "Expense record not found.")
    record_audit(db, "expense.deleted", "operational_expense", exp.id, admin.id, {"title": exp.title, "amount_paise": exp.amount_paise})
    db.delete(exp)
    db.commit()
    return ok(request, {"deletedId": expense_id})

class AdminOrderStatusOverrideIn(BaseModel):
    status: str = Field(pattern="^(accepted|preparing|ready|collected|cancelled)$")
    reason: str = Field(default="Administrator order override", min_length=3, max_length=255)

@router.get("/orders")
def list_all_admin_orders_v1(
    request: Request,
    status: str | None = None,
    paymentStatus: str | None = None,
    counterId: int | None = Query(default=None, gt=0),
    search: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=100, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    stmt = select(Order).options(joinedload(Order.items), joinedload(Order.user)).order_by(Order.created_at.desc())
    if status:
        try:
            stmt = stmt.where(Order.status == OrderStatus(status.strip().lower()))
        except ValueError:
            raise ApiError(422, "INVALID_STATUS", f"Unknown order status '{status}'.")
    if paymentStatus:
        try:
            stmt = stmt.where(Order.payment_status == PaymentStatus(paymentStatus.strip().lower()))
        except ValueError:
            raise ApiError(422, "INVALID_PAYMENT_STATUS", f"Unknown payment status '{paymentStatus}'.")
    if counterId is not None:
        stmt = stmt.where(Order.counter_id == counterId)
    if search and search.strip():
        term = f"%{search.strip()}%"
        stmt = stmt.join(Order.user).where(
            or_(Order.order_number.ilike(term), User.full_name.ilike(term), User.email.ilike(term))
        )
    rows = db.scalars(stmt.offset(offset).limit(limit)).unique().all()
    return ok(
        request,
        {
            "count": len(rows),
            "items": [serialize_order_v1(o, include_customer=True) for o in rows],
        },
    )

@router.post("/orders/{order_id}/status")
def admin_override_order_status_v1(
    order_id: int,
    payload: AdminOrderStatusOverrideIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    order = db.scalar(
        select(Order)
        .options(joinedload(Order.items), joinedload(Order.user), joinedload(Order.payments), joinedload(Order.status_history))
        .where(Order.id == order_id)
        .with_for_update()
    )
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")

    desired = payload.status.strip().lower()
    prev = order.status.value
    now = datetime.now(timezone.utc)
    refund_info = None

    if desired == "cancelled":
        if order.status in {OrderStatus.COMPLETED, OrderStatus.COLLECTED, OrderStatus.CANCELLED, OrderStatus.REFUND_PENDING}:
            raise ApiError(409, "INVALID_TRANSITION", f"Cannot cancel order in state '{order.status.value}'.")
        _restore_order_inventory_once(db, order, f"Admin cancelled #{order.id}: {payload.reason}", actor_user_id=admin.id)
        order.cancelled_at = now
        if order.payment_status == PaymentStatus.PAID:
            order.status = OrderStatus.REFUND_PENDING
            order.payment_status = PaymentStatus.REFUND_PENDING
            paid_rec = next((p for p in order.payments if p.status == "SUCCESS"), None)
            rf = _initiate_gateway_refund(db, order, paid_rec, order.effective_total_paise, payload.reason, admin.id)
            refund_info = {"refundId": rf.id, "status": rf.status, "amountPaise": rf.amount_paise}
        else:
            order.status = OrderStatus.CANCELLED
    else:
        if order.payment_status != PaymentStatus.PAID:
            raise ApiError(409, "PAYMENT_REQUIRED", "Cannot advance unpaid order to fulfillment states.")
        order.status = OrderStatus(desired)
        order.handled_by_staff_id = order.handled_by_staff_id or admin.id
        if desired == "accepted" and order.accepted_at is None:
            order.accepted_at = now
        elif desired == "ready":
            order.ready_at = now
        elif desired == "collected":
            order.collected_at = now

    record_order_status_change(db, order, prev, order.status.value.upper(), admin.id, payload.reason)
    notify_user(
        db,
        order.user_id,
        "order_status",
        f"Order #{order.order_number or order.id} updated",
        f"Order status updated to {order.status.value.upper()}: {payload.reason}",
        entity_type="order",
        entity_id=order.id,
    )
    record_audit(db, "admin.order_status_override", "order", order.id, admin.id, {"from": prev, "to": order.status.value, "reason": payload.reason})
    commit_and_flush_outbox(db)
    db.refresh(order)
    return ok(request, {"order": serialize_order_v1(order, include_customer=True, include_history=True), "refund": refund_info})

@router.get("/feedback")
def list_admin_customer_feedback_v1(
    request: Request,
    minRating: int | None = Query(default=None, ge=1, le=5),
    maxRating: int | None = Query(default=None, ge=1, le=5),
    limit: int = Query(default=100, ge=1, le=300),
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    stmt = (
        select(OrderReview)
        .options(joinedload(OrderReview.user), joinedload(OrderReview.order).joinedload(Order.items))
        .order_by(OrderReview.created_at.desc())
    )
    if minRating is not None:
        stmt = stmt.where(OrderReview.rating >= minRating)
    if maxRating is not None:
        stmt = stmt.where(OrderReview.rating <= maxRating)
    reviews = db.scalars(stmt.limit(limit)).unique().all()

    all_ratings = db.scalars(select(OrderReview.rating)).all()
    avg_rating = round(sum(all_ratings) / len(all_ratings), 2) if all_ratings else None
    dist = {str(star): sum(1 for r in all_ratings if r == star) for star in range(1, 6)}

    return ok(
        request,
        {
            "totalReviews": len(all_ratings),
            "averageRating": avg_rating,
            "ratingDistribution": dist,
            "items": [
                {
                    "id": rv.id,
                    "orderId": rv.order_id,
                    "orderNumber": rv.order.order_number if rv.order else f"#{rv.order_id}",
                    "customerName": rv.user.full_name if rv.user else f"User #{rv.user_id}",
                    "customerEmail": rv.user.email if rv.user else "",
                    "rating": rv.rating,
                    "comment": rv.comment,
                    "itemsOrdered": [it.product_name for it in (rv.order.items if rv.order else [])],
                    "createdAt": as_utc(rv.created_at),
                }
                for rv in reviews
            ],
        },
    )

EPHEMERAL_TABLES_SET = {
    "refresh_sessions",
    "pickup_tokens",
    "checkout_idempotency",
    "outbox_events",
    "ai_interactions",
    "temporary_api_logs",
}

@router.get("/database/overview")
def get_admin_database_overview_v1(
    request: Request,
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    cfg = get_settings()
    dialect = engine.dialect.name

    alembic_rev = "unknown"
    try:
        rev_row = db.execute(text("SELECT version_num FROM alembic_version LIMIT 1")).fetchone()
        if rev_row:
            alembic_rev = str(rev_row[0])
    except Exception:
        pass

    db_size_bytes: int | None = None
    if dialect == "postgresql":
        try:
            sz = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
            db_size_bytes = int(sz) if sz is not None else None
        except Exception:
            pass
    elif dialect == "sqlite":
        try:
            pc = db.execute(text("PRAGMA page_count")).scalar() or 0
            ps = db.execute(text("PRAGMA page_size")).scalar() or 4096
            db_size_bytes = int(pc) * int(ps)
        except Exception:
            pass

    table_stats = []
    total_rows = 0
    for tbl_name in sorted(Base.metadata.tables.keys()):
        try:
            cnt = int(db.execute(text(f'SELECT COUNT(*) FROM "{tbl_name}"')).scalar() or 0)
        except Exception:
            cnt = 0
        total_rows += cnt
        table_stats.append({
            "tableName": tbl_name,
            "rowCount": cnt,
            "retentionClass": "ephemeral_4d_eligible" if tbl_name in EPHEMERAL_TABLES_SET else "permanent_protected",
        })

    pool_info: dict[str, Any] = {"poolClass": type(engine.pool).__name__}
    if hasattr(engine.pool, "size"):
        try:
            pool_info["poolSize"] = engine.pool.size()  # type: ignore[misc]
            pool_info["checkedOut"] = engine.pool.checkedout()  # type: ignore[misc]
        except Exception:
            pass

    return ok(
        request,
        {
            "dialect": dialect,
            "alembicRevision": alembic_rev,
            "databaseSizeBytes": db_size_bytes,
            "totalTables": len(table_stats),
            "totalRowsAcrossTables": total_rows,
            "pgbouncerTransactionMode": cfg.db_use_pgbouncer_transaction_pool,
            "pool": pool_info,
            "tables": table_stats,
        },
    )

@router.post("/database/analyze")
def run_admin_database_analyze_v1(
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    dialect = engine.dialect.name
    integrity_status = "ok"
    if dialect == "sqlite":
        chk = db.execute(text("PRAGMA quick_check")).fetchone()
        integrity_status = str(chk[0]) if chk else "unknown"
        db.execute(text("ANALYZE"))
        db.execute(text("PRAGMA optimize"))
    else:
        db.execute(text("ANALYZE"))
    record_audit(db, "database.analyze_executed", "database", dialect, admin.id, {"integrity": integrity_status})
    db.commit()
    return ok(request, {"dialect": dialect, "integrityStatus": integrity_status, "analyzedAt": datetime.now(timezone.utc).isoformat()})

@router.get("/security/overview")
def get_admin_security_overview_v1(
    request: Request,
    limit: int = Query(default=60, ge=1, le=200),
    _: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    since_24h = now - timedelta(hours=24)

    events = db.scalars(select(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(limit)).all()
    events_24h = db.scalars(select(SecurityEvent).where(SecurityEvent.created_at >= since_24h)).all()

    failed_logins_24h = sum(1 for e in events_24h if e.event_type == "auth.login_failed")
    rate_limits_24h = sum(1 for e in events_24h if "rate_limit" in e.event_type)
    invalid_webhooks_24h = sum(1 for e in events_24h if "webhook" in e.event_type)

    active_sessions = db.scalar(
        select(func.count(RefreshSession.id)).where(RefreshSession.revoked_at.is_(None), RefreshSession.expires_at > now)
    ) or 0
    admins = db.scalars(select(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True))).all()
    mfa_enabled_admins = sum(1 for a in admins if a.mfa_enabled)

    return ok(
        request,
        {
            "summary24h": {
                "totalSecurityEvents": len(events_24h),
                "failedLogins": failed_logins_24h,
                "rateLimitBlocks": rate_limits_24h,
                "invalidWebhookSignatures": invalid_webhooks_24h,
                "activeRefreshSessions": int(active_sessions),
                "activeAdmins": len(admins),
                "mfaEnabledAdmins": mfa_enabled_admins,
            },
            "recentEvents": [
                {
                    "id": ev.id,
                    "eventType": ev.event_type,
                    "severity": ev.severity,
                    "ipAddress": ev.ip_address,
                    "identifier": ev.identifier,
                    "userId": ev.user_id,
                    "path": ev.path,
                    "details": parse_json_safe(ev.details_json, {}),
                    "createdAt": as_utc(ev.created_at),
                }
                for ev in events
            ],
        },
    )

@router.post("/security/users/{user_id}/revoke-sessions")
def revoke_user_all_sessions_v1(
    user_id: int,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    target = db.get(User, user_id)
    if target is None:
        raise ApiError(404, "USER_NOT_FOUND", "User not found.")
    target.token_version += 1
    revoked_count = revoke_refresh_sessions(db, user_id=target.id)
    db.add(
        SecurityEvent(
            event_type="security.sessions_revoked",
            severity="info",
            user_id=target.id,
            identifier=target.email,
            details_json=json.dumps({"revoked_by_admin_id": admin.id, "refresh_sessions_revoked": revoked_count}),
        )
    )
    record_audit(db, "security.user_sessions_revoked", "user", target.id, admin.id, {"revoked_refresh_sessions": revoked_count})
    db.commit()
    return ok(request, {"userId": target.id, "revokedRefreshSessions": revoked_count, "newTokenVersion": target.token_version})
