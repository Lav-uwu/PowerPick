"""A complete no-charge demo gateway, plus optional signed live Razorpay checkout."""

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for
from .. import database
from ..services.notifications import queue_order
from ..services.orders import cancel_in_data, event, expire_pending, record_payment
from ..services.payments import PaymentError, gateway_request, online_enabled, valid_signature
from .auth import login_required
from .store import owned_order

bp = Blueprint("payments", __name__)


@bp.get("/orders/<reference>/payment")
@login_required
def pay(reference):
    """Keep real payment credentials server-side and label all demo activity."""
    expire_pending()
    order = owned_order(database.read(), reference)
    if order["status"] != "awaiting_payment":
        return redirect(url_for("store.order_detail", reference=reference))
    return render_template("payment.html", order=order, key_id=current_app.config["RAZORPAY_KEY_ID"], online_enabled=online_enabled())


@bp.post("/payments/demo/<reference>")
@login_required
def demo(reference):
    """Simulate success/failure/cancellation without collecting payment details."""
    if not current_app.config["DEMO_PAYMENTS"]:
        abort(404)
    expire_pending()
    outcome = request.form.get("outcome")
    if outcome not in {"success", "failure", "cancel"}:
        abort(400)
    def simulate(data):
        order = owned_order(data, reference)
        if order["payment_method"] != "demo":
            abort(400, description="This order does not use the demo gateway.")
        if order["status"] != "awaiting_payment":
            return order
        if outcome == "success":
            order.update(status="placed", payment_status="demo_paid")
            event(order, "Demo payment approved", "Simulation completed. No money was charged.")
            queue_order(data, order, "Demo order confirmed", "Your demo payment was approved. No money was charged. You can follow this order through the sample fulfillment workflow.")
        elif outcome == "failure":
            order["payment_status"] = "demo_failed"
            event(order, "Demo payment declined", "No charge was made. You can retry within the reservation window.")
        else:
            cancel_in_data(data, order, "Demo checkout cancelled. No money was charged.")
        return order
    order = database.mutate(simulate)
    if outcome == "failure" and order["status"] == "awaiting_payment":
        flash("Demo payment declined. No charge was made. Try again or cancel.", "error")
        return redirect(url_for("payments.pay", reference=reference))
    return redirect(url_for("store.order_detail", reference=reference))


@bp.post("/payments/verify/<reference>")
@login_required
def verify(reference):
    """Verify the callback HMAC, then fetch the authoritative captured payment."""
    order = owned_order(database.read(), reference)
    payment_id, provider_id = request.form.get("razorpay_payment_id", ""), request.form.get("razorpay_order_id", "")
    signature = request.form.get("razorpay_signature", "")
    if order["payment_method"] != "razorpay" or not order["provider_order_id"] or provider_id != order["provider_order_id"] or not payment_id.startswith("pay_") or not payment_id.replace("_", "").isalnum():
        abort(400, description="Invalid payment reference.")
    if not valid_signature(f"{order['provider_order_id']}|{payment_id}", signature, current_app.config["RAZORPAY_KEY_SECRET"]):
        abort(400, description="Payment signature could not be verified.")
    try:
        payment = gateway_request("payments/" + payment_id)
        if payment.get("order_id") != order["provider_order_id"]:
            raise ValueError("Payment does not match this order.")
        confirmed = record_payment(payment)
        flash("Payment confirmed." if confirmed and confirmed["payment_status"] == "paid" else "Payment is awaiting capture. Check this order again shortly.", "success")
    except (PaymentError, ValueError) as error:
        flash(str(error), "error")
    return redirect(url_for("store.order_detail", reference=reference))


@bp.post("/payments/webhook")
def webhook():
    """Authenticate the raw webhook and process captures/refunds idempotently."""
    if not valid_signature(request.get_data(), request.headers.get("X-Razorpay-Signature", ""), current_app.config["RAZORPAY_WEBHOOK_SECRET"]):
        abort(400, description="Invalid webhook signature.")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    entities = payload.get("payload", {})
    if payload.get("event") in {"payment.captured", "order.paid"}:
        payment = entities.get("payment", {}).get("entity", {})
        if not payment.get("id") or not payment.get("order_id"):
            abort(400)
        try:
            record_payment(payment)
        except ValueError:
            abort(400, description="Payment amount mismatch.")
    elif payload.get("event") == "refund.processed":
        refund = entities.get("refund", {}).get("entity", {})
        def reconcile_refund(data):
            order = next((o for o in data["orders"] if o["provider_payment_id"] and o["provider_payment_id"] == refund.get("payment_id")), None)
            if order and refund.get("amount") == order["total"] and order["payment_status"] != "refunded":
                order.update(payment_status="refunded", provider_refund_id=refund.get("id", ""))
                event(order, "Refund completed", "The refund was processed to your original payment method.")
                queue_order(data, order, "Refund completed", "Your refund was processed. The time for the credit to appear depends on your bank.")
        database.mutate(reconcile_refund)
    return {"received": True}
