"""geometry.py — UI geometry validation.

Owner: Mathan (M7)

Judges layout only from the UI hierarchy XML (Appium ``page_source``) and screen
facts. Deterministic bounds arithmetic: no vision, no AI. Standard library only.

``check(hierarchy_xml, profile_config)`` returns :class:`vmf.results.Finding`
objects of kind ``off_screen``, ``overlap``, ``tap_target`` or
``cutout_overlap``. ``validate_geometry`` is a thin wrapper kept for existing
callers.

profile_config contract (every key optional; not final until the device
catalogue and M5 land, so unknown keys are ignored)::

    width_px, height_px   screen size in pixels
    resolution            "WxH" string, used when width_px/height_px are absent
    density_dpi           screen density; ``density`` is accepted as an alias
    cutout_rects          list of (x1, y1, x2, y2) display-cutout rectangles
    gesture_rects         list of (x1, y1, x2, y2) gesture-bar rectangles

Screen size not given: the bottom-right corner of the root node (the first node
with usable bounds) is used, and that is logged. Density not given: the
tap-target check is skipped with a warning; a density is never guessed.
"""
import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from vmf.results import Finding

# Minimum share of the SMALLER clickable element that must be covered by another
# clickable element before the pair is reported as overlapping.
OVERLAP_MIN_RATIO = 0.2
# Minimum tap-target size in dp (px / (density_dpi / 160)); below it is reported.
MIN_TAP_DP = 48
# Android's baseline density: 1 dp == 1 px at 160 dpi.
DP_BASELINE_DPI = 160
# Seconds to wait for each adb command in get_display_zones.
ADB_TIMEOUT_S = 10

