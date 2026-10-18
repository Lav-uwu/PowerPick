"""Explicit JSON initialization and administrator management from a trusted terminal."""

import click
from werkzeug.security import generate_password_hash
from . import database
from .utils import now


def register_commands(app):
    """Attach small operational commands to the Flask CLI."""
    @app.cli.command("init-db")
    def init_db():
        """Create local/private hosted JSON, preserving all existing data."""
        database.mutate(lambda data: None)
        click.echo("JSON database ready. Existing data was preserved.")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True, default="Store admin")
    @click.password_option(confirmation_prompt=True)
    def create_admin(email, name, password):
        """Create or promote an administrator; there are no default passwords."""
        from .routes.auth import valid_email
        email = email.strip().lower()
        if not valid_email(email) or not 10 <= len(password) <= 128 or not 2 <= len(name.strip()) <= 80:
            raise click.ClickException("Use a valid email, name and password of 10–128 characters.")
        password_hash = generate_password_hash(password)
        def save(data):
            user = next((u for u in data["users"] if u["email"] == email), None)
            if user:
                user.update(is_admin=True, password_hash=password_hash)
            else:
                database.insert(data, "users", {"name": name.strip(), "email": email, "password_hash": password_hash, "is_admin": True, "created_at": now()})
        database.mutate(save)
        click.echo(f"Administrator ready: {email}. Sign in, then open /admin.")

    @app.cli.command("expire-payments")
    def expire_payments():
        """Release abandoned reservations older than thirty minutes."""
        from .services.orders import expire_pending
        click.echo(f"Released {expire_pending()} expired reservations.")
