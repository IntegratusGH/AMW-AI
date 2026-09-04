"""Production WSGI server (Waitress) - run in the foreground.

Binds to HOST:PORT from the .env file (0.0.0.0 by default) so the app is
reachable from other computers on the network.

    venv\\Scripts\\python.exe service\\run_waitress.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from waitress import serve  # noqa: E402

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402


def main() -> None:
    app = create_app()
    print(f"Serving {Config.APP_NAME} on http://{Config.HOST}:{Config.PORT}")
    serve(app, host=Config.HOST, port=Config.PORT, threads=8)


if __name__ == "__main__":
    main()