_BOUNDS_RE = re.compile(r"^\s*\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]\s*$")
_RESOLUTION_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$")
_RECT_RE = re.compile(r"Rect\(\s*(-?\d+),\s*(-?\d+)\s*-\s*(-?\d+),\s*(-?\d+)\s*\)")
_INSETS_RECT_RE = re.compile(r"insets=Rect\([^)]*\)")
_FRAME_RE = re.compile(r"(?<![A-Za-z])m?[Ff]rame=\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_INIT_SIZE_RE = re.compile(r"\binit=(\d+)x(\d+)")

Rect = tuple[int, int, int, int]


@dataclass
class _Node:
    order: int
    element_id: str
    bounds: Rect | None
    clickable_enabled: bool
    displayed_false: bool
    scrollable_ancestor: bool
    ancestors: frozenset = field(default_factory=frozenset)


# ---------------------------------------------------------------------------
# Parsing (M7.1)
# ---------------------------------------------------------------------------

def _parse_bounds(value: str | None) -> Rect | None:
    if not value:
        return None
    m = _BOUNDS_RE.match(value)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


# Public name for other modules (the M14 locator fallback parses the same bounds strings).
parse_bounds = _parse_bounds


def _area(r: Rect) -> int:
    return max(0, r[2] - r[0]) * max(0, r[3] - r[1])


def _intersection(a: Rect, b: Rect) -> int:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def _parse_nodes(hierarchy_xml: str) -> list[_Node]:
    try:
        root = ET.fromstring(hierarchy_xml)
    except ET.ParseError as e:
        raise ValueError(f"malformed hierarchy XML: {e}") from e

    nodes: list[_Node] = []
    counter = 0
    # (element, ancestor order ids, any ancestor scrollable)
    stack = [(root, (), False)]
    while stack:
        el, ancestors, scroll_anc = stack.pop()
        order = counter
        counter += 1
        bounds = _parse_bounds(el.get("bounds"))
        if bounds is None:
            logging.debug("geometry: node %d (%s) has missing/invalid bounds; skipped",
                          order, el.get("class") or el.tag)
        elif _area(bounds) <= 0:
            logging.debug("geometry: node %d (%s) has zero area; skipped",
                          order, el.get("class") or el.tag)
            bounds = None
        rid = el.get("resource-id") or ""
        element_id = rid if rid else f"{el.get('class') or el.tag}[{order}]"
        nodes.append(_Node(
            order=order,
            element_id=element_id,
            bounds=bounds,
            clickable_enabled=el.get("clickable") == "true" and el.get("enabled") != "false",
            displayed_false=el.get("displayed") == "false",
            scrollable_ancestor=scroll_anc,
            ancestors=frozenset(ancestors),
        ))
        child_scroll = scroll_anc or el.get("scrollable") == "true"
        child_anc = ancestors + (order,)
        # reversed so the stack pops children in document order
        for child in reversed(list(el)):
            stack.append((child, child_anc, child_scroll))
    nodes.sort(key=lambda n: n.order)
    return nodes


def _rect_list(value, name: str) -> list[Rect]:
    rects: list[Rect] = []
    for item in value or []:
        try:
            x1, y1, x2, y2 = (int(v) for v in item)
        except (TypeError, ValueError):
            logging.warning("geometry: ignoring invalid %s entry %r", name, item)
            continue
        rects.append((x1, y1, x2, y2))
    return rects


def _density(profile_config: dict) -> float | None:
    for key in ("density_dpi", "density"):
        value = profile_config.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            d = float(value)
        except (TypeError, ValueError):
            continue
        if d > 0:
            return d
    return None


def _screen_from_profile(profile_config: dict) -> tuple[int, int] | None:
    w, h = profile_config.get("width_px"), profile_config.get("height_px")
    try:
        if w is not None and h is not None and int(w) > 0 and int(h) > 0:
            return int(w), int(h)
    except (TypeError, ValueError):
        pass
    res = profile_config.get("resolution")
    if isinstance(res, str):
        m = _RESOLUTION_RE.match(res)
        if m and int(m.group(1)) > 0 and int(m.group(2)) > 0:
            return int(m.group(1)), int(m.group(2))
        logging.warning("geometry: cannot parse resolution %r", res)
    return None


# ---------------------------------------------------------------------------
# Checks (M7.3)
# ---------------------------------------------------------------------------

def check(hierarchy_xml: str, profile_config: dict) -> list[Finding]:
    """Check the hierarchy for layout problems and return the findings.

    Args:
        hierarchy_xml:  Raw XML from Appium ``page_source``.
        profile_config: Dict described in the module docstring (all keys optional).

    Returns:
        Findings sorted by (kind, element_id, bounds), duplicates removed.

    Empty or whitespace-only ``hierarchy_xml`` logs a warning and returns ``[]``;
    that means "could not judge", NOT "layout is fine". Malformed XML raises
    ``ValueError``.
    """
    if not hierarchy_xml or not hierarchy_xml.strip():
        logging.warning("geometry: empty hierarchy XML; cannot judge layout")
        return []
    profile_config = profile_config or {}
    nodes = _parse_nodes(hierarchy_xml)
    boxed = [n for n in nodes if n.bounds is not None]
    if not boxed:
        return []

    screen = _screen_from_profile(profile_config)
    if screen is None:
        root = boxed[0].bounds
        screen = (root[2], root[3])
        logging.info("geometry: screen size not in profile; using root node bounds %dx%d",
                     screen[0], screen[1])
    width, height = screen

    density = _density(profile_config)
    if density is None:
        logging.warning("geometry: no density in profile; skipping tap-target check")

    cutouts = _rect_list(profile_config.get("cutout_rects"), "cutout_rects")
    gestures = _rect_list(profile_config.get("gesture_rects"), "gesture_rects")

    findings: dict[tuple, Finding] = {}

    def add(kind: str, node: _Node, detail: str) -> None:
        f = Finding(kind=kind, element_id=node.element_id, bounds=node.bounds, detail=detail)
        findings[(kind, f.element_id, f.bounds, detail)] = f

    # a) off_screen
    for n in boxed:
        if n.scrollable_ancestor or n.displayed_false:
            continue
        x1, y1, x2, y2 = n.bounds
        sides = []
        if x1 < 0:
            sides.append(f"left by {-x1}px")
        if y1 < 0:
            sides.append(f"top by {-y1}px")
        if x2 > width:
            sides.append(f"right by {x2 - width}px")
        if y2 > height:
            sides.append(f"bottom by {y2 - height}px")
        if sides:
            add("off_screen", n,
                f"extends outside the {width}x{height} screen: " + ", ".join(sides))

    tappable = [n for n in boxed if n.clickable_enabled]

    # b) overlap
    for i, a in enumerate(tappable):
        for b in tappable[i + 1:]:
            if a.order in b.ancestors or b.order in a.ancestors:
                continue
            inter = _intersection(a.bounds, b.bounds)
            smaller = min(_area(a.bounds), _area(b.bounds))
            if inter and inter / smaller >= OVERLAP_MIN_RATIO:
                add("overlap", a,
                    f"overlaps {b.element_id} {list(b.bounds)}: {inter}px^2 "
                    f"= {inter / smaller:.0%} of the smaller element "
                    f"(minimum {OVERLAP_MIN_RATIO:.0%}); pair {a.element_id} / {b.element_id}")

    # c) tap_target
    if density is not None:
        for n in tappable:
            side = min(n.bounds[2] - n.bounds[0], n.bounds[3] - n.bounds[1])
            dp = side * DP_BASELINE_DPI / density
            if dp < MIN_TAP_DP:
                add("tap_target", n,
                    f"smaller side is {dp:.1f} dp ({side}px at {density:g} dpi); "
                    f"minimum is {MIN_TAP_DP} dp")

    # d) cutout_overlap (display cutout and gesture bar zone)
    for n in tappable:
        for zone_rects, label in ((cutouts, "display cutout"), (gestures, "gesture bar zone")):
            for z in zone_rects:
                if _intersection(n.bounds, z):
                    add("cutout_overlap", n, f"overlaps {label} {list(z)}")

    return sorted(findings.values(),
                  key=lambda f: (f.kind, f.element_id, f.bounds, f.detail))


def validate_geometry(hierarchy_xml: str, profile: dict) -> list[Finding]:
    """Backward-compatible wrapper: delegates to :func:`check`."""
    return check(hierarchy_xml, profile)


# ---------------------------------------------------------------------------
# Cutout and gesture zones from the device (M7.4)
#
# NOTE: the real `dumpsys display` / `dumpsys window` output format varies by
# Android version. These parsers are written against hand-made samples, so they
# must be checked against real output on a device.
# ---------------------------------------------------------------------------

def parse_cutout_rects(dumpsys_display_text: str) -> list[Rect]:
    """Extract display-cutout rectangles written as ``Rect(l, t - r, b)``.

    Reads ``DisplayCutout`` lines. The ``insets=Rect(...)`` value is not a
    bounding rectangle and is ignored. When a ``Bounds=[...]`` group exists only
    its rectangles are used. All-zero rectangles and duplicates are dropped.
    """
    rects: list[Rect] = []
    for line in (dumpsys_display_text or "").splitlines():
        start = line.find("DisplayCutout")
        if start < 0:
            continue
        segment = line[start:]
        b = segment.find("Bounds=[")
        if b >= 0:
            end = segment.find("]", b)
            segment = segment[b:end if end >= 0 else None]
        else:
            segment = _INSETS_RECT_RE.sub("", segment)
        for m in _RECT_RE.finditer(segment):
            r = tuple(int(g) for g in m.groups())
            if r != (0, 0, 0, 0) and r not in rects:
                rects.append(r)
    return rects


def parse_gesture_rects(dumpsys_window_text: str, screen: tuple[int, int]) -> list[Rect]:
    """Best effort: return navigation-bar inset frames from ``dumpsys window``.

    Looks at lines mentioning ``navigationBars`` or ``ITYPE_NAVIGATION_BAR`` and
    reads ``frame=[l,t][r,b]``. Frames with no area are dropped, and, when
    ``screen`` has a positive size, frames that miss the screen. Returns ``[]``
    when nothing matches.
    """
    width, height = screen if screen else (0, 0)
    rects: list[Rect] = []
    for line in (dumpsys_window_text or "").splitlines():
        if "navigationBars" not in line and "ITYPE_NAVIGATION_BAR" not in line:
            continue
        for m in _FRAME_RE.finditer(line):
            r = tuple(int(g) for g in m.groups())
            if _area(r) <= 0:
                continue
            if width > 0 and height > 0 and not _intersection(r, (0, 0, width, height)):
                continue
            if r not in rects:
                rects.append(r)
    return rects


def _default_runner(argv: list[str]):
    return subprocess.run(argv, capture_output=True, text=True, timeout=ADB_TIMEOUT_S)


def get_display_zones(serial: str, runner=None) -> dict:
    """Read cutout and gesture zones from a device via adb.

    Runs ``adb -s <serial> shell dumpsys display`` and ``... dumpsys window``
    through ``runner(argv)``, which must return an object with ``returncode`` and
    ``stdout`` (``subprocess.run`` semantics; the default adds a timeout). Each
    command that fails, times out or cannot start is logged as a warning and
    yields an empty list for its part.

    Returns:
        ``{"cutout_rects": [...], "gesture_rects": [...]}`` for use as
        profile_config entries.
    """
    run = runner or _default_runner

    def dumpsys(what: str) -> str | None:
        argv = ["adb", "-s", serial, "shell", "dumpsys", what]
        try:
            result = run(argv)
        except (OSError, subprocess.SubprocessError) as e:
            logging.warning("geometry: %s failed: %s", " ".join(argv), e)
            return None
        if result.returncode != 0:
            logging.warning("geometry: %s exited with %s", " ".join(argv), result.returncode)
            return None
        return result.stdout or ""

    display = dumpsys("display")
    window = dumpsys("window")
    screen = (0, 0)
    if window:
        m = _INIT_SIZE_RE.search(window)
        if m:
            screen = (int(m.group(1)), int(m.group(2)))
    return {
        "cutout_rects": parse_cutout_rects(display) if display is not None else [],
        "gesture_rects": parse_gesture_rects(window, screen) if window is not None else [],
    }
