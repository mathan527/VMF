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


## try_fallback.py and eval_fallback.py (M14)

```powershell
python scripts\try_fallback.py --hierarchy results\12\emulator-5554\step_03_hierarchy.xml --package com.example.testapp --intent "tap the Login button" --locator "id=com.example.testapp:id/btn_login"
python scripts\eval_fallback.py [--model qwen2.5:1.5b] [--only renamed_id]
```

| Script | Purpose |
|---|---|
| `try_fallback.py` | Shows the candidates, the resolution and the reason for one saved hierarchy, using a REAL local Ollama. |
| `eval_fallback.py` | Runs the labelled cases in `tests/fixtures/locator_drift/` with and without the heuristic stage and prints hit rate, wrong-element rate, no-match rate and median latency. |

Both need Ollama running on localhost with the model pulled, and print a clear error otherwise. No accuracy or latency numbers are recorded in the repo: run `eval_fallback.py` on your machine.
