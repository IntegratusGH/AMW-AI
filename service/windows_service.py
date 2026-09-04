"""Windows service wrapper for the AMW-AI web application.

Install / manage (run an *elevated* PowerShell / cmd from the project root):
    venv\\Scripts\\python.exe service\\windows_service.py install
    venv\\Scripts\\python.exe service\\windows_service.py start
    venv\\Scripts\\python.exe service\\windows_service.py stop
    venv\\Scripts\\python.exe service\\windows_service.py remove

To start automatically at boot:
    venv\\Scripts\\python.exe service\\windows_service.py --startup auto install
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import servicemanager  # noqa: E402
import win32event  # noqa: E402
import win32service  # noqa: E402
import win32serviceutil  # noqa: E402


class AMWAIService(win32serviceutil.ServiceFramework):
    _svc_name_ = "AMWAIWeb"
    _svc_display_name_ = "AMW-AI Web Application"
    _svc_description_ = (
        "Serves the AMW-AI Flask web application via Waitress on the local network."
    )

    def __init__(self, args):
        super().__init__(args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.server = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        if self.server is not None:
            # Ask the Waitress asyncore loop to shut down.
            self.server.close()
        win32event.SetEvent(self.hWaitStop)

    def SvcDoRun(self):
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        self._run_server()

    def _run_server(self):
        # Ensure relative paths (.env, templates) resolve correctly.
        os.chdir(str(BASE_DIR))

        from waitress.server import create_server
        from app import create_app
        from app.config import Config

        app = create_app()
        self.server = create_server(
            app, host=Config.HOST, port=Config.PORT, threads=8
        )
        try:
            self.server.run()
        except Exception as exc:  # pragma: no cover - logged to Event Viewer
            servicemanager.LogErrorMsg(f"AMW-AI service error: {exc}")


if __name__ == "__main__":
    if len(sys.argv) == 1:
        # Called by the Windows Service Control Manager.
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(AMWAIService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(AMWAIService)
