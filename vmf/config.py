"""config.py — Shared constants moved out of vmf/runner/runner.py.

Values are unchanged; ``vmf.runner.runner`` imports them back, so
``vmf.runner.runner.APPIUM_URL`` and the other names still resolve.
"""
APPIUM_URL = "http://localhost:4723"
DEFAULT_TIMEOUT_S = 10
KEYCODE_HOME = 3
RESULTS_DIR = "results"
