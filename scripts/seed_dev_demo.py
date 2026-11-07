"""Local-development sample data for ACE Canteen. NEVER run against a live canteen.

Creates (if missing) one admin, one staff and one student account plus a sample
menu, suppliers, batches and a week of order history so dashboards and demand
forecasts have something to show during local development.

Safety rules enforced by this script:
  * Refuses to run when ENVIRONMENT is production/prod/staging.
  * Refuses to run against anything other than a local SQLite database.
  * Never uses fixed passwords. Each account gets a random password that is printed
    once when the account is created. Existing accounts are left untouched unless
    --rotate-passwords is passed.

Usage:
    python scripts/seed_dev_demo.py [--rotate-passwords]
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR / "backend"))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402

DEMO_EMAIL_DOMAIN = "example.edu"  # RFC 2606 reserved example domain for local dev only


def _guard() -> None:
    settings = get_settings()
    if settings.environment.lower() in {"production", "prod", "staging"}:
        raise SystemExit("Refusing to seed demo data: ENVIRONMENT is %s." % settings.environment)
    if not settings.database_url.startswith("sqlite"):
        raise SystemExit("Refusing to seed demo data into a non-SQLite database. Demo data is for local development only.")


def seed(rotate_passwords: bool = False) -> None:
    _guard()
    from app.database import Base, SessionLocal, engine
    from app.models import (Category, InventoryMovement, InventoryMovementType, Order, OrderItem, OrderStatus,
                            PaymentStatus, Product, ProductBatch, PurchaseOrder, Supplier, User, UserRole)
    from app.security import hash_password

    Base.metadata.create_all(bind=engine)
    created_credentials: list[tuple[str, str, str]] = []

    with SessionLocal() as db:
        accounts = [("Dev Administrator", f"admin.dev@{DEMO_EMAIL_DOMAIN}", UserRole.ADMIN),
                    ("Dev Kitchen Staff", f"staff.dev@{DEMO_EMAIL_DOMAIN}", UserRole.STAFF),
                    ("Dev Student", f"student.dev@{DEMO_EMAIL_DOMAIN}", UserRole.STUDENT)]
        users: dict[str, User] = {}
        for name, email, role in accounts:
            user = db.scalar(select(User).where(User.email == email))
            if user is None or rotate_passwords:
                password = secrets.token_urlsafe(12)
                if user is None:
                    user = User(full_name=name, email=email, role=role, is_active=True, password_hash=hash_password(password))
                    db.add(user)
                else:
                    user.password_hash = hash_password(password)
                    user.token_version += 1
                user.must_change_password = False
                db.flush()
                created_credentials.append((role.value, email, password))
            users[role.value] = user

        # The previous college-demo script created accounts with published passwords.
        # Disable them so a copied database cannot be signed into with those passwords.
        for legacy_email in ("admin@acecanteen.com", "kitchen@acecanteen.com", "student@acecanteen.com", "faculty@acecanteen.com"):
            legacy = db.scalar(select(User).where(User.email == legacy_email))
            if legacy is not None and legacy.is_active:
                legacy.is_active = False
                legacy.password_hash = hash_password(secrets.token_urlsafe(32))
                legacy.token_version += 1
                print(f"[!] Disabled legacy demo account with a published password: {legacy_email}")

        categories = {}
        for name, slug, item_type, desc in [
            ("Snacks", "dev-snacks", "food", "Quick bites"), ("Meals", "dev-meals", "food", "Lunch plates"),
            ("Beverages", "dev-beverages", "food", "Hot and cold drinks"), ("Stationery", "dev-stationery", "stationery", "Study supplies")]:
            cat = db.scalar(select(Category).where(Category.slug == slug))
            if cat is None:
                cat = Category(name=f"{name} (dev)", slug=slug, item_type=item_type, description=desc, is_active=True)
                db.add(cat); db.flush()
            categories[slug] = cat

        products = {}
        for name, slug, price, stock, diet, allergens in [
            ("Samosa (2 pcs)", "dev-snacks", "20.00", 120, "vegetarian", "gluten"),
            ("Veg Puff", "dev-snacks", "25.00", 60, "vegetarian", "gluten,dairy"),
            ("Veg Thali", "dev-meals", "90.00", 50, "vegetarian", "dairy"),
            ("Egg Fried Rice", "dev-meals", "80.00", 40, "contains egg", "egg,soy"),
            ("Masala Chai", "dev-beverages", "12.00", 200, "vegetarian", "dairy"),
            ("Cold Coffee", "dev-beverages", "40.00", 60, "vegetarian", "dairy"),
            ("Notebook 192 pages", "dev-stationery", "60.00", 100, None, None)]:
            cat = categories[slug]
            prod = db.scalar(select(Product).where(Product.name == name, Product.category_id == cat.id))
            if prod is None:
                prod = Product(category_id=cat.id, name=name, item_type=cat.item_type, price=Decimal(price), stock_quantity=stock,
                               is_available=True, dietary_info=diet, allergens=allergens, description=f"{name} (development sample)")
                db.add(prod); db.flush()
                db.add(InventoryMovement(product_id=prod.id, movement_type=InventoryMovementType.INITIAL, quantity_delta=stock,
                                         quantity_before=0, quantity_after=stock, reason="Development sample stock"))
            products[name] = prod

        supplier = db.scalar(select(Supplier).where(Supplier.name == "Dev Dairy Supplier"))
        if supplier is None:
            supplier = Supplier(name="Dev Dairy Supplier", contact_name="Sample Contact", phone="+91 00000 00000", is_active=True)
            db.add(supplier); db.flush()
            db.add(PurchaseOrder(supplier_id=supplier.id, created_by_user_id=users["admin"].id, status="ordered",
                                 lines_json='[{"product_id": %d, "quantity": 50, "unit_cost": "6.00", "expiry_date": null}]' % products["Masala Chai"].id,
                                 ordered_at=datetime.now(timezone.utc)))
            db.add(ProductBatch(product_id=products["Veg Puff"].id, supplier_id=supplier.id, batch_code="DEV-PUFF-1",
                                quantity_received=30, quantity_remaining=20, unit_cost=Decimal("14.00"),
                                received_date=date.today() - timedelta(days=1), expiry_date=date.today() + timedelta(days=2)))

        if db.scalar(select(Order.id).where(Order.notes == "dev-sample-history")) is None:
            student = users["student"]
            for offset in range(1, 15):
                when = datetime.now(timezone.utc) - timedelta(days=offset, hours=2)
                order = Order(user_id=student.id, token_number=900 + offset, token_date=when.date().isoformat(),
                              status=OrderStatus.COMPLETED, payment_status=PaymentStatus.PAID, subtotal=Decimal("52.00"),
                              notes="dev-sample-history", created_at=when, updated_at=when, collected_at=when + timedelta(minutes=12))
                db.add(order); db.flush()
                db.add(OrderItem(order_id=order.id, product_id=products["Samosa (2 pcs)"].id, product_name="Samosa (2 pcs)",
                                 unit_price=Decimal("20.00"), quantity=2, line_total=Decimal("40.00")))
                db.add(OrderItem(order_id=order.id, product_id=products["Masala Chai"].id, product_name="Masala Chai",
                                 unit_price=Decimal("12.00"), quantity=1, line_total=Decimal("12.00")))
        db.commit()

    print("Development sample data is ready (local SQLite only).")
    if created_credentials:
        print("New development credentials (shown once; they are not stored anywhere else):")
        for role, email, password in created_credentials:
            print(f"  {role:<8} {email}  /  {password}")
    else:
        print("Existing development accounts were kept. Use --rotate-passwords to issue new passwords.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed local development sample data")
    parser.add_argument("--rotate-passwords", action="store_true", help="Issue new random passwords for the dev accounts")
    seed(parser.parse_args().rotate_passwords)
