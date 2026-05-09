"""Distributor area — for accounts with account_type='distributor'.

Stub for now: shows a welcome screen, account profile, and a roadmap of
what's coming. Catalogue publishing isn't built yet, so the dashboard is
deliberately sparse.

A `_require_distributor` decorator gates every route in this blueprint —
store-type users hitting /distributor/* are bounced to the store dashboard.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

bp = Blueprint("distributor", __name__, url_prefix="/distributor")


def _require_distributor(view):
    """Block store accounts from distributor pages and vice-versa."""
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.account.is_distributor:
            flash("That area is for distributor accounts.", "info")
            return redirect(url_for("sync_ui.dashboard"))
        return view(*args, **kwargs)
    return wrapper


@bp.route("/dashboard")
@_require_distributor
def dashboard():
    return render_template("distributor/dashboard.html", account=current_user.account)
