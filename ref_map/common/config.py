"""Shared configuration and naming helpers for ref_map analytics."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")


@dataclass(frozen=True)
class SeedSpec:
    """Canonical contract for a seed universe and all derived names."""

    mode: str
    property_name: str
    prefix: str
    description: str
    legacy: bool = False


DEFAULT_SEED_MODE = "all-assignees"

SEED_SPECS: dict[str, SeedSpec] = {
    "all-assignees": SeedSpec(
        mode="all-assignees",
        property_name="matt_marx_founding_patent_seed_all",
        prefix="all_assignees",
        description="all initial assignees in the Founding Patents linkage",
    ),
    "corporate": SeedSpec(
        mode="corporate",
        property_name="matt_marx_founding_patent_seed_corporate",
        prefix="corporate",
        description="Founding Patents links excluding universities, hospitals, and government",
    ),
    "young-firm": SeedSpec(
        mode="young-firm",
        property_name="matt_marx_founding_patent_seed_young_firm",
        prefix="young_firm",
        description="strict-corporate assignees no more than three years old at application",
    ),
    "young-firm-university": SeedSpec(
        mode="young-firm-university",
        property_name="matt_marx_founding_patent_seed_young_firm_university",
        prefix="young_firm_university",
        description=(
            "union of age-based young-firm assignees and university assignees, "
            "excluding hospitals, government, and established corporations"
        ),
    ),
    "patent": SeedSpec(
        mode="patent",
        property_name="matt_marx_paper_patent_v2",
        prefix="patent",
        description="all links in the Patent-Paper Pairs dataset",
    ),
    "legacy-startup": SeedSpec(
        mode="legacy-startup",
        property_name="matt_marx_paper_patent_startup",
        prefix="startup",
        description="legacy broad Founding Patents property retained for reproducibility",
        legacy=True,
    ),
}

SEED_MODE_CHOICES = tuple(SEED_SPECS)
CANONICAL_FOUNDING_PATENT_MODES = (
    "all-assignees",
    "corporate",
    "young-firm",
    "young-firm-university",
)
CANONICAL_FOUNDING_PATENT_PROPERTIES = tuple(
    SEED_SPECS[mode].property_name for mode in CANONICAL_FOUNDING_PATENT_MODES
)
CANONICAL_FOUNDING_PATENT_INDEX_NAMES = (
    "work_matt_founding_all",
    "work_matt_founding_corporate",
    "work_matt_founding_young_firm",
    "work_matt_founding_young_firm_university",
)
SEED_PROP_BY_MODE = {
    mode: spec.property_name for mode, spec in SEED_SPECS.items()
}


def configured_seed_mode() -> str:
    return os.environ.get("ANALYTICS_SEED_MODE", DEFAULT_SEED_MODE)


def resolve_seed_mode(mode: str | None) -> str:
    resolved = (mode or configured_seed_mode()).strip().lower().replace("_", "-")
    if resolved == "startup":
        raise ValueError(
            "Seed mode 'startup' is ambiguous and no longer accepted. "
            "Use 'all-assignees' for the same broad seed semantics, "
            "'young-firm' for the age-based startup definition, or "
            "'young-firm-university' for young firms plus universities, or "
            "'legacy-startup' to reproduce an existing startup_* run."
        )
    if resolved not in SEED_SPECS:
        choices = ", ".join(SEED_MODE_CHOICES)
        raise ValueError(f"Unknown seed mode {mode!r}; choose one of: {choices}")
    return resolved


def seed_spec_for_mode(mode: str) -> SeedSpec:
    return SEED_SPECS[resolve_seed_mode(mode)]


def seed_prop_for_mode(mode: str) -> str:
    return seed_spec_for_mode(mode).property_name


def prefix_for_mode(mode: str) -> str:
    return seed_spec_for_mode(mode).prefix


def add_seed_mode_argument(
    parser: argparse.ArgumentParser,
    *,
    help_text: str = "Seed universe used to resolve default properties.",
) -> None:
    parser.add_argument(
        "--seed-mode",
        choices=SEED_MODE_CHOICES,
        default=resolve_seed_mode(configured_seed_mode()),
        help=f"{help_text} Default: {DEFAULT_SEED_MODE}.",
    )


def output_token_for_mode(mode: str) -> str:
    """Filesystem-safe seed token used in default output names."""
    return prefix_for_mode(mode)


def hops_prop_for_mode(prefix: str, is_test: bool) -> str:
    return f"{prefix}_min_hops_test" if is_test else f"{prefix}_min_hops"


def reach_prop_for_mode(prefix: str, is_test: bool) -> str:
    return f"{prefix}_reachable_test" if is_test else f"{prefix}_reachable"


def reach_count_props(prefix: str) -> tuple[str, str]:
    return f"{prefix}_reach_count", f"{prefix}_forward_reach_count"


def par_prop_for_mode(prefix: str) -> str:
    return f"{prefix}_par"


def hll_backward_prop_for_mode(prefix: str) -> str:
    return f"{prefix}_hll_backward_count"


def reachability_graph_for_mode(prefix: str) -> tuple[str, str, str]:
    graph_name = f"{prefix}_reachability"
    anchor_label = "".join(part.capitalize() for part in prefix.split("_")) + "Anchor"
    anchor_name = f"{prefix}_anchor"
    return graph_name, anchor_label, anchor_name
