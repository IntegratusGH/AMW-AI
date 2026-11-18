$ErrorActionPreference = "Stop"
$msi = "C:\Users\ThayabharenB\Dev\AMW-AI-1\dist\AMW-AI-Setup-1.88.0.msi"
$log = "C:\ProgramData\AMW-AI\logs\install-1.88.0-clean.log"
$p = Start-Process msiexec.exe -ArgumentList @("/i","`"$msi`"","/qn","/norestart","/l*v","`"$log`"") -Wait -PassThru
Write-Host "msiexec exit code: $($p.ExitCode)"
exit $p.ExitCode
