from dataclasses import dataclass


@dataclass
class Step:
    step_index: int
    action: str
    status: str  # "pass" | "fail" | "skipped"
    duration_ms: int
    screenshot_path: str
    hierarchy_path: str


@dataclass
class Verdict:
    result: str  # "pass" | "crash" | "hang" | "anr"
    cause: str
    failed_step: int | None
    trigger_behaviour: str | None


@dataclass
class Finding:
    kind: str  # "off_screen" | "overlap" | "tap_target" | "cutout_overlap"
    element_id: str
    bounds: tuple[int, int, int, int]
    detail: str


@dataclass
class PerfRow:
    metric: str  # "launch_time" | "jank_pct" | "pss"
    median: float
    spread_pct: float
    baseline: float | None
    regression_pct: float | None
    noisy: bool
    unstable: bool
