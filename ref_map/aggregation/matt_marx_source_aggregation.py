"""Aggregate the four Founding Patents seed-definition CIF properties by source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ref_map.aggregation.source_par_suite import aggregate_source_par_suite
from ref_map.centrality.founding_patent_cif_suite import (
    DEFAULT_DAMPING_FACTOR,
    expected_output_properties,
)
from ref_map.centrality.par_core import damping_suffix
from ref_map.common.config import (
    CANONICAL_FOUNDING_PATENT_MODES,
    prefix_for_mode,
)

DEFAULT_OUTPUT_DIR = Path("ref_map/aggregation/output")


def expected_aggregation_properties(
    damping_factor: float = DEFAULT_DAMPING_FACTOR,
) -> dict[str, str]:
    properties = expected_output_properties(
        CANONICAL_FOUNDING_PATENT_MODES,
        damping_factor,
    )
    return {
        prefix_for_mode(mode): prop
        for mode, prop in zip(
            CANONICAL_FOUNDING_PATENT_MODES,
            properties,
            strict=True,
        )
    }


def expected_aggregation_outputs(
    *,
    damping_factor: float = DEFAULT_DAMPING_FACTOR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    scopes: tuple[str, ...] = ("all", "articles"),
) -> dict[str, dict[str, Path]]:
    destination = Path(output_dir)
    damping_token = damping_suffix(damping_factor).replace(".", "_")
    keys = expected_aggregation_properties(damping_factor)
    outputs: dict[str, dict[str, Path]] = {}
    for scope in scopes:
        article_suffix = "_articles" if scope == "articles" else ""
        outputs[scope] = {
            key: destination
            / f"source_par_{key}_full_{damping_token}{article_suffix}.json"
            for key in keys
        }
    return outputs


def aggregate_matt_marx_sources(
    *,
    damping_factor: float = DEFAULT_DAMPING_FACTOR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    scope: str = "both",
    min_papers: int = 1,
    num_partitions: int = 1,
    resume: bool = False,
) -> dict[str, Any]:
    if scope not in {"all", "articles", "both"}:
        raise ValueError("scope must be one of: all, articles, both")
    scopes = ("all", "articles") if scope == "both" else (scope,)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    report = aggregate_source_par_suite(
        score_properties=expected_aggregation_properties(damping_factor),
        output_paths=expected_aggregation_outputs(
            damping_factor=damping_factor,
            output_dir=destination,
            scopes=scopes,
        ),
        min_papers=min_papers,
        num_partitions=num_partitions,
        resume=resume,
    )
    report["damping_factor"] = damping_factor
    report["scope"] = scope
    meta_path = destination / "matt_marx_source_aggregation_meta.json"
    temporary = meta_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(meta_path)
    print(f"[matt-marx-agg] Audit report: {meta_path}")
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Aggregate all four canonical Matt Marx seed-set CIF properties "
            "by source with one Neo4j scan per partition."
        )
    )
    parser.add_argument(
        "--damping-factor",
        type=float,
        default=DEFAULT_DAMPING_FACTOR,
    )
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--scope",
        choices=["all", "articles", "both"],
        default="both",
        help="Output scope; both is computed in the same graph scan (default: both).",
    )
    parser.add_argument("--min-papers", type=int, default=1)
    parser.add_argument(
        "--num-partitions",
        type=int,
        default=1,
        help="Number of concurrent source-ID partitions (default: 1).",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print canonical input properties and output paths without connecting.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.dry_run:
        if args.num_partitions < 1:
            raise ValueError("num_partitions must be >= 1")
        print(f"[dry-run] Partitions: {args.num_partitions}")
        scopes = (
            ("all", "articles")
            if args.scope == "both"
            else (args.scope,)
        )
        print(f"[dry-run] Properties: {expected_aggregation_properties(args.damping_factor)}")
        for scope, paths in expected_aggregation_outputs(
            damping_factor=args.damping_factor,
            output_dir=args.output_dir,
            scopes=scopes,
        ).items():
            for key, path in paths.items():
                print(f"[dry-run] {scope}:{key} -> {path}")
        return
    aggregate_matt_marx_sources(
        damping_factor=args.damping_factor,
        output_dir=args.output_dir,
        scope=args.scope,
        min_papers=args.min_papers,
        num_partitions=args.num_partitions,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
