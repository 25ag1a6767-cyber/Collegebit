"""
Inspect and cleanup database counters and discounts in ace_canteen.db
"""
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Add backend to sys.path
backend_path = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.database import SessionLocal
from app.models import (
    CanteenCounter,
    Discount,
    Product,
    Order,
    Cart,
    UserCounterAssignment,
    User,
    OperationalExpense,
)
from sqlalchemy import select, delete, update

def main():
    db = SessionLocal()
    try:
        counters = db.scalars(select(CanteenCounter).order_by(CanteenCounter.id)).all()
        print(f"Total current counters: {len(counters)}")
        for c in counters:
            print(f"  ID {c.id}: {c.name} ({c.location}) - active: {c.is_active}")

        discounts = db.scalars(select(Discount).order_by(Discount.id)).all()
        print(f"Total current discounts: {len(discounts)}")
        for d in discounts:
            print(f"  ID {d.id}: {d.code} - {d.description} - active: {d.is_active}")

        # Ensure Counter 1 and Counter 2 exist and have exact names
        c1 = db.get(CanteenCounter, 1)
        if c1:
            c1.name = "Main Counter"
            c1.location = "Ground Floor - Main Dining Hall"
            c1.capacity_per_slot = 300
            c1.is_active = True
        else:
            c1 = CanteenCounter(
                id=1,
                name="Main Counter",
                location="Ground Floor - Main Dining Hall",
                capacity_per_slot=300,
                is_active=True,
            )
            db.add(c1)

        c2 = db.get(CanteenCounter, 2)
        if c2:
            c2.name = "Counter A"
            c2.location = "Ground Floor - Station A"
            c2.capacity_per_slot = 200
            c2.is_active = True
        else:
            c2 = CanteenCounter(
                id=2,
                name="Counter A",
                location="Ground Floor - Station A",
                capacity_per_slot=200,
                is_active=True,
            )
            db.add(c2)

        db.flush()

        # Reassign products: alternate between counter 1 and counter 2 or assign to counter 1 if > 2
        products = db.scalars(select(Product)).all()
        for idx, p in enumerate(products):
            if p.counter_id not in (1, 2) or p.counter_id is None:
                p.counter_id = 1 if (idx % 2 == 0) else 2
        print(f"Reassigned {len(products)} products to Counter 1 and Counter 2")

        # Reassign orders with counter_id > 2
        db.execute(
            update(Order).where(Order.counter_id > 2).values(counter_id=1)
        )
        # Reassign carts with counter_id > 2
        db.execute(
            update(Cart).where(Cart.counter_id > 2).values(counter_id=1)
        )
        # Reassign operational expenses
        db.execute(
            update(OperationalExpense).where(OperationalExpense.counter_id > 2).values(counter_id=1)
        )

        # Clean user assignments for counters > 2
        db.execute(
            delete(UserCounterAssignment).where(UserCounterAssignment.counter_id > 2)
        )

        # Delete all counters with id > 2
        deleted_counters = db.execute(
            delete(CanteenCounter).where(CanteenCounter.id > 2)
        )
        print(f"Deleted extra counters: {deleted_counters.rowcount}")

        # Ensure staff user has assignment to counters 1 and 2
        staff_user = db.scalar(select(User).where(User.email == "25ag1a6767@gmail.com"))
        if staff_user:
            # Delete existing
            db.execute(delete(UserCounterAssignment).where(UserCounterAssignment.user_id == staff_user.id))
            db.add(UserCounterAssignment(user_id=staff_user.id, counter_id=1))
            db.add(UserCounterAssignment(user_id=staff_user.id, counter_id=2))
            print(f"Assigned staff {staff_user.email} to Counter 1 and Counter 2")

        # Cleanup random test discounts like SAVE10%
        # Keep clean discounts like CAMPUS10, WELCOME20, or create them if none
        discounts_to_delete = db.scalars(
            select(Discount).where(Discount.code.like("SAVE10%"))
        ).all()
        for d in discounts_to_delete:
            db.delete(d)
        print(f"Deleted {len(discounts_to_delete)} temporary/hex discounts")

        # Ensure we have standard clean discounts
        campus10 = db.scalar(select(Discount).where(Discount.code == "CAMPUS10"))
        if not campus10:
            campus10 = Discount(
                code="CAMPUS10",
                description="10% off on all campus canteen orders (Min ₹50)",
                discount_percent=10.0,
                discount_paise=0,
                min_order_paise=5000,
                usage_limit=500,
                is_active=True,
            )
            db.add(campus10)
        else:
            campus10.is_active = True

        welcome20 = db.scalar(select(Discount).where(Discount.code == "WELCOME20"))
        if not welcome20:
            welcome20 = Discount(
                code="WELCOME20",
                description="₹20 flat discount for students (Min ₹100)",
                discount_percent=0.0,
                discount_paise=2000,
                min_order_paise=10000,
                usage_limit=200,
                is_active=True,
            )
            db.add(welcome20)
        else:
            welcome20.is_active = True

        db.commit()
        print("Database cleanup committed successfully!")

        final_counters = db.scalars(select(CanteenCounter).order_by(CanteenCounter.id)).all()
        print(f"\nFinal counters ({len(final_counters)}):")
        for c in final_counters:
            print(f"  ID {c.id}: {c.name} ({c.location})")

        final_discounts = db.scalars(select(Discount).order_by(Discount.id)).all()
        print(f"\nFinal discounts ({len(final_discounts)}):")
        for d in final_discounts:
            print(f"  ID {d.id}: {d.code} - {d.description} - active: {d.is_active}")

    finally:
        db.close()

if __name__ == "__main__":
    main()
