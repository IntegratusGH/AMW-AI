"""Administration routes: manage users, roles and view the audit log."""
from __future__ import annotations

from flask import abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.blueprints.admin import admin_bp
from app.extensions import db
from app.forms import (
    CategoryForm,
    CreateADUserForm,
    CreateUserForm,
    EditUserForm,
    ModuleForm,
    ResetPasswordForm,
    RoleForm,
)
from app.models import (
    AUTH_AD,
    BUILTIN_ICONS,
    ICON_SIZE_DEFAULT,
    ICON_SIZE_MAX,
    ICON_SIZE_MIN,
    ICON_UPLOAD,
    ROLE_ADMIN,
    AuditLog,
    Category,
    Module,
    Role,
    User,
    record_audit,
)
from app.utils.ad import ADError, ad_lookup_user
from app.utils.decorators import admin_required
from app.utils.uploads import delete_module_icon, save_module_icon, slugify


def _role_choices():
    return [(r.id, r.name) for r in Role.query.order_by(Role.name).all()]


def _builtin_icon_choices():
    return [(n, n) for n in BUILTIN_ICONS]


def _category_choices():
    """Dropdown options for a module's category (0 == none). Inactive
    categories are still offered, flagged, so existing links are not lost."""
    cats = Category.query.order_by(Category.sort_order, Category.name).all()
    return [(0, "— None —")] + [
        (c.id, c.name if c.is_active else f"{c.name} (disabled)") for c in cats
    ]


def _clamp_icon_size(value) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return ICON_SIZE_DEFAULT
    return max(ICON_SIZE_MIN, min(ICON_SIZE_MAX, size))


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
                is_ai_trainer=form.is_ai_trainer.data,
                must_change_password=form.must_change_password.data,
            )
            user.set_password(form.password.data)
            db.session.add(user)
            record_audit("user_created", f"username={user.username}")
            db.session.commit()
            flash(f"User '{user.username}' has been created.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/user_form.html", form=form, mode="create")


