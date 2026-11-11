"""Idempotent sample data for local development."""
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Category, Product, InventoryMovement, InventoryMovementType, CanteenCounter, User, UserRole, UserCounterAssignment
from app.security import hash_password

DEMO = [
    ("Snacks", "snacks", "food", "Quick bites between classes.", [("Samosa", "Crispy potato-filled snack", "12.00", 80), ("Veg Puff", "Flaky vegetable puff", "20.00", 50), ("Sandwich", "Fresh vegetable sandwich", "35.00", 30)]),
    ("Drinks", "drinks", "food", "Refreshments for your break.", [("Tea", "Hot campus tea", "10.00", 100), ("Coffee", "Hot coffee", "15.00", 80), ("Lime Juice", "Fresh lime drink", "25.00", 35)]),
    ("Stationery", "stationery", "stationery", "Everyday study essentials.", [("Blue Pen", "Blue ballpoint pen", "10.00", 200), ("Notebook", "Single-line notebook", "45.00", 70), ("Pencil", "HB graphite pencil", "5.00", 150)]),
]

def seed_demo_data(db: Session) -> None:
    # 1. Ensure the 2 primary counters exist
    main_counter = db.scalar(select(CanteenCounter).where(CanteenCounter.name == "Main Counter"))
    if main_counter is None:
        main_counter = CanteenCounter(
            name="Main Counter",
            location="Ground Floor - Main Dining Hall",
            is_active=True,
            capacity_per_slot=20,
            opening_hours="09:00-17:00"
        )
        db.add(main_counter)
        db.flush()

    counter_a = db.scalar(select(CanteenCounter).where(CanteenCounter.name == "Counter A"))
    if counter_a is None:
        counter_a = CanteenCounter(
            name="Counter A",
            location="First Floor - Counter A",
            is_active=True,
            capacity_per_slot=15,
            opening_hours="09:00-17:00"
        )
        db.add(counter_a)
        db.flush()

    # 2. Ensure default admin user exists
    admin = db.scalar(select(User).where(User.email == "25ag1a6767@aceec.ac.in"))
    if admin is None:
        admin = User(
            full_name="ACE Admin",
            email="25ag1a6767@aceec.ac.in",
            phone="25ag1a6767",
            password_hash=hash_password("ace@25ag1a67676"),
            role=UserRole.ADMIN,
            is_active=True,
        )
        db.add(admin)
        db.flush()
    elif not admin.phone:
        admin.phone = "25ag1a6767"
        db.flush()

    # 3. Ensure default staff user exists
    staff = db.scalar(select(User).where(User.email == "25ag1a6767@gmail.com"))
    if staff is None:
        staff = User(
            full_name="ACE Kitchen Staff",
            email="25ag1a6767@gmail.com",
            phone="25ag1a6767staff",
            password_hash=hash_password("ace@25ag1a67676staff"),
            role=UserRole.STAFF,
            is_active=True,
        )
        db.add(staff)
        db.flush()
    elif not staff.phone:
        staff.phone = "25ag1a6767staff"
        db.flush()

    # Assign staff to both counters
    for c in [main_counter, counter_a]:
        assignment = db.scalar(select(UserCounterAssignment).where(
            UserCounterAssignment.user_id == staff.id,
            UserCounterAssignment.counter_id == c.id
        ))
        if assignment is None:
            db.add(UserCounterAssignment(user_id=staff.id, counter_id=c.id))

    # 4. Categories and Products
    for name, slug, item_type, description, products in DEMO:
        category = db.scalar(select(Category).where(Category.slug == slug))
        if category is None:
            category = Category(name=name, slug=slug, item_type=item_type, description=description, is_active=True)
            db.add(category)
            db.flush()
        for product_name, product_description, price, stock in products:
            existing = db.scalar(select(Product).where(Product.name == product_name, Product.category_id == category.id))
            target_counter = counter_a.id if item_type == "stationery" else main_counter.id
            if existing is None:
                product = Product(
                    category_id=category.id,
                    counter_id=target_counter,
                    name=product_name,
                    description=product_description,
                    item_type=item_type,
                    price=Decimal(price),
                    stock_quantity=stock,
                    is_available=True
                )
                db.add(product)
                db.flush()
                if stock > 0:
                    db.add(InventoryMovement(product_id=product.id, movement_type=InventoryMovementType.INITIAL, quantity_delta=stock, quantity_before=0, quantity_after=stock, reason="Initial demo stock balance"))
            elif existing.counter_id is None:
                existing.counter_id = target_counter
    db.commit()
