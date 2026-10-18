"""Catalog, basket, India-only checkout, customer orders, reviews, and support."""

import re
import secrets
from collections import Counter
from datetime import timedelta
from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from .. import database
from ..utils import INDIAN_STATES, now, order_view, parse_date, product_view
from ..services.cart import cart_quote
from ..services.notifications import queue, queue_order
from ..services.orders import cancel_in_data, create_order, event, expire_pending
from ..services.payments import PaymentError, gateway_request, online_enabled
from .auth import login_required, valid_email

bp = Blueprint("store", __name__)


def product_or_404(data, identifier=None, slug=None):
    """Find an active product from a supplied JSON snapshot."""
    product = next((p for p in data["products"] if p["active"] and (p["id"] == identifier if identifier is not None else p["slug"] == slug)), None)
    if not product:
        abort(404, description="This product is no longer available.")
    return product


def filtered_products(data):
    """Search plain strings and apply category, stock, price, and sort controls."""
    products = [p for p in data["products"] if p["active"]]
    search = request.args.get("q", "").strip().lower()[:100]
    category = request.args.get("category", "")[:80]
    max_price = request.args.get("max_price", type=int)
    if search:
        products = [p for p in products if search in " ".join([p["name"], p["sku"], p["category"], p["specification"], p["description"]]).lower()]
    if category:
        products = [p for p in products if p["category"] == category]
    if request.args.get("stock") == "1":
        products = [p for p in products if p["stock"] > 0]
    if max_price is not None and max_price >= 0:
        products = [p for p in products if p["price"] <= max_price * 100]
    sort = request.args.get("sort", "featured")
    key = {"price_asc": lambda p: p["price"], "price_desc": lambda p: -p["price"], "name": lambda p: p["name"]}.get(sort, lambda p: p["id"])
    return [product_view(p, data) for p in sorted(products, key=key)]


@bp.get("/")
def home():
    """Render the full catalog or just its result panel for enhanced filtering."""
    data = database.read()
    categories = sorted(Counter(p["category"] for p in data["products"] if p["active"]).items())
    return render_template("partials/catalog.html" if request.args.get("partial") == "1" else "index.html",
        products=filtered_products(data), categories=categories, total_products=sum(count for _, count in categories))


@bp.get("/api/products")
def products_json():
    """Expose public catalog JSON only; account/order JSON is always private."""
    products = filtered_products(database.read())
    return jsonify(currency="INR", tax_included=True, products=products)


@bp.get("/products/<slug>")
def product_detail(slug):
    """Show specifications and genuine customer reviews without fabricated ratings."""
    data = database.read()
    product = product_view(product_or_404(data, slug=slug), data)
    purchased = g.user and any(o["user_id"] == g.user["id"] and o["status"] == "delivered" and any(line["product_id"] == product["id"] for line in o["lines"]) for o in data["orders"])
    reviewed = g.user and any(r["user_id"] == g.user["id"] for r in product["reviews"])
    related = [product_view(p, data) for p in data["products"] if p["active"] and p["category"] == product["category"] and p["id"] != product["id"]][:3]
    return render_template("product.html", product=product, can_review=purchased and not reviewed, related=related)


@bp.post("/products/<int:product_id>/reviews")
@login_required
def review(product_id):
    """Allow one verified review after delivery, with server-side rating limits."""
    rating, body = request.form.get("rating", type=int), request.form.get("body", "").strip()
    if rating not in range(1, 6) or not 10 <= len(body) <= 2000:
        abort(400, description="Choose a rating and write a review of 10–2,000 characters.")
    def save(data):
        product = product_or_404(data, identifier=product_id)
        purchased = any(o["user_id"] == g.user["id"] and o["status"] == "delivered" and any(line["product_id"] == product_id for line in o["lines"]) for o in data["orders"])
        if not purchased or any(r["user_id"] == g.user["id"] and r["product_id"] == product_id for r in data["reviews"]):
            abort(403, description="Reviews require a delivered purchase and are limited to one per product.")
        database.insert(data, "reviews", {"user_id": g.user["id"], "product_id": product_id, "rating": rating, "body": body, "created_at": now()})
        return product["slug"]
    slug = database.mutate(save)
    flash("Thank you. Your review has been published.", "success")
    return redirect(url_for("store.product_detail", slug=slug, _anchor="reviews"))


@bp.get("/cart")
def cart():
    """Show editable basket quantities and a current, server-priced summary."""
    return render_template("cart.html", quote=cart_quote())


