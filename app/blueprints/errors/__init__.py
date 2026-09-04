"""Error-handling blueprint."""
from flask import Blueprint

errors_bp = Blueprint("errors", __name__)

from app.blueprints.errors import handlers  # noqa: E402,F401
