"""Student loyalty rewards and prepaid-wallet account APIs."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.envelope import ApiError, ok
from app.models import Order, Payment, PaymentStatus, User, UserRole, RewardTransaction, WalletTransaction
from app.modules.common import (
    as_utc,
    commit_and_flush_outbox,
    record_audit,
    record_wallet_movement,
    reward_points_balance,
    wallet_balance_paise,
)
from app.modules.orders_payments import _mark_order_paid_and_notify
from app.security import get_current_user, require_roles

router = APIRouter(tags=["rewards-wallet"])


class WalletCreditIn(BaseModel):
    userId: int = Field(gt=0)
    amountPaise: int = Field(gt=0, le=5_000_000)
    reason: str = Field(min_length=3, max_length=255)
    referenceKey: str = Field(min_length=8, max_length=100)


@router.get("/rewards/me")
def get_my_rewards_v1(
    request: Request,
    user: User = Depends(require_roles("student")),
    db: Session = Depends(get_db),
):
    points = reward_points_balance(db, user.id)
    wallet = wallet_balance_paise(db, user.id)
    reward_rows = db.scalars(
        select(RewardTransaction)
        .where(RewardTransaction.user_id == user.id)
        .order_by(RewardTransaction.created_at.desc(), RewardTransaction.id.desc())
        .limit(30)
    ).all()
    wallet_rows = db.scalars(
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user.id)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
        .limit(30)
    ).all()
    return ok(request, {
        "pointsBalance": points,
        "walletBalancePaise": wallet,
        "pointsEarnedPerTenRupees": 1,
        "paisePerRedeemedPoint": 10,
        "rewards": [
            {
                "pointsDelta": row.points_delta,
                "reason": row.reason,
                "orderId": row.order_id,
                "createdAt": as_utc(row.created_at),
            }
            for row in reward_rows
        ],
        "walletTransactions": [
            {
                "amountPaise": row.amount_paise,
                "type": row.transaction_type,
                "reason": row.reason,
                "orderId": row.order_id,
                "createdAt": as_utc(row.created_at),
            }
            for row in wallet_rows
        ],
    })


@router.get("/wallet/me")
def get_my_wallet_v1(
    request: Request,
    limit: int = Query(default=30, ge=1, le=100),
    user: User = Depends(require_roles("student")),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user.id)
        .order_by(WalletTransaction.created_at.desc(), WalletTransaction.id.desc())
        .limit(limit)
    ).all()
    return ok(request, {
        "balancePaise": wallet_balance_paise(db, user.id),
        "transactions": [
            {
                "amountPaise": row.amount_paise,
                "type": row.transaction_type,
                "reason": row.reason,
                "orderId": row.order_id,
                "createdAt": as_utc(row.created_at),
            }
            for row in rows
        ],
        "topUps": "Prepaid credit is issued by an administrator after the canteen verifies payment.",
    })


@router.post("/admin/wallet/credit", status_code=201)
def issue_prepaid_wallet_credit_v1(
    payload: WalletCreditIn,
    request: Request,
    admin: User = Depends(require_roles("admin")),
    db: Session = Depends(get_db),
):
    student = db.scalar(
        select(User).where(User.id == payload.userId, User.role == UserRole.STUDENT).with_for_update()
    )
    if student is None:
        raise ApiError(404, "STUDENT_NOT_FOUND", "Student account not found.")
    reference = "admin-credit:" + payload.referenceKey.strip()
    movement = record_wallet_movement(
        db,
        student.id,
        payload.amountPaise,
        "prepaid_credit",
        payload.reason.strip(),
        reference,
        actor_user_id=admin.id,
    )
    balance = wallet_balance_paise(db, student.id)
    record_audit(
        db,
        "wallet.prepaid_credit_issued",
        "user",
        student.id,
        admin.id,
        {"amount_paise": payload.amountPaise, "wallet_transaction_id": movement.id},
    )
    db.commit()
    return ok(request, {
        "studentId": student.id,
        "transactionId": movement.id,
        "amountPaise": movement.amount_paise,
        "balancePaise": balance,
    }, status_code=201)


@router.post("/orders/{order_id}/pay-with-wallet", status_code=200)
def pay_order_with_wallet_v1(
    order_id: int,
    request: Request,
    user: User = Depends(require_roles("student")),
    db: Session = Depends(get_db),
):
    order = db.scalar(
        select(Order)
        .where(Order.id == order_id)
        .with_for_update()
    )
    if order is None:
        raise ApiError(404, "ORDER_NOT_FOUND", "Order not found.")
    if order.user_id != user.id:
        raise ApiError(403, "FORBIDDEN", "You can only pay for your own orders.")
    if order.payment_status == PaymentStatus.PAID:
        raise ApiError(409, "ORDER_ALREADY_PAID", "This order is already paid.")
    if order.payment_status != PaymentStatus.PENDING or order.status.value != "pending":
        raise ApiError(409, "ORDER_NOT_PAYABLE", "This order is no longer payable.")

    amount_paise = order.effective_total_paise
    movement = record_wallet_movement(
        db,
        user.id,
        -amount_paise,
        "order_payment",
        f"Prepaid wallet payment for order #{order.id}",
        f"order:{order.id}:wallet-payment",
        order_id=order.id,
    )
    payment = Payment(
        order_id=order.id,
        provider="campusbite_wallet",
        provider_payment_id=f"wallet-tx-{movement.id}",
        amount_paise=amount_paise,
        currency="INR",
        status="SUCCESS",
    )
    db.add(payment)
    db.flush()
    _mark_order_paid_and_notify(
        db,
        order,
        payment,
        f"wallet-tx-{movement.id}",
        None,
        user.id,
        "prepaid_wallet",
    )
    record_audit(
        db,
        "wallet.order_paid",
        "order",
        order.id,
        user.id,
        {"amount_paise": amount_paise, "wallet_transaction_id": movement.id},
    )
    commit_and_flush_outbox(db)
    return ok(request, {
        "orderId": order.id,
        "paymentStatus": order.payment_status.value,
        "amountPaise": amount_paise,
        "walletBalancePaise": wallet_balance_paise(db, user.id),
    })