@admin_bp.route("/users/create-ad", methods=["GET", "POST"])
@admin_required
def create_ad_user():
    if not current_app.config.get("AD_ENABLED"):
        flash("Active Directory sign-in is not enabled on this server.", "warning")
        return redirect(url_for("admin.users"))
    form = CreateADUserForm()
    form.role.choices = _role_choices()
    if form.validate_on_submit():
        typed = form.username.data.strip()
        try:
            ad_user = ad_lookup_user(typed)
        except ADError as exc:
            flash(f"Directory lookup failed: {exc}", "danger")
            return render_template("admin/ad_user_form.html", form=form)
        if ad_user is None:
            flash(f"User '{typed}' was not found in Active Directory.", "danger")
            return render_template("admin/ad_user_form.html", form=form)

        username = ad_user.username
        email = (form.email.data or ad_user.email or "").strip()
        full_name = (form.full_name.data or ad_user.full_name or "").strip() or None
        if not email:
            flash("No email found in AD for this user; enter one manually.", "danger")
            return render_template("admin/ad_user_form.html", form=form)

        if User.query.filter_by(username=username).first():
            flash(f"An account for '{username}' already exists.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("That email address is already registered.", "danger")
        else:
            user = User(
                username=username,
                email=email,
                full_name=full_name,
                role_id=form.role.data,
                is_active=form.is_active.data,
                is_ai_trainer=form.is_ai_trainer.data,
                auth_source=AUTH_AD,
                must_change_password=False,
            )
            db.session.add(user)
            record_audit("ad_user_added", f"username={username}")
            db.session.commit()
            flash(f"Active Directory user '{username}' has been added.", "success")
            return redirect(url_for("admin.users"))
    return render_template("admin/ad_user_form.html", form=form)


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
        user.is_ai_trainer = form.is_ai_trainer.data
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
    if user.is_ad_user:
        flash("AD accounts have no local password to reset.", "warning")
        return redirect(url_for("admin.users"))
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


# ===== AI automation modules =============================================
@admin_bp.route("/modules")
@admin_required
def modules():
    all_modules = Module.query.order_by(
        Module.sort_order, Module.category_id, Module.name
    ).all()
    categories = Category.query.order_by(
        Category.sort_order, Category.name
    ).all()
    return render_template(
        "admin/modules.html", modules=all_modules, categories=categories)


def _apply_icon(form, module) -> bool:
    """Set module icon from the form. Returns False (with a flash) if an
    upload was requested but no image is available."""
    if form.icon_kind.data == ICON_UPLOAD:
        if form.icon_file.data:
            new_name = save_module_icon(form.icon_file.data)
            if module.is_upload_icon and module.icon_value:
                delete_module_icon(module.icon_value)
            module.icon_kind = ICON_UPLOAD
            module.icon_value = new_name
        elif module.is_upload_icon and module.icon_value:
            module.icon_kind = ICON_UPLOAD  # keep existing upload
        else:
            flash("Choose an image to upload, or pick a built-in icon.", "danger")
            return False
    else:
        if module.is_upload_icon and module.icon_value:
            delete_module_icon(module.icon_value)
        module.icon_kind = "builtin"
        module.icon_value = form.icon_builtin.data or BUILTIN_ICONS[0]
    return True


@admin_bp.route("/modules/create", methods=["GET", "POST"])
@admin_required
def create_module():
    form = ModuleForm()
    form.icon_builtin.choices = _builtin_icon_choices()
    form.category_id.choices = _category_choices()
    if form.validate_on_submit():
        key = (form.key.data or slugify(form.name.data)).strip()
        if Module.query.filter_by(key=key).first():
            flash(f"A module with key '{key}' already exists.", "danger")
        else:
            module = Module(
                key=key,
                name=form.name.data.strip(),
                description=(form.description.data or "").strip() or None,
                category_id=form.category_id.data or None,
                status=form.status.data,
                accent=form.accent.data,
                icon_size=_clamp_icon_size(form.icon_size.data),
                is_active=form.is_active.data,
                sort_order=form.sort_order.data or 0,
            )
            if not _apply_icon(form, module):
                return render_template(
                    "admin/module_form.html", form=form, mode="create",
                    builtin_icons=BUILTIN_ICONS)
            # Auto-enable for Administrators so they never lose visibility.
            admin_role = Role.query.filter_by(name=ROLE_ADMIN).first()
            if admin_role:
                module.roles.append(admin_role)
            db.session.add(module)
            record_audit("module_created", f"key={key}")
            db.session.commit()
            flash(f"Module '{module.name}' has been created.", "success")
            return redirect(url_for("admin.modules"))
    return render_template(
        "admin/module_form.html", form=form, mode="create",
        builtin_icons=BUILTIN_ICONS)


@admin_bp.route("/modules/<int:module_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_module(module_id: int):
    module = db.session.get(Module, module_id) or abort(404)
    form = ModuleForm(obj=module)
    form.icon_builtin.choices = _builtin_icon_choices()
    form.category_id.choices = _category_choices()
    if request.method == "GET":
        form.icon_kind.data = module.icon_kind
        form.category_id.data = module.category_id or 0
        form.icon_builtin.data = (
            module.icon_value if not module.is_upload_icon else BUILTIN_ICONS[0]
        )
    if form.validate_on_submit():
        key = (form.key.data or slugify(form.name.data)).strip()
        dup = Module.query.filter(
            Module.key == key, Module.id != module.id
        ).first()
        if dup:
            flash(f"A module with key '{key}' already exists.", "danger")
            return render_template(
                "admin/module_form.html", form=form, mode="edit",
                module=module, builtin_icons=BUILTIN_ICONS)
        if not _apply_icon(form, module):
            return render_template(
                "admin/module_form.html", form=form, mode="edit",
                module=module, builtin_icons=BUILTIN_ICONS)
        module.key = key
        module.name = form.name.data.strip()
        module.description = (form.description.data or "").strip() or None
        module.category_id = form.category_id.data or None
        module.status = form.status.data
        module.accent = form.accent.data
        module.icon_size = _clamp_icon_size(form.icon_size.data)
        module.is_active = form.is_active.data
        module.sort_order = form.sort_order.data or 0
        record_audit("module_updated", f"key={key}")
        db.session.commit()
        flash(f"Module '{module.name}' has been updated.", "success")
        return redirect(url_for("admin.modules"))
    return render_template(
        "admin/module_form.html", form=form, mode="edit",
        module=module, builtin_icons=BUILTIN_ICONS)


@admin_bp.route("/modules/<int:module_id>/toggle-active", methods=["POST"])
@admin_required
def toggle_module(module_id: int):
    module = db.session.get(Module, module_id) or abort(404)
    module.is_active = not module.is_active
    record_audit(
        "module_activated" if module.is_active else "module_deactivated",
        f"key={module.key}",
    )
    db.session.commit()
    state = "activated" if module.is_active else "deactivated"
    flash(f"Module '{module.name}' has been {state}.", "success")
    return redirect(url_for("admin.modules"))


@admin_bp.route("/modules/<int:module_id>/move", methods=["POST"])
@admin_required
def move_module(module_id: int):
    direction = request.form.get("direction", "up")
    ordered = Module.query.order_by(Module.sort_order, Module.name).all()
    for i, m in enumerate(ordered):  # normalise to remove ties
        m.sort_order = i
    idx = next((i for i, m in enumerate(ordered) if m.id == module_id), None)
    if idx is not None:
        j = idx - 1 if direction == "up" else idx + 1
        if 0 <= j < len(ordered):
            ordered[idx].sort_order, ordered[j].sort_order = (
                ordered[j].sort_order, ordered[idx].sort_order)
    db.session.commit()
    return redirect(url_for("admin.modules"))


@admin_bp.route("/modules/<int:module_id>/delete", methods=["POST"])
@admin_required
def delete_module(module_id: int):
    module = db.session.get(Module, module_id) or abort(404)
    name = module.name
    if module.is_upload_icon and module.icon_value:
        delete_module_icon(module.icon_value)
    db.session.delete(module)
    record_audit("module_deleted", f"key={module.key}")
    db.session.commit()
    flash(f"Module '{name}' has been deleted.", "success")
    return redirect(url_for("admin.modules"))


# ===== Module categories =================================================
@admin_bp.route("/categories/create", methods=["GET", "POST"])
@admin_required
def create_category():
    form = CategoryForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        if Category.query.filter_by(name=name).first():
            flash(f"A category named '{name}' already exists.", "danger")
        else:
            last = Category.query.order_by(Category.sort_order.desc()).first()
            category = Category(
                name=name,
                is_active=form.is_active.data,
                sort_order=(last.sort_order + 1) if last else 0,
            )
            db.session.add(category)
            record_audit("category_created", f"name={name}")
            db.session.commit()
            flash(f"Category '{name}' has been created.", "success")
            return redirect(url_for("admin.modules"))
    return render_template("admin/category_form.html", form=form, mode="create")


@admin_bp.route("/categories/<int:category_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_category(category_id: int):
    category = db.session.get(Category, category_id) or abort(404)
    form = CategoryForm(obj=category)
    if form.validate_on_submit():
        name = form.name.data.strip()
        dup = Category.query.filter(
            Category.name == name, Category.id != category.id
        ).first()
        if dup:
            flash(f"A category named '{name}' already exists.", "danger")
            return render_template(
                "admin/category_form.html", form=form, mode="edit",
                category=category)
        category.name = name
        category.is_active = form.is_active.data
        record_audit("category_updated", f"name={name}")
        db.session.commit()
        flash(f"Category '{name}' has been updated.", "success")
        return redirect(url_for("admin.modules"))
    return render_template(
        "admin/category_form.html", form=form, mode="edit", category=category)


@admin_bp.route("/categories/<int:category_id>/toggle-active", methods=["POST"])
@admin_required
def toggle_category(category_id: int):
    category = db.session.get(Category, category_id) or abort(404)
    category.is_active = not category.is_active
    record_audit(
        "category_activated" if category.is_active else "category_deactivated",
        f"name={category.name}",
    )
    db.session.commit()
    state = "activated" if category.is_active else "deactivated"
    flash(f"Category '{category.name}' has been {state}.", "success")
    return redirect(url_for("admin.modules"))


@admin_bp.route("/categories/<int:category_id>/delete", methods=["POST"])
@admin_required
def delete_category(category_id: int):
    category = db.session.get(Category, category_id) or abort(404)
    name = category.name
    # Modules keep existing; their category_id is cleared (uncategorised).
    db.session.delete(category)
    record_audit("category_deleted", f"name={name}")
    db.session.commit()
    flash(f"Category '{name}' has been deleted; its modules are now "
          f"uncategorised.", "success")
    return redirect(url_for("admin.modules"))


@admin_bp.route("/roles/<int:role_id>/modules", methods=["GET", "POST"])
@admin_required
def manage_role_modules(role_id: int):
    role = db.session.get(Role, role_id) or abort(404)
    all_modules = Module.query.order_by(
        Module.category_id, Module.sort_order, Module.name
    ).all()
    if request.method == "POST":
        chosen = {int(x) for x in request.form.getlist("module_ids")}
        role.modules = [m for m in all_modules if m.id in chosen]
        record_audit(
            "role_modules_updated", f"role={role.name} count={len(role.modules)}")
        db.session.commit()
        flash(f"Modules for role '{role.name}' have been updated.", "success")
        return redirect(url_for("admin.roles"))
    enabled = {m.id for m in role.modules}
    return render_template(
        "admin/role_modules.html", role=role, modules=all_modules, enabled=enabled)


@admin_bp.route("/audit")
@admin_required
def audit_log():
    page = request.args.get("page", 1, type=int)
    logs = (
        AuditLog.query.order_by(AuditLog.created_at.desc())
        .paginate(page=page, per_page=25, error_out=False)
    )
    return render_template("admin/audit.html", logs=logs)
