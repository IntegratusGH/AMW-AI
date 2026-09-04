# Runs a PowerShell command elevated (High integrity) in the current session
# via a temporary Scheduled Task, waits for it to finish, and returns its
# exit code. Works for members of Administrators without an interactive UAC
# prompt. Intended for automated server administration on this machine.
#
#   powershell -ExecutionPolicy Bypass -File scripts\elevate_run.ps1 -Command "..."

param(
    [Parameter(Mandatory = $true)][string]$Command,
    [int]$TimeoutSeconds = 3600,
    [string]$TaskName = "AMW_ElevRun"
)

$ErrorActionPreference = "Stop"
$ps = (Get-Command powershell.exe).Source
$enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($Command))

$action = New-ScheduledTaskAction -Execute $ps `
    -Argument "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $enc"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -RunLevel Highest -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Seconds $TimeoutSeconds)
$task = New-ScheduledTask -Action $action -Principal $principal -Settings $settings

Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

$elapsed = 0
Start-Sleep -Seconds 2
do {
    Start-Sleep -Seconds 3
    $elapsed += 3
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    $state = (Get-ScheduledTask -TaskName $TaskName).State
} while ($state -eq "Running" -and $elapsed -lt $TimeoutSeconds)

$code = $info.LastTaskResult
Write-Output "TASK_STATE=$state"
Write-Output "TASK_EXITCODE=$code"
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
exit $code