@bp.post("/cart/add")
def cart_add():
    """Add items with stock checks and optional JSON feedback for the UI."""
    product = product_or_404(database.read(), identifier=request.form.get("product_id", type=int))
    quantity = request.form.get("quantity", 1, type=int)
    basket = dict(session.get("cart", {}))
    updated = basket.get(str(product["id"]), 0) + quantity if quantity is not None else 0
    success = quantity is not None and quantity > 0 and 1 <= updated <= min(99, product["stock"])
    message = f"{product['name']} added to your basket." if success else "That quantity is unavailable. Please check the current stock."
    if success:
        basket[str(product["id"])] = updated
        session["cart"] = basket
        session.pop("checkout_key", None)
    if request.headers.get("Accept") == "application/json":
        return jsonify(message=message, count=sum(basket.values())), 200 if success else 409
    flash(message, "success" if success else "error")
    return redirect(url_for("store.cart"))


@bp.post("/cart/update")
def cart_update():
    """Set a quantity or remove a line; a zero quantity always permits removal."""
    identifier, quantity = request.form.get("product_id", type=int), request.form.get("quantity", type=int)
    product = database.find(database.read(), "products", identifier)
    if not product:
        abort(404)
    basket = dict(session.get("cart", {}))
    if quantity is None or quantity < 0 or quantity > min(99, product["stock"]):
        flash("Choose a quantity within the available stock.", "error")
    elif quantity == 0:
        basket.pop(str(identifier), None)
    else:
        basket[str(identifier)] = quantity
    session["cart"] = basket
    session.pop("checkout_key", None)
    return redirect(url_for("store.cart"))


@bp.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    """Validate Indian addresses and start a demo, COD, or configured Razorpay order."""
    expire_pending()
    if request.method == "POST":
        key = request.form.get("checkout_key", "")
        existing = next((o for o in database.read()["orders"] if o["checkout_key"] == key and o["user_id"] == g.user["id"]), None)
        if existing:
            return order_destination(existing)
        if not key or key != session.get("checkout_key"):
            abort(400, description="Your basket changed. Please review checkout and submit again.")
    quote = cart_quote()
    if not quote["lines"]:
        return redirect(url_for("store.cart"))
    if request.method == "POST":
        address = {key: request.form.get(key, "").strip() for key in ("name", "phone", "address", "city", "state", "pincode")}
        valid_address = (2 <= len(address["name"]) <= 80 and 8 <= len(address["address"]) <= 400 and 2 <= len(address["city"]) <= 80
            and address["state"] in INDIAN_STATES and re.fullmatch(r"[1-9]\d{5}", address["pincode"]) and re.fullmatch(r"[6-9]\d{9}", address["phone"]))
        method = request.form.get("payment_method", "")
        methods = {"cod"} | ({"demo"} if current_app.config["DEMO_PAYMENTS"] else set()) | ({"razorpay"} if online_enabled() else set())
        if not valid_address or not request.form.get("terms") or request.form.get("country", "India") != "India":
            flash("Complete your Indian delivery address, 10-digit mobile number, and accept the store policies.", "error")
        elif method not in methods:
            flash("Choose an available payment method.", "error")
        else:
            try:
                order = create_order(g.user, session.get("cart", {}), address, method, key)
            except ValueError as error:
                flash(str(error), "error")
                return redirect(url_for("store.cart"))
            if method == "razorpay" and not order["provider_order_id"]:
                try:
                    provider = gateway_request("orders", {"amount": order["total"], "currency": "INR", "receipt": order["reference"]})
                    def attach(data):
                        row = database.find(data, "orders", order["id"])
                        row["provider_order_id"] = provider["id"]
                        return row
                    order = database.mutate(attach)
                except (PaymentError, KeyError):
                    database.mutate(lambda data: cancel_in_data(data, database.find(data, "orders", order["id"]), "Online payment could not start. No charge was made."))
                    session.pop("checkout_key", None)
                    flash("Online payment is currently unavailable. Please choose another method.", "error")
                    return redirect(url_for("store.checkout"))
            session.pop("cart", None)
            session.pop("checkout_key", None)
            return order_destination(order)
    session.setdefault("checkout_key", secrets.token_hex(24))
    return render_template("checkout.html", quote=quote, online_enabled=online_enabled())


