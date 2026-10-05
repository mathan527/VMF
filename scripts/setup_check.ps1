<#
.SYNOPSIS
    VMF (Virtual Mobile Farm) - Environment Setup Verification Script
    Module: M0 - Environment Setup & Verification
    Owner: Dhatri
#>

$script:TotalChecks = 0
$script:PassedChecks = 0
$script:FailedItems = @()

function Write-Check {
    param(
        [string]$Name,
        [bool]$Condition,
        [string]$Detail = ""
    )
    $script:TotalChecks++
    if ($Condition) {
        $script:PassedChecks++
        Write-Host ("[PASS] {0}" -f $Name) -ForegroundColor Green
    } else {
        $script:FailedItems += $Name
        $msg = "[FAIL] {0}" -f $Name
        if ($Detail) { $msg += "  -> $Detail" }
        Write-Host $msg -ForegroundColor Red
    }
}

function Test-CommandExists {
    param([string]$Command)
    return [bool](Get-Command $Command -ErrorAction SilentlyContinue)
}

Write-Host "`n===== VMF Environment Check =====`n" -ForegroundColor Cyan

Write-Host "-- Virtualization / WHPX --" -ForegroundColor Yellow

try {
    $whpxCheck = Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -ErrorAction Stop
    $vmFirmwareEnabled = ($whpxCheck.State -eq "Enabled")
    Write-Check "Virtualization enabled in firmware (BIOS/UEFI)" $vmFirmwareEnabled "Inferred from WHPX state: $($whpxCheck.State)"
} catch {
    Write-Check "Virtualization enabled in firmware (BIOS/UEFI)" $false "Could not verify - run as Administrator"
}

try {
    $whpxFeature = Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -ErrorAction Stop
    Write-Check "Windows Hypervisor Platform (WHPX) feature enabled" ($whpxFeature.State -eq "Enabled") "State: $($whpxFeature.State)"
} catch {
    Write-Check "Windows Hypervisor Platform (WHPX) feature enabled" $false "Could not query feature: $($_.Exception.Message)"
}

Write-Host "`n-- Android SDK --" -ForegroundColor Yellow

$AndroidSdkRoot = $env:ANDROID_SDK_ROOT
if (-not $AndroidSdkRoot) { $AndroidSdkRoot = $env:ANDROID_HOME }

Write-Check "ANDROID_SDK_ROOT (or ANDROID_HOME) is set" ([bool]$AndroidSdkRoot) "Set one of these env vars to your SDK path"

if ($AndroidSdkRoot) {
    $adbPath = Join-Path $AndroidSdkRoot "platform-tools\adb.exe"
    Write-Check "platform-tools present (adb.exe)" (Test-Path $adbPath) $adbPath

    $emulatorPath = Join-Path $AndroidSdkRoot "emulator\emulator.exe"
    Write-Check "emulator.exe present" (Test-Path $emulatorPath) $emulatorPath

    $sdkManagerPath = Join-Path $AndroidSdkRoot "cmdline-tools\latest\bin\sdkmanager.bat"
    $installedPackages = ""
    if (Test-Path $sdkManagerPath) {
        try {
            $installedPackages = & $sdkManagerPath --list_installed 2>$null | Out-String
        } catch {
            $installedPackages = ""
        }
    }
    Write-Check "sdkmanager available" (Test-Path $sdkManagerPath) $sdkManagerPath

    $requiredImages = @(
        "system-images;android-29;google_apis;x86_64",
        "system-images;android-31;google_apis;x86_64",
        "system-images;android-35;google_apis;x86_64",
        "system-images;android-29;default;x86_64",
        "system-images;android-31;default;x86_64",
        "system-images;android-35;default;x86_64"
    )

    foreach ($image in $requiredImages) {
        $found = $installedPackages -match [regex]::Escape($image)
        Write-Check "System image: $image" $found "Install with: sdkmanager `"$image`""
    }
} else {
    Write-Check "platform-tools present (adb.exe)" $false "SDK root not set, cannot check"
    Write-Check "emulator.exe present" $false "SDK root not set, cannot check"
    Write-Check "sdkmanager available" $false "SDK root not set, cannot check"
}

Write-Host "`n-- Appium / Node / Python --" -ForegroundColor Yellow

Write-Check "Node.js installed" (Test-CommandExists "node") "Install Node.js LTS"
Write-Check "npm installed" (Test-CommandExists "npm")
Write-Check "Appium CLI installed" (Test-CommandExists "appium") "npm install -g appium"

if (Test-CommandExists "appium") {
    try {
        $driverList = (& appium driver list --installed 2>&1 | Out-String)
        $driverFound = $driverList.ToLower().Contains("uiautomator2")
        Write-Check "UiAutomator2 driver installed" $driverFound "Install with: appium driver install uiautomator2"
    } catch {
        Write-Check "UiAutomator2 driver installed" $false "Could not run appium driver list"
    }
} else {
    Write-Check "UiAutomator2 driver installed" $false "Appium CLI not found"
}

Write-Check "Python 3 installed" (Test-CommandExists "python") "Install Python 3.x"

if (Test-CommandExists "python") {
    try {
        $pyCheck = python -c "import yaml, appium, psutil, jinja2; print('OK')" 2>&1
        $pyPackagesOk = $pyCheck -match "OK"
        Write-Check "Required Python packages" $pyPackagesOk "$pyCheck"
    } catch {
        Write-Check "Required Python packages" $false "Import check failed to run"
    }
} else {
    Write-Check "Required Python packages" $false "Python not found"
}

Write-Host "`n===== Summary =====" -ForegroundColor Cyan
Write-Host ("{0} / {1} checks passed" -f $script:PassedChecks, $script:TotalChecks)

if ($script:FailedItems.Count -gt 0) {
    Write-Host "`nFailed items:" -ForegroundColor Red
    foreach ($item in $script:FailedItems) {
        Write-Host "  - $item" -ForegroundColor Red
    }
    exit 1
} else {
    Write-Host "`nAll checks passed. Machine is ready." -ForegroundColor Green
    exit 0
}