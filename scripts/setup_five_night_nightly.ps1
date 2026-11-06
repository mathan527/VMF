[CmdletBinding()]
param(
    [string]$TaskName = "VMFNightly",
    [int]$RunHour = 1
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdmin) {
    throw "Run this script from PowerShell opened with 'Run as administrator'."
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$nightlyScript = Join-Path $repoRoot "scripts\nightly.py"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop

if ($task.Principal.LogonType -ne "Password") {
    throw "Task '$TaskName' is not configured with stored-password logon; refusing to change its account."
}
if (-not (Test-Path $nightlyScript)) {
    throw "Nightly runner not found: $nightlyScript"
}

$pythonExe = $task.Actions[0].Execute
if (-not (Test-Path $pythonExe)) {
    throw "Python executable not found: $pythonExe"
}

$firstRun = (Get-Date).Date.AddDays(1).AddHours($RunHour)
$triggers = @(0..4 | ForEach-Object {
    New-ScheduledTaskTrigger -Once -At ($firstRun.AddDays($_))
})
$action = New-ScheduledTaskAction `
    -Execute $pythonExe `
    -Argument ('"{0}" --direct --skip-cleanup' -f $nightlyScript) `
    -WorkingDirectory $repoRoot
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries:$false `
    -DontStopIfGoingOnBatteries:$true `
    -StartWhenAvailable:$true `
    -WakeToRun:$true `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -MultipleInstances IgnoreNew

$accountName = $task.Principal.UserId
if ($accountName -notmatch "\\") {
    $accountName = "$env:USERDOMAIN\$accountName"
}
$securePassword = Read-Host "Enter the current Windows password for $accountName" -AsSecureString
$passwordPointer = [IntPtr]::Zero
try {
    $passwordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($passwordPointer)
    Set-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -User $accountName `
        -Password $plainPassword | Out-Null
} finally {
    if ($passwordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($passwordPointer)
    }
    $plainPassword = $null
    $securePassword.Dispose()
}

function Invoke-PowerCfg {
    param([string[]]$PowerArgs)

    & powercfg @PowerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "powercfg $($PowerArgs -join ' ') failed with exit code $LASTEXITCODE."
    }
}

Invoke-PowerCfg @("/setacvalueindex", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE", "1")
Invoke-PowerCfg @("/setdcvalueindex", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE", "1")
Invoke-PowerCfg @("/setactive", "SCHEME_CURRENT")

$updated = Get-ScheduledTask -TaskName $TaskName
if (-not $updated.Settings.WakeToRun -or -not $updated.Settings.StartWhenAvailable) {
    throw "Task '$TaskName' was updated but wake/catch-up settings did not persist."
}
if ($updated.Triggers.Count -ne 5) {
    throw "Task '$TaskName' was updated but does not have exactly five triggers."
}

Write-Host "Task '$TaskName' is configured for five runs:"
0..4 | ForEach-Object {
    Write-Host "  $($firstRun.AddDays($_).ToString('yyyy-MM-dd HH:mm'))"
}
Write-Host "WakeToRun and StartWhenAvailable: enabled"
Write-Host "Power sleep timeout was not changed; manually sleeping the PC is supported by the wake timer."
Write-Host "After each run, the PC stays awake until you sleep or shut it down."
Write-Host "Keep the PC plugged in and do not close its lid if that forces hibernation."
Write-Host "Logs: $repoRoot\logs\nightly_*.log"
