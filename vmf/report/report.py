"""report.py — Test report generation.

Owner: Gagan (M11)
ARCH stub provided here.

Real HTML/Excel report generation from SQLite results is due in M11.
"""
import logging


def generate_report(run_id: int, output_dir: str = "reports") -> str:
    """ARCH stub — real HTML/Excel report generation: M11.

    Reads run results from storage and produces a formatted report.
    During ARCH the stub returns a deterministic fake path string without
    reading any database or writing any file.

    Args:
        run_id:     The run identifier produced by ``storage.create_run()``.
        output_dir: Directory where the report will be written.

    Returns:
        Absolute path (or fake path during ARCH) to the generated report.
    """
    path = f"{output_dir}/run_{run_id}_report.html"
    logging.info("[STUB] report.generate_report run_id=%s -> %s", run_id, path)
    return path
