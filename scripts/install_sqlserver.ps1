<#
    install_sqlserver.ps1
    ---------------------------------------------------------------------------
    One-click, unattended installer for SQL Server 2022 Developer Edition.

    What it does (after a SINGLE UAC approval):
      1. Self-elevates to Administrator.
      2. Uses the SSEI bootstrapper to download the full media (CAB).
      3. Extracts the media and runs an unattended Database-Engine install
         (Mixed Mode, default instance, TCP enabled) using ConfigurationFile.ini.
      4. Reads the 'sa' password from .env and passes it as /SAPWD (never printed).
      5. Opens the Windows Firewall for TCP 1433.
      6. Initializes the AMW-AI database (scripts\init_db.py).

    Usage (from the project root, any shell):
        powershell -ExecutionPolicy Bypass -File scripts\install_sqlserver.ps1

    Progress is written to scripts\sql\install.log so it can be monitored.
#>
param(
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'

# Resolve project paths (script lives in <root>\scripts).
$ScriptPath = $MyInvocation.MyCommand.Definition
$ScriptDir = Split-Path -Parent $ScriptPath
$Root      = Split-Path -Parent $ScriptDir
$SqlDir    = Join-Path $ScriptDir 'sql'
$LogFile   = Join-Path $SqlDir 'install.log'
$Media     = 'C:\Users\Administrator\SQLMedia'
$Boot      = Join-Path $Media 'SQL2022-SSEI-Dev.exe'
$Config    = Join-Path $SqlDir 'ConfigurationFile.ini'
$EnvFile   = Join-Path $Root '.env'
$Python    = Join-Path $Root 'venv\Scripts\python.exe'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---------------------------------------------------------------------------
# Phase 1: elevation. If not admin, relaunch elevated (one UAC prompt) & exit.
# ---------------------------------------------------------------------------
if (-not (Test-Admin)) {
    Write-Host 'Requesting Administrator elevation (approve the UAC prompt)...'
    if (Test-Path $LogFile) { Remove-Item $LogFile -Force }
    # Pass the script path as a single array element; Start-Process adds quoting
    # automatically when needed. Do NOT embed literal quotes here.
    Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @(
        '-ExecutionPolicy', 'Bypass', '-NoProfile', '-File', $ScriptPath, '-Elevated'
    )
    Write-Host "Elevated installer launched. Monitor progress in:`n  $LogFile"
    exit 0
}

# ---------------------------------------------------------------------------
# Phase 2: elevated work. Transcript to the log so the caller can tail it.
# A boot marker is written first so early failures are still visible.
# ---------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $SqlDir | Out-Null
"[$(Get-Date -Format s)] Elevated phase started. ScriptPath=$ScriptPath" |
    Out-File -FilePath $LogFile -Encoding utf8 -Append
try {
    Start-Transcript -Path $LogFile -Append | Out-Null
} catch {
    "[$(Get-Date -Format s)] Start-Transcript failed: $($_.Exception.Message)" |
        Out-File -FilePath $LogFile -Encoding utf8 -Append
}

try {
    Write-Host "=== SQL Server install started $(Get-Date -Format s) ==="

    # Read the sa password from .env (value is never echoed).
    if (-not (Test-Path $EnvFile)) { throw ".env not found at $EnvFile" }
    $pwdLine = Get-Content $EnvFile | Where-Object { $_ -match '^DB_PASSWORD=' } | Select-Object -First 1
    if (-not $pwdLine) { throw 'DB_PASSWORD not set in .env' }
    $SaPwd = $pwdLine -replace '^DB_PASSWORD=', ''
    if ([string]::IsNullOrWhiteSpace($SaPwd)) { throw 'DB_PASSWORD is empty in .env' }

    # --- Step 1: download full media (CAB) via the bootstrapper -------------
    New-Item -ItemType Directory -Force -Path $Media | Out-Null
    if (-not (Test-Path $Boot)) {
        Write-Host 'Downloading SSEI bootstrapper...'
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -UseBasicParsing -OutFile $Boot `
            -Uri 'https://download.microsoft.com/download/c/c/9/cc9c6797-383c-4b24-8920-dc057c1de9d3/SQL2022-SSEI-Dev.exe'
    }

    $extractor = Get-ChildItem $Media -Filter 'SQLServer2022*.exe' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -ne $Boot } | Select-Object -First 1
    if (-not $extractor) {
        Write-Host 'Downloading SQL Server media (CAB, ~1.5 GB); this can take several minutes...'
        Start-Process -FilePath $Boot -Wait -ArgumentList @(
            '/ACTION=Download', '/MEDIATYPE=CAB', "/MEDIAPATH=$Media", '/QUIET'
        )
        $extractor = Get-ChildItem $Media -Filter 'SQLServer2022*.exe' |
            Where-Object { $_.FullName -ne $Boot } | Select-Object -First 1
    }
    if (-not $extractor) { throw 'Media self-extractor (SQLServer2022*.exe) not found after download.' }
    Write-Host "Media extractor: $($extractor.Name)"

    # --- Step 2: extract the media -----------------------------------------
    $Extracted = Join-Path $Media 'setup'
    $Setup     = Join-Path $Extracted 'setup.exe'
    if (-not (Test-Path $Setup)) {
        Write-Host 'Extracting media...'
        Start-Process -FilePath $extractor.FullName -Wait -ArgumentList @('/qs', "/x:$Extracted")
    }
    if (-not (Test-Path $Setup)) { throw "setup.exe not found in $Extracted after extraction." }

    # --- Step 3: unattended install ----------------------------------------
    Write-Host 'Running unattended SQL Server setup (this can take 10-20 minutes)...'
    $proc = Start-Process -FilePath $Setup -Wait -PassThru -ArgumentList @(
        "/ConfigurationFile=$Config",
        "/SAPWD=$SaPwd",
        '/IACCEPTSQLSERVERLICENSETERMS'
    )
    Write-Host "Setup exit code: $($proc.ExitCode)"
    if ($proc.ExitCode -ne 0 -and $proc.ExitCode -ne 3010) {
        throw "SQL Server setup failed with exit code $($proc.ExitCode). See SQL Server Setup logs."
    }

    # --- Step 4: firewall for TCP 1433 -------------------------------------
    if (-not (Get-NetFirewallRule -DisplayName 'SQL Server (TCP 1433)' -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName 'SQL Server (TCP 1433)' -Direction Inbound `
            -Protocol TCP -LocalPort 1433 -Action Allow | Out-Null
        Write-Host 'Firewall rule for TCP 1433 created.'
    }

    # --- Step 5: ensure service running ------------------------------------
    Set-Service -Name 'MSSQLSERVER' -StartupType Automatic
    Start-Service -Name 'MSSQLSERVER' -ErrorAction SilentlyContinue
    (Get-Service 'MSSQLSERVER') | Format-Table Name, Status, StartType -AutoSize | Out-String | Write-Host

    # --- Step 6: initialize the AMW-AI database ----------------------------
    if (Test-Path $Python) {
        Write-Host 'Initializing AMW-AI database...'
        & $Python (Join-Path $ScriptDir 'init_db.py')
        Write-Host "init_db.py exit code: $LASTEXITCODE"
    } else {
        Write-Host "Python venv not found at $Python; skipping DB init. Run scripts\init_db.py manually."
    }

    Write-Host "=== SQL Server install finished $(Get-Date -Format s) ==="
}
catch {
    $msg = "ERROR: $($_.Exception.Message)"
    Write-Host $msg
    "[$(Get-Date -Format s)] $msg" | Out-File -FilePath $LogFile -Encoding utf8 -Append
}
finally {
    try { Stop-Transcript | Out-Null } catch { }
    "[$(Get-Date -Format s)] Elevated phase exited." |
        Out-File -FilePath $LogFile -Encoding utf8 -Append
}
