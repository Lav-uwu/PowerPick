"""Plain dictionary helpers, formatting, and India-specific form choices."""

from datetime import datetime, timezone
from . import database

INDIAN_STATES = ["Andaman and Nicobar Islands", "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chandigarh", "Chhattisgarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi", "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jammu and Kashmir", "Jharkhand", "Karnataka", "Kerala", "Ladakh", "Lakshadweep", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Puducherry", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal"]


def now():
    """Store timezone-aware UTC ISO strings, which JSON can serialize directly."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value):
    """Convert persisted ISO timestamps when comparing windows or formatting dates."""
    return datetime.fromisoformat(value)


def product_view(product, data):
    """Add computed stock/review fields to a copy without changing stored inventory."""
    result = dict(product)
    reviews = []
    for review in data["reviews"]:
        if review["product_id"] == product["id"]:
            user = database.find(data, "users", review["user_id"])
            reviews.append({**review, "author": user["name"] if user else "Customer"})
    result["reviews"] = sorted(reviews, key=lambda r: r["created_at"], reverse=True)
    result["rating"] = round(sum(r["rating"] for r in reviews) / len(reviews), 1) if reviews else None
    stock = product["stock"]
    result["stock_label"] = "Out of stock" if stock == 0 else f"Only {stock} left" if stock < 10 else "In stock"
    return result


def order_view(order, data):
    """Attach current product imagery to immutable order line snapshots."""
    result = dict(order)
    result["lines"] = [{**line, "product": database.find(data, "products", line["product_id"])} for line in order["lines"]]
    return result
