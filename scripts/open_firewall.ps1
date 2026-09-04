# Opens the Windows Firewall for the AMW-AI web application so other
# computers on the network can reach it. Run in an ELEVATED PowerShell.
#
#   powershell -ExecutionPolicy Bypass -File scripts\open_firewall.ps1

param(
    [int]$Port = 8080
)

$ruleName = "AMW-AI Web ($Port)"

if (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue) {
    Write-Host "Firewall rule '$ruleName' already exists."
} else {
    New-NetFirewallRule -DisplayName $ruleName `
        -Direction Inbound -Action Allow `
        -Protocol TCP -LocalPort $Port -Profile Any | Out-Null
    Write-Host "Created inbound firewall rule '$ruleName' for TCP port $Port."
}
