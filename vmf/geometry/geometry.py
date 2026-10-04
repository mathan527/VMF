"""geometry.py — UI geometry validation.

Owner: Mathan (M7)
ARCH stub provided here.

Real off-screen / overlap / tap-target / cutout-overlap checks are due in M7.
"""
import logging

from vmf.results import Finding


def validate_geometry(hierarchy_xml: str, profile: dict) -> list[Finding]:
    """ARCH stub — real UI-bounds checks: M7.

    Parses the view hierarchy XML and checks every element against the
    device profile (cutout, density, display bounds) for geometry issues.
    During ARCH the stub always returns an empty list (no findings).

    Args:
        hierarchy_xml: Raw XML string from ``driver.page_source``.
        profile:       Active device profile dict (cutout, density, ...).

    Returns:
        List of :class:`vmf.results.Finding` instances (empty in ARCH stub).
    """
    logging.info("[STUB] geometry.validate_geometry profile=%s xml_len=%d",
                 profile, len(hierarchy_xml))
    return []
