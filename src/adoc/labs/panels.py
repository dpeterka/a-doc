"""Panel-grouping helpers for the labs surfaces (lab-taxonomy layer).

Every surface that groups analytes by panel (the `/labs` index, the
per-analyte detail page, and `reason.context`'s `_labs_section`) imports
`PANEL_ORDER`/`panel_for`/`panel_sort_key` from here instead of re-deriving
its own order, so grouping stays identical - and deterministic, which
matters for prompt caching (PLAN.md) - across all three.

Grouping itself is driven entirely by `labs.validate.AnalyteSpec.panel`
(hand-curated, feature/lab-taxonomy); this module adds no new analyte
knowledge, just a stable display order and a bucket ("Other") for anything
not in a curated panel. Deliberately does not force one-off/exotic analytes
into a panel - "Other" is not a defect, it's the correct home for an analyte
that isn't a recurring series or a member of a real clinical panel.
"""

from __future__ import annotations

from adoc.labs.validate import ANALYTE_SPECS, AnalyteSpec, canonicalize

OTHER_PANEL = "Other"

# Curated clinical display order (item 5 of the lab-taxonomy plan). Anything
# with `panel=None`, or a panel string not listed here, is grouped under
# `OTHER_PANEL`, which always sorts last regardless of where it would fall
# alphabetically - see `panel_sort_key`.
PANEL_ORDER: tuple[str, ...] = (
    "CBC",
    "Comprehensive Metabolic Panel",
    "Lipid Panel",
    "Thyroid",
    "Inflammation",
    "Iron Studies",
    "Hormones",
    "Vitamins & Nutrition",
    "Heavy Metals",
    "Autoimmune Serology",
    "Tick-Borne Serology",
    "Immunology/Flow Cytometry",
    "Allergen IgE",
    "Mold IgG Panel",
    "Tumor Markers",
    "Coagulation",
    "Urinalysis",
    "Stool Studies",
    "Bone Density",
)


def spec_for(name: str) -> AnalyteSpec | None:
    """The `AnalyteSpec` for canonical (or raw) analyte `name`, trying
    `canonicalize` first and falling back to a direct `ANALYTE_SPECS`
    lookup (mirrors `validate_row`'s own fallback) - `None` if neither
    resolves to a known spec."""
    canonical = canonicalize(name) or name
    return ANALYTE_SPECS.get(canonical)


def panel_for(name: str) -> str:
    """The display panel for analyte `name` - `OTHER_PANEL` if `name`
    isn't a known analyte, or is one with no curated panel."""
    spec = spec_for(name)
    if spec is None or spec.panel is None:
        return OTHER_PANEL
    return spec.panel


def derived_from_note(name: str) -> str | None:
    """A short "calculated from X and Y" note for a derived analyte (e.g.
    TSAT from Iron + TIBC), or `None` if `name` isn't derived from anything
    (the common case)."""
    spec = spec_for(name)
    if spec is None or not spec.derived_from:
        return None
    if len(spec.derived_from) == 1:
        joined = spec.derived_from[0]
    else:
        joined = ", ".join(spec.derived_from[:-1]) + f" and {spec.derived_from[-1]}"
    return f"calculated from {joined}"


def panel_sort_key(name: str) -> tuple[int, str, str]:
    """Sort key for grouping analytes by panel: curated `PANEL_ORDER` index
    first (`OTHER_PANEL` always sorts last, even alphabetically-earlier
    than a listed panel would be), then alphabetical by panel name (covers
    a panel string that exists on a spec but isn't in `PANEL_ORDER`), then
    alphabetical by analyte name within a panel - fully deterministic, so
    two callers building the same set of analytes always render them in
    the same order (needed for prompt-cache-stable `_labs_section` output).
    """
    panel = panel_for(name)
    if panel == OTHER_PANEL:
        order_index = len(PANEL_ORDER) + 1
    elif panel in PANEL_ORDER:
        order_index = PANEL_ORDER.index(panel)
    else:
        order_index = len(PANEL_ORDER)
    return (order_index, panel, name)
