"""Development entry point.

For production use the Waitress runner or the Windows service instead:
    venv\\Scripts\\python.exe service\\run_waitress.py
"""
from app import create_app
from app.config import Config

app = create_app()

if __name__ == "__main__":
    app.run(host=Config.HOST, port=Config.PORT, debug=True)
