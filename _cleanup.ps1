$ErrorActionPreference = "Stop"
$old  = "{9B3DC72F-8A6D-4C70-B7BF-B785DC010E86}"   # stale 1.86.0
$new  = "{EF378224-6335-460A-9467-3E78B21200D2}"   # good 1.88.0
$log1 = "C:\ProgramData\AMW-AI\logs\uninstall-1.86.0.log"
$log2 = "C:\ProgramData\AMW-AI\logs\repair-1.88.0.log"

Write-Host "== Uninstalling stale 1.86.0 =="
$p = Start-Process msiexec.exe -ArgumentList @("/x",$old,"/qn","/norestart","/l*v","`"$log1`"") -Wait -PassThru
Write-Host "uninstall exit: $($p.ExitCode)"

Write-Host "== Repairing 1.88.0 (restore files + service) =="
$p = Start-Process msiexec.exe -ArgumentList @("/fa",$new,"/qn","/norestart","/l*v","`"$log2`"") -Wait -PassThru
Write-Host "repair exit: $($p.ExitCode)"
