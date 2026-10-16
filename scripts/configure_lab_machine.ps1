<#
.SYNOPSIS
    VMF (Virtual Mobile Farm) - Lab Machine Configuration & Hardening
    Module: M12 — Scheduling, Hardening & Cleanup (Steps M12.1, M12.3)
    Owner: Dhatri

.DESCRIPTION
    Configures a host Windows PC for unattended overnight matrix execution:
    1. Disables standby/sleep and hibernation (powercfg).
    2. Configures Windows Update active hours (00:00–07:00).
    3. Adds Windows Defender antivirus exclusions for Android SDK, AVD folder, and workspace.
    4. Registers the VMFNightly job in Windows Task Scheduler to run daily at 01:00.
    
    Must be run as Administrator in PowerShell.
#>

[CmdletBinding()]
param(
    [string]$Time = "01:00",
    [string]$TaskName = "VMFNightly",
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

Write-Host "`n=======================================================" -ForegroundColor Cyan
Write-Host "   VMF M12 - Lab Machine Hardening & Scheduler Setup   " -ForegroundColor Cyan
Write-Host "=======================================================`n" -ForegroundColor Cyan

# 0. Check Administrator Privileges
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Warning "This script requires Administrator privileges to set power settings, Defender exclusions, and scheduled tasks."
    Write-Host "Please restart PowerShell as Administrator and re-run this script." -ForegroundColor Red
    exit 1
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = (Resolve-Path "$ScriptDir\..").Path
$NightlyPy = Join-Path $RepoRoot "scripts\nightly.py"
$PythonExe = (Get-Command python -ErrorAction SilentlyContinue).Source

if (-not $PythonExe) {
    Write-Error "Python executable not found in PATH. Ensure Python 3 is installed and in system PATH."
    exit 1
}

# 1. Machine Hardening: Power Settings (M12.3)
Write-Host "[1/4] Applying Power & Sleep Hardening..." -ForegroundColor Yellow
try {
    # Standby timeout AC: 0 (Never sleep)
    powercfg /change standby-timeout-ac 0
    # Monitor timeout AC: 15 minutes (monitor can turn off, PC stays awake)
    powercfg /change monitor-timeout-ac 15
    # Hibernate timeout AC: 0 (Never hibernate)
    powercfg /change hibernate-timeout-ac 0
    Write-Host "  [PASS] Sleep and hibernation disabled on AC power." -ForegroundColor Green
} catch {
    Write-Warning "  [WARN] Failed to set powercfg: $($_.Exception.Message)"
}

# 2. Windows Update Active Hours (M12.3)
Write-Host "`n[2/4] Configuring Windows Update Active Hours (00:00 - 07:00)..." -ForegroundColor Yellow
try {
    $uxPath = "HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings"
    if (-not (Test-Path $uxPath)) {
        New-Item -Path $uxPath -Force | Out-Null
    }
    Set-ItemProperty -Path $uxPath -Name "ActiveHoursStart" -Value 0 -Type DWord -Force
    Set-ItemProperty -Path $uxPath -Name "ActiveHoursEnd" -Value 7 -Type DWord -Force
    Set-ItemProperty -Path $uxPath -Name "IsActiveHoursEnabled" -Value 1 -Type DWord -Force
    Write-Host "  [PASS] Windows Update active hours configured covering 00:00 to 07:00." -ForegroundColor Green
} catch {
    Write-Warning "  [WARN] Could not update active hours registry: $($_.Exception.Message)"
}

# 3. Windows Defender Exclusions (M12.3)
Write-Host "`n[3/4] Adding Windows Defender Antivirus Exclusions..." -ForegroundColor Yellow
$sdkPath = $env:ANDROID_SDK_ROOT
if (-not $sdkPath) { $sdkPath = $env:ANDROID_HOME }
if (-not $sdkPath) { $sdkPath = "D:\Android\Sdk" }

$avdPath = Join-Path $env:USERPROFILE ".android"
$pathsToExclude = @($RepoRoot, $sdkPath, $avdPath)

foreach ($path in $pathsToExclude) {
    if (Test-Path $path) {
        try {
            Add-MpPreference -ExclusionPath $path -ErrorAction Stop
            Write-Host "  [PASS] Defender exclusion added: $path" -ForegroundColor Green
        } catch {
            Write-Warning "  [WARN] Failed to add Defender exclusion for $path : $($_.Exception.Message)"
        }
    } else {
        Write-Host "  [SKIP] Path does not exist yet: $path" -ForegroundColor Gray
    }
}

# 4. Windows Task Scheduler Registration (M12.1)
Write-Host "`n[4/4] Registering Daily Scheduled Task '$TaskName' at $Time..." -ForegroundColor Yellow
$actionCmd = "`"$PythonExe`" `"$NightlyPy`""
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

try {
    # Check if task already exists
    $existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  [INFO] Unregistered previous task '$TaskName'." -ForegroundColor Cyan
    }

    $action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$NightlyPy`"" -WorkingDirectory $RepoRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At $Time
    $principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType InteractiveOrPassword -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries:$false -DontStopIfGoingOnBatteries:$true -StartWhenAvailable:$true -ExecutionTimeLimit (New-TimeSpan -Hours 6)

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
    Write-Host "  [PASS] Scheduled task '$TaskName' registered successfully!" -ForegroundColor Green
    Write-Host "         Command    : $actionCmd"
    Write-Host "         Schedule   : Daily at $Time"
    Write-Host "         Run Level  : Highest (Administrator)"
} catch {
    # Fallback to schtasks command if PowerShell cmdlets fail
    Write-Host "  [INFO] Trying fallback via schtasks.exe..." -ForegroundColor Cyan
    try {
        $schCmd = "schtasks /create /tn `"$TaskName`" /tr `"$actionCmd`" /sc daily /st $Time /rl HIGHEST /f"
        Invoke-Expression $schCmd
        Write-Host "  [PASS] Task registered via schtasks.exe." -ForegroundColor Green
    } catch {
        Write-Error "  [FAIL] Could not register scheduled task: $($_.Exception.Message)"
    }
}

Write-Host "`n=======================================================" -ForegroundColor Cyan
Write-Host "             Machine Hardening Complete!               " -ForegroundColor Green
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "Next Steps in Lab:"
Write-Host " 1. Test logged-off execution (M12.2):"
Write-Host "    Start-ScheduledTask -TaskName `"$TaskName`""
Write-Host "    Then lock/log-off to confirm emulators boot cleanly."
Write-Host " 2. Monitor execution log in: $RepoRoot\logs\`n"

if ($RunNow) {
    Write-Host "Starting '$TaskName' now..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName $TaskName
}