def order_destination(order):
    """Route unpaid orders to their matching gateway and other orders to tracking."""
    return redirect(url_for("payments.pay", reference=order["reference"]) if order["status"] == "awaiting_payment" else url_for("store.order_detail", reference=order["reference"]))


def owned_order(data, reference):
    """Never reveal another customer's order, even if its reference is known."""
    order = next((o for o in data["orders"] if o["reference"] == reference and o["user_id"] == g.user["id"]), None)
    if not order:
        abort(404)
    return order


@bp.get("/account")
@login_required
def account():
    """Show the signed-in customer's history, support replies and message inbox."""
    data = database.read()
    own = lambda records: sorted([r for r in records if r.get("user_id") == g.user["id"]], key=lambda r: r["created_at"], reverse=True)
    return render_template("account.html", orders=own(data["orders"]), notifications=own(data["notifications"])[:30], tickets=own(data["tickets"]))


@bp.get("/orders/<reference>")
@login_required
def order_detail(reference):
    """Show genuine fulfillment events and administrator-supplied carrier tracking."""
    expire_pending()
    data = database.read()
    order = owned_order(data, reference)
    eligible_return = order["status"] == "delivered" and order["delivered_at"] and parse_date(now()) <= parse_date(order["delivered_at"]) + timedelta(days=current_app.config["RETURN_WINDOW_DAYS"])
    return render_template("order.html", order=order_view(order, data), eligible_return=eligible_return)


@bp.post("/orders/<reference>/cancel")
@login_required
def order_cancel(reference):
    """Cancel an owned order before packing and release inventory exactly once."""
    def cancel(data):
        order = owned_order(data, reference)
        if order["status"] not in {"placed", "awaiting_payment"}:
            abort(400, description="This order can no longer be cancelled online. Please contact support.")
        cancel_in_data(data, order, "Cancelled at your request. Captured live payments enter the refund review queue.")
    database.mutate(cancel)
    return redirect(url_for("store.order_detail", reference=reference))


@bp.post("/orders/<reference>/return")
@login_required
def request_return(reference):
    """Record a return request within the configured domestic return window."""
    reason = request.form.get("reason", "").strip()
    if not 10 <= len(reason) <= 1500:
        abort(400, description="Describe the reason in 10–1,500 characters.")
    def save(data):
        order = owned_order(data, reference)
        if order["status"] != "delivered" or not order["delivered_at"] or parse_date(now()) > parse_date(order["delivered_at"]) + timedelta(days=current_app.config["RETURN_WINDOW_DAYS"]):
            abort(400, description="This order is outside the online return window. Contact support for help.")
        order.update(status="return_requested", return_reason=reason)
        event(order, "Return requested", reason)
        queue_order(data, order, "Return request received", "Support will review your request and send the next steps.")
    database.mutate(save)
    flash("Your return request has been received.", "success")
    return redirect(url_for("store.order_detail", reference=reference))


@bp.route("/support", methods=["GET", "POST"])
def support():
    """Accept guest and account enquiries; store replies in the customer's inbox."""
    if request.method == "POST":
        fields = {key: request.form.get(key, "").strip() for key in ("name", "email", "subject", "message")}
        fields["email"] = fields["email"].lower()
        if not 2 <= len(fields["name"]) <= 80 or not valid_email(fields["email"]) or not 3 <= len(fields["subject"]) <= 120 or not 10 <= len(fields["message"]) <= 4000:
            flash("Complete all fields with a valid email and a message of 10–4,000 characters.", "error")
        elif request.form.get("website"):
            abort(400)
        else:
            def save(data):
                ticket = database.insert(data, "tickets", {**fields, "user_id": g.user["id"] if g.user else None, "reply": "", "status": "open", "created_at": now()})
                queue(data, fields["email"], f"Support request #{ticket['id']} received", "We received your message. PowerPick support will reply by email. Signed-in customers can also check their account.", ticket["user_id"])
                return ticket
            ticket = database.mutate(save)
            flash(f"Request #{ticket['id']} received. We'll reply by email; signed-in customers can also check their account.", "success")
            return redirect(url_for("store.support"))
    return render_template("support.html")


@bp.get("/policies/<policy>")
def policy(policy):
    """Serve the store policies linked from checkout, support, and the footer."""
    titles = {"shipping": "Shipping & delivery", "returns": "Returns & refunds", "privacy": "Privacy policy", "terms": "Terms of service", "warranty": "Product warranty"}
    if policy not in titles:
        abort(404)
    return render_template("policy.html", policy=policy, title=titles[policy])
