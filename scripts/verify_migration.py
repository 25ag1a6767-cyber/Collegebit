import sqlite3
con = sqlite3.connect("backend/ace_canteen.db")
cols = [c[1] for c in con.cursor().execute("PRAGMA table_info(order_items)").fetchall()]
print("order_items columns:", cols)
assert "collected_quantity" in cols
assert "is_collected" in cols
assert "collected_at" in cols
print("Migration verified successfully!")
