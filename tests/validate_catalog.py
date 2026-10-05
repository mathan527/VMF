#!/usr/bin/env python3
"""
validate_catalog.py
Module: M1 — Reference-Model Catalogue (M1.9)
Owner: Dhatri

Validates reference-models.yaml against the rules in the Final Execution Plan
(Rev2) section M1 and the lead's review of 3 Oct 2026.

Usage:
    python validate_catalog.py reference-models.yaml

Exit code 0 = PASS (safe to run M1.10 peer check / send for approval).
Exit code 1 = FAIL (at least one error printed below).

Checks:
  - Exactly 24 rows: the 8 brands x 3 tiers in device-matrix.yaml, each once,
    no duplicates, no missing combinations.
  - No field is missing or empty (M1.9: "fails if any row is missing, any
    field is empty, any URL is missing").
  - source_spec (and source_secondary, if present) look like real URLs.
  - ram_gb matches the tier rule (M1.2): low=4, medium=6, high>=8.
  - cutout is one of punch-hole / notch / none (per the YAML's own comment).
  - gms is a real boolean.
  - behaviour_pack is a non-empty list.
  - assumed_fields is a list of short field NAMES only, not sentences
    (lead review section 4.4) -- long entries or entries containing an
    explanatory dash are rejected.
  - No value anywhere in the row contains the literal marker "TODO" --
    draft placeholders must be resolved before this passes.
  - density_dpi, when listed in derived_fields, actually matches the
    sqrt(width^2+height^2)/screen_inches formula from M1.6 (rounded),
    within +/-1 of rounding tolerance.

Rows marked BLOCKED in notes (a real, verified market gap pending a lead
decision -- see Final Execution Plan section 7) are NOT given a free pass:
they still run through every check above and will correctly FAIL the
ram_gb-vs-tier check on purpose. That failure is the signal for the lead,
not a bug in this script. BLOCKED rows are listed separately in the summary
so the output distinguishes "still needs real data" from "needs a lead
decision on a known gap."
"""

import sys
import re
import math

try:
    import yaml
except ImportError:
    print("FAIL: PyYAML is not installed. Run: pip install pyyaml --break-system-packages")
    sys.exit(1)


REQUIRED_BRANDS = ["Samsung", "Vivo", "OnePlus", "Xiaomi", "Google", "Motorola", "Honor", "Huawei"]
REQUIRED_TIERS = ["low", "medium", "high"]
TIER_RAM = {"low": 4, "medium": 6, "high": 8}  # high = "8 or more" per M1.2
VALID_CUTOUTS = {"punch-hole", "notch", "none"}

