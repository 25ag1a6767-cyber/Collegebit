"""Production relational schema for ACE Canteen (SQLite & PostgreSQL)."""
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def rupees_to_paise(value: Decimal | int | float | str | None) -> int:
    if value is None:
        return 0
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    return int((dec * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def paise_to_rupees(paise: int | None) -> Decimal:
    if paise is None:
        return Decimal("0.00")
    return (Decimal(int(paise)) / Decimal(100)).quantize(Decimal("0.01"))


class UserRole(str, enum.Enum):
    STUDENT = "student"
    STAFF = "staff"
    ADMIN = "admin"


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    ACCEPTED = "accepted"
    PREPARING = "preparing"
    READY = "ready"
    COMPLETED = "completed"
    COLLECTED = "collected"
    CANCELLED = "cancelled"
    REFUND_PENDING = "refund_pending"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    REFUND_PENDING = "refund_pending"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"


def new_uuid() -> str:
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_uuid: Mapped[str | None] = mapped_column(String(36), default=new_uuid, unique=True, index=True, nullable=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    phone: Mapped[str | None] = mapped_column(String(30), unique=True, index=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, default="!not-set!")
    role: Mapped[UserRole] = mapped_column(Enum(UserRole, native_enum=False, length=20), default=UserRole.STUDENT, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    dietary_preference: Mapped[str | None] = mapped_column(String(50), nullable=True)
    staff_permissions_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    mfa_secret_encrypted: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mfa_recovery_hashes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=True)
    orders: Mapped[list["Order"]] = relationship(back_populates="user", foreign_keys="Order.user_id")
    counter_assignments: Mapped[list["UserCounterAssignment"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class RefreshSession(Base):
    """Rotating refresh token family for session management and revocation."""
    __tablename__ = "refresh_sessions"
    __table_args__ = (
        Index("ix_refresh_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_refresh_sessions_family", "family_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family_id: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    user: Mapped[User] = relationship()


class CanteenCounter(Base):
    """Physical pickup / kitchen service counter with independent slot capacity and hours."""
    __tablename__ = "canteen_counters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    location: Mapped[str] = mapped_column(String(150), nullable=False, default="Main Building")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    capacity_per_slot: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    opening_hours: Mapped[str] = mapped_column(String(500), default="09:00-17:00", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    assignments: Mapped[list["UserCounterAssignment"]] = relationship(back_populates="counter", cascade="all, delete-orphan")


class UserCounterAssignment(Base):
    """Scopes staff members to the specific canteen counters they are authorized to operate."""
    __tablename__ = "user_counter_assignments"
    __table_args__ = (UniqueConstraint("user_id", "counter_id", name="uq_user_counter"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    counter_id: Mapped[int] = mapped_column(ForeignKey("canteen_counters.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    user: Mapped[User] = relationship(back_populates="counter_assignments")
    counter: Mapped[CanteenCounter] = relationship(back_populates="assignments")


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (CheckConstraint("item_type IN ('food', 'stationery')", name="ck_categories_item_type"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    slug: Mapped[str] = mapped_column(String(90), unique=True, nullable=False)
    item_type: Mapped[str] = mapped_column(String(20), default="food", nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("ix_products_category_active", "category_id", "is_available"),
        Index("ix_products_counter_available", "counter_id", "is_available"),
        CheckConstraint("price > 0", name="ck_products_price_positive"),
        CheckConstraint("stock_quantity >= 0", name="ck_products_stock_nonnegative"),
        CheckConstraint("item_type IN ('food', 'stationery')", name="ck_products_item_type"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False)
    counter_id: Mapped[int | None] = mapped_column(ForeignKey("canteen_counters.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    item_type: Mapped[str] = mapped_column(String(20), default="food", nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    price_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    cost_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=True)
    stock_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    preparation_minutes: Mapped[int] = mapped_column(Integer, default=10, server_default="10", nullable=False)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(500))
    dietary_info: Mapped[str | None] = mapped_column(String(255))
    allergens: Mapped[str | None] = mapped_column(String(255))
    dietary_tags: Mapped[str | None] = mapped_column(String(255))
    allergen_tags: Mapped[str | None] = mapped_column(String(255))
    ingredients_list: Mapped[str | None] = mapped_column(Text)
    customizations_json: Mapped[str | None] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=True)
    category: Mapped[Category] = relationship(back_populates="products")
    counter: Mapped[CanteenCounter | None] = relationship()
    order_items: Mapped[list["OrderItem"]] = relationship(back_populates="product")
    recipe_items: Mapped[list["RecipeItem"]] = relationship(back_populates="product", cascade="all, delete-orphan")

    @property
    def category_name(self) -> str | None:
        return self.category.name if self.category else None

    @property
    def effective_price_paise(self) -> int:
        if self.price_paise is not None and self.price_paise > 0:
            return int(self.price_paise)
        return rupees_to_paise(self.price)


class MenuItemAvailability(Base):
    """Optional date/time service windows and slot-specific quantity caps for menu items."""
    __tablename__ = "menu_item_availability"
    __table_args__ = (Index("ix_menu_item_avail_product_date", "product_id", "service_date"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    service_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    start_time: Mapped[str] = mapped_column(String(8), nullable=False)
    end_time: Mapped[str] = mapped_column(String(8), nullable=False)
    available_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    product: Mapped[Product] = relationship()


class Cart(Base):
    """Server-persisted student shopping cart shared across web and Android."""
    __tablename__ = "carts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    counter_id: Mapped[int | None] = mapped_column(ForeignKey("canteen_counters.id", ondelete="SET NULL"), nullable=True)
    coupon_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    pickup_slot: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    items: Mapped[list["CartItem"]] = relationship(back_populates="cart", cascade="all, delete-orphan")


class CartItem(Base):
    __tablename__ = "cart_items"
    __table_args__ = (UniqueConstraint("cart_id", "product_id", name="uq_cart_product"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cart_id: Mapped[int] = mapped_column(ForeignKey("carts.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    customizations_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    cart: Mapped[Cart] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()


class Discount(Base):
    """Approved canteen discount / promotional offer configurable by admin."""
    __tablename__ = "discounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255))
    discount_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.00"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    min_order_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    max_discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    usage_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    staff_approvable: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("token_number", "token_date", name="uq_order_daily_token"),
        Index("ix_orders_status_created", "status", "created_at"),
        Index("ix_orders_counter_status_created", "counter_id", "status", "created_at"),
        CheckConstraint("subtotal >= 0", name="ck_orders_subtotal_nonnegative"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_uuid: Mapped[str | None] = mapped_column(String(36), default=new_uuid, unique=True, index=True, nullable=True)
    order_number: Mapped[str | None] = mapped_column(String(40), unique=True, index=True, nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    handled_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    counter_id: Mapped[int | None] = mapped_column(ForeignKey("canteen_counters.id", ondelete="SET NULL"), nullable=True)
    token_number: Mapped[int | None] = mapped_column(Integer)
    token_date: Mapped[str | None] = mapped_column(String(10))
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus, native_enum=False, length=30), default=OrderStatus.PENDING, nullable=False)
    payment_status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus, native_enum=False, length=30), default=PaymentStatus.PENDING, nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), server_default="0.00", nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), server_default="0.00", nullable=False)
    final_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), server_default="0.00", nullable=False)
    subtotal_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    discount_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    tax_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    total_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0)
    coupon_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    manual_pickup_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500))
    exception_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    gateway_order_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    payment_signature: Mapped[str | None] = mapped_column(String(128))
    pickup_slot: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    pickup_slot_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user: Mapped[User] = relationship(back_populates="orders", foreign_keys=[user_id])
    handled_by_staff: Mapped[User | None] = relationship(foreign_keys=[handled_by_staff_id])
    counter: Mapped[CanteenCounter | None] = relationship()
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    status_history: Mapped[list["OrderStatusHistory"]] = relationship(back_populates="order", cascade="all, delete-orphan")
    payments: Mapped[list["Payment"]] = relationship(back_populates="order", cascade="all, delete-orphan")

    @property
    def effective_total_paise(self) -> int:
        if self.total_paise is not None and self.total_paise > 0:
            return int(self.total_paise)
        if self.final_total and Decimal(self.final_total) > 0:
            return rupees_to_paise(self.final_total)
        return rupees_to_paise(self.subtotal)


class AppSetting(Base):
    """Small key/value store for runtime switches and operational policies."""
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        CheckConstraint("unit_price > 0", name="ck_order_items_unit_price_positive"),
        CheckConstraint("line_total >= 0", name="ck_order_items_line_total_nonnegative"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    product_name: Mapped[str] = mapped_column(String(120), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit_price_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    customizations_json: Mapped[str | None] = mapped_column(Text, default="{}")
    line_total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    line_total_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="order_items")


class OrderStatusHistory(Base):
    """Immutable state-transition log for every order."""
    __tablename__ = "order_status_history"
    __table_args__ = (Index("ix_order_status_history_order_created", "order_id", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_uuid: Mapped[str | None] = mapped_column(String(36), default=new_uuid, unique=True, index=True, nullable=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    previous_status: Mapped[str] = mapped_column(String(40), nullable=False)
    new_status: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    order: Mapped[Order] = relationship(back_populates="status_history")
    actor: Mapped[User | None] = relationship()


class Payment(Base):
    """Authoritative payment intent/capture record separate from order fulfillment state."""
    __tablename__ = "payments"
    __table_args__ = (
        Index("ix_payments_order_created", "order_id", "created_at"),
        Index("ix_payments_provider_payment", "provider_payment_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_uuid: Mapped[str | None] = mapped_column(String(36), default=new_uuid, unique=True, index=True, nullable=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(30), default="razorpay", nullable=False)
    provider_order_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    provider_payment_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    late_capture_flagged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    order: Mapped[Order] = relationship(back_populates="payments")
    refunds: Mapped[list["Refund"]] = relationship(back_populates="payment_record")


class Refund(Base):
    """Provider-backed refund record with gateway reference and lifecycle status."""
    __tablename__ = "refunds"
    __table_args__ = (Index("ix_refunds_order_created", "order_id", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_uuid: Mapped[str | None] = mapped_column(String(36), default=new_uuid, unique=True, index=True, nullable=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    payment_record_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"), nullable=True)
    payment_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider_refund_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    amount_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=True)
    order: Mapped[Order] = relationship()
    payment_record: Mapped[Payment | None] = relationship(back_populates="refunds")


class PickupToken(Base):
    """Short-lived opaque QR token issued for an eligible paid order."""
    __tablename__ = "pickup_tokens"
    __table_args__ = (Index("ix_pickup_tokens_order", "order_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_uuid: Mapped[str | None] = mapped_column(String(36), default=new_uuid, unique=True, index=True, nullable=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    order: Mapped[Order] = relationship()


class Pickup(Base):
    """Single-use collection receipt written atomically when an order is handed over."""
    __tablename__ = "pickups"
    __table_args__ = (UniqueConstraint("order_id", name="uq_pickups_order"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_uuid: Mapped[str | None] = mapped_column(String(36), default=new_uuid, unique=True, index=True, nullable=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    pickup_token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    collected_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    order: Mapped[Order] = relationship()
    collector: Mapped[User | None] = relationship()


class Ingredient(Base):
    """Raw kitchen ingredient tracked for recipe consumption, restocking and wastage."""
    __tablename__ = "ingredients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False, default="g")
    reorder_level: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    current_quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    cost_per_unit: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class RecipeItem(Base):
    """Maps a menu product to the ingredient quantity consumed per serving."""
    __tablename__ = "recipe_items"
    __table_args__ = (UniqueConstraint("product_id", "ingredient_id", name="uq_recipe_product_ingredient"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity_per_serving: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    product: Mapped[Product] = relationship(back_populates="recipe_items")
    ingredient: Mapped[Ingredient] = relationship()


class StockMovement(Base):
    """Append-only ledger for ingredient restocks, recipe consumption and wastage."""
    __tablename__ = "stock_movements"
    __table_args__ = (Index("ix_stock_movements_ingredient_created", "ingredient_id", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False)
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity_delta: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    quantity_before: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    quantity_after: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ingredient: Mapped[Ingredient] = relationship()
    actor: Mapped[User | None] = relationship()


class InventoryMovementType(str, enum.Enum):
    INITIAL = "initial"
    RECEIPT = "receipt"
    ADJUSTMENT = "adjustment"
    WASTE = "waste"
    RESERVATION = "reservation"
    RELEASE = "release"


class InventoryMovement(Base):
    __tablename__ = "inventory_movements"
    __table_args__ = (
        CheckConstraint("quantity_delta != 0", name="ck_inventory_movement_nonzero_delta"),
        CheckConstraint("quantity_before >= 0", name="ck_inventory_movement_before_nonnegative"),
        CheckConstraint("quantity_after >= 0", name="ck_inventory_movement_after_nonnegative"),
        Index("ix_inventory_movements_product_created", "product_id", "created_at"),
        Index("ix_inventory_movements_created", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    movement_type: Mapped[InventoryMovementType] = mapped_column(Enum(InventoryMovementType, native_enum=False, length=20), nullable=False)
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_before: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_after: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    product: Mapped[Product] = relationship()
    actor: Mapped[User | None] = relationship()
    order: Mapped[Order | None] = relationship()


class AuditEvent(Base):
    """Append-only record of privileged operational changes and important events."""
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_created", "created_at"), Index("ix_audit_events_actor_created", "actor_user_id", "created_at"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(80))
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    actor: Mapped[User | None] = relationship()


class Notification(Base):
    """Durable, per-user in-app notification with explicit read state."""
    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", "created_at"),
        Index("ix_notifications_user_read", "user_id", "is_read"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    notification_type: Mapped[str] = mapped_column(String(40), nullable=False, default="general")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[str | None] = mapped_column(String(80))
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PushSubscription(Base):
    """Browser Web Push subscription endpoint per authenticated user."""
    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint: Mapped[str] = mapped_column(String(600), unique=True, nullable=False)
    p256dh: Mapped[str] = mapped_column(String(200), nullable=False)
    auth: Mapped[str] = mapped_column(String(100), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OutboxEvent(Base):
    """Transactional outbox event for reliable WebSocket and push delivery."""
    __tablename__ = "outbox_events"
    __table_args__ = (Index("ix_outbox_events_delivered_created", "delivered_at", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    topic: Mapped[str] = mapped_column(String(60), nullable=False)
    counter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Favorite(Base):
    """Student's saved catalogue items; one row per student/product."""
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "product_id", name="uq_favorites_user_product"), Index("ix_favorites_user_created", "user_id", "created_at"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    product: Mapped[Product] = relationship()


class OrderReview(Base):
    """Verified student feedback; one review per completed paid order."""
    __tablename__ = "order_reviews"
    __table_args__ = (UniqueConstraint("order_id", name="uq_order_reviews_order"), CheckConstraint("rating >= 1 AND rating <= 5", name="ck_order_reviews_rating_range"), Index("ix_order_reviews_created", "created_at"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_uuid: Mapped[str | None] = mapped_column(String(36), default=new_uuid, unique=True, index=True, nullable=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow)
    order: Mapped[Order] = relationship()
    user: Mapped[User] = relationship()


class PaymentWebhookEvent(Base):
    """Durable idempotency ledger for payment-provider webhook deliveries."""
    __tablename__ = "payment_webhook_events"
    __table_args__ = (Index("ix_payment_webhook_events_received", "received_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    event_name: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    processing_status: Mapped[str] = mapped_column(String(30), default="processed", server_default="processed", nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Supplier(Base):
    """Canteen supplier contact record."""
    __tablename__ = "suppliers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    contact_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(40))
    email: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PurchaseOrder(Base):
    """Supplier purchase order; lines are stored as a validated JSON snapshot."""
    __tablename__ = "purchase_orders"
    __table_args__ = (Index("ix_purchase_orders_supplier_id", "supplier_id"), Index("ix_purchase_orders_status", "status"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    lines_json: Mapped[str] = mapped_column(Text, nullable=False)
    total_cost_paise: Mapped[int | None] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    supplier: Mapped[Supplier] = relationship()
    created_by: Mapped[User | None] = relationship()


class ProductBatch(Base):
    """Received stock batch with optional expiry date and supplier traceability."""
    __tablename__ = "product_batches"
    __table_args__ = (CheckConstraint("quantity_received > 0", name="ck_batch_received_positive"), CheckConstraint("quantity_remaining >= 0", name="ck_batch_remaining_nonnegative"), Index("ix_product_batches_expiry", "expiry_date"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"))
    batch_code: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity_received: Mapped[int] = mapped_column(Integer, nullable=False)
    quantity_remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    received_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(String(255))
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    product: Mapped[Product] = relationship()
    supplier: Mapped[Supplier | None] = relationship()


class BatchReservation(Base):
    """Tracks which received stock batches were consumed by a checkout reservation."""
    __tablename__ = "batch_reservations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_batch_reservation_quantity_positive"),
        Index("ix_batch_reservations_order", "order_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_uuid: Mapped[str | None] = mapped_column(String(36), default=new_uuid, unique=True, index=True, nullable=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False)
    batch_id: Mapped[int] = mapped_column(ForeignKey("product_batches.id", ondelete="RESTRICT"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CheckoutIdempotency(Base):
    """Durable user-scoped checkout idempotency ledger."""
    __tablename__ = "checkout_idempotency"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_checkout_idempotency_user_key"),
        Index("ix_checkout_idempotency_created", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="processing")
    response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    user: Mapped[User] = relationship()


class ForecastRun(Base):
    """Persisted demand-forecast run with measured backtest accuracy metrics."""
    __tablename__ = "forecast_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forecast_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(50), nullable=False)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    items: Mapped[list["ForecastItem"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class ForecastItem(Base):
    """Per-item, per-slot predicted quantity with uncertainty bounds and staff override."""
    __tablename__ = "forecast_items"
    __table_args__ = (Index("ix_forecast_items_run_product", "forecast_run_id", "product_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forecast_run_id: Mapped[int] = mapped_column(ForeignKey("forecast_runs.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    time_slot: Mapped[str] = mapped_column(String(50), nullable=False)
    predicted_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    lower_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    upper_bound: Mapped[int] = mapped_column(Integer, nullable=False)
    recommended_prep: Mapped[int] = mapped_column(Integer, nullable=False)
    confidence_score: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=Decimal("0.70"), nullable=False)
    low_confidence_warning: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    staff_approved_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    override_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actual_sold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_waste: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run: Mapped[ForecastRun] = relationship(back_populates="items")
    product: Mapped[Product] = relationship()


class AIInteraction(Base):
    """Privacy-preserving AI telemetry: stores input hashes and structured metadata, never PII or secrets."""
    __tablename__ = "ai_interactions"
    __table_args__ = (Index("ix_ai_interactions_feature_created", "feature", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    feature: Mapped[str] = mapped_column(String(50), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(60), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OperationalExpense(Base):
    """Recorded canteen operating, utility, wage, packaging or supplier expense for profit analytics."""
    __tablename__ = "operational_expenses"
    __table_args__ = (
        Index("ix_operational_expenses_date_cat", "expense_date", "category"),
        CheckConstraint("amount_paise >= 0", name="ck_operational_expenses_nonnegative"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    expense_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="ingredients")
    counter_id: Mapped[int | None] = mapped_column(ForeignKey("canteen_counters.id", ondelete="SET NULL"), nullable=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    reference_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    reference_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DailySalesArchive(Base):
    """Permanent aggregated daily financial & operational rollup preserved by the hybrid retention engine."""
    __tablename__ = "daily_sales_archives"
    __table_args__ = (
        UniqueConstraint("archive_date", "counter_key", name="uq_daily_sales_archive_date_counter"),
        Index("ix_daily_sales_archives_date", "archive_date"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    archive_date: Mapped[date] = mapped_column(Date, nullable=False)
    counter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counter_key: Mapped[int] = mapped_column(Integer, default=0, nullable=False)  # 0 = global rollup
    total_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    paid_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancelled_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unique_customers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gross_revenue_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    discount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    tax_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    net_revenue_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    refund_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    cogs_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    wastage_loss_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    operating_expense_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    estimated_profit_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class RetentionCleanupRun(Base):
    """Audit log of scheduled 4-day hybrid retention executions and backup-health safety gate checks."""
    __tablename__ = "retention_cleanup_runs"
    __table_args__ = (Index("ix_retention_cleanup_runs_started", "started_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retention_days: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="running")
    backup_healthy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    backup_detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archived_days_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deleted_counts_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)


class BackupRun(Base):
    """Catalog of encrypted GFS database backups (7 daily, 4 weekly, 3 monthly) and restore drills."""
    __tablename__ = "backup_runs"
    __table_args__ = (Index("ix_backup_runs_created", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="daily")
    database_engine: Mapped[str] = mapped_column(String(20), nullable=False)
    primary_path: Mapped[str] = mapped_column(String(500), nullable=False)
    secondary_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    restore_tested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="completed", nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)


class GeneratedReport(Base):
    """Audit trail of automated or on-demand PDF, Excel and CSV business reports."""
    __tablename__ = "generated_reports"
    __table_args__ = (Index("ix_generated_reports_created", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_type: Mapped[str] = mapped_column(String(50), nullable=False)
    period_label: Mapped[str | None] = mapped_column(String(60), nullable=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    format: Mapped[str] = mapped_column(String(15), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    emailed_to: Mapped[str | None] = mapped_column(String(500), nullable=True)
    summary_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SecurityEvent(Base):
    """Security monitoring event log: failed logins, rate limits, invalid webhooks, MFA failures."""
    __tablename__ = "security_events"
    __table_args__ = (Index("ix_security_events_type_created", "event_type", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="warning")
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    details_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TemporaryApiLog(Base):
    """Short-lived operational API diagnostic log purged automatically after 4 days."""
    __tablename__ = "temporary_api_logs"
    __table_args__ = (Index("ix_temporary_api_logs_created", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    path: Mapped[str] = mapped_column(String(200), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# --- Automatic Decimal <-> integer paise synchronization ---
@event.listens_for(Product, "before_insert")
@event.listens_for(Product, "before_update")
def _sync_product_paise(_mapper, _connection, target: Product) -> None:
    if target.price is not None:
        target.price_paise = rupees_to_paise(target.price)
    elif target.price_paise is not None:
        target.price = paise_to_rupees(target.price_paise)


@event.listens_for(Order, "before_insert")
@event.listens_for(Order, "before_update")
def _sync_order_paise(_mapper, _connection, target: Order) -> None:
    target.subtotal_paise = rupees_to_paise(target.subtotal)
    target.discount_paise = rupees_to_paise(target.discount_amount or Decimal("0.00"))
    target.tax_paise = rupees_to_paise(target.tax_amount or Decimal("0.00"))
    computed_total = max(0, target.subtotal_paise - target.discount_paise + target.tax_paise)
    if target.final_total is None or Decimal(target.final_total) == Decimal("0.00"):
        target.final_total = paise_to_rupees(computed_total)
        target.total_paise = computed_total
    else:
        target.total_paise = rupees_to_paise(target.final_total)


@event.listens_for(OrderItem, "before_insert")
@event.listens_for(OrderItem, "before_update")
def _sync_order_item_paise(_mapper, _connection, target: OrderItem) -> None:
    if target.unit_price is not None:
        target.unit_price_paise = rupees_to_paise(target.unit_price)
    if target.line_total is not None:
        target.line_total_paise = rupees_to_paise(target.line_total)


@event.listens_for(Refund, "before_insert")
@event.listens_for(Refund, "before_update")
def _sync_refund_paise(_mapper, _connection, target: Refund) -> None:
    if target.amount is not None:
        target.amount_paise = rupees_to_paise(target.amount)
    elif target.amount_paise is not None:
        target.amount = paise_to_rupees(target.amount_paise)
