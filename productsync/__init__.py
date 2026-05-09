"""ProductSync Flask application factory."""
from __future__ import annotations

import os
from datetime import datetime

# Use the OS certificate store for TLS verification rather than certifi's bundled
# CA list. Lets us trust enterprise/AV intermediaries (e.g. Avast HTTPS scanning
# on Windows) and on Linux/Heroku falls back to the system ca-certificates package.
# Must run before any TLS connection is opened.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # truststore is optional; falls back to certifi if not installed

from flask import Flask, redirect, render_template, url_for
from flask_login import current_user
from werkzeug.security import generate_password_hash

from .config import Config
from .extensions import db, login_manager, migrate


def create_app(config: type[Config] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=False)
    app.config.from_object(config or Config)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"

    from .models import User

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    # Blueprints
    from .auth.routes import bp as auth_bp
    from .catalogue.routes import bp as catalogue_bp
    from .addons_ui.routes import bp as addons_ui_bp
    from .orders_ui.routes import bp as orders_ui_bp
    from .sync_ui.routes import bp as sync_ui_bp
    from .webhooks.routes import bp as webhooks_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(catalogue_bp)
    app.register_blueprint(addons_ui_bp)
    app.register_blueprint(orders_ui_bp)
    app.register_blueprint(sync_ui_bp)
    app.register_blueprint(webhooks_bp)

    @app.route("/")
    def root():
        if current_user.is_authenticated:
            return redirect(url_for("sync_ui.dashboard"))
        return render_template("marketing/landing.html")

    # ── Jinja: relative-time filter ────────────────────────────────────────
    @app.template_filter("naturaltime")
    def naturaltime(dt):
        if not dt:
            return ""
        secs = (datetime.utcnow() - dt).total_seconds()
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs / 60)}m ago"
        if secs < 86400:
            return f"{int(secs / 3600)}h ago"
        days = int(secs / 86400)
        if days < 30:
            return f"{days}d ago"
        return dt.strftime("%Y-%m-%d")

    # ── Context processor: nav notifications dropdown ──────────────────────
    @app.context_processor
    def inject_nav_data():
        if not current_user.is_authenticated:
            return {"nav_recent_events": []}
        from .models import ChannelAccount, SyncJob, WebhookEvent
        aid = current_user.account_id
        events = []
        for j in (
            SyncJob.query.filter_by(account_id=aid)
            .order_by(SyncJob.created_at.desc())
            .limit(3).all()
        ):
            events.append({
                "kind": "job",
                "title": j.kind.replace("_", " "),
                "channel": j.channel_account.display_name if j.channel_account else "—",
                "status": j.status,
                "when": j.created_at,
                "href": url_for("sync_ui.log"),
            })
        for e in (
            WebhookEvent.query
            .join(ChannelAccount, WebhookEvent.channel_account_id == ChannelAccount.id)
            .filter(ChannelAccount.account_id == aid)
            .order_by(WebhookEvent.received_at.desc())
            .limit(3).all()
        ):
            events.append({
                "kind": "webhook",
                "title": e.topic,
                "channel": e.channel_account.display_name if e.channel_account else "—",
                "status": "valid" if e.signature_valid else "invalid",
                "when": e.received_at,
                "href": url_for("sync_ui.log"),
            })
        events.sort(key=lambda x: x["when"], reverse=True)
        return {"nav_recent_events": events[:5]}

    @app.cli.command("seed")
    def seed_command():
        """Create the default account + admin user if missing."""
        from .models import Account, User as UserModel
        if Account.query.count() == 0:
            account = Account(name="Default")
            db.session.add(account)
            db.session.flush()
            db.session.add(
                UserModel(
                    account_id=account.id,
                    name="Admin",
                    email="admin@example.com",
                    password_hash=generate_password_hash("changeme123"),
                )
            )
            db.session.commit()
            print("Seeded Default account + admin@example.com (password: changeme123)")
        else:
            print("Already seeded.")

    if os.environ.get("ENABLE_SCHEDULER") == "1" and not app.config.get("TESTING"):
        from .scheduler import start_scheduler
        start_scheduler(app)

    return app