REQUIRED_FIELDS = [
    "brand", "tier", "model", "launch_year", "resolution", "screen_inches",
    "density_dpi", "ram_gb", "cutout", "gms", "behaviour_pack",
    "source_spec", "source_popularity", "assumed_fields",
]

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def is_empty(value):
    """True if a required field is missing, None, or an empty/whitespace string."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    return False


def contains_todo(value):
    """Recursively check any string in this value for a literal TODO marker."""
    if isinstance(value, str):
        return "TODO" in value.upper()
    if isinstance(value, list):
        return any(contains_todo(v) for v in value)
    if isinstance(value, dict):
        return any(contains_todo(v) for v in value.values())
    return False


def parse_resolution(resolution):
    """Parse 'W x H' or 'WxH' style strings into (width, height) ints."""
    if not isinstance(resolution, str):
        return None
    cleaned = resolution.lower().replace(" ", "")
    match = re.match(r"^(\d+)x(\d+)$", cleaned)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def row_label(row):
    return f"{row.get('brand', '?')}/{row.get('tier', '?')}"


def validate(path):
    errors = []
    warnings = []
    blocked_rows = []

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        print(f"FAIL: {path} is not valid YAML -- {e}")
        return 1
    except FileNotFoundError:
        print(f"FAIL: {path} not found")
        return 1

    if not data or "models" not in data:
        print(f"FAIL: {path} has no top-level 'models' list")
        return 1

    rows = data["models"]

    # --- Row count and coverage ------------------------------------------
    if len(rows) != 24:
        errors.append(f"Expected exactly 24 rows (8 brands x 3 tiers), found {len(rows)}.")

    seen_combos = set()
    for row in rows:
        brand = row.get("brand")
        tier = row.get("tier")
        combo = (brand, tier)
        if combo in seen_combos:
            errors.append(f"Duplicate row for {brand}/{tier}.")
        seen_combos.add(combo)

        if brand not in REQUIRED_BRANDS:
            errors.append(
                f"{row_label(row)}: brand '{brand}' is not one of the device-matrix.yaml "
                f"brands {REQUIRED_BRANDS}. (Check for sub-brand strings like 'Redmi' or "
                f"'Moto' leaking into the brand field.)"
            )
        if tier not in REQUIRED_TIERS:
            errors.append(f"{row_label(row)}: tier '{tier}' is not one of {REQUIRED_TIERS}.")

    for brand in REQUIRED_BRANDS:
        for tier in REQUIRED_TIERS:
            if (brand, tier) not in seen_combos:
                errors.append(f"Missing row for {brand}/{tier}.")

    # --- Per-row field checks ----------------------------------------------
    for row in rows:
        label = row_label(row)

        # Required fields present and non-empty
        for field in REQUIRED_FIELDS:
            if field not in row:
                errors.append(f"{label}: missing field '{field}'.")
                continue
            value = row[field]
            if field == "behaviour_pack":
                if not isinstance(value, list) or len(value) == 0:
                    errors.append(f"{label}: 'behaviour_pack' must be a non-empty list.")
            elif field == "assumed_fields":
                if not isinstance(value, list):
                    errors.append(
                        f"{label}: 'assumed_fields' must be a list (even if empty: []), "
                        f"not a bare string."
                    )
                else:
                    for entry in value:
                        if not isinstance(entry, str):
                            continue
                        if len(entry) > 60 or " — " in entry or " - " in entry:
                            errors.append(
                                f"{label}: assumed_fields entry looks like a sentence, not a "
                                f"field name (lead review section 4.4): '{entry[:50]}...'"
                            )
            elif field == "gms":
                if not isinstance(value, bool):
                    errors.append(f"{label}: 'gms' must be true or false, got {value!r}.")
            elif is_empty(value):
                errors.append(f"{label}: field '{field}' is empty.")

        # TODO marker anywhere in the row
        if contains_todo(row):
            errors.append(f"{label}: contains a 'TODO' placeholder marker -- not resolved yet.")

        # URLs
        for url_field in ("source_spec", "source_secondary"):
            if url_field in row and not is_empty(row[url_field]):
                if not URL_RE.match(str(row[url_field]).strip()):
                    errors.append(
                        f"{label}: '{url_field}' does not look like a URL "
                        f"(must start with http:// or https://): {row[url_field]!r}"
                    )

        # RAM tier rule (M1.2)
        tier = row.get("tier")
        ram_gb = row.get("ram_gb")
        if tier in TIER_RAM and isinstance(ram_gb, (int, float)):
            expected = TIER_RAM[tier]
            ok = (ram_gb == expected) if tier != "high" else (ram_gb >= expected)
            if not ok:
                msg = (
                    f"{label}: ram_gb={ram_gb} does not match the tier rule "
                    f"({'exactly' if tier != 'high' else 'at least'} {expected} GB for "
                    f"tier '{tier}')."
                )
                errors.append(msg)

        # cutout
        cutout = row.get("cutout")
        if cutout and cutout not in VALID_CUTOUTS:
            errors.append(f"{label}: cutout '{cutout}' is not one of {sorted(VALID_CUTOUTS)}.")

        # density_dpi sanity check against M1.6's formula, when derived
        derived = row.get("derived_fields") or []
        if "density_dpi" in derived:
            dims = parse_resolution(row.get("resolution", ""))
            screen_inches = row.get("screen_inches")
            density_dpi = row.get("density_dpi")
            if dims and isinstance(screen_inches, (int, float)) and isinstance(density_dpi, (int, float)):
                width, height = dims
                expected_dpi = round(math.sqrt(width ** 2 + height ** 2) / screen_inches)
                if abs(expected_dpi - density_dpi) > 1:
                    warnings.append(
                        f"{label}: density_dpi={density_dpi} does not match the M1.6 formula "
                        f"(computed {expected_dpi} from resolution {row.get('resolution')} and "
                        f"screen_inches {screen_inches}). Check for a transposed resolution or "
                        f"a typo."
                    )

        # Track BLOCKED rows separately for the summary (not an error source)
        notes = row.get("notes", "") or ""
        if "BLOCKED" in notes.upper():
            blocked_rows.append(label)

    # --- Report --------------------------------------------------------
    print(f"Validating {path} ...\n")

    if warnings:
        print(f"WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  - {w}")
        print()

    if errors:
        print(f"FAILED -- {len(errors)} error(s):\n")
        for e in errors:
            print(f"  - {e}")
        print()
        if blocked_rows:
            print(f"Note: {len(blocked_rows)} row(s) are marked BLOCKED in notes (known market")
            print("gaps pending a lead decision, per Final Execution Plan section 7):")
            for b in blocked_rows:
                print(f"  - {b}")
            print("These are expected to fail the ram_gb check until the lead decides; that is")
            print("not a bug in this script.\n")
        return 1

    print(f"PASS -- 24/24 rows valid, no missing fields, no missing URLs, all RAM tiers match.")
    if blocked_rows:
        print(f"\nNote: {len(blocked_rows)} row(s) are marked BLOCKED in notes but still passed")
        print("validation (meaning they currently satisfy the RAM tier, e.g. after a lead")
        print("decision was applied). Confirm these are intentional before tagging catalog-v1:")
        for b in blocked_rows:
            print(f"  - {b}")
    if warnings:
        print(f"\n{len(warnings)} warning(s) above did not block PASS but are worth checking.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python validate_catalog.py reference-models.yaml")
        sys.exit(1)
    sys.exit(validate(sys.argv[1]))
