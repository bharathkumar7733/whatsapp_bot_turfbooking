import app.db as db
db.init_db()
with db.get_conn() as c:
    c.execute("UPDATE bookings SET status='cancelled' WHERE 1=1")
    count = c.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
print(f"Done. {count} bookings cleared (set to cancelled).")
