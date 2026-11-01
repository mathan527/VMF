<#
.SYNOPSIS
    VMF — Setup 5-Minute Recurring Sample Job for Unattended / Logged-Off Verification
    Module: M12.2 / M15.4 Acceptance Verification
    Owner: Dhatri (dhatri-devices)

.DESCRIPTION
    Creates a temporary scheduled task 'VMF_Sample_5Min' that executes every 5 minutes.
    Configured for 30 minutes of unattended testing (running ~6 cycles).
    Captures screenshots to logs/sample_5min_runs/ and updates docs/unattended_5min_verification.md.
#>

[CmdletBinding()]
param(
    [string]$TaskName = "VMF_Sample_5Min",
    [string]$Password = ""
)

$ErrorActionPreference = "Stop"

Write-Host "`n=======================================================" -ForegroundColor Cyan
Write-Host "    VMF M12.2/M15.4 - Setup 5-Minute Sample Test Job   " -ForegroundColor Cyan
Write-Host "=======================================================`n" -ForegroundColor Cyan

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path "$ScriptDir\..").Path
$TestScript = Join-Path $RepoRoot "scripts\sample_5min_logged_out_test.py"
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source

if (-not $PythonExe) {
    Write-Error "Python executable not found. Make sure Python 3 is in your system PATH."
    exit 1
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$cmdLine = "`"$PythonExe`" `"$TestScript`""

Write-Host "Target Script : $TestScript"
Write-Host "Current User  : $currentUser"
Write-Host "Frequency     : Every 5 minutes (Active for 24 hours)`n"

try {
    # Unregister if previously existed
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed existing task '$TaskName'." -ForegroundColor Gray
    }
} catch {
    # Ignore error if unregister fails
}

$registered = $false

if ($Password) {
    # Stored password enables execution even when completely signed out / logged off
    Write-Host "[INFO] Registering task to run whether user is logged on or not (with password)..." -ForegroundColor Cyan
    try {
        $action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$TestScript`"" -WorkingDirectory $RepoRoot
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 1)
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Password -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries:$false -DontStopIfGoingOnBatteries:$true -StartWhenAvailable:$true -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -User $currentUser -Password $Password | Out-Null
        Write-Host "[PASS] Task '$TaskName' registered with stored credentials (runs even when fully signed out)!" -ForegroundColor Green
        $registered = $true
    } catch {
        Write-Host "[INFO] PowerShell registration failed, trying schtasks fallback..." -ForegroundColor Cyan
        try {
            $schCmd = "schtasks /create /tn `"$TaskName`" /tr `"$cmdLine`" /sc minute /mo 5 /ru `"$currentUser`" /rp `"$Password`" /rl HIGHEST /f"
            Invoke-Expression $schCmd
            Write-Host "[PASS] Task registered via schtasks with stored credentials." -ForegroundColor Green
            $registered = $true
        } catch {
            Write-Warning "Could not register with password: $($_.Exception.Message)"
        }
    }
}

if (-not $registered) {
    try {
        $action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$TestScript`"" -WorkingDirectory $RepoRoot
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 1)
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries:$false -DontStopIfGoingOnBatteries:$true -StartWhenAvailable:$true -ExecutionTimeLimit (New-TimeSpan -Minutes 4)

        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        Write-Host "[PASS] Task '$TaskName' registered successfully!" -ForegroundColor Green
    } catch {
        Write-Host "[INFO] Registering via schtasks fallback..." -ForegroundColor Cyan
        $schCmd = "schtasks /create /tn `"$TaskName`" /tr `"$cmdLine`" /sc minute /mo 5 /rl HIGHEST /f"
        Invoke-Expression $schCmd
        Write-Host "[PASS] Task registered via schtasks." -ForegroundColor Green
    }
}

Write-Host "`n-------------------------------------------------------" -ForegroundColor Yellow
Write-Host "Unattended 30-Minute Verification Plan:" -ForegroundColor Yellow
Write-Host "1. LOCK YOUR SCREEN (Press Win + L) or Sign Out."
Write-Host "   (The task will execute every 5 minutes in the background)."
Write-Host "2. Leave the PC unattended for ~30 minutes (6 test cycles)."
Write-Host "3. Log back in and inspect:"
Write-Host "   - Report Table: $RepoRoot\docs\unattended_5min_verification.md"
Write-Host "   - Screenshots : $RepoRoot\logs\sample_5min_runs\"
Write-Host "   - Log File    : $RepoRoot\logs\sample_5min_test_log.txt"
Write-Host "4. To stop the job after 30 minutes, run:"
Write-Host "   powershell -File scripts\remove_5min_job.ps1"
Write-Host "-------------------------------------------------------`n" -ForegroundColor Yellow
