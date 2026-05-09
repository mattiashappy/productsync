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
    if current_user.is_authenticated:
        return redirect(url_for("sync_ui.dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for("sync_ui.dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("auth/login.html")


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for("sync_ui.dashboard"))

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
            return render_template("auth/signup.html", form=form)

        account = Account(name=form["company"] or f"{form['name']}'s workspace")
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
        flash(f"Welcome to ProductSync, {user.name.split()[0]}!", "info")
        return redirect(url_for("sync_ui.dashboard"))

    return render_template("auth/signup.html", form=form)


@bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("root"))
