import re

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from ..models import Account, User

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.route("/login", methods=["GET", "POST"])
def login():
    from .. import _dashboard_url_for  # avoid circular import at module load
    if current_user.is_authenticated:
        return redirect(_dashboard_url_for(current_user))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(_dashboard_url_for(user))
        flash("Invalid email or password.", "error")
    return render_template("auth/login.html")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    return _signup_handler(account_type="store",
                           template="auth/signup.html",
                           welcome_redirect_endpoint="sync_ui.dashboard")


@bp.route("/signup/distributor", methods=["GET", "POST"])
def signup_distributor():
    return _signup_handler(account_type="distributor",
                           template="auth/signup_distributor.html",
                           welcome_redirect_endpoint="distributor.dashboard")


def _signup_handler(*, account_type: str, template: str, welcome_redirect_endpoint: str):
    """Shared signup core for both store and distributor flows.
    What differs by type:
      - which form fields are accepted (and which are required)
      - the rendered template
      - the post-signup destination + welcome flash
    """
    if current_user.is_authenticated:
        return redirect(url_for(welcome_redirect_endpoint))

    is_distributor = account_type == "distributor"

    # Common fields
    form = {
        "name": request.form.get("name", "").strip(),
        "email": request.form.get("email", "").strip().lower(),
        "company": request.form.get("company", "").strip(),
        "industry": request.form.get("industry", "").strip(),
        "website": request.form.get("website", "").strip(),
        "country": request.form.get("country", "").strip(),
        # Store-only
        "current_platforms": request.form.get("current_platforms", "").strip(),
        # Distributor-only
        "product_count_estimate": request.form.get("product_count_estimate", "").strip(),
    }

    if request.method == "POST":
        password = request.form.get("password", "")
        password_confirm = request.form.get("password_confirm", "")
        terms = request.form.get("terms")

        errors: list[str] = []
        if not form["name"]:
            errors.append("Please enter your name.")
        if not EMAIL_RE.match(form["email"]):
            errors.append("Please enter a valid email address.")
        if len(password) < 8:
            errors.append("Password must be at least 8 characters.")
        if password != password_confirm:
            errors.append("Passwords don't match.")
        if not terms:
            errors.append("Please accept the terms to continue.")
        # Distributors MUST give a brand name — it's their public identity to
        # subscribed stores. Stores can leave the company blank (we'll generate one).
        if is_distributor and not form["company"]:
            errors.append("Please enter your brand or company name — stores will see this.")
        if not errors and User.query.filter_by(email=form["email"]).first():
            errors.append("An account with this email already exists. Try signing in instead.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(template, form=form)

        account_name = (
            form["company"]
            or (form["name"] + ("'s catalogue" if is_distributor else "'s workspace"))
        )
        account = Account(
            name=account_name,
            account_type=account_type,
            industry=form["industry"] or None,
            website=_normalize_url(form["website"]),
            country=form["country"] or None,
            # Type-specific: only persist the field that applies
            current_platforms=form["current_platforms"] or None if not is_distributor else None,
            product_count_estimate=form["product_count_estimate"] or None if is_distributor else None,
        )
        db.session.add(account)
        db.session.flush()
        user = User(
            account_id=account.id,
            name=form["name"],
            email=form["email"],
            password_hash=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()

        login_user(user)
        if is_distributor:
            flash(f"Welcome to ProductSync, {user.name.split()[0]}! "
                  f"You're set up as a distributor — your brand profile is live. "
                  f"The catalogue workspace is coming soon.", "info")
        else:
            flash(f"Welcome to ProductSync, {user.name.split()[0]}!", "info")
        return redirect(url_for(welcome_redirect_endpoint))

    return render_template(template, form=form)


def _normalize_url(value: str) -> str | None:
    """Make sure a website URL has a scheme so links work later."""
    value = (value or "").strip()
    if not value:
        return None
    if not (value.startswith("http://") or value.startswith("https://")):
        return "https://" + value
    return value


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("root"))
