"""Aggregate all four M5 union-graph PAR properties by source."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ref_map.aggregation.source_par_suite import aggregate_source_par_suite
from ref_map.centrality.m5_placebo_par_suite import (
    ARMS,
    DEFAULT_DAMPING_FACTOR,
    expected_output_properties,
)

DEFAULT_OUTPUT_DIR = Path("ref_map/output")


def _output_name(arm_name: str, *, article_only: bool) -> str:
    token = "m5u_startup" if arm_name == "true" else f"m5u_{arm_name}"
    article_suffix = "_articles" if article_only else ""
    return f"source_par_{token}_full{article_suffix}.json"


def aggregate_m5_sources(
    *,
    damping_factor: float = DEFAULT_DAMPING_FACTOR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    min_papers: int = 1,
    num_partitions: int = 1,
    article_only: bool = False,
    resume: bool = False,
) -> dict[str, Any]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    properties = expected_output_properties(damping_factor)
    score_properties = {
        arm.name: prop for arm, prop in zip(ARMS, properties, strict=True)
    }
    scope = "articles" if article_only else "all"
    output_paths = {
        scope: {
            arm.name: output / _output_name(arm.name, article_only=article_only)
            for arm in ARMS
        }
    }
    report = aggregate_source_par_suite(
        score_properties=score_properties,
        output_paths=output_paths,
        min_papers=min_papers,
        num_partitions=num_partitions,
        resume=resume,
    )
    report["damping_factor"] = damping_factor
    report["article_only"] = article_only
    meta_path = output / (
        "m5_source_aggregation_articles_meta.json"
        if article_only
        else "m5_source_aggregation_meta.json"
    )
    temporary = meta_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(meta_path)
    print(f"[m5-agg] Audit report: {meta_path}")
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run source aggregation for the four M5 PAR properties."
    )
    parser.add_argument("--damping-factor", type=float, default=DEFAULT_DAMPING_FACTOR)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--min-papers", type=int, default=1)
    parser.add_argument("--num-partitions", type=int, default=1)
    parser.add_argument("--article-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    aggregate_m5_sources(
        damping_factor=args.damping_factor,
        output_dir=args.output_dir,
        min_papers=args.min_papers,
        num_partitions=args.num_partitions,
        article_only=args.article_only,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
