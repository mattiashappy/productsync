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
    """Shared signup logic for both store and distributor flows.
    The only differences between the two are the account_type written to the
    DB, the rendered template, and where the user lands after signing up.
    """
    if current_user.is_authenticated:
        return redirect(url_for(welcome_redirect_endpoint))

    form = {
        "name": request.form.get("name", "").strip(),
        "email": request.form.get("email", "").strip().lower(),
        "company": request.form.get("company", "").strip(),
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
        if not errors and User.query.filter_by(email=form["email"]).first():
            errors.append("An account with this email already exists. Try signing in instead.")

        if errors:
            for e in errors:
                flash(e, "error")
            return render_template(template, form=form)

        # Distributors get the company name front-and-centre; stores can leave it blank.
        account_name = (
            form["company"]
            or (form["name"] + ("'s catalogue" if account_type == "distributor"
                                 else "'s workspace"))
        )
        account = Account(name=account_name, account_type=account_type)
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
        if account_type == "distributor":
            flash(f"Welcome to ProductSync, {user.name.split()[0]}! "
                  f"You're set up as a distributor. Catalogue publishing is coming soon.", "info")
        else:
            flash(f"Welcome to ProductSync, {user.name.split()[0]}!", "info")
        return redirect(url_for(welcome_redirect_endpoint))

    return render_template(template, form=form)


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("root"))
