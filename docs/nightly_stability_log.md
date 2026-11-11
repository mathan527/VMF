# VMF Nightly Stability Log (5-Night Log)

**Module**: M12.5 & M15.4  
**Owner**: Dhatri  
**Required for Acceptance**: 5 unattended nights starting from Wednesday 28 October to Sunday 1 November 2026 (Final Execution Plan Revision 2 §8, M12.5, M15.4).

---

## Stability Run Table

| Night | Date | Finished? | Failures | Notes / Fix Applied | Verified By |
|---|---|---|---|---|---|
| Night 1 | 2026-10-28 | Yes | 0 | Run #180: 1/1 passed (0 failures). Evidence: results/180 | Dhatri |
| Night 2 | 2026-11-06 | Yes | 0 verdict failures; hierarchy evidence error | Run #186: 24/24 verdicts passed, but Huawei_medium_31 hierarchy capture failed; not a clean evidence run. Evidence: results/186 | Dhatri |
| Night 3 | 2026-11-07 | Yes | 0 | Run #190: 24/24 passed (0 failures). Evidence: results/190 | Dhatri |
| Night 4 | 2026-11-08 | Yes | 0 | Run #191: 24/24 passed (0 failures). Evidence: results/191 | Dhatri |
| Night 5 | 2026-11-09 | Yes | 0 | Run #192: 24/24 passed (0 failures). Evidence: results/192 | Dhatri |

---

## Log Rules
- Each night's full terminal and matrix log is saved in `logs/nightly_<YYYYMMDD_HHMMSS>.log`.
- Any unexpected reboot, hang, or missed profile is recorded in this log with the exact fix applied.
- Done-when criteria: 5 clean nights, 0 unhandled crash/hang errors (verified for Gate 3 handover).
| auto | 2026-11-10 | Yes | 0 | Run #193: 24/24 passed (0 failures). Evidence: results/193 | Dhatri |
| auto | 2026-11-11 | Yes | 0 | Run #194: 24/24 passed (0 failures). Evidence: results/194 | Dhatri |
