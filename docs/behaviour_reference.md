# VMF OEM Behaviour Reference Sheet

**Module**: M5 — Profile Settings and OEM Behaviour Packs  
**Owner**: Dhatri  
**Specification**: Virtual Mobile Farm — Final Execution Plan §5.2, §5.3, §12 (M5.1–M5.12)  
**Traceability**: Answers Drawbacks D2 (OEM behaviour attribution) and D5 (device environment fidelity)

---

## 1. Overview

Emulators cannot install vendor-proprietary Android skins (such as Samsung One UI or Xiaomi HyperOS/MIUI). To close the OEM fidelity gap ($0 cost, virtual-first), the Virtual Mobile Farm reproduces the specific OS parameters (Layer B) and runtime behaviours (Layer C) that cause real "works on one brand, fails on another" defects.

---

## 2. Layer B: Profile Settings Reference

These display and system configuration options are applied via ADB after device boot per reference model specification.

| Setting | Command / Mechanism | What It Simulates | Affected Brands / Tiers | How Verified |
|---|---|---|---|---|
| **Display Cutout** | `cmd overlay enable com.android.internal.display.cutout.emulation.<type>` | Hardware notch / punch-hole camera obscuring interactive layout elements | Samsung, Xiaomi, Vivo, OnePlus (punch-hole); Motorola, Honor (notch) | `cmd overlay list` (overlay shows `[x]`); fallback to `corner` on API 29 per Issue EX-1 |
| **Gesture Navigation** | `cmd overlay enable com.android.internal.systemui.navbar.gestural`<br>`settings put secure navigation_mode 2` | Modern edge swipe gesture bar colliding with bottom buttons / toolbars | Medium and High tiers across all brands | `settings get secure navigation_mode` returns `2`; overlay list shows `[x]` |
| **Font Scale** | `settings put system font_scale 1.15` | Vendor / accessibility enlarged typography causing text clipping / truncation | All brands (default 1.15 for test flows) | `settings get system font_scale` returns `1.15` |
| **Display Density** | `wm density <dpi>` | Vendor custom DPI scaling causing layout reflow / coordinate shift | Sourced per reference model in `reference-models.yaml` (e.g. 260, 387, 420 dpi) | `wm density` readback matches `Override density: <dpi>` |
| **Dark Mode** | `cmd uimode night yes`<br>`settings put secure ui_night_mode 2` | System night mode revealing low-contrast or hardcoded dark text on dark surfaces | All profiles with `dark_mode: true` | `dumpsys uimode` (`mComputedNightMode=true`) |
| **Locale** | `settings put system system_locales <locale>`<br>`setprop persist.sys.locale <locale>` | Non-English script rendering and expansion (e.g. `ta-IN`, `hi-IN`) | Selected regional test profiles | `settings get system system_locales` |

---

## 3. Layer C: OEM Behaviour Packs Reference

The most common real-world OEM bugs arise from aggressive vendor background task killers, battery managers, memory pressure, and runtime permission revocations.

| Behaviour | Exact ADB Command | What It Simulates | Brands Represented | How Verified |
|---|---|---|---|---|
| **`background_kill`** | `input keyevent 3` (Home)<br>`sleep 5`<br>`am kill <pkg>`<br>`monkey -p <pkg> -c android.intent.category.LAUNCHER 1` | Aggressive background process termination when user leaves app; verifies activity state preservation | Xiaomi, Vivo, Huawei, Honor, OnePlus | `pidof <pkg>` is empty after `am kill`; returns a new PID upon launcher relaunch |
| **`restrict_background`** | `cmd appops set <pkg> RUN_ANY_IN_BACKGROUND ignore` | Vendor battery saver blocking background execution, jobs, alarms, and network sync | Xiaomi, Vivo, Huawei | `cmd appops get <pkg> RUN_ANY_IN_BACKGROUND` contains `ignore` |
| **`standby_bucket`** | `am set-standby-bucket <pkg> restricted` (API 31+)<br>`am set-standby-bucket <pkg> rare` (API 29) | Samsung "Sleeping Apps" & OnePlus power optimizations throttling app execution | Samsung, OnePlus | `am get-standby-bucket <pkg>` matches requested bucket name or code (`40` = rare, `45` = restricted) |
| **`doze`** | `dumpsys battery unplug`<br>`dumpsys deviceidle force-idle` | Aggressive deep sleep / standby idling typical of Chinese OEM battery profiles | All brands (especially Xiaomi, Vivo, Huawei, Honor, OnePlus) | `dumpsys deviceidle` reports `mForceIdle=true` and `mState=IDLE` |
| **`always_finish_activities`** | `settings put global always_finish_activities 1` | Low memory eviction where background activities are immediately destroyed on leave | Low tier (4 GB) of all brands | `settings get global always_finish_activities` returns `1` |
| **`trim_memory`** | `am send-trim-memory <pkg> RUNNING_CRITICAL` | Severe host and device memory pressure forcing app cache trimming and low-RAM handling | Low tier (4 GB) of all brands | ActivityManager returns exit code 0 on the active process |
| **`permission_revoke`** | `pm revoke <pkg> <permission>` | OEM "Permission Auto-Reset" or vendor security managers revoking runtime permissions mid-flow | All brands | `dumpsys package <pkg>` shows `granted=false` or `pm check-permission` returns `denied` |

---

## 4. Brand Behaviour Packs Matrix

Per Final Execution Plan §5.3 and `reference-models.yaml`:

| Brand | Baseline Behaviour Pack | Low-Tier Additions (RAM Pressure) |
|---|---|---|
| **Samsung** | `standby_bucket`, `doze`, `permission_revoke` | `always_finish_activities`, `trim_memory` |
| **Vivo** | `background_kill`, `restrict_background`, `doze`, `permission_revoke` | `always_finish_activities`, `trim_memory` |
| **OnePlus** | `background_kill`, `standby_bucket`, `doze`, `permission_revoke` | `always_finish_activities`, `trim_memory` |
| **Xiaomi** | `background_kill`, `restrict_background`, `doze`, `permission_revoke` | `always_finish_activities`, `trim_memory` |
| **Google** | `doze`, `permission_revoke` | `always_finish_activities`, `trim_memory` |
| **Motorola** | `doze`, `permission_revoke` | `always_finish_activities`, `trim_memory` |
| **Honor** | `background_kill`, `doze`, `permission_revoke` | `always_finish_activities`, `trim_memory` |
| **Huawei** | `background_kill`, `restrict_background`, `doze`, `permission_revoke` | `always_finish_activities`, `trim_memory` |

---

## 5. Teardown and Profile Reset (`reset_profile`)

To prevent state leakage between sequential runs, `reset_profile(serial, pkg)` undoes all applied settings and behaviours:
1. `dumpsys deviceidle unforce`
2. `dumpsys battery reset`
3. `settings put global always_finish_activities 0`
4. `cmd appops set <pkg> RUN_ANY_IN_BACKGROUND default`
5. `am set-standby-bucket <pkg> active`
6. `cmd overlay disable com.android.internal.systemui.navbar.gestural` & `settings put secure navigation_mode 0`
7. Disable any active display cutout emulation overlays (`cmd overlay disable <pkg>`)
8. `settings put system font_scale 1.0`
9. `wm density reset`
10. `cmd uimode night no` & `settings put secure ui_night_mode 1`
11. Re-grant any permissions revoked during testing (`pm grant <pkg> <permission>`)
