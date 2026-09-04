"""Authentication routes: login, logout, change password."""
from __future__ import annotations

from datetime import datetime

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.blueprints.auth import auth_bp
from app.extensions import db
from app.forms import ChangePasswordForm, LoginForm
from app.models import User, record_audit

# Endpoints an authenticated-but-must-change-password user may still reach.
_ALLOWED_WHILE_FORCED = {"auth.change_password", "auth.logout", "static"}


def _is_safe_next(target: str | None) -> bool:
    return bool(target) and target.startswith("/") and not target.startswith("//")


@auth_bp.before_app_request
def enforce_password_change():
    """Force users flagged for password change onto the change-password page."""
    if current_user.is_authenticated and getattr(current_user, "must_change_password", False):
        if request.endpoint not in _ALLOWED_WHILE_FORCED:
            return redirect(url_for("auth.change_password"))
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user is None or not user.check_password(form.password.data):
            record_audit("login_failed", f"username={form.username.data.strip()}")
            db.session.commit()
            flash("Invalid username or password.", "danger")
            return render_template("auth/login.html", form=form), 401
        if not user.is_active:
            record_audit("login_blocked", "account deactivated", user=user)
            db.session.commit()
            flash("This account has been deactivated. Contact an administrator.", "danger")
            return render_template("auth/login.html", form=form), 403

        login_user(user, remember=form.remember.data)
        user.last_login_at = datetime.utcnow()
        record_audit("login_success", user=user)
        db.session.commit()

        next_page = request.args.get("next")
        if not _is_safe_next(next_page):
            next_page = url_for("main.dashboard")
        flash(f"Welcome back, {user.full_name or user.username}!", "success")
        return redirect(next_page)

    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout")
@login_required
def logout():
    record_audit("logout")
    db.session.commit()
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Your current password is incorrect.", "danger")
            return render_template("auth/change_password.html", form=form)
        current_user.set_password(form.new_password.data)
        current_user.must_change_password = False
        record_audit("password_changed", "self-service")
        db.session.commit()
        flash("Your password has been updated.", "success")
        return redirect(url_for("main.profile"))
    return render_template("auth/change_password.html", form=form)
