"""api.py — HTTP API application factory.

Owner: Gagan (M12)
ARCH stub provided here.

Real FastAPI application with routes for run management, status polling,
and report retrieval is due in M12.  Deployment via Windows Task Scheduler.
"""
import logging


def create_app():
    """ARCH stub — real FastAPI application factory: M12.

    Creates and configures the FastAPI application with routes for:
    - POST /run         — start a new test run
    - GET  /run/{id}    — query run status
    - GET  /report/{id} — fetch the generated report

    During ARCH this returns a sentinel ``object()`` without importing
    FastAPI (FastAPI is not a required dependency at ARCH stage).

    Returns:
        A placeholder sentinel during ARCH.
        M12 will return a real ``fastapi.FastAPI()`` instance.
    """
    logging.info("[STUB] api.create_app called (real FastAPI app: M12)")
    return object()   # sentinel; replaced by FastAPI() in M12
