<#
.SYNOPSIS
    VMF — Remove 5-Minute Recurring Sample Job
    Module: M12.2 / M15.4 Acceptance Verification
    Owner: Dhatri (dhatri-devices)
#>

[CmdletBinding()]
param(
    [string]$TaskName = "VMF_Sample_5Min"
)

$ErrorActionPreference = "SilentlyContinue"

Write-Host "Unregistering task '$TaskName'..." -ForegroundColor Cyan

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[PASS] Task '$TaskName' successfully removed via PowerShell." -ForegroundColor Green
} catch {
    # Fallback to schtasks
    schtasks /delete /tn $TaskName /f | Out-Null
    Write-Host "[PASS] Task '$TaskName' removed via schtasks." -ForegroundColor Green
}
