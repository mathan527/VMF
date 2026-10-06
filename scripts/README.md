# scripts/

| Script | Purpose |
|---|---|
| `run_m4_once.py` | Runs one scenario once on one already-booted device through `vmf.runner.run` and prints the pass/fail/skipped counts. |

## run_m4_once.py

```powershell
python scripts/run_m4_once.py --serial emulator-5554 --apk path\to\app.apk --scenario scenarios\login_flow.yaml --port 8200
```

| Argument | Meaning |
|---|---|
| `--serial` | ADB serial of the device. |
| `--apk` | APK to install and test. |
| `--scenario` | Scenario YAML path. |
| `--port` | UiAutomator2 `systemPort` for this device (not the Appium server port). |

Needs a running Appium server at `APPIUM_URL` (`vmf/config.py`, `http://localhost:4723`) and a booted emulator. Evidence is written under `results/<run_id>/<serial>/`.
