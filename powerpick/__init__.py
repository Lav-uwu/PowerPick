"""Flask setup, session security, common template data, and error pages."""

import os
import secrets
from datetime import timedelta
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, abort, g, render_template, request, session
from . import database

ROOT = Path(__file__).resolve().parent.parent


def create_app(test_config=None):
    """Create an app with local JSON or private hosted JSON; no SQL is used."""
    load_dotenv(ROOT / ".env")
    if os.getenv("VERCEL") and (not os.getenv("BLOB_READ_WRITE_TOKEN") or not os.getenv("SECRET_KEY")):
        raise RuntimeError("Set SECRET_KEY and connect a private Vercel Blob store for JSON persistence.")
    app = Flask(__name__, static_folder=str(ROOT / "public"), static_url_path="")
    app.config.update(
        SECRET_KEY=os.getenv("SECRET_KEY") or secrets.token_hex(32),
        JSON_FILE=str(ROOT / os.getenv("JSON_FILE", "instance/powerpick.json")),
        BLOB_READ_WRITE_TOKEN=os.getenv("BLOB_READ_WRITE_TOKEN", ""),
        JSON_BLOB_PATH=os.getenv("JSON_BLOB_PATH", "powerpick/database.json"),
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(days=7), MAX_CONTENT_LENGTH=1024 * 1024,
        APP_URL=os.getenv("APP_URL", "http://127.0.0.1:5000").rstrip("/"),
        SHIPPING_FEE_PAISE=int(os.getenv("SHIPPING_FEE_PAISE", "7900")),
        FREE_SHIPPING_THRESHOLD_PAISE=int(os.getenv("FREE_SHIPPING_THRESHOLD_PAISE", "149900")),
        RETURN_WINDOW_DAYS=int(os.getenv("RETURN_WINDOW_DAYS", "7")),
        DEMO_PAYMENTS=os.getenv("DEMO_PAYMENTS", "true").lower() == "true",
        RAZORPAY_KEY_ID=os.getenv("RAZORPAY_KEY_ID", ""),
        RAZORPAY_KEY_SECRET=os.getenv("RAZORPAY_KEY_SECRET", ""),
        RAZORPAY_WEBHOOK_SECRET=os.getenv("RAZORPAY_WEBHOOK_SECRET", ""),
        SUPPORT_EMAIL=os.getenv("SUPPORT_EMAIL", ""),
    )
    if test_config:
        app.config.update(test_config)
    from .routes import auth, store, admin, payments
    from .cli import register_commands
    from .utils import INDIAN_STATES, parse_date
    for blueprint in (auth.bp, store.bp, admin.bp, payments.bp):
        app.register_blueprint(blueprint)
    register_commands(app)

    @app.before_request
    def session_security():
        """Resolve users from private JSON and verify all browser mutation tokens."""
        g.user = database.find(database.read(), "users", session["user_id"]) if session.get("user_id") else None
        session.setdefault("csrf_token", secrets.token_urlsafe(32))
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint != "payments.webhook":
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(str(supplied), session["csrf_token"]):
                abort(400, description="Your session expired. Refresh this page and try again.")

    @app.context_processor
    def shared_data():
        """Navigation counts and store settings shared by server-rendered pages."""
        return {"csrf_token": session.get("csrf_token", ""), "cart_count": sum(session.get("cart", {}).values()),
            "shipping_threshold": app.config["FREE_SHIPPING_THRESHOLD_PAISE"], "shipping_fee": app.config["SHIPPING_FEE_PAISE"],
            "return_window": app.config["RETURN_WINDOW_DAYS"], "support_email": app.config["SUPPORT_EMAIL"],
            "indian_states": INDIAN_STATES, "demo_payments": app.config["DEMO_PAYMENTS"]}

    @app.template_filter("money")
    def money(paise):
        """Format integer paise as rupees for the India-only storefront."""
        value = int(paise or 0) / 100
        return "₹" + (f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}")

    @app.template_filter("date")
    def date(value):
        """Display stored UTC timestamps in Indian Standard Time."""
        if not value:
            return "—"
        # A fixed IST offset avoids requiring an OS timezone database on Windows.
        from datetime import timezone
        return parse_date(value).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%d %b %Y, %I:%M %p")

    @app.after_request
    def security_headers(response):
        """Keep account responses private and prevent MIME guessing and framing."""
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if request.endpoint != "static":
            response.headers["Cache-Control"] = "no-store"
        if request.method == "POST" and response.status_code < 400 and os.getenv("SMTP_HOST"):
            # A single short attempt keeps Vercel requests bounded; the admin can retry.
            from .services.notifications import deliver_pending
            try:
                deliver_pending(limit=1)
            except database.DatabaseError:
                app.logger.warning("Email delivery deferred while JSON storage is busy.")
        return response

    @app.errorhandler(database.DatabaseError)
    def storage_error(error):
        """Report storage failures without showing private paths or credentials."""
        app.logger.error("JSON storage error: %s", type(error).__name__)
        return render_template("error.html", code=503, message="The store is temporarily busy. Please try again shortly."), 503

    for code in (400, 403, 404, 413):
        app.register_error_handler(code, page_error)
    return app


def page_error(error):
    """Render consistent HTTP error pages with a route back to the catalog."""
    return render_template("error.html", code=error.code, message=error.description), error.code
