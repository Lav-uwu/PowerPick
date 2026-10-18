"""Price basket lines from current product records; never trust browser amounts."""

from decimal import Decimal, ROUND_HALF_UP
from flask import current_app, session
from .. import database
from ..utils import product_view


def cart_quote(data=None, cart=None):
    """Compute inclusive GST and domestic shipping in integer paise."""
    data = data if data is not None else database.read()
    cart = cart if cart is not None else session.get("cart", {})
    lines, subtotal, tax = [], 0, 0
    for key, quantity in cart.items():
        product = database.find(data, "products", int(key)) if str(key).isdigit() else None
        if not product:
            continue
        total = product["price"] * quantity
        included_tax = int((Decimal(total) * product["tax_rate"] / (100 + product["tax_rate"])).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        lines.append({"product": product_view(product, data), "quantity": quantity, "total": total,
            "available": product["active"] and product["stock"] >= quantity})
        subtotal += total
        tax += included_tax
    threshold = current_app.config["FREE_SHIPPING_THRESHOLD_PAISE"]
    shipping = current_app.config["SHIPPING_FEE_PAISE"] if 0 < subtotal < threshold else 0
    return {"lines": lines, "subtotal": subtotal, "tax": tax, "shipping": shipping, "total": subtotal + shipping,
            "remaining": max(0, threshold - subtotal), "valid": bool(lines) and all(line["available"] for line in lines)}
