"""Durable customer inbox and optional SMTP outbox; no background worker needed."""

import os
import smtplib
from email.message import EmailMessage
from flask import current_app
from .. import database
from ..utils import now


def queue(data, email, subject, body, user_id=None):
    """Queue messages in the same atomic JSON change as the order/support action."""
    return database.insert(data, "notifications", {"email": email, "subject": subject, "body": body,
        "user_id": user_id, "sent": False, "created_at": now()})


def queue_order(data, order, subject, detail):
    """Build an order communication for the customer's inbox and email address."""
    user = database.find(data, "users", order["user_id"])
    link = f"{current_app.config['APP_URL']}/orders/{order['reference']}"
    queue(data, user["email"], f"{subject} · {order['reference']}", f"Hi {order['name']},\n\n{detail}\n\nView your order: {link}\n\nPowerPick", user["id"])


def deliver_pending(limit=1):
    """Try queued mail without losing messages when SMTP is unavailable."""
    if not os.getenv("SMTP_HOST"):
        return 0
    messages = [n for n in database.read()["notifications"] if not n["sent"]][:limit]
    delivered = 0
    for notification in messages:
        try:
            message = EmailMessage()
            message["From"] = os.getenv("MAIL_FROM", "PowerPick <orders@example.com>")
            message["To"], message["Subject"] = notification["email"], notification["subject"]
            message.set_content(notification["body"])
            with smtplib.SMTP(os.getenv("SMTP_HOST"), int(os.getenv("SMTP_PORT", "587")), timeout=8) as smtp:
                if os.getenv("SMTP_USE_TLS", "true").lower() == "true":
                    smtp.starttls()
                if os.getenv("SMTP_USERNAME"):
                    smtp.login(os.getenv("SMTP_USERNAME"), os.getenv("SMTP_PASSWORD", ""))
                smtp.send_message(message)
            def mark_sent(data):
                row = database.find(data, "notifications", notification["id"])
                if row:
                    row["sent"] = True
            database.mutate(mark_sent)
            delivered += 1
        except (OSError, smtplib.SMTPException, ValueError, database.DatabaseError):
            current_app.logger.warning("Mail delivery deferred; notification %s remains queued", notification["id"])
    return delivered
