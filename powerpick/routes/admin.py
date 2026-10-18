"""Protected catalog, inventory, fulfillment, returns, support, and outbox controls."""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from .. import database
from ..utils import now, order_view
from ..services.notifications import deliver_pending, queue, queue_order
from ..services.orders import cancel_in_data, event, restock
from ..services.payments import PaymentError, gateway_request
from .auth import admin_required

bp = Blueprint("admin", __name__, url_prefix="/admin")
TRANSITIONS = {"awaiting_payment": ["cancelled"], "placed": ["packed", "cancelled"], "packed": ["shipped"],
    "shipped": ["delivered"], "delivered": [], "cancelled": [], "return_requested": ["return_approved", "delivered"],
    "return_approved": ["returned"], "returned": [], "payment_review": ["cancelled"]}


@bp.get("")
@bp.get("/")
@admin_required
def dashboard():
    """Show real operational counts and simple tabs instead of decorative charts."""
    data = database.read()
    orders = sorted(data["orders"], key=lambda o: o["created_at"], reverse=True)
    return render_template("admin/index.html", data=data, orders=orders,
        tickets=sorted(data["tickets"], key=lambda t: t["created_at"], reverse=True),
        notifications=sorted(data["notifications"], key=lambda n: n["created_at"], reverse=True),
        low_stock=sum(p["active"] and p["stock"] < 10 for p in data["products"]),
        open_orders=sum(o["status"] in {"placed", "packed", "shipped", "payment_review"} for o in orders),
        open_tickets=sum(t["status"] == "open" for t in data["tickets"]))


