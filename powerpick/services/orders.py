"""Atomic order creation and lifecycle rules for JSON records."""

import secrets
from datetime import timedelta
from flask import current_app
from .. import database
from ..utils import now, parse_date
from .cart import cart_quote
from .notifications import queue_order


def event(order, title, detail=""):
    """Append a truthful timeline event to the order being changed."""
    order["events"].append({"title": title, "detail": detail, "created_at": now()})


def restock(data, order):
    """Release each reservation once, inside the same database mutation."""
    if order["stock_released"]:
        return
    for line in order["lines"]:
        product = database.find(data, "products", line["product_id"])
        product["stock"] += line["quantity"]
    order["stock_released"] = True


def expire_in_data(data):
    """Release abandoned demo/online holds after thirty minutes."""
    cutoff = parse_date(now()) - timedelta(minutes=30)
    count = 0
    for order in data["orders"]:
        if order["status"] == "awaiting_payment" and parse_date(order["created_at"]) < cutoff:
            restock(data, order)
            order.update(status="cancelled", payment_status="expired")
            event(order, "Payment window expired", "Reserved stock was released. Please place a new order.")
            count += 1
    return count


def expire_pending():
    """Avoid a write unless there are expired orders to release."""
    cutoff = parse_date(now()) - timedelta(minutes=30)
    if any(o["status"] == "awaiting_payment" and parse_date(o["created_at"]) < cutoff for o in database.read()["orders"]):
        return database.mutate(expire_in_data)
    return 0


def create_order(user, cart, address, method, checkout_key):
    """Reprice, validate, reserve, and create once in one JSON transaction."""
    def save(data):
        existing = next((o for o in data["orders"] if o["checkout_key"] == checkout_key and o["user_id"] == user["id"]), None)
        if existing:
            return existing
        expire_in_data(data)
        quote = cart_quote(data, cart)
        if not quote["valid"]:
            raise ValueError("Some quantities are no longer available. Please update your basket.")
        lines = []
        for line in quote["lines"]:
            product = database.find(data, "products", line["product"]["id"])
            product["stock"] -= line["quantity"]
            lines.append({"product_id": product["id"], "name": product["name"], "quantity": line["quantity"], "price": product["price"], "tax_rate": product["tax_rate"]})
        order = database.insert(data, "orders", {**address, "reference": "PP-" + secrets.token_hex(5).upper(),
            "checkout_key": checkout_key, "user_id": user["id"], "country": "India", "currency": "INR",
            "subtotal": quote["subtotal"], "tax": quote["tax"], "shipping": quote["shipping"], "total": quote["total"],
            "payment_method": method, "payment_status": "due_on_delivery" if method == "cod" else "pending",
            "status": "placed" if method == "cod" else "awaiting_payment", "lines": lines, "events": [],
            "created_at": now(), "delivered_at": None, "stock_released": False, "provider_order_id": "",
            "provider_payment_id": "", "provider_refund_id": "", "tracking_carrier": "", "tracking_number": "",
            "tracking_url": "", "return_reason": "", "return_resolution": ""})
        if method == "cod":
            event(order, "Order placed", "Your order is confirmed. Payment is due on delivery.")
            queue_order(data, order, "Order confirmed", "Your order is confirmed. We'll send another update when it ships.")
        else:
            event(order, "Awaiting payment", "Your items are reserved for thirty minutes.")
        return order
    return database.mutate(save)


def cancel_in_data(data, order, reason):
    """Cancel an eligible order and queue the relevant refund communication."""
    if order["status"] not in {"placed", "awaiting_payment", "payment_review"}:
        raise ValueError("This order can no longer be cancelled online. Please contact support.")
    restock(data, order)
    order["status"] = "cancelled"
    if order["payment_status"] == "paid":
        order["payment_status"] = "refund_pending"
    elif order["payment_status"] == "demo_paid":
        order["payment_status"] = "demo_refunded"
    else:
        order["payment_status"] = "cancelled"
    event(order, "Order cancelled", reason)
    queue_order(data, order, "Order cancelled", reason)


def record_payment(payment):
    """Accept a real capture only when provider ID, currency and amount match."""
    def reconcile(data):
        order = next((o for o in data["orders"] if o["provider_order_id"] and o["provider_order_id"] == payment.get("order_id")), None)
        if not order or payment.get("status") != "captured":
            return order
        if payment.get("amount") != order["total"] or payment.get("currency") != "INR":
            raise ValueError("Payment amount could not be verified. Please contact support.")
        if order["payment_status"] in {"paid", "refund_pending", "refunded"}:
            return order
        order.update(payment_status="paid", provider_payment_id=payment["id"])
        order["status"] = "payment_review" if order["stock_released"] else "placed"
        detail = "Payment arrived after the reservation closed. Support will arrange fulfillment or a refund." if order["stock_released"] else "Payment received. Your order is being prepared."
        event(order, "Payment received", detail)
        queue_order(data, order, "Payment received", detail)
        return order
    return database.mutate(reconcile)
