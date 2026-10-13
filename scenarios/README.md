# scenarios/

YAML scenario files executed by the M4 runner (`vmf.runner.run`).

| File | Package | Notes |
|---|---|---|
| `login_flow.yaml` | `com.example.testapp` | Example login flow. Its header says "schema v1, confirm with Dhatri", so the schema is not yet confirmed. |

## Format (as read by `vmf/runner/runner.py`)

```yaml
name: "Login"                 # scenario name stored with the run
package: "com.example.testapp"
steps:
  - action: tap               # tap | type | swipe | wait_for | assert_visible | back | home | oem.<behaviour>
    locator: {resource_id: "..."}   # or {text: "..."} or {content-desc: "..."}
    text: "..."               # type only
    direction: "up"           # swipe only: up | down | left | right
    timeout_s: 10             # optional; default is DEFAULT_TIMEOUT_S in vmf/config.py
```

- `oem.<behaviour>` calls the function of that name in `vmf.oem_packs`. An unknown name is recorded as `skipped`.
- Steps run in order. A failing step is recorded as `fail` and the run continues.
- Locators must match elements the app exposes to UiAutomator2 (resource-id, text or content-desc).


## Optional: locator fallback (M14, off by default)

If the app's UI changes between builds, a renamed id makes a `tap` or `type` step fail with "could not find the element".
A step can opt in to being healed by a small local model (Ollama on localhost, $0):

```yaml
  - action: tap
    locator: {resource_id: "com.example.testapp:id/btn_login"}
    fallback: true                      # opt in; steps without it are never healed
    intent: "tap the Login button"      # required with fallback: true (a human sentence)
```

- Only action steps are healed (`tap`, `type`). `wait_for` and `assert_visible` are NEVER healed: a missing element the app should show may be the defect.
- It runs only when enabled for the run (`options["locator_fallback"]` or `VMF_LOCATOR_FALLBACK=1`) and never in the unrecorded performance flow.
- A healed step still has status `pass`; it is listed in `results/<run_id>/<profile>/locator_fallbacks.json` and logged as a WARNING, and the orchestrator summary has `locator_fallbacks: {profile: count}`.
- `fallback: true` without an `intent` is rejected when the scenario is loaded with the fallback enabled, naming the step.
- Setup: install Ollama, `ollama pull qwen2.5:1.5b` (or set `VMF_OLLAMA_MODEL`; keep it at 3B parameters or fewer). Try it with `scripts/try_fallback.py`, measure it with `scripts/eval_fallback.py`.
