import sqlite3
from pathlib import Path

db_path = Path("C:/Users/HP/Desktop/collegebits/backend/ace_canteen.db")
con = sqlite3.connect(db_path)
cur = con.cursor()
rows = cur.execute("SELECT id, email, role, is_active FROM users").fetchall()
print(f"Total users in {db_path}: {len(rows)}")
for r in rows:
    print(r)
con.close()
