"""Razorpay REST integration; sensitive payment details stay inside its checkout."""

import base64
import hashlib
import hmac
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from flask import current_app


class PaymentError(Exception):
    """A safe, customer-facing gateway error without credentials or raw responses."""


def online_enabled():
    """Offer online payment only when both server credentials exist."""
    return bool(current_app.config["RAZORPAY_KEY_ID"] and current_app.config["RAZORPAY_KEY_SECRET"])


def gateway_request(path, data=None):
    """Call a fixed Razorpay endpoint with HTTP Basic authentication and a timeout."""
    if not online_enabled():
        raise PaymentError("Online payment is currently unavailable. Please choose cash on delivery.")
    credentials = f"{current_app.config['RAZORPAY_KEY_ID']}:{current_app.config['RAZORPAY_KEY_SECRET']}"
    headers = {"Authorization": "Basic " + base64.b64encode(credentials.encode()).decode(), "Content-Type": "application/json"}
    payload = json.dumps(data).encode() if data is not None else None
    request = Request("https://api.razorpay.com/v1/" + path, data=payload, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=12) as response:
            return json.load(response)
    except (HTTPError, URLError, OSError, ValueError):
        raise PaymentError("The payment provider could not complete this request. Please try again.") from None


def valid_signature(message, signature, secret):
    """Compare HMAC signatures in constant time for checkout and webhook payloads."""
    if not secret or not signature:
        return False
    digest = hmac.new(secret.encode(), message if isinstance(message, bytes) else message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, signature)
