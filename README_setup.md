# VMF — Environment Setup (README)

Module: M0 — Environment Setup & Verification
Owner: Dhatri

This document takes a fresh Windows PC to a state where `setup_check.ps1`
reports 18/18 PASS. Follow the steps in order. Every command is copy-paste
ready for PowerShell.

Estimated time: 30–45 minutes, plus one restart.

---

## 0. Before you start

- You need **Administrator rights** on this machine for two steps (enabling
  WHPX, and verifying it afterwards).
- Pick a project folder with **plenty of free space on a drive that isn't
  C:** if possible — Android system images and later, 24 AVDs, will use
  significant disk space. These instructions use `D:\vmf` as the project
  folder and `D:\Android\Sdk` as the SDK location; substitute your own
  drive letter if different, but avoid spaces in the path if you can — a
  few Android tools (`avdmanager`, some Appium/ADB commands) don't always
  quote paths correctly, and a space in the path can cause hard-to-debug
  failures later. If your path does contain a space, always wrap it in
  quotes when running `cd` or referencing it directly, e.g. `cd "D:\My Folder\vmf"`.

---

## 1. Enable Windows Hypervisor Platform (WHPX)

This is a Windows feature the Android emulator needs to run with hardware
acceleration.

1. Click **Start**, type `PowerShell`.
2. Right-click **Windows PowerShell** → **Run as administrator** → **Yes** on
   the UAC prompt.
3. Run:

   ```powershell
   Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All
   ```

4. When prompted, **restart your PC**.

### Verify (after restart, any normal PowerShell window)

```powershell
Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform
```

Confirm the output shows:

```
State : Enabled
```

---

## 2. Create your project and SDK folders

Open a normal PowerShell window:

```powershell
New-Item -ItemType Directory -Path "D:\vmf" -Force
New-Item -ItemType Directory -Path "D:\Android\Sdk" -Force
```

---

## 3. Download and install Android cmdline-tools

```powershell
Invoke-WebRequest -Uri "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip" -OutFile "D:\Android\cmdline-tools.zip"
Expand-Archive -Path "D:\Android\cmdline-tools.zip" -DestinationPath "D:\Android\Sdk\cmdline-tools-temp"
New-Item -ItemType Directory -Path "D:\Android\Sdk\cmdline-tools\latest" -Force
Move-Item "D:\Android\Sdk\cmdline-tools-temp\cmdline-tools\*" "D:\Android\Sdk\cmdline-tools\latest"
Remove-Item "D:\Android\Sdk\cmdline-tools-temp" -Recurse -Force
Remove-Item "D:\Android\cmdline-tools.zip"
```

---

## 4. Set environment variables

```powershell
[System.Environment]::SetEnvironmentVariable("ANDROID_SDK_ROOT", "D:\Android\Sdk", "User")
[System.Environment]::SetEnvironmentVariable("Path", "D:\Android\Sdk\cmdline-tools\latest\bin;D:\Android\Sdk\platform-tools;D:\Android\Sdk\emulator;" + $env:Path, "User")
```

**Close this PowerShell window completely and open a new one** — environment
variable changes only take effect in a fresh window.

### Verify

```powershell
echo $env:ANDROID_SDK_ROOT
adb --version
emulator -version
```

All three should resolve without error and reference your SDK path.

---

## 5. Install platform-tools, emulator, and required system images

```powershell
sdkmanager "platform-tools"
sdkmanager "emulator"
sdkmanager "system-images;android-29;google_apis;x86_64"
sdkmanager "system-images;android-31;google_apis;x86_64"
sdkmanager "system-images;android-35;google_apis;x86_64"
sdkmanager "system-images;android-29;default;x86_64"
sdkmanager "system-images;android-31;default;x86_64"
sdkmanager "system-images;android-35;default;x86_64"
```

If prompted to accept licenses, type `y`, or run `sdkmanager --licenses`
first and accept everything.

> The `default` (AOSP) images are used for Huawei device profiles, which
> ship without Google Play Services. The `google_apis` images cover
> everything else.

---

## 6. Install Node.js

Download the LTS installer from https://nodejs.org and run it with default
settings.

### Verify

```powershell
node -v
npm -v
```

---

## 7. Install Appium and the UiAutomator2 driver

```powershell
npm install -g appium
appium driver install uiautomator2
```

### Verify

```powershell
appium driver list --installed
```

Should list `uiautomator2` as installed.

---

## 8. Install Python

Download from https://python.org (any current Python 3.x release). During
installation, **check the box "Add python.exe to PATH"** — this is easy to
miss and causes every later step to fail silently.

### Verify

```powershell
python --version
```

---

## 9. Install required Python packages

```powershell
pip install pyyaml Appium-Python-Client psutil jinja2
```

---

## 10. Run the verification script

1. Copy `setup_check.ps1` into your project folder, e.g. `D:\vmf\`.
2. Open PowerShell **as Administrator** (needed for the script to correctly
   read the WHPX/virtualization status — running it in a normal window will
   report false failures on those two checks even if everything is fine).
3. Allow the script to run (one-time, per machine):

   ```powershell
   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
   Unblock-File -Path "D:\vmf\setup_check.ps1"
   ```

4. Run it:

   ```powershell
   cd "D:\vmf"
   .\setup_check.ps1
   ```

You should see **18/18 checks passed** and "All checks passed. Machine is
ready."

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `requires elevation` error on any WHPX/virtualization command | Not running PowerShell as Administrator | Right-click PowerShell → Run as administrator |
| Script won't run: "not digitally signed" | Default execution policy blocks scripts | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force` |
| Script still won't run after the above | File is marked as downloaded/untrusted by Windows | `Unblock-File -Path "<path to script>"` |
| `[FAIL] Virtualization enabled in firmware` even though everything else passes | Some Windows builds don't reliably report this specific field via WMI/systeminfo | If WHPX itself shows Enabled, firmware virtualization is confirmed on — this is a known false negative on some systems, not a real problem |
| `[FAIL] UiAutomator2 driver installed` even though `appium driver list --installed` shows it | Older script version had a fragile string match | Make sure you're on the current `setup_check.ps1` |
| Environment variable changes don't seem to apply | Old PowerShell window still has the old session's variables cached | Close the window completely (not just minimize) and open a new one |
| Paths with spaces (e.g. folder names with spaces) cause odd errors | Some Android/Appium tools don't quote paths internally | Avoid spaces in your SDK/project path where possible; if unavoidable, always quote paths yourself in commands (`cd "D:\My Folder\vmf"`) |

---

## Done-when checklist

- [ ] WHPX enabled and confirmed
- [ ] Android SDK installed with all 6 required system images
- [ ] Node, Appium, and the UiAutomator2 driver installed
- [ ] Python 3 with pyyaml, Appium-Python-Client, psutil, jinja2 installed
- [ ] `setup_check.ps1` reports 18/18 PASS

Once all boxes are checked, M0 is complete for this machine.
