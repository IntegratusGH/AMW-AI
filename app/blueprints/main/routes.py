"""Main application routes: dashboard, operations, profile, settings."""
from __future__ import annotations

from flask import abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.blueprints.main import main_bp
from app.extensions import db
from app.forms import ProfileForm, SettingsForm
from app.models import ROLE_ADMIN, ROLE_OPERATOR, User, record_audit

# AI automation modules shown on the Operations page, grouped by AMW business
# line.  "status" is "available" for delivered modules and "planned" for the
# roadmap; "icon" maps to a symbol in templates/main/_icons.html and "accent"
# selects its colour.
AI_OPERATIONS = [
    {
        "name": "Registration & Compliance",
        "modules": [
            {
                "title": "Registration Updater AI",
                "desc": "Automates vehicle registration (DMT) updates and keeps "
                        "ownership records in sync.",
                "icon": "file-check", "accent": "blue", "status": "available",
            },
        ],
    },
    {
        "name": "After-Sales & Service",
        "modules": [
            {
                "title": "Service Scheduler AI",
                "desc": "Books and optimises workshop and Pitstop service "
                        "appointments automatically.",
                "icon": "calendar", "accent": "green", "status": "available",
            },
            {
                "title": "Service & Warranty Reminder AI",
                "desc": "Proactively reminds customers of service due dates and "
                        "warranty expiry.",
                "icon": "bell", "accent": "amber", "status": "planned",
            },
            {
                "title": "Spare Parts Inventory AI",
                "desc": "Forecasts demand and optimises spare-part, tyre and "
                        "lubricant stock levels.",
                "icon": "package", "accent": "teal", "status": "planned",
            },
        ],
    },
    {
        "name": "Finance & Leasing",
        "modules": [
            {
                "title": "Payment Reconciliation AI",
                "desc": "Matches customer receipts, bank statements and leasing "
                        "payments automatically.",
                "icon": "credit-card", "accent": "green", "status": "available",
            },
            {
                "title": "Leasing & Insurance Document AI",
                "desc": "Extracts and verifies KYC and contract documents for "
                        "AMW Capital Leasing.",
                "icon": "shield", "accent": "purple", "status": "planned",
            },
            {
                "title": "Invoice & Expense OCR AI",
                "desc": "Reads supplier invoices and posts expense data without "
                        "manual entry.",
                "icon": "scan", "accent": "amber", "status": "planned",
            },
        ],
    },
    {
        "name": "Sales & Customer Engagement",
        "modules": [
            {
                "title": "Email / Message Handler AI",
                "desc": "Triages incoming email, WhatsApp and SMS and drafts "
                        "suggested replies.",
                "icon": "mail", "accent": "blue", "status": "available",
            },
            {
                "title": "Showroom Assistant AI",
                "desc": "Answers product, pricing and availability questions "
                        "across all brands.",
                "icon": "message-circle", "accent": "purple", "status": "planned",
            },
            {
                "title": "Vehicle Sales Lead AI",
                "desc": "Qualifies and follows up showroom enquiries to boost "
                        "conversions.",
                "icon": "target", "accent": "blue", "status": "planned",
            },
        ],
    },
    {
        "name": "Back-office & Insight",
        "modules": [
            {
                "title": "Customer Feedback & Sentiment AI",
                "desc": "Analyses after-sales feedback and reviews to surface "
                        "issues early.",
                "icon": "star", "accent": "teal", "status": "planned",
            },
            {
                "title": "Sales & Service Analytics AI",
                "desc": "Turns sales and service data into forecasts and live "
                        "dashboards.",
                "icon": "chart", "accent": "blue", "status": "planned",
            },
            {
                "title": "HR Recruitment Screening AI",
                "desc": "Screens career applications and shortlists candidates "
                        "faster.",
                "icon": "user-check", "accent": "purple", "status": "planned",
            },
        ],
    },
]


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
    if not current_user.has_role(ROLE_OPERATOR, ROLE_ADMIN):
        abort(403)
    return render_template("main/operations.html", operations=AI_OPERATIONS)


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
