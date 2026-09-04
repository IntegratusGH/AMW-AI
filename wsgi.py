"""WSGI entry point for production WSGI hosts.

    waitress-serve --host=0.0.0.0 --port=8080 wsgi:application
"""
from app import create_app

application = create_app()
