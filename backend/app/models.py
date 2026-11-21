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
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import expression

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CaseInsensitiveEnum(TypeDecorator):
    """Store uppercase enum member names and decode legacy mixed/lowercase values safely."""

    impl = String
    cache_ok = True

    def __init__(self, enum_cls: type[enum.Enum], length: int = 30, **kwargs):
        self.enum_cls = enum_cls
        super().__init__(length=length, **kwargs)

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, self.enum_cls):
            return value.name
        text_val = str(value).strip()
        try:
            return self.enum_cls[text_val.upper()].name
        except KeyError:
            return self.enum_cls(text_val.lower()).name

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, self.enum_cls):
            return value
        text_val = str(value).strip()
        try:
            return self.enum_cls[text_val.upper()]
        except KeyError:
            return self.enum_cls(text_val.lower())


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
    role: Mapped[UserRole] = mapped_column(CaseInsensitiveEnum(UserRole, length=20), default=UserRole.STUDENT, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    dietary_preference: Mapped[str | None] = mapped_column(String(50), nullable=True)
    allergen_preferences_json: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    status: Mapped[OrderStatus] = mapped_column(CaseInsensitiveEnum(OrderStatus, length=30), default=OrderStatus.PENDING, nullable=False)
    payment_status: Mapped[PaymentStatus] = mapped_column(CaseInsensitiveEnum(PaymentStatus, length=30), default=PaymentStatus.PENDING, nullable=False)
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
        if self.final_total is not None and (
            Decimal(self.final_total) > 0
            or (self.discount_amount is not None and Decimal(self.discount_amount) > 0)
        ):
            return rupees_to_paise(self.final_total)
        if self.total_paise is not None and (
            self.total_paise > 0 or (self.discount_paise is not None and self.discount_paise > 0)
        ):
            return int(self.total_paise)
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
    movement_type: Mapped[InventoryMovementType] = mapped_column(CaseInsensitiveEnum(InventoryMovementType, length=20), nullable=False)
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


class DevicePushToken(Base):
    """Native mobile push token (Expo Push / FCM / APNs bridge) per authenticated user."""
    __tablename__ = "device_push_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    expo_push_token: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


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


# ==============================================================================
# Phase 61–82 Operational, Intelligence, Support, Safety & Fulfillment Models
# ==============================================================================
class ProductFulfillmentConfig(Base):
    """Per-item Parcel vs Ready-to-Eat rules, packaging charges, and station routing."""
    __tablename__ = "product_fulfillment_configs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    supports_parcel: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    supports_ready_to_eat: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    packaging_charge_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    packaging_instructions: Mapped[str | None] = mapped_column(String(255), nullable=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("kitchen_stations.id", ondelete="SET NULL"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class OrderItemFulfillment(Base):
    """Authoritative per-order-item fulfillment record (PARCEL vs READY_TO_EAT) and packing verification."""
    __tablename__ = "order_item_fulfillments"
    __table_args__ = (
        Index("ix_order_item_fulfillments_order_type", "order_id", "fulfillment_type"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    order_item_id: Mapped[int] = mapped_column(ForeignKey("order_items.id", ondelete="CASCADE"), unique=True, nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    fulfillment_type: Mapped[str] = mapped_column(String(20), default="READY_TO_EAT", nullable=False)  # PARCEL | READY_TO_EAT
    packaging_charge_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    packaging_instructions: Mapped[str | None] = mapped_column(String(255), nullable=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("kitchen_stations.id", ondelete="SET NULL"), nullable=True)
    packing_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)  # pending | preparing | packed | serving | ready
    packing_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    packed_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    packed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KitchenStation(Base):
    """Kitchen preparation station with capacity-based admission and auto-pause controls."""
    __tablename__ = "kitchen_stations"
    __table_args__ = (
        Index("ix_kitchen_stations_counter_active", "counter_id", "is_active"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("saas_outlets.id", ondelete="CASCADE"), nullable=True)
    counter_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    station_type: Mapped[str] = mapped_column(String(40), default="general", nullable=False)  # grill | tandoor | beverages | meals | packing
    max_concurrent_orders: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    active_order_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    auto_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SupportTicket(Base):
    """Customer support & complaint management ticket (Phase 66)."""
    __tablename__ = "support_tickets"
    __table_args__ = (
        Index("ix_support_tickets_user_status", "user_id", "status"),
        Index("ix_support_tickets_tenant_status", "tenant_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_number: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False)  # payment | order | missing_item | incorrect_item | delayed_order | pickup | hygiene | account | other
    priority: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)  # low | normal | high | urgent
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)  # OPEN | ASSIGNED | IN_PROGRESS | WAITING_FOR_CUSTOMER | RESOLVED | CLOSED
    subject: Mapped[str] = mapped_column(String(180), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    assigned_staff_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    escalated_to_manager: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_response_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class SupportTicketMessage(Base):
    """Conversation thread & audit log on a customer support ticket."""
    __tablename__ = "support_ticket_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    is_internal_note: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FoodSafetyChecklist(Base):
    """Daily kitchen cleaning, staff hygiene, and equipment maintenance inspection records (Phase 67)."""
    __tablename__ = "food_safety_checklists"
    __table_args__ = (
        Index("ix_food_safety_checklists_date_type", "inspection_date", "checklist_type"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("saas_outlets.id", ondelete="CASCADE"), nullable=True)
    inspection_date: Mapped[date] = mapped_column(Date, default=date.today, nullable=False)
    checklist_type: Mapped[str] = mapped_column(String(40), nullable=False)  # daily_cleaning | staff_hygiene | equipment_maintenance | receiving_verification
    station_name: Mapped[str] = mapped_column(String(120), default="Main Kitchen", nullable=False)
    items_checked_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    corrective_action: Mapped[str | None] = mapped_column(String(500), nullable=True)
    submitted_by_staff_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    approved_by_manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TemperatureLog(Base):
    """Refrigerator, freezer, and hot-holding food temperature monitoring log (Phase 67)."""
    __tablename__ = "temperature_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("saas_outlets.id", ondelete="CASCADE"), nullable=True)
    equipment_or_item: Mapped[str] = mapped_column(String(140), nullable=False)
    storage_Zone: Mapped[str] = mapped_column(String(30), default="cold_storage", nullable=False)  # cold_storage | freezer | hot_holding
    temperature_celsius: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    within_safe_range: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    corrective_action: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recorded_by_staff_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class NotificationDeliveryLog(Base):
    """Reliable notification delivery tracking with exponential backoff retry & dead-letter queue (Phase 65)."""
    __tablename__ = "notification_delivery_logs"
    __table_args__ = (
        Index("ix_notif_delivery_status_retry", "status", "next_retry_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(30), default="in_app", nullable=False)  # in_app | email | web_push | sms
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="delivered", nullable=False)  # queued | sent | delivered | failed | dead_letter
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UserNotificationPreference(Base):
    """Per-user channel preferences for reliable notifications (Phase 65)."""
    __tablename__ = "user_notification_preferences"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    in_app_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    email_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    web_push_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    ntfy_topic: Mapped[str | None] = mapped_column(String(80), unique=True, nullable=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    telegram_link_token: Mapped[str | None] = mapped_column(String(48), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class WasteRecord(Base):
    """Sustainability & food/ingredient waste ledger with single-deduction inventory integration (Phase 81 Module 1)."""
    __tablename__ = "waste_records"
    __table_args__ = (
        Index("ix_waste_records_tenant_date", "tenant_id", "recorded_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("saas_outlets.id", ondelete="SET NULL"), nullable=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    ingredient_id: Mapped[int | None] = mapped_column(ForeignKey("ingredients.id", ondelete="SET NULL"), nullable=True)
    item_name: Mapped[str] = mapped_column(String(140), nullable=False)
    waste_category: Mapped[str] = mapped_column(String(40), nullable=False)  # prepared_food | unserved_food | returned_food | spoiled_food | expired_ingredient | prep_waste
    quantity: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), default="units", nullable=False)
    cost_loss_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    stock_deducted: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    recorded_by_staff_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    approved_by_manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PackagingLedger(Base):
    """Disposable vs reusable packaging tracking (Phase 81 Module 1)."""
    __tablename__ = "packaging_ledgers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("saas_outlets.id", ondelete="SET NULL"), nullable=True)
    packaging_type: Mapped[str] = mapped_column(String(40), nullable=False)  # disposable_used | reusable_issued | reusable_returned | damaged_packaging
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    recorded_by_staff_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StaffShift(Base):
    """Workforce & shift scheduling with station allocation and conflict detection (Phase 81 Module 2)."""
    __tablename__ = "staff_shifts"
    __table_args__ = (
        Index("ix_staff_shifts_staff_start", "staff_user_id", "shift_start"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("saas_outlets.id", ondelete="SET NULL"), nullable=True)
    staff_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    station_role: Mapped[str] = mapped_column(String(40), default="kitchen_prep", nullable=False)  # kitchen_prep | packing | pickup | inventory | counter
    shift_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    shift_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    break_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="scheduled", nullable=False)  # scheduled | active | completed | absent | cancelled
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    late_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    handover_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by_manager_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PurchaseRequisition(Base):
    """Procurement requisition workflow connected to PO generation and goods receiving (Phase 81 Module 4)."""
    __tablename__ = "purchase_requisitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    requisition_number: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("saas_outlets.id", ondelete="SET NULL"), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True)
    items_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    estimated_total_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending_approval", nullable=False)  # pending_approval | approved | po_generated | rejected
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id", ondelete="SET NULL"), nullable=True)
    requested_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SupplierInvoice(Base):
    """Supplier invoice matched against Purchase Order and Goods Receipt with discrepancy flagging (Phase 81 Module 4)."""
    __tablename__ = "supplier_invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invoice_reference: Mapped[str] = mapped_column(String(60), unique=True, index=True, nullable=False)
    purchase_order_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    invoice_amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    po_expected_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    discrepancy_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    discrepancy_flagged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    payment_status: Mapped[str] = mapped_column(String(30), default="recorded", nullable=False)  # recorded | verified | disputed | settled_offline
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    recorded_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MenuPricingRule(Base):
    """Smart Menu & Dynamic Pricing rule with manager approval and min/max safeguards (Phase 81 Module 5)."""
    __tablename__ = "menu_pricing_rules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    outlet_id: Mapped[int | None] = mapped_column(ForeignKey("saas_outlets.id", ondelete="SET NULL"), nullable=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    rule_name: Mapped[str] = mapped_column(String(120), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(30), default="scheduled_window", nullable=False)  # scheduled_window | outlet_override | promo | combo
    meal_window: Mapped[str] = mapped_column(String(30), default="lunch", nullable=False)  # breakfast | lunch | evening | all_day
    proposed_price_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    min_price_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    max_price_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending_approval", nullable=False)  # pending_approval | approved | rejected | expired
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GroupOrder(Base):
    """Collaborative friend/classmate/department group cart & checkout (Phase 82 Module 2)."""
    __tablename__ = "group_orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    invite_code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    organizer_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(140), nullable=False)
    counter_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    pickup_slot: Mapped[str] = mapped_column(String(60), default="12:45-13:00", nullable=False)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False)  # open | locked | paid | collected | cancelled
    payment_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)  # pending | paid
    total_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GroupOrderParticipantItem(Base):
    """Individual participant item line inside a collaborative GroupOrder (Phase 82 Module 2)."""
    __tablename__ = "group_order_participant_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_order_id: Mapped[int] = mapped_column(ForeignKey("group_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    participant_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    participant_name: Mapped[str] = mapped_column(String(120), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False)
    product_name: Mapped[str] = mapped_column(String(140), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    fulfillment_type: Mapped[str] = mapped_column(String(20), default="READY_TO_EAT", nullable=False)  # PARCEL | READY_TO_EAT
    unit_price_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    packaging_charge_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    line_total_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    pickup_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OrderQualityFeedback(Base):
    """Multi-dimensional food quality, taste, packaging, wait-time & hygiene feedback (Phase 82 Module 4)."""
    __tablename__ = "order_quality_feedbacks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    food_quality_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    taste_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    packaging_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    wait_time_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    outlet_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    missing_or_incorrect_items: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    hygiene_concern: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    manager_response: Mapped[str | None] = mapped_column(String(500), nullable=True)
    escalated_ticket_id: Mapped[int | None] = mapped_column(ForeignKey("support_tickets.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MealPassPlan(Base):
    """Configurable Daily, Weekly, and Monthly prepaid meal pass plans (Phase 82 Module 5)."""
    __tablename__ = "meal_pass_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_code: Mapped[str] = mapped_column(String(40), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    pass_period: Mapped[str] = mapped_column(String(20), nullable=False)  # daily | weekly | monthly
    total_meals: Mapped[int] = mapped_column(Integer, nullable=False)
    validity_days: Mapped[int] = mapped_column(Integer, nullable=False)
    price_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StudentMealPass(Base):
    """Student's purchased meal pass activated only after verified online payment (Phase 82 Module 5)."""
    __tablename__ = "student_meal_passes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("meal_pass_plans.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="payment_pending", nullable=False)  # payment_pending | active | exhausted | expired | cancelled
    remaining_meals: Mapped[int] = mapped_column(Integer, nullable=False)
    gateway_order_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    gateway_payment_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MealPassUsage(Base):
    """Atomic single-meal deduction record preventing duplicate meal pass usage (Phase 82 Module 5)."""
    __tablename__ = "meal_pass_usages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_pass_id: Mapped[int] = mapped_column(ForeignKey("student_meal_passes.id", ondelete="CASCADE"), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    meals_deducted: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DailyCanteenClosing(Base):
    """End-of-day financial & operational reconciliation lock with auditable adjustment entries (Phase 82 Module 7)."""
    __tablename__ = "daily_canteen_closings"
    __table_args__ = (
        UniqueConstraint("business_date", "counter_id", name="uq_daily_closing_date_counter"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    counter_id: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    total_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cancelled_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    uncollected_orders: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_payments_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    pending_payments_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    failed_payments_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    wastage_loss_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    discrepancy_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    adjustments_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    submitted_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    approved_by_manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PlatformSuperAdmin(Base):
    """Explicit platform-level operator registry separating SaaS Super Admins from College Tenant Admins."""
    __tablename__ = "platform_super_admins"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    privilege_level: Mapped[str] = mapped_column(String(30), default="platform_operator", nullable=False)  # platform_owner | platform_operator
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    granted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class SaasTenantProfile(Base):
    """College profile, legal/contact settings & verified email sender config isolated per tenant."""
    __tablename__ = "saas_tenant_profiles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), unique=True, index=True, nullable=False)
    tenant_code: Mapped[str] = mapped_column(String(24), unique=True, index=True, nullable=False)
    official_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    campus_info: Mapped[str | None] = mapped_column(String(500), nullable=True)
    operating_hours_json: Mapped[str] = mapped_column(Text, default='{"open":"08:00","close":"18:00"}', nullable=False)
    canteen_settings_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    approved_domains_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    terms_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    privacy_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notification_sender_mode: Mapped[str] = mapped_column(String(30), default="collegebits_default", nullable=False)  # collegebits_default | custom_domain
    custom_sender_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_domain_verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default=expression.false(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class AdminInvitation(Base):
    """Single-use cryptographic invitation for college owners, admins, finance & managers (stores ONLY SHA-256 hash)."""
    __tablename__ = "admin_invitations"
    __table_args__ = (
        Index("ix_admin_invitations_tenant_email", "tenant_id", "email", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(140), nullable=False)
    tenant_role: Mapped[str] = mapped_column(String(40), nullable=False)  # college_owner | college_admin | finance_admin | canteen_manager | kitchen_manager | staff_admin | read_only_auditor
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)  # pending | accepted | revoked | expired
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invited_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    accepted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class EmailChangeRequest(Base):
    """Verified admin email change workflow with optional platform-approved recovery when old email is lost."""
    __tablename__ = "email_change_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    old_email: Mapped[str] = mapped_column(String(255), nullable=False)
    new_email: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)  # pending | verified | cancelled | expired
    recovery_approved_by_super_admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PasswordResetTokenRecord(Base):
    """Single-use expiring password reset token ledger (stores ONLY SHA-256 token hash)."""
    __tablename__ = "password_reset_token_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OwnershipTransferRequest(Base):
    """Auditable College Ownership Transfer & Emergency Platform Recovery workflow."""
    __tablename__ = "ownership_transfer_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    from_owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    target_email: Mapped[str] = mapped_column(String(255), nullable=False)
    target_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    transfer_type: Mapped[str] = mapped_column(String(30), default="owner_initiated", nullable=False)  # owner_initiated | platform_recovery
    previous_owner_action: Mapped[str] = mapped_column(String(30), default="demote_to_admin", nullable=False)  # demote_to_admin | revoke
    status: Mapped[str] = mapped_column(String(30), default="pending_target", nullable=False)  # pending_target | pending_platform | completed | cancelled
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    platform_approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class EquipmentAsset(Base):
    """Canteen kitchen & cooling equipment registry with maintenance tracking (Phase 83 Module 5)."""
    __tablename__ = "equipment_assets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("kitchen_stations.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(140), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)  # chiller | freezer | oven | stove | fryer | pos
    asset_code: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    operational_status: Mapped[str] = mapped_column(String(30), default="operational", nullable=False)  # operational | degraded | maintenance | down
    last_maintenance_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_maintenance_due: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class EquipmentMaintenanceTicket(Base):
    """Staff breakdown report & manager maintenance assignment (Phase 83 Module 5)."""
    __tablename__ = "equipment_maintenance_tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment_assets.id", ondelete="CASCADE"), nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(20), default="high", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False)  # open | assigned | in_progress | resolved
    issue_summary: Mapped[str] = mapped_column(String(500), nullable=False)
    resolution_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reported_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    assigned_to_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ==============================================================================
# PHASE 89 — ENTERPRISE INTELLIGENCE & RELIABILITY 3.0 MODELS
# ==============================================================================
class DecisionIntelligenceRecord(Base):
    """Evaluates before/after operational impact of real canteen actions (Phase 89 Module 1 & 4)."""
    __tablename__ = "decision_intelligence_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    action_category: Mapped[str] = mapped_column(String(60), nullable=False)  # pricing | staffing | capacity | inventory | prep
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(80), nullable=False)
    baseline_value: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    observed_value: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    delta_pct: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    confidence_band: Mapped[str] = mapped_column(String(30), default="empirical", nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    evaluated_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class OperationalMemoryEntry(Base):
    """Searchable operational memory & incident knowledge base with expiry controls (Phase 89 Module 2)."""
    __tablename__ = "operational_memory_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(60), nullable=False)  # rush_playbook | supplier_note | equipment | safety | festival
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    source_reference: Mapped[str | None] = mapped_column(String(160), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MultiAgentProposal(Base):
    """Non-autonomous multi-agent optimization proposals requiring explicit human approval (Phase 89 Module 3)."""
    __tablename__ = "multi_agent_proposals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    proposing_agent: Mapped[str] = mapped_column(String(50), nullable=False)  # demand_agent | procurement_agent | kitchen_agent | finance_agent
    proposal_type: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    estimated_impact_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending_approval", nullable=False)  # pending_approval | approved | rejected
    approved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class UtilityMeterReading(Base):
    """Electricity, water, solar & LPG utility meter readings (Phase 89 Module 7)."""
    __tablename__ = "utility_meter_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    counter_id: Mapped[int | None] = mapped_column(ForeignKey("canteen_counters.id", ondelete="SET NULL"), nullable=True)
    utility_type: Mapped[str] = mapped_column(String(40), nullable=False)  # electricity_kwh | water_liters | lpg_kg | solar_kwh
    reading_value: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    cost_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), default=0, nullable=False)
    source_mode: Mapped[str] = mapped_column(String(30), default="manual_entry", nullable=False)  # manual_entry | metered_sensor
    notes: Mapped[str | None] = mapped_column(String(300), nullable=True)
    recorded_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WaterQualityTest(Base):
    """Potable kitchen water TDS/pH/chlorine safety inspection ledger (Phase 89 Module 8)."""
    __tablename__ = "water_quality_tests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    counter_id: Mapped[int | None] = mapped_column(ForeignKey("canteen_counters.id", ondelete="SET NULL"), nullable=True)
    source_point: Mapped[str] = mapped_column(String(120), nullable=False)
    tds_ppm: Mapped[int] = mapped_column(Integer, nullable=False)
    ph_level: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    free_chlorine_ppm: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("0.30"), nullable=False)
    temperature_c: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    is_compliant: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(400), nullable=True)
    tested_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    tested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class FireEmergencyEvent(Base):
    """Fire, LPG shutoff & emergency drill / incident response checklist log (Phase 89 Module 9)."""
    __tablename__ = "fire_emergency_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    zone: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)  # smoke_drill | lpg_shutoff_drill | thermal_alarm | evacuation_test
    severity: Mapped[str] = mapped_column(String(20), default="drill", nullable=False)  # drill | warning | critical
    is_drill: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)  # active | acknowledged | resolved
    evacuation_checklist_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reported_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    resolved_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class EnvironmentalSensorReading(Base):
    """Cold-storage, humidity & kitchen sensor telemetry with explicit staleness tracking (Phase 89 Module 11-12)."""
    __tablename__ = "environmental_sensor_readings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    station_id: Mapped[int | None] = mapped_column(ForeignKey("kitchen_stations.id", ondelete="SET NULL"), nullable=True)
    sensor_code: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    sensor_type: Mapped[str] = mapped_column(String(50), nullable=False)  # cold_storage_temp | kitchen_humidity | exhaust_airflow | lpg_ppm
    reading_value: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    source_mode: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)  # manual | hardware_iot
    threshold_min: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    threshold_max: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    recorded_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProcurementRfq(Base):
    """Digital Request for Quotation (RFQ) for competitive supplier bidding (Phase 89 Module 14)."""
    __tablename__ = "procurement_rfqs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    required_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    unit: Mapped[str] = mapped_column(String(24), default="kg", nullable=False)
    target_max_unit_paise: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="open", nullable=False)  # open | awarded | closed
    awarded_bid_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProcurementBid(Base):
    """Supplier bid submitted against an open RFQ (Phase 89 Module 14)."""
    __tablename__ = "procurement_bids"
    __table_args__ = (
        UniqueConstraint("rfq_id", "supplier_id", name="uq_rfq_supplier_bid"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("procurement_rfqs.id", ondelete="CASCADE"), nullable=False, index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False)
    unit_price_paise: Mapped[int] = mapped_column(Integer, nullable=False)
    lead_time_days: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(400), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="submitted", nullable=False)  # submitted | awarded | rejected
    submitted_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class GoodsReceivingInspection(Base):
    """Automated intake quality & expiry inspection blocking expired stock (Phase 89 Module 16-17)."""
    __tablename__ = "goods_receiving_inspections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    purchase_order_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_orders.id", ondelete="SET NULL"), nullable=True)
    ingredient_id: Mapped[int] = mapped_column(ForeignKey("ingredients.id", ondelete="RESTRICT"), nullable=False)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id", ondelete="RESTRICT"), nullable=False)
    batch_code: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    received_quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    package_temp_c: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    packaging_intact: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    inspection_status: Mapped[str] = mapped_column(String(30), nullable=False)  # accepted | rejected_expired | rejected_damaged
    rejection_reason: Mapped[str | None] = mapped_column(String(400), nullable=True)
    inspected_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    inspected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MealCreditAllocation(Base):
    """Institutional / department / scholarship meal credit allowance strictly separate from cash wallet (Phase 89 Module 19)."""
    __tablename__ = "meal_credit_allocations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    department_name: Mapped[str] = mapped_column(String(120), nullable=False)
    program_name: Mapped[str] = mapped_column(String(120), nullable=False)
    allocated_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    remaining_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    expires_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)  # active | exhausted | expired | revoked
    issued_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class MealCreditRedemption(Base):
    """Idempotent ledger of institutional meal-credit redemptions (Phase 89 Module 19)."""
    __tablename__ = "meal_credit_redemptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("meal_credit_allocations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), nullable=True)
    amount_paise: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    redeemed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DatabaseRecoveryDrill(Base):
    """Auditable database backup restoration & row-count verification ledger (Phase 89 Module 27)."""
    __tablename__ = "database_recovery_drills"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True)
    backup_run_id: Mapped[int | None] = mapped_column(ForeignKey("backup_runs.id", ondelete="SET NULL"), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)  # passed | failed
    verified_table_counts_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    restore_duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    executed_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class WebhookSubscription(Base):
    """Tenant-scoped SSRF-validated webhook subscription for developer portal (Phase 89 Module 29)."""
    __tablename__ = "webhook_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    target_url: Mapped[str] = mapped_column(String(500), nullable=False)
    event_types_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=expression.true(), nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TenantConfigBlueprint(Base):
    """Versioned SaaS tenant configuration template for zero-downtime rollout & rollback (Phase 89 Module 30)."""
    __tablename__ = "tenant_config_blueprints"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_blueprint_name_version"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300), nullable=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TenantBlueprintDeployment(Base):
    """Records applied/rolled-back configuration blueprint snapshots per college tenant."""
    __tablename__ = "tenant_blueprint_deployments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("saas_tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    blueprint_id: Mapped[int] = mapped_column(ForeignKey("tenant_config_blueprints.id", ondelete="CASCADE"), nullable=False)
    previous_config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="applied", nullable=False)  # applied | rolled_back
    applied_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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
    if target.final_total is None or (Decimal(target.final_total) == Decimal("0.00") and computed_total > 0):
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
