<#
.SYNOPSIS
    VMF — Setup 10-Minute Recurring Sample Job for Logged-Out / User-Switch Verification
    Module: M12.2 Test Helper
    Owner: Dhatri

.DESCRIPTION
    Creates a temporary scheduled task 'VMF_Sample_10Min' that runs every 10 minutes.
    Configured to run whether the user is logged on or not.
    This lets you lock the screen or switch user accounts for 30 minutes, then come back
    and check if 3 consecutive test cycles ran and captured screenshots in the background.
#>

[CmdletBinding()]
param(
    [string]$TaskName = "VMF_Sample_10Min",
    [string]$Password = ""
)

$ErrorActionPreference = "Stop"

Write-Host "`n=======================================================" -ForegroundColor Cyan
Write-Host "    VMF M12.2 - Setup 10-Minute Sample Test Job        " -ForegroundColor Cyan
Write-Host "=======================================================`n" -ForegroundColor Cyan

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path "$ScriptDir\..").Path
$TestScript = Join-Path $RepoRoot "scripts\sample_logged_out_test.py"
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source

if (-not $PythonExe) {
    Write-Error "Python executable not found. Make sure Python 3 is in your system PATH."
    exit 1
}

$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$cmdLine = "`"$PythonExe`" `"$TestScript`""

Write-Host "Target Script : $TestScript"
Write-Host "Current User  : $currentUser"
Write-Host "Frequency     : Every 10 minutes (Active for 24 hours)`n"

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
    # True logged-off execution requires stored password in Windows Task Scheduler
    Write-Host "[INFO] Registering task to run whether user is logged on or not (with password)..." -ForegroundColor Cyan
    try {
        $action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$TestScript`"" -WorkingDirectory $RepoRoot
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 1)
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Password -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries:$false -DontStopIfGoingOnBatteries:$true -StartWhenAvailable:$true -ExecutionTimeLimit (New-TimeSpan -Minutes 9)

        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -User $currentUser -Password $Password | Out-Null
        Write-Host "[PASS] Task '$TaskName' registered with stored credentials (runs even when signed out)!" -ForegroundColor Green
        $registered = $true
    } catch {
        Write-Host "[INFO] PowerShell registration failed, trying schtasks fallback..." -ForegroundColor Cyan
        try {
            $schCmd = "schtasks /create /tn `"$TaskName`" /tr `"$cmdLine`" /sc minute /mo 10 /ru `"$currentUser`" /rp `"$Password`" /rl HIGHEST /f"
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
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Days 1)
        $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries:$false -DontStopIfGoingOnBatteries:$true -StartWhenAvailable:$true -ExecutionTimeLimit (New-TimeSpan -Minutes 9)

        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
        Write-Host "[PASS] Task '$TaskName' registered successfully (runs when screen is locked or user is logged in)!" -ForegroundColor Green
    } catch {
        Write-Host "[INFO] Registering via schtasks fallback..." -ForegroundColor Cyan
        $schCmd = "schtasks /create /tn `"$TaskName`" /tr `"$cmdLine`" /sc minute /mo 10 /rl HIGHEST /f"
        Invoke-Expression $schCmd
        Write-Host "[PASS] Task registered via schtasks." -ForegroundColor Green
    }
}

Write-Host "`n-------------------------------------------------------" -ForegroundColor Yellow
Write-Host "How to verify over the next 30 minutes:" -ForegroundColor Yellow
Write-Host "1. LOCK YOUR SCREEN (Press Win + L) or switch users."
Write-Host "   (The task will run every 10 minutes in the background without display interruption)."
Write-Host "2. Leave the PC locked for 30 minutes (3 test cycles will execute)."
Write-Host "3. Log back in and inspect:"
Write-Host "   - Log file   : $RepoRoot\logs\sample_test_log.txt"
Write-Host "   - Screenshots: $RepoRoot\logs\sample_test_runs\`n"
Write-Host "Note: If you want to literally SIGN OUT / LOG OFF (destroying the Windows session):" -ForegroundColor Gray
Write-Host "      Windows Task Scheduler requires your password to run background tasks." -ForegroundColor Gray
Write-Host "      Run: powershell -File scripts\setup_sample_job.ps1 -Password 'your_windows_password'" -ForegroundColor Gray
Write-Host "      OR open taskschd.msc -> '$TaskName' -> Properties -> 'Run whether user is logged on or not'." -ForegroundColor Gray
Write-Host "-------------------------------------------------------`n" -ForegroundColor Yellow
