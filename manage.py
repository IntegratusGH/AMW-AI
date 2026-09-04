"""Flask CLI / migration entry point.

Usage:
    set FLASK_APP=manage.py
    flask --app manage.py db upgrade
"""
from app import create_app

app = create_app()