@bp.route("/products/new", methods=["GET", "POST"])
@bp.route("/products/<int:identifier>/edit", methods=["GET", "POST"])
@admin_required
def product_edit(identifier=None):
    """Create/edit complete catalog records with bounded price, tax, and stock values."""
    data = database.read()
    product = database.find(data, "products", identifier) if identifier else None
    if identifier and not product:
        abort(404)
    if request.method == "POST":
        fields = {key: request.form.get(key, "").strip() for key in ("name", "sku", "category", "description", "specification", "unit", "image_url")}
        try:
            price = int((Decimal(request.form.get("price", "0")) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            stock, tax, image = (int(request.form.get(key, "0")) for key in ("stock", "tax_rate", "image_index"))
            if not 1 <= price <= 100000000 or not 0 <= stock <= 1000000 or not 0 <= tax <= 100 or image not in range(12):
                raise ValueError
            if not 2 <= len(fields["name"]) <= 120 or not 2 <= len(fields["category"]) <= 80 or not 2 <= len(fields["sku"]) <= 40 or not 10 <= len(fields["description"]) <= 4000 or not 2 <= len(fields["specification"]) <= 200 or not 1 <= len(fields["unit"]) <= 60:
                raise ValueError
            if len(fields["image_url"]) > 500 or (fields["image_url"] and not fields["image_url"].startswith(("https://", "/assets/"))):
                raise ValueError
            def save(snapshot):
                if any(p["sku"].lower() == fields["sku"].lower() and p["id"] != identifier for p in snapshot["products"]):
                    raise ValueError("That SKU already exists.")
                values = {**fields, "price": price, "stock": stock, "tax_rate": tax, "image_index": image, "active": bool(request.form.get("active"))}
                if identifier:
                    row = database.find(snapshot, "products", identifier)
                    if not row:
                        abort(404)
                    row.update(values)
                else:
                    stem = re.sub(r"[^a-z0-9]+", "-", fields["name"].lower()).strip("-") or "product"
                    row = database.insert(snapshot, "products", {**values, "slug": stem + "-" + str(max((p["id"] for p in snapshot["products"]), default=0) + 1)})
                return row
            database.mutate(save)
            flash("Product saved.", "success")
            return redirect(url_for("admin.dashboard", tab="products"))
        except (ValueError, InvalidOperation, OverflowError) as error:
            flash(str(error) if str(error) == "That SKU already exists." else "Check all fields. Use a valid positive price, stock, tax rate, image and product details.", "error")
    return render_template("admin/product.html", product=product)


@bp.route("/orders/<reference>", methods=["GET", "POST"])
@admin_required
def order_manage(reference):
    """Enforce fulfillment transitions, tracking information, and return decisions."""
    if request.method == "POST":
        next_status = request.form.get("status", "")
        resolution = request.form.get("resolution", "").strip()[:1500]
        carrier = request.form.get("tracking_carrier", "").strip()[:80]
        number = request.form.get("tracking_number", "").strip()[:100]
        tracking_url = request.form.get("tracking_url", "").strip()[:500]
        def save(data):
            order = next((o for o in data["orders"] if o["reference"] == reference), None)
            if not order:
                abort(404)
            if next_status not in TRANSITIONS.get(order["status"], []):
                raise ValueError("That status change is not allowed.")
            if next_status == "shipped" and (not carrier or not number or not tracking_url.startswith("https://")):
                raise ValueError("Shipping requires a carrier, tracking number, and HTTPS tracking link.")
            if order["status"] == "return_requested" and len(resolution) < 10:
                raise ValueError("Include return instructions or a clear reason for declining the request.")
            if next_status == "cancelled":
                cancel_in_data(data, order, resolution or "Cancelled by the store. Contact support for details.")
                return
            previous = order["status"]
            order["status"] = next_status
            if next_status == "shipped":
                order.update(tracking_carrier=carrier, tracking_number=number, tracking_url=tracking_url)
            if next_status == "delivered" and previous == "shipped":
                order["delivered_at"] = now()
                if order["payment_method"] == "cod":
                    order["payment_status"] = "collected"
            if previous == "return_requested":
                order["return_resolution"] = resolution
            if next_status == "returned":
                restock(data, order)
                if order["payment_status"] == "demo_paid":
                    order["payment_status"] = "demo_refunded"
                elif order["payment_status"] in {"paid", "collected"}:
                    order["payment_status"] = "refund_pending"
            title = "Return declined" if previous == "return_requested" and next_status == "delivered" else next_status.replace("_", " ").capitalize()
            detail = resolution or (f"Sent via {carrier}. Tracking number: {number}." if next_status == "shipped" else f"Your order is now {next_status.replace('_', ' ')}.")
            event(order, title, detail)
            queue_order(data, order, title, detail)
        try:
            database.mutate(save)
            flash("Order updated and customer notified.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("admin.order_manage", reference=reference))
    data = database.read()
    order = next((o for o in data["orders"] if o["reference"] == reference), None)
    if not order:
        abort(404)
    return render_template("admin/order.html", order=order_view(order, data), transitions=TRANSITIONS.get(order["status"], []))


@bp.post("/orders/<reference>/refund")
@admin_required
def refund(reference):
    """Refund live payments through Razorpay or explicitly record an offline refund."""
    order = next((o for o in database.read()["orders"] if o["reference"] == reference), None)
    if not order or order["status"] not in {"cancelled", "returned"} or order["payment_status"] != "refund_pending":
        abort(400, description="This order is not eligible for a refund action.")
    if order["provider_refund_id"]:
        flash("A refund is already being processed.", "success")
        return redirect(url_for("admin.order_manage", reference=reference))
    if order["payment_method"] == "razorpay":
        # Claim this action atomically before contacting the gateway to stop double clicks.
        def claim(data):
            row = database.find(data, "orders", order["id"])
            if row.get("refund_in_progress") or row["payment_status"] != "refund_pending":
                abort(400, description="A refund action is already in progress.")
            row["refund_in_progress"] = True
        database.mutate(claim)
        try:
            response = gateway_request("payments/" + order["provider_payment_id"] + "/refund", {"amount": order["total"], "notes": {"order": reference}})
        except PaymentError as error:
            # Keep the claim: the provider may have received a request despite a timeout.
            flash(str(error) + " Check the Razorpay dashboard before retrying; support can reconcile its webhook.", "error")
            return redirect(url_for("admin.order_manage", reference=reference))
        def save(data):
            row = database.find(data, "orders", order["id"])
            row["provider_refund_id"] = response["id"]
            event(row, "Refund initiated", "The payment provider is processing a refund.")
            queue_order(data, row, "Refund initiated", "Your refund was initiated. We'll update you when the provider confirms processing.")
        database.mutate(save)
    elif order["payment_method"] == "cod":
        reference_note = request.form.get("refund_reference", "").strip()
        if not 5 <= len(reference_note) <= 200:
            flash("Enter the reference for the offline refund you have already completed.", "error")
            return redirect(url_for("admin.order_manage", reference=reference))
        def save(data):
            row = database.find(data, "orders", order["id"])
            if row["payment_status"] != "refund_pending":
                return
            row["payment_status"] = "refunded_offline"
            event(row, "Offline refund recorded", reference_note)
            queue_order(data, row, "Offline refund recorded", f"The store recorded a completed offline refund. Reference: {reference_note}")
        database.mutate(save)
    flash("Refund action saved.", "success")
    return redirect(url_for("admin.order_manage", reference=reference))


@bp.post("/support/<int:identifier>/reply")
@admin_required
def support_reply(identifier):
    """Save a support response and queue email/inbox communication atomically."""
    reply = request.form.get("reply", "").strip()
    if not 10 <= len(reply) <= 4000:
        abort(400, description="Write a reply of 10–4,000 characters.")
    def save(data):
        ticket = database.find(data, "tickets", identifier)
        if not ticket:
            abort(404)
        ticket.update(reply=reply, status="answered")
        queue(data, ticket["email"], f"Support reply #{identifier} · {ticket['subject']}", reply, ticket["user_id"])
    database.mutate(save)
    flash("Reply saved and queued for delivery.", "success")
    return redirect(url_for("admin.dashboard", tab="support"))


@bp.post("/outbox/retry")
@admin_required
def retry_mail():
    """Retry one queued SMTP message without blocking the request for a long batch."""
    count = deliver_pending(limit=1)
    flash("One queued email was delivered." if count else "No email delivered. Check SMTP settings or the pending queue.", "success" if count else "error")
    return redirect(url_for("admin.dashboard", tab="communications"))
