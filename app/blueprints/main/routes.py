"""Main application routes: dashboard, operations, profile, settings."""
from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.main import main_bp
from app.extensions import db
from app.forms import ProfileForm, SettingsForm
from app.models import ROLE_ADMIN, ROLE_OPERATOR, User, record_audit


@main_bp.route("/")
@login_required
def index():
    return redirect(url_for("main.dashboard"))


@main_bp.route("/dashboard")
@login_required
def dashboard():
    stats = None
    if current_user.is_administrator:
        stats = {
            "total_users": User.query.count(),
            "active_users": User.query.filter_by(is_active=True).count(),
            "inactive_users": User.query.filter_by(is_active=False).count(),
        }
    return render_template("main/dashboard.html", stats=stats)


@main_bp.route("/operations")
@login_required
def operations():
    # Blank placeholder page - operational behaviour to be defined later.
    if not current_user.has_role(ROLE_OPERATOR, ROLE_ADMIN):
        from flask import abort

        abort(403)
    return render_template("main/operations.html")


@main_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    form = ProfileForm(obj=current_user)
    if form.validate_on_submit():
        existing = User.query.filter(
            User.email == form.email.data.strip(), User.id != current_user.id
        ).first()
        if existing:
            flash("That email address is already in use.", "danger")
            return render_template("main/profile.html", form=form)
        current_user.full_name = form.full_name.data
        current_user.email = form.email.data.strip()
        record_audit("profile_updated", "self-service")
        db.session.commit()
        flash("Your profile has been updated.", "success")
        return redirect(url_for("main.profile"))
    return render_template("main/profile.html", form=form)


@main_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    form = SettingsForm(obj=current_user)
    if form.validate_on_submit():
        current_user.theme = form.theme.data
        db.session.commit()
        flash("Your settings have been saved.", "success")
        return redirect(url_for("main.settings"))
    return render_template("main/settings.html", form=form)


@main_bp.route("/theme", methods=["POST"])
@login_required
def set_theme():
    """Quick theme toggle from the top bar; returns to the previous page."""
    theme = request.form.get("theme", "light")
    if theme not in {"light", "dark"}:
        theme = "light"
    current_user.theme = theme
    db.session.commit()
    return redirect(request.referrer or url_for("main.dashboard"))
