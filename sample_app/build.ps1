# Builds sample_app/build/sample.apk without Gradle or Android Studio.
# Needs: Android SDK build-tools + a platform jar, a JDK, and python on PATH.
#
#   .\build.ps1                       -> sample_app\build\sample.apk
#   .\build.ps1 -StartDelayMs 500     -> sample_app\build_slow\sample.apk, the same app with a
#                                        500 ms sleep in onCreate (a deliberately slowed build
#                                        for the M8 regression check; same package and key)
#   .\build.ps1 -AnrAfterMs 3000      -> sample_app\build_anr\sample.apk, 3 s after start the main
#                                        thread blocks for 40 s (a build that ANRs when tapped, for
#                                        the M6 ANR check)
#   .\build.ps1 -BrokenLayout         -> sample_app\build_broken\sample.apk: a button drawn under the
#                                        display cutout, two overlapping buttons and a tiny tap target
#                                        (for the M7 check)
param([int]$StartDelayMs = 0, [int]$AnrAfterMs = 0, [switch]$BrokenLayout)
$ErrorActionPreference = "Stop"

$sdk   = if ($env:ANDROID_HOME) { $env:ANDROID_HOME } else { "$env:LOCALAPPDATA\Android\Sdk" }
$bt    = "$sdk\build-tools\35.0.0"   # 34.0.0's d8 crashes on this app's inner class
$jar   = "$sdk\platforms\android-34\android.jar"
$jbr   = "C:\Program Files\Android\Android Studio\jbr\bin"
$javac = if (Test-Path "$jbr\javac.exe") { "$jbr\javac.exe" } else { (Get-Command javac).Source }
$keytool = if (Test-Path "$jbr\keytool.exe") { "$jbr\keytool.exe" } else { (Get-Command keytool).Source }
if (-not $env:JAVA_HOME -and (Test-Path "$jbr\javac.exe")) { $env:JAVA_HOME = Split-Path $jbr -Parent }  # d8/apksigner need it
$here  = $PSScriptRoot
$out   = if ($BrokenLayout) { "$here\build_broken" } elseif ($AnrAfterMs -gt 0) { "$here\build_anr" } elseif ($StartDelayMs -gt 0) { "$here\build_slow" } else { "$here\build" }

foreach ($p in @("$bt\aapt2.exe", "$bt\d8.bat", "$bt\zipalign.exe", "$bt\apksigner.bat", $jar)) {
    if (-not (Test-Path $p)) { throw "missing: $p" }
}

if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory "$out\classes", "$out\dex" | Out-Null

# the Java source: the original, or a copy with a sleep injected at the start of onCreate
$source = "$here\src\com\vmf\sample\MainActivity.java"
if ($StartDelayMs -gt 0 -or $AnrAfterMs -gt 0 -or $BrokenLayout) {
    $patchedDir = "$out\src\com\vmf\sample"
    New-Item -ItemType Directory $patchedDir -Force | Out-Null
    $marker = "super.onCreate(savedInstanceState);"
    $code = Get-Content $source -Raw
    if (-not $code.Contains($marker)) { throw "cannot find $marker in $source" }
    $delay = $marker
    if ($StartDelayMs -gt 0) { $delay += "`n        try { Thread.sleep($StartDelayMs); } catch (InterruptedException ignored) { }  // simulated slow start-up" }
    if ($AnrAfterMs -gt 0) { $delay += "`n        new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(new Runnable() { public void run() { try { Thread.sleep(40000); } catch (InterruptedException ignored) { } } }, $AnrAfterMs);  // simulated hang: ANR when tapped" }
    $code = $code.Replace($marker, $delay)
    if ($BrokenLayout) {
        $mk2 = "setContentView(root);"
        if (-not $code.Contains($mk2)) { throw "cannot find $mk2 in $source" }
        $broken = @"
android.widget.FrameLayout frame = new android.widget.FrameLayout(this);
        frame.addView(root);
        android.view.WindowManager.LayoutParams wlp = getWindow().getAttributes();
        wlp.layoutInDisplayCutoutMode = 3;  // ALWAYS: draw into the cutout area
        getWindow().setAttributes(wlp);
        getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_LAYOUT_NO_LIMITS);
        requestWindowFeature(android.view.Window.FEATURE_NO_TITLE);
        getWindow().getDecorView().setSystemUiVisibility(android.view.View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN | android.view.View.SYSTEM_UI_FLAG_LAYOUT_STABLE);
        Button notch = new Button(this); notch.setText("N"); notch.setContentDescription("notch_button");
        android.widget.FrameLayout.LayoutParams l1 = new android.widget.FrameLayout.LayoutParams(118, 100); l1.leftMargin = 492; l1.topMargin = 0; frame.addView(notch, l1);
        Button a = new Button(this); a.setText("A"); a.setContentDescription("overlap_a");
        android.widget.FrameLayout.LayoutParams l2 = new android.widget.FrameLayout.LayoutParams(400, 200); l2.leftMargin = 100; l2.topMargin = 1400; frame.addView(a, l2);
        Button b = new Button(this); b.setText("B"); b.setContentDescription("overlap_b");
        android.widget.FrameLayout.LayoutParams l3 = new android.widget.FrameLayout.LayoutParams(400, 200); l3.leftMargin = 250; l3.topMargin = 1450; frame.addView(b, l3);
        Button tiny = new Button(this); tiny.setText("."); tiny.setContentDescription("tiny_target");
        android.widget.FrameLayout.LayoutParams l4 = new android.widget.FrameLayout.LayoutParams(40, 40); l4.leftMargin = 800; l4.topMargin = 1000; frame.addView(tiny, l4);
        setContentView(frame);
"@
        $code = $code.Replace($mk2, $broken)
    }
    Set-Content "$patchedDir\MainActivity.java" $code
    $source = "$patchedDir\MainActivity.java"
}

# 1. manifest -> base apk (no resources needed)
& "$bt\aapt2.exe" link -o "$out\base.apk" -I $jar --manifest "$here\AndroidManifest.xml" `
    --min-sdk-version 24 --target-sdk-version 34 --version-code 1 --version-name 1.0
if ($LASTEXITCODE) { throw "aapt2 failed" }

# 2. java -> class files -> classes.dex
& $javac --release 8 -cp $jar -d "$out\classes" $source
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
