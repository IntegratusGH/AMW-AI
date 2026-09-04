"""Application-wide error handlers."""
from __future__ import annotations

from flask import render_template

from app.blueprints.errors import errors_bp
from app.extensions import db


@errors_bp.app_errorhandler(401)
def unauthorized(error):
    return render_template("errors/error.html", code=401,
                           title="Authentication required",
                           message="Please sign in to continue."), 401


@errors_bp.app_errorhandler(403)
def forbidden(error):
    return render_template("errors/error.html", code=403,
                           title="Access denied",
                           message="You do not have permission to view this page."), 403


@errors_bp.app_errorhandler(404)
def not_found(error):
    return render_template("errors/error.html", code=404,
                           title="Page not found",
                           message="The page you requested could not be found."), 404


@errors_bp.app_errorhandler(500)
def server_error(error):
    db.session.rollback()
    return render_template("errors/error.html", code=500,
                           title="Server error",
                           message="Something went wrong. Please try again later."), 500
