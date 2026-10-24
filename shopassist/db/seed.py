"""Deterministic demo data. Order data is synthetic by necessity (no company publishes real customer
orders); the schema, statuses, payment methods and per-product return policies follow Amazon.in's
public help pages. Dates are relative to today() so every scenario (an item 3 days into a 7-day
window, a third-party shipment 7 days late, ...) stays valid whenever the project runs.

Usage:  python -m shopassist.db.seed
"""
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from ..config import settings, today

SCHEMA = Path(__file__).with_name("schema.sql")

CUSTOMERS = [
    # id, name, email, phone, tier, member_since
    ("C001", "Aarav Mehta", "aarav.mehta@example.com", "+91 98200 11001", "standard", "2023-02-14"),
    ("C002", "Sarah Johnson", "sarah.j@example.com", "+91 98200 11002", "prime", "2021-08-03"),
    ("C003", "Rohan Gupta", "rohan.gupta@example.com", "+91 98200 11003", "standard", "2024-05-20"),
    ("C004", "Emily Chen", "emily.chen@example.com", "+91 98200 11004", "standard", "2022-11-11"),
    ("C005", "Ananya Iyer", "ananya.iyer@example.com", "+91 98200 11005", "prime", "2020-01-30"),
    ("C006", "David Kim", "david.kim@example.com", "+91 98200 11006", "standard", "2025-03-09"),
]

PRODUCTS = [
    # id, name, category, price, return_policy, window_days, warranty_months, in_stock
    ("P-EARBUDS", "Wireless Earbuds Pro (ANC)", "electronics", 2499, "replacement_only", 7, 12, 25),
    ("P-CASE", "Shockproof Phone Case", "accessories", 399, "returnable", 10, 0, 100),
    ("P-WATCH", "FitPulse Smart Watch", "electronics", 5999, "replacement_only", 7, 12, 10),
    ("P-TSHIRT", "Cotton Crew T-Shirt (Pack of 3)", "fashion", 899, "returns_and_exchange", 10, 0, 50),
    ("P-SHOES", "StrideMax Running Shoes", "fashion", 3499, "returns_and_exchange", 10, 0, 5),
    ("P-CHAIR", "ErgoFlex Office Chair", "furniture", 15499, "returnable", 10, 12, 3),
    ("P-JACKET", "Classic Denim Jacket", "fashion", 2799, "returns_and_exchange", 10, 0, 0),
    ("P-MIXER", "PowerChef Mixer Grinder 750W", "home_kitchen", 3299, "brand_replacement", 10, 24, 0),
    ("P-GIFTCARD", "Digital Gift Card (Rs 2,000)", "gift_cards", 2000, "non_returnable", 0, 0, 999),
    ("P-SPEAKER", "BassBoom Bluetooth Speaker", "electronics", 1999, "replacement_only", 7, 6, 12),
    ("P-HEADPHONES", "QuietWave ANC Headphones", "electronics", 7499, "replacement_only", 10, 12, 4),
    ("P-SERUM", "Vitamin C Face Serum", "beauty", 649, "non_returnable", 0, 0, 40),
    ("P-BOTTLE", "Insulated Steel Bottle 1L", "home_kitchen", 799, "returnable", 10, 0, 60),
    ("P-BACKPACK", "TrailLite Laptop Backpack", "luggage", 1499, "returnable", 30, 6, 20),
    ("P-NOVEL", "The Silent Orbit (Paperback)", "books", 499, "returnable", 10, 0, 30),
    ("P-PHONE", "Nova X5 Smartphone (8GB/128GB)", "mobiles", 18999, "service_centre_replacement", 7, 12, 8),
]
PRICES = {p[0]: p[3] for p in PRODUCTS}

