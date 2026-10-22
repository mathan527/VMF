<#
.SYNOPSIS
    VMF - Remove Sample 10-Minute Job & Clean Temporary Test Files
    Module: M12.2 Cleanup
    Owner: Dhatri
#>

[CmdletBinding()]
param(
    [string]$TaskName = "VMF_Sample_10Min",
    [switch]$KeepLogs
)

$ErrorActionPreference = "SilentlyContinue"

Write-Host "`n=======================================================" -ForegroundColor Cyan
Write-Host "       VMF - Clean Up Temporary Sample Job Files       " -ForegroundColor Cyan
Write-Host "=======================================================`n" -ForegroundColor Cyan

# 1. Unregister Scheduled Task
try {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "[PASS] Unregistered scheduled task '$TaskName'." -ForegroundColor Green
    } else {
        schtasks /delete /tn $TaskName /f | Out-Null
        Write-Host "[INFO] Scheduled task '$TaskName' removed." -ForegroundColor Green
    }
} catch {
    Write-Warning "Task was not found or already removed."
}

# 2. Clean temporary sample files if requested
if (-not $KeepLogs) {
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    $RepoRoot = (Resolve-Path "$ScriptDir\..").Path
    $SampleLog = Join-Path $RepoRoot "logs\sample_test_log.txt"
    $SampleDir = Join-Path $RepoRoot "logs\sample_test_runs"

    if (Test-Path $SampleLog) {
        Remove-Item $SampleLog -Force
        Write-Host "[PASS] Removed temporary log file: $SampleLog" -ForegroundColor Green
    }
    if (Test-Path $SampleDir) {
        Remove-Item $SampleDir -Recurse -Force
        Write-Host "[PASS] Removed temporary test screenshots folder: $SampleDir" -ForegroundColor Green
    }
} else {
    Write-Host "[INFO] Preserved sample test logs and screenshots." -ForegroundColor Gray
}

Write-Host "`nCleanup complete! Your system is clean.`n" -ForegroundColor Cyan
