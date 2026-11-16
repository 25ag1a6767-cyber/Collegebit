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
    PARTIALLY_COLLECTED = "partially_collected"
    COMPLETED = "completed"
    COLLECTED = "collected"
    CANCELLED = "cancelled"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"


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
    loyalty_points_redeemed: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
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
    collected_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_collected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
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


class RewardTransaction(Base):
    """Append-only ledger for student reward point grants and redemptions."""
    __tablename__ = "reward_transactions"
    __table_args__ = (
        CheckConstraint("points_delta != 0", name="ck_reward_transaction_nonzero"),
        Index("ix_reward_transactions_user_created", "user_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    points_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    reference_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WalletTransaction(Base):
    """Append-only ledger for administratively issued prepaid credit and order spending."""
    __tablename__ = "wallet_transactions"
    __table_args__ = (
        CheckConstraint("amount_paise != 0", name="ck_wallet_transaction_nonzero"),
        Index("ix_wallet_transactions_user_created", "user_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    reference_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


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


class PickupSlot(Base):
    """Scheduled pickup slot capacity per physical counter (Counter 1 & Counter 2)."""
    __tablename__ = "pickup_slots"
    __table_args__ = (
        UniqueConstraint("counter_id", "slot_date", "slot_label", name="uq_pickup_slot_counter_date_label"),
        Index("ix_pickup_slots_date_counter", "slot_date", "counter_id"),
        CheckConstraint("max_capacity > 0", name="ck_pickup_slots_capacity_positive"),
        CheckConstraint("reserved_count >= 0", name="ck_pickup_slots_reserved_nonnegative"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    counter_id: Mapped[int] = mapped_column(ForeignKey("canteen_counters.id", ondelete="CASCADE"), nullable=False)
    slot_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    slot_label: Mapped[str] = mapped_column(String(60), nullable=False)
    max_capacity: Mapped[int] = mapped_column(Integer, default=250, nullable=False)
    reserved_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BulkOrder(Base):
    """Master bulk order supporting 1 to 3,000+ recipients with centralized or individual payments."""
    __tablename__ = "bulk_orders"
    __table_args__ = (
        Index("ix_bulk_orders_coord_created", "coordinator_id", "created_at"),
        Index("ix_bulk_orders_pickup_date_status", "pickup_date", "status"),
        CheckConstraint("total_paise >= 0", name="ck_bulk_orders_total_nonnegative"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bulk_order_number: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    coordinator_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    department_name: Mapped[str] = mapped_column(String(140), nullable=False)
    event_title: Mapped[str] = mapped_column(String(160), nullable=False, default="Department Meal Order")
    pickup_date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    pickup_slot: Mapped[str] = mapped_column(String(60), nullable=False, default="12:30-13:00")
    counter_id: Mapped[int | None] = mapped_column(ForeignKey("canteen_counters.id", ondelete="SET NULL"), nullable=True)
    cutoff_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payment_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="centralized")  # centralized | individual
    distribution_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="individual")  # individual | representative
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")  # draft | verified | reserved | preparing | packing | ready | partially_collected | collected | cancelled
    payment_status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")  # pending | partially_paid | paid | failed
    total_recipients: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_meal_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    paid_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    paid_recipients_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_generated_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    collected_recipients_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    inventory_reserved: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    coordinator: Mapped[User] = relationship(foreign_keys=[coordinator_id])
    items: Mapped[list["BulkOrderItem"]] = relationship(back_populates="bulk_order", cascade="all, delete-orphan")
    recipients: Mapped[list["BulkOrderRecipient"]] = relationship(back_populates="bulk_order", cascade="all, delete-orphan")
    payments: Mapped[list["BulkOrderPayment"]] = relationship(back_populates="bulk_order", cascade="all, delete-orphan")


class BulkOrderItem(Base):
    """Menu item line and batch preparation/packing state inside a master bulk order."""
    __tablename__ = "bulk_order_items"
    __table_args__ = (
        Index("ix_bulk_order_items_bulk_product", "bulk_order_id", "product_id"),
        CheckConstraint("quantity > 0", name="ck_bulk_order_items_qty_positive"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bulk_order_id: Mapped[int] = mapped_column(ForeignKey("bulk_orders.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    product_name: Mapped[str] = mapped_column(String(140), nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    packed_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ready_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    collected_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    line_total_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    prep_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)  # pending | preparing | packing | ready | collected
    bulk_order: Mapped[BulkOrder] = relationship(back_populates="items")


class BulkOrderRecipient(Base):
    """Individual recipient inside a master bulk order (supports 1 to 3,000+ recipients)."""
    __tablename__ = "bulk_order_recipients"
    __table_args__ = (
        UniqueConstraint("bulk_order_id", "recipient_identifier", name="uq_bulk_recipient_order_ident"),
        Index("ix_bulk_recipients_order_status", "bulk_order_id", "payment_status", "pickup_status"),
        Index("ix_bulk_recipients_user_id", "user_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bulk_order_id: Mapped[int] = mapped_column(ForeignKey("bulk_orders.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    recipient_identifier: Mapped[str] = mapped_column(String(120), nullable=False)
    full_name: Mapped[str] = mapped_column(String(140), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    payment_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)  # pending | paid | failed
    pickup_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)  # pending | eligible | collected | cancelled
    collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    collected_counter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    bulk_order: Mapped[BulkOrder] = relationship(back_populates="recipients")
    allocations: Mapped[list["BulkOrderAllocation"]] = relationship(back_populates="recipient", cascade="all, delete-orphan")


class BulkOrderAllocation(Base):
    """Specific meal item allocation assigned to a bulk order recipient."""
    __tablename__ = "bulk_order_allocations"
    __table_args__ = (
        Index("ix_bulk_allocations_order_recipient", "bulk_order_id", "recipient_id"),
        CheckConstraint("quantity > 0", name="ck_bulk_allocations_qty_positive"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bulk_order_id: Mapped[int] = mapped_column(ForeignKey("bulk_orders.id", ondelete="CASCADE"), nullable=False)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("bulk_order_recipients.id", ondelete="CASCADE"), nullable=False)
    bulk_order_item_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_order_items.id", ondelete="SET NULL"), nullable=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    product_name: Mapped[str] = mapped_column(String(140), nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    line_total_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)

    recipient: Mapped[BulkOrderRecipient] = relationship(back_populates="allocations")


class BulkOrderPayment(Base):
    """Payment record for either a centralized master bulk order or an individual recipient allocation."""
    __tablename__ = "bulk_order_payments"
    __table_args__ = (
        Index("ix_bulk_payments_order_recipient", "bulk_order_id", "recipient_id", "status"),
        CheckConstraint("amount_paise >= 0", name="ck_bulk_payments_amount_nonnegative"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bulk_order_id: Mapped[int] = mapped_column(ForeignKey("bulk_orders.id", ondelete="CASCADE"), nullable=False)
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_order_recipients.id", ondelete="SET NULL"), nullable=True)
    payer_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    provider: Mapped[str] = mapped_column(String(40), default="razorpay", nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(120), unique=True, index=True, nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(120), unique=True, index=True, nullable=True)
    signature_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), default="INR", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="CREATED", nullable=False)  # CREATED | PENDING | SUCCESS | FAILED
    late_capture_flagged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    bulk_order: Mapped[BulkOrder] = relationship(back_populates="payments")


class BulkMealToken(Base):
    """Cryptographic QR pickup token for a single bulk order recipient. Stores ONLY token_hash (SHA-256) in DB."""
    __tablename__ = "bulk_meal_tokens"
    __table_args__ = (
        Index("ix_bulk_meal_tokens_order_recipient", "bulk_order_id", "recipient_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bulk_order_id: Mapped[int] = mapped_column(ForeignKey("bulk_orders.id", ondelete="CASCADE"), nullable=False)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("bulk_order_recipients.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    key_nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    distribution_mode: Mapped[str] = mapped_column(String(20), default="individual", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active | redeemed | revoked | expired
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    distributed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    redeemed_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    redeemed_counter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class BulkPickupRecord(Base):
    """Immutable audit record of each redeemed bulk meal token at Counter 1 or Counter 2."""
    __tablename__ = "bulk_pickup_records"
    __table_args__ = (
        Index("ix_bulk_pickup_records_order_time", "bulk_order_id", "picked_up_at"),
        Index("ix_bulk_pickup_records_counter_time", "counter_id", "picked_up_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bulk_order_id: Mapped[int] = mapped_column(ForeignKey("bulk_orders.id", ondelete="CASCADE"), nullable=False)
    recipient_id: Mapped[int] = mapped_column(ForeignKey("bulk_order_recipients.id", ondelete="CASCADE"), nullable=False)
    token_id: Mapped[int] = mapped_column(ForeignKey("bulk_meal_tokens.id", ondelete="CASCADE"), unique=True, nullable=False)
    counter_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    staff_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    collected_via: Mapped[str] = mapped_column(String(30), default="individual_qr", nullable=False)  # individual_qr | representative_batch | manual_override
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)
    picked_up_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class InventoryReservation(Base):
    """Temporary or committed stock reservation with automatic expiry tracking."""
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        Index("ix_inv_reservations_product_status", "product_id", "status", "expires_at"),
        CheckConstraint("quantity > 0", name="ck_inv_reservations_qty_positive"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=True)
    bulk_order_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_orders.id", ondelete="CASCADE"), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active | committed | released | expired
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PaymentIncident(Base):
    """Permanent financial/fulfilment incident ledger under the strict no-refunds policy."""
    __tablename__ = "payment_incidents"
    __table_args__ = (
        Index("ix_payment_incidents_status_created", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    bulk_order_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_orders.id", ondelete="SET NULL"), nullable=True)
    recipient_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_order_recipients.id", ondelete="SET NULL"), nullable=True)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id", ondelete="SET NULL"), nullable=True)
    bulk_payment_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_order_payments.id", ondelete="SET NULL"), nullable=True)
    incident_type: Mapped[str] = mapped_column(String(50), nullable=False)  # late_capture | kitchen_unfulfilled | bulk_allocation_exception
    amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False)  # open | under_review | resolved
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    resolution_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    flagged_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BulkBackgroundJob(Base):
    """Tracks asynchronous/batched background jobs for large CSV imports, token generation, and exports."""
    __tablename__ = "bulk_background_jobs"
    __table_args__ = (
        Index("ix_bulk_bg_jobs_order_status", "bulk_order_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    bulk_order_id: Mapped[int | None] = mapped_column(ForeignKey("bulk_orders.id", ondelete="CASCADE"), nullable=True)
    job_type: Mapped[str] = mapped_column(String(40), nullable=False)  # csv_import | token_generation | token_export | reservation_cleanup
    status: Mapped[str] = mapped_column(String(30), default="queued", nullable=False)  # queued | running | completed | failed
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WalkinPosOrder(Base):
    """Staff-controlled walk-in POS cash order integrated with live inventory and daily settlements."""
    __tablename__ = "walkin_pos_orders"
    __table_args__ = (
        Index("ix_walkin_pos_created", "created_at"),
        CheckConstraint("total_paise >= 0", name="ck_walkin_pos_total_nonnegative"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pos_receipt_number: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    counter_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    staff_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    customer_category: Mapped[str] = mapped_column(String(20), default="student", nullable=False)  # student | faculty | guest
    customer_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payment_method: Mapped[str] = mapped_column(String(20), default="cash", nullable=False)  # cash | upi_pos
    total_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    items_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GuestOtpChallenge(Base):
    """OTP verification challenge for external guests, parents, and faculty/student email/mobile login."""
    __tablename__ = "guest_otp_challenges"
    __table_args__ = (
        Index("ix_guest_otp_phone_created", "phone_or_email", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone_or_email: Mapped[str] = mapped_column(String(255), nullable=False)
    role_category: Mapped[str] = mapped_column(String(20), default="guest", nullable=False)  # student | faculty | guest
    otp_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SaasTenant(Base):
    """Multi-college SaaS tenant (Platform -> College/Tenant -> Campus -> Canteen/Outlet)."""
    __tablename__ = "saas_tenants"
    __table_args__ = (
        Index("ix_saas_tenants_status_plan", "status", "plan_tier"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_slug: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    college_name: Mapped[str] = mapped_column(String(180), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="onboarding", nullable=False)  # onboarding | active | suspended | offboarding
    plan_tier: Mapped[str] = mapped_column(String(30), default="STARTER", nullable=False)  # STARTER | PROFESSIONAL | ENTERPRISE
    brand_primary_color: Mapped[str] = mapped_column(String(20), default="#0f172a", nullable=False)
    brand_accent_color: Mapped[str] = mapped_column(String(20), default="#f59e0b", nullable=False)
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    custom_domain: Mapped[str | None] = mapped_column(String(180), unique=True, nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    onboarding_step: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    max_campuses: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_outlets: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_staff_accounts: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    bulk_ordering_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    advanced_analytics_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    rate_limit_rpm: Mapped[int] = mapped_column(Integer, default=600, nullable=False)
    encrypted_gateway_config_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class SaasCampus(Base):
    """Physical campus belonging to a SaaS college tenant."""
    __tablename__ = "saas_campuses"
    __table_args__ = (
        Index("ix_saas_campuses_tenant", "tenant_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=False)
    campus_name: Mapped[str] = mapped_column(String(140), nullable=False)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SaasOutlet(Base):
    """Canteen or food court outlet inside a SaaS college campus."""
    __tablename__ = "saas_outlets"
    __table_args__ = (
        Index("ix_saas_outlets_tenant_campus", "tenant_id", "campus_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=False)
    campus_id: Mapped[int] = mapped_column(ForeignKey("saas_campuses.id", ondelete="CASCADE"), nullable=False)
    outlet_name: Mapped[str] = mapped_column(String(140), nullable=False)
    vendor_name: Mapped[str | None] = mapped_column(String(140), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SaasSubscription(Base):
    """SaaS subscription & billing lifecycle (separate from student food payments)."""
    __tablename__ = "saas_subscriptions"
    __table_args__ = (
        Index("ix_saas_subscriptions_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=False)
    plan_tier: Mapped[str] = mapped_column(String(30), default="STARTER", nullable=False)
    billing_cycle: Mapped[str] = mapped_column(String(20), default="monthly", nullable=False)  # monthly | annual
    status: Mapped[str] = mapped_column(String(30), default="trial", nullable=False)  # trial | active | past_due | suspended | cancelled
    amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    invoice_number: Mapped[str | None] = mapped_column(String(60), nullable=True)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    grace_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SaasTenantMember(Base):
    """Authoritative membership binding a User to a specific SaaS College Tenant."""
    __tablename__ = "saas_tenant_members"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_saas_tenant_member_user"),
        Index("ix_saas_tenant_members_user", "user_id", "tenant_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    tenant_role: Mapped[str] = mapped_column(String(30), default="student", nullable=False)  # college_admin | canteen_manager | staff | faculty | student
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SaasTenantOrder(Base):
    """Tenant-isolated order record enforcing strict cross-college separation."""
    __tablename__ = "saas_tenant_orders"
    __table_args__ = (
        Index("ix_saas_tenant_orders_tenant_created", "tenant_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=False)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("saas_outlets.id", ondelete="SET NULL"), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    order_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    item_summary: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(30), default="paid", nullable=False)
    pickup_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="confirmed", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SaasApiKey(Base):
    """Scoped API key & outbound webhook subscription for college ERP / SIS integrations."""
    __tablename__ = "saas_api_keys"
    __table_args__ = (
        Index("ix_saas_api_keys_tenant", "tenant_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    scopes: Mapped[str] = mapped_column(String(255), default="orders:read,menu:read", nullable=False)
    webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
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