# id, customer, status, payment, placed (days ago), delivered (days ago | None), city, [(product, qty)],
# shipment: (shipped_by, carrier, tracking, eta (days ago; negative = future), last_location, updated days ago)
ORDERS = [
    (1001, "C001", "delivered", "card", 6, 3, "Mumbai", [("P-EARBUDS", 1), ("P-CASE", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71001", 3, "Delivered - Mumbai", 3)),
    (1002, "C001", "shipped", "upi", 3, None, "Mumbai", [("P-WATCH", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71002", -2, "Bhiwandi fulfilment centre", 0)),
    (1003, "C001", "order_received", "pay_on_delivery", 1, None, "Mumbai", [("P-TSHIRT", 1)], None),
    (1004, "C002", "delivered", "card", 19, 15, "Bengaluru", [("P-SHOES", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71004", 15, "Delivered - Bengaluru", 15)),
    (1005, "C002", "delivered", "card", 5, 2, "Bengaluru", [("P-CHAIR", 1)],
     ("third_party", "Delhivery", "DLV5501005", 2, "Delivered - Bengaluru", 2)),
    (1006, "C002", "delivered", "upi", 9, 6, "Bengaluru", [("P-JACKET", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71006", 6, "Delivered - Bengaluru", 6)),
    (1007, "C003", "delivered", "upi", 4, 1, "Delhi", [("P-MIXER", 1)],
     ("third_party", "Blue Dart", "BD77810007", 1, "Delivered - Delhi", 1)),
    (1008, "C003", "delivered", "amazon_pay", 8, 5, "Delhi", [("P-GIFTCARD", 1)],
     ("amazon", "Email delivery", "EGC-1008", 5, "Delivered by email", 5)),
    (1009, "C003", "out_for_delivery", "pay_on_delivery", 3, None, "Delhi", [("P-SPEAKER", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71009", 0, "Out for delivery - Delhi", 0)),
    (1010, "C004", "shipped", "card", 14, None, "Kolkata", [("P-HEADPHONES", 1)],
     ("third_party", "Delhivery", "DLV5501010", 7, "Nagpur transit hub", 6)),
    (1011, "C004", "cancelled", "pay_on_delivery", 20, None, "Kolkata", [("P-BOTTLE", 2)], None),
    (1012, "C004", "delivered", "netbanking", 14, 10, "Kolkata", [("P-SPEAKER", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71012", 10, "Delivered - Kolkata", 10)),
    (1013, "C005", "delivered", "card", 15, 12, "Chennai", [("P-BACKPACK", 1), ("P-BOTTLE", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71013", 12, "Delivered - Chennai", 12)),
    (1014, "C005", "preparing_for_shipment", "card", 1, None, "Chennai", [("P-HEADPHONES", 1)], None),
    (1015, "C006", "delivered", "upi", 6, 3, "Hyderabad", [("P-SERUM", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71015", 3, "Delivered - Hyderabad", 3)),
    (1016, "C006", "shipped", "amazon_pay", 2, None, "Hyderabad", [("P-NOVEL", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71016", -1, "Hyderabad delivery station", 0)),
    (1017, "C006", "delivered", "card", 7, 4, "Hyderabad", [("P-PHONE", 1)],
     ("amazon", "Amazon Shipping", "AMZIN71017", 4, "Delivered - Hyderabad", 4)),
]


def _d(base: date, days_ago: int | None) -> str | None:
    return None if days_ago is None else (base - timedelta(days=days_ago)).isoformat()


def seed(db_path: Path | str = settings.db_path, on: date | None = None) -> Path:
    """(Re)create the database from scratch."""
    base = on or today()
    db_path = Path(db_path)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA journal_mode=WAL")  # readers (dashboard) don't block the writer (agent)
        con.executescript(SCHEMA.read_text(encoding="utf-8"))
        con.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?)", CUSTOMERS)
        con.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?)", PRODUCTS)
        item_id = 1
        for oid, cust, status, pay, placed, delivered, city, items, ship in ORDERS:
            total = sum(PRICES[p] * q for p, q in items)
            con.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
                        (oid, cust, status, pay, total, _d(base, placed), _d(base, delivered), city))
            for product, qty in items:
                con.execute("INSERT INTO order_items VALUES (?,?,?,?,?)", (item_id, oid, product, qty, PRICES[product]))
                item_id += 1
            if ship:
                shipped_by, carrier, tracking, eta, location, updated = ship
                con.execute("INSERT INTO shipments VALUES (?,?,?,?,?,?,?,0)",
                            (oid, shipped_by, carrier, tracking, _d(base, eta), location, _d(base, updated)))
        con.execute("INSERT INTO meta VALUES ('seeded_on', ?)", (base.isoformat(),))
        con.commit()
    finally:
        con.close()
    return db_path


if __name__ == "__main__":
    path = seed()
    print(f"Seeded {path} with {len(CUSTOMERS)} customers, {len(PRODUCTS)} products, {len(ORDERS)} orders.")
