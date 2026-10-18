"""Password-based customer identity, safe redirects, and protected admin access."""

import hashlib
import re
import secrets
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from .. import database
from ..utils import now, parse_date

bp = Blueprint("auth", __name__)


def valid_email(email):
    """Perform basic syntax validation without claiming mailbox verification."""
    return len(email) <= 254 and bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email))


def safe_next(value):
    """Allow only local redirects after account forms."""
    parsed = urlsplit(value or "")
    return value if value and value.startswith("/") and not value.startswith("//") and not parsed.netloc and "\\" not in value else url_for("store.home")


def login_required(function):
    """Redirect unsigned visitors to sign-in and preserve their destination."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if not g.user:
            return redirect(url_for("auth.login", next=request.path))
        return function(*args, **kwargs)
    return wrapped


def admin_required(function):
    """Recheck the saved role for every admin page and mutation."""
    @wraps(function)
    @login_required
    def wrapped(*args, **kwargs):
        if not g.user["is_admin"]:
            abort(403, description="This area is for store administrators.")
        return function(*args, **kwargs)
    return wrapped


def start_session(user):
    """Rotate login state and the CSRF token while keeping the visitor's basket."""
    cart = session.get("cart", {})
    session.clear()
    session.update(user_id=user["id"], cart=cart, csrf_token=secrets.token_urlsafe(32))
    session.permanent = True


@bp.route("/login", methods=["GET", "POST"])
def login():
    """Check a password hash and throttle failures using the shared JSON database."""
    destination = safe_next(request.values.get("next"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()[:255]
        password = request.form.get("password", "")[:129]
        key = hashlib.sha256(email.encode()).hexdigest()
        cutoff = parse_date(now()) - timedelta(minutes=15)
        def authenticate(data):
            data["auth_attempts"] = [a for a in data["auth_attempts"] if parse_date(a["created_at"]) > cutoff]
            if sum(a["key"] == key for a in data["auth_attempts"]) >= 8:
                return "limited", None
            user = next((u for u in data["users"] if u["email"] == email), None)
            if user and check_password_hash(user["password_hash"], password):
                data["auth_attempts"] = [a for a in data["auth_attempts"] if a["key"] != key]
                return "ok", user
            data["auth_attempts"].append({"key": key, "created_at": now()})
            return "invalid", None
        status, user = database.mutate(authenticate)
        if status == "ok":
            start_session(user)
            return redirect(destination)
        flash("Too many attempts. Try again in 15 minutes." if status == "limited" else "Email or password is incorrect.", "error")
        if status == "limited":
            return render_template("auth.html", mode="login", next=destination), 429
    return render_template("auth.html", mode="login", next=destination)


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    """Register a customer; a browser can never assign itself an admin role."""
    destination = safe_next(request.values.get("next"))
    if request.method == "POST":
        name, email = request.form.get("name", "").strip(), request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not 2 <= len(name) <= 80 or not valid_email(email) or not 10 <= len(password) <= 128 or not request.form.get("terms"):
            flash("Use a valid name, email, password of 10–128 characters, and accept the store policies.", "error")
        else:
            password_hash = generate_password_hash(password)
            def register(data):
                if any(u["email"] == email for u in data["users"]):
                    return None
                return database.insert(data, "users", {"name": name, "email": email, "password_hash": password_hash, "is_admin": False, "created_at": now()})
            user = database.mutate(register)
            if user:
                start_session(user)
                flash("Your PowerPick account is ready.", "success")
                return redirect(destination)
            flash("An account already uses that email. Please sign in.", "error")
    return render_template("auth.html", mode="signup", next=destination)


@bp.post("/logout")
def logout():
    """Remove authentication while preserving the cart for a later visit."""
    cart = session.get("cart", {})
    session.clear()
    session["cart"] = cart
    return redirect(url_for("store.home"))
