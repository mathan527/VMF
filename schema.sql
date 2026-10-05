PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY,
    apk_name TEXT NOT NULL,
    scenario TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    profile TEXT NOT NULL,
    result TEXT NOT NULL,
    cause TEXT NOT NULL,
    failed_step INTEGER,
    trigger_behaviour TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    profile TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    screenshot_path TEXT NOT NULL,
    hierarchy_path TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profile_config (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    profile TEXT NOT NULL,
    config_json TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS perf_results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    profile TEXT NOT NULL,
    metric TEXT NOT NULL,
    median REAL NOT NULL,
    spread_pct REAL NOT NULL,
    baseline REAL,
    regression_pct REAL,
    noisy INTEGER NOT NULL,
    unstable INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS perf_baselines (
    id INTEGER PRIMARY KEY,
    profile TEXT NOT NULL,
    metric TEXT NOT NULL,
    baseline REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_results_run_id
    ON device_results(run_id);
CREATE INDEX IF NOT EXISTS idx_device_results_profile
    ON device_results(profile);

CREATE INDEX IF NOT EXISTS idx_steps_run_id
    ON steps(run_id);
CREATE INDEX IF NOT EXISTS idx_steps_profile
    ON steps(profile);

CREATE INDEX IF NOT EXISTS idx_profile_config_run_id
    ON profile_config(run_id);
CREATE INDEX IF NOT EXISTS idx_profile_config_profile
    ON profile_config(profile);

CREATE INDEX IF NOT EXISTS idx_perf_results_run_id
    ON perf_results(run_id);
CREATE INDEX IF NOT EXISTS idx_perf_results_profile
    ON perf_results(profile);
CREATE INDEX IF NOT EXISTS idx_perf_results_metric
    ON perf_results(metric);

CREATE INDEX IF NOT EXISTS idx_perf_baselines_profile_metric
    ON perf_baselines(profile, metric);
