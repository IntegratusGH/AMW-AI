"""Administration routes: manage users, roles and view the audit log."""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.blueprints.admin import admin_bp
from app.extensions import db
from app.forms import CreateUserForm, EditUserForm, ResetPasswordForm, RoleForm
from app.models import ROLE_ADMIN, AuditLog, Role, User, record_audit
from app.utils.decorators import admin_required


def _role_choices():
    return [(r.id, r.name) for r in Role.query.order_by(Role.name).all()]


def _active_admin_count() -> int:
    return (
        User.query.join(Role)
        .filter(Role.name == ROLE_ADMIN, User.is_active.is_(True))
        .count()
    )


@admin_bp.route("/users")
@admin_required
def users():
    all_users = User.query.order_by(User.username).all()
    return render_template("admin/users.html", users=all_users)


@admin_bp.route("/users/create", methods=["GET", "POST"])
@admin_required
def create_user():
    form = CreateUserForm()
    form.role.choices = _role_choices()
    if form.validate_on_submit():
        if User.query.filter_by(username=form.username.data.strip()).first():
            flash("That username is already taken.", "danger")
        elif User.query.filter_by(email=form.email.data.strip()).first():
            flash("That email address is already registered.", "danger")
        else:
            user = User(
                username=form.username.data.strip(),
                email=form.email.data.strip(),
                full_name=form.full_name.data,
                role_id=form.role.data,
                is_active=form.is_active.data,
                must_change_password=form.must_change_password.data,
            )
            user.set_password(form.password.data)
            db.session.add(user)
            record_audit("user_created", f"username={user.username}")
            db.session.commit()
            flash(f"User '{user.username}' has been created.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, mode="create")


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id: int):
    user = db.session.get(User, user_id) or abort(404)
    form = EditUserForm(obj=user)
    form.role.choices = _role_choices()
    if request.method == "GET":
        form.role.data = user.role_id
    if form.validate_on_submit():
        dup = User.query.filter(
            User.email == form.email.data.strip(), User.id != user.id
        ).first()
        if dup:
            flash("That email address is already in use.", "danger")
            return render_template("admin/user_form.html", form=form, mode="edit", user=user)
        # Guard: do not strip the last active administrator.
        losing_admin = user.is_administrator and (
            form.role.data != user.role_id or not form.is_active.data
        )
        if losing_admin and _active_admin_count() <= 1:
            flash("You cannot remove the last active administrator.", "warning")
            return render_template("admin/user_form.html", form=form, mode="edit", user=user)
        user.username = form.username.data.strip()
        user.email = form.email.data.strip()
        user.full_name = form.full_name.data
        user.role_id = form.role.data
        user.is_active = form.is_active.data
        record_audit("user_updated", f"username={user.username}")
        db.session.commit()
        flash(f"User '{user.username}' has been updated.", "success")
        return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, mode="edit", user=user)


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
@admin_required
def toggle_active(user_id: int):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "warning")
        return redirect(url_for("admin.users"))
    if user.is_active and user.is_administrator and _active_admin_count() <= 1:
        flash("You cannot deactivate the last active administrator.", "warning")
        return redirect(url_for("admin.users"))
    user.is_active = not user.is_active
    action = "user_activated" if user.is_active else "user_deactivated"
    record_audit(action, f"username={user.username}")
    db.session.commit()
    state = "activated" if user.is_active else "deactivated"
    flash(f"User '{user.username}' has been {state}.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/reset-password", methods=["GET", "POST"])
@admin_required
def reset_password(user_id: int):
    user = db.session.get(User, user_id) or abort(404)
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user.set_password(form.password.data)
        user.must_change_password = form.must_change_password.data
        record_audit("password_reset", f"username={user.username}")
        db.session.commit()
        flash(f"Password for '{user.username}' has been reset.", "success")
        return redirect(url_for("admin.users"))
    return render_template("admin/reset_password.html", form=form, user=user)


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def delete_user(user_id: int):
    user = db.session.get(User, user_id) or abort(404)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "warning")
        return redirect(url_for("admin.users"))
    if user.is_administrator and _active_admin_count() <= 1:
        flash("You cannot delete the last active administrator.", "warning")
        return redirect(url_for("admin.users"))
    username = user.username
    db.session.delete(user)
    record_audit("user_deleted", f"username={username}")
    db.session.commit()
    flash(f"User '{username}' has been deleted.", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/roles", methods=["GET", "POST"])
@admin_required
def roles():
    form = RoleForm()
    if form.validate_on_submit():
        if Role.query.filter_by(name=form.name.data.strip()).first():
            flash("A role with that name already exists.", "danger")
        else:
            role = Role(name=form.name.data.strip(), description=form.description.data)
            db.session.add(role)
            record_audit("role_created", f"name={role.name}")
            db.session.commit()
            flash(f"Role '{role.name}' has been created.", "success")
            return redirect(url_for("admin.roles"))
    all_roles = Role.query.order_by(Role.name).all()
    return render_template("admin/roles.html", form=form, roles=all_roles)


@admin_bp.route("/audit")
@admin_required
def audit_log():
    page = request.args.get("page", 1, type=int)
    logs = (
        AuditLog.query.order_by(AuditLog.created_at.desc())
        .paginate(page=page, per_page=25, error_out=False)
    )
    return render_template("admin/audit.html", logs=logs)
