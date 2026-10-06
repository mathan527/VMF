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
