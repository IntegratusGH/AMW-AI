# Install / control the AMW-AI Windows service. Run in an ELEVATED PowerShell
# from the project root.
#
#   powershell -ExecutionPolicy Bypass -File scripts\manage_service.ps1 install
#   powershell -ExecutionPolicy Bypass -File scripts\manage_service.ps1 start
#   powershell -ExecutionPolicy Bypass -File scripts\manage_service.ps1 stop
#   powershell -ExecutionPolicy Bypass -File scripts\manage_service.ps1 remove

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("install", "start", "stop", "restart", "remove", "status")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "venv\Scripts\python.exe"
$svc = Join-Path $root "service\windows_service.py"

switch ($Action) {
    "install" { & $python $svc --startup auto install }
    "start"   { & $python $svc start }
    "stop"    { & $python $svc stop }
    "restart" { & $python $svc restart }
    "remove"  { & $python $svc remove }
    "status"  { Get-Service -Name "AMWAIWeb" }
}
