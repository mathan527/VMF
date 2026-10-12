# Builds sample_app/build/sample.apk without Gradle or Android Studio.
# Needs: Android SDK build-tools + a platform jar, a JDK, and python on PATH.
$ErrorActionPreference = "Stop"

$sdk   = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { "$env:LOCALAPPDATA\Android\Sdk" }
$bt    = "$sdk\build-tools\35.0.0"   # 34.0.0's d8 crashes on this app's inner class
$jar   = "$sdk\platforms\android-34\android.jar"
$jbr   = "C:\Program Files\Android\Android Studio\jbr\bin"
$javac = if (Test-Path "$jbr\javac.exe") { "$jbr\javac.exe" } else { (Get-Command javac).Source }
$keytool = if (Test-Path "$jbr\keytool.exe") { "$jbr\keytool.exe" } else { (Get-Command keytool).Source }
if (-not $env:JAVA_HOME -and (Test-Path "$jbr\javac.exe")) { $env:JAVA_HOME = Split-Path $jbr -Parent }  # d8/apksigner need it
$here  = $PSScriptRoot
$out   = "$here\build"

foreach ($p in @("$bt\aapt2.exe", "$bt\d8.bat", "$bt\zipalign.exe", "$bt\apksigner.bat", $jar)) {
    if (-not (Test-Path $p)) { throw "missing: $p" }
}

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory "$out\classes", "$out\dex" | Out-Null

# 1. manifest -> base apk (no resources needed)
& "$bt\aapt2.exe" link -o "$out\base.apk" -I $jar --manifest "$here\AndroidManifest.xml" `
    --min-sdk-version 24 --target-sdk-version 34 --version-code 1 --version-name 1.0
if ($LASTEXITCODE) { throw "aapt2 failed" }

# 2. java -> class files -> classes.dex
& $javac --release 8 -cp $jar -d "$out\classes" "$here\src\com\vmf\sample\MainActivity.java"
if ($LASTEXITCODE) { throw "javac failed" }
$classes = Get-ChildItem "$out\classes" -Recurse -Filter *.class | ForEach-Object FullName
& "$bt\d8.bat" --lib $jar --min-api 24 --output "$out\dex" @classes
if ($LASTEXITCODE) { throw "d8 failed" }

# 3. add classes.dex to the apk, align, sign with a throw-away debug key
python -c "import shutil,zipfile; shutil.copy(r'$out\base.apk', r'$out\unaligned.apk'); z=zipfile.ZipFile(r'$out\unaligned.apk','a'); z.write(r'$out\dex\classes.dex','classes.dex'); z.close()"
if ($LASTEXITCODE) { throw "zip failed" }
& "$bt\zipalign.exe" -f -p 4 "$out\unaligned.apk" "$out\aligned.apk"
if ($LASTEXITCODE) { throw "zipalign failed" }
$ks = "$here\debug.keystore"
if (-not (Test-Path $ks)) {
    & $keytool -genkeypair -keystore $ks -storepass android -keypass android -alias debug `
        -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=VMF Sample Debug"
    if ($LASTEXITCODE) { throw "keytool failed" }
}
& "$bt\apksigner.bat" sign --ks $ks --ks-pass pass:android --key-pass pass:android `
    --out "$out\sample.apk" "$out\aligned.apk"
if ($LASTEXITCODE) { throw "apksigner failed" }
& "$bt\apksigner.bat" verify "$out\sample.apk"
Write-Output "built $out\sample.apk"
