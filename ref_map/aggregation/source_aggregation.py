"""Unified source aggregation entrypoint (reachability/direct/par)."""

from __future__ import annotations

import argparse
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from neo4j import GraphDatabase

from ref_map.common.config import (
    DEFAULT_SEED_MODE,
    NEO4J_DATABASE,
    add_seed_mode_argument,
    hops_prop_for_mode,
    output_token_for_mode,
    par_prop_for_mode,
    prefix_for_mode,
    resolve_seed_mode,
    seed_prop_for_mode,
)
from ref_map.export.neo4j_export import _resolve_neo4j_config
from ref_map.aggregation.source_reachability import (
    PARTITION_ID_PREFIX as REACH_PART_PREFIX,
    compute_source_aggregation as compute_reachability_source_aggregation,
    compute_source_direct,
    save_results as save_reachability_results,
    merge_partition_outputs as merge_reachability_partitions,
)
from ref_map.aggregation.source_par import (
    PARTITION_ID_PREFIX as PAR_PART_PREFIX,
    compute_source_aggregation as compute_par_source_aggregation,
    merge_partition_outputs as merge_par_partitions,
    save_results as save_par_results,
)

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def _par_variant_suffix(par_prop: str, default_par_prop: str) -> str:
    if par_prop == default_par_prop:
        return ""
    if par_prop.startswith(f"{default_par_prop}_"):
        raw = par_prop[len(default_par_prop) + 1 :]
    else:
        raw = par_prop
    token = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_").lower()
    return f"_{token}" if token else ""


def _parse_depth_thresholds(raw: str) -> list[int] | None:
    if not raw:
        return None
    values: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        values.append(int(token))
    parsed = sorted({v for v in values if v >= 0})
    return parsed or None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Unified source aggregation for reachability/direct/PAR."
    )
    parser.add_argument(
        "--target",
        choices=["reachability", "direct", "par"],
        required=True,
        help="Aggregation target.",
    )
    parser.add_argument(
        "--mode",
        choices=["test", "full"],
        required=True,
        help="Data mode.",
    )
    add_seed_mode_argument(parser)
    parser.add_argument(
        "--hops-prop",
        default=None,
        help="Override hops property (reachability/direct).",
    )
    parser.add_argument(
        "--par-prop",
        default=None,
        help="Override PAR property (par target).",
    )
    parser.add_argument(
        "--min-papers",
        type=int,
        default=1,
        help="Minimum papers per source.",
    )
    parser.add_argument(
        "--num-partitions",
        type=int,
        default=1,
        help="Partition count for aggregations that support partitions.",
    )
    parser.add_argument(
        "--partition-id",
        type=int,
        default=0,
        help="Single partition id when num-partitions=1.",
    )
    parser.add_argument(
        "--depth-thresholds",
        default="",
        help="Comma-separated depth thresholds (reachability only).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path (default under ref_map/aggregation/output).",
    )
    parser.add_argument(
        "--article-only",
        action="store_true",
        help="Restrict aggregation input to works with type='article'.",
    )
    return parser.parse_args()


def _resolve_graph_context(
    mode: str, prefix: str, hops_override: str | None
) -> tuple[str, str, str]:
    is_test = mode == "test"
    if is_test:
        label = os.environ.get("ANALYTICS_LABEL", "WorkTest")
        rel_type = os.environ.get("ANALYTICS_REL_TYPE", "CITES_TEST")
        default_hops = hops_prop_for_mode(prefix, is_test=True)
    else:
        label = "Work"
        rel_type = "CITES"
        default_hops = hops_prop_for_mode(prefix, is_test=False)
    hops_prop = hops_override or os.environ.get("ANALYTICS_HOPS_PROP", default_hops)
    return label, rel_type, hops_prop


def run_source_aggregation(
    *,
    target: str,
    mode: str,
    seed_mode: str = DEFAULT_SEED_MODE,
    hops_prop: str | None = None,
    par_prop: str | None = None,
    min_papers: int = 1,
    num_partitions: int = 1,
    partition_id: int = 0,
    depth_thresholds: list[int] | None = None,
    output: str | Path | None = None,
    article_only: bool = False,
) -> Path:
    seed_mode = resolve_seed_mode(seed_mode)
    prefix = prefix_for_mode(seed_mode)
    output_token = output_token_for_mode(seed_mode)
    seed_prop = seed_prop_for_mode(seed_mode)
    label, rel_type, resolved_hops_prop = _resolve_graph_context(mode, prefix, hops_prop)
    resolved_par_prop = par_prop or par_prop_for_mode(prefix)
    depth_thresholds = sorted({d for d in (depth_thresholds or []) if d >= 0})

    if num_partitions < 1:
        raise ValueError("--num-partitions must be >= 1")
    if not (0 <= partition_id < num_partitions):
        raise ValueError("--partition-id must be in [0, num-partitions)")
    if target == "direct" and num_partitions > 1:
        raise ValueError("--num-partitions is not supported for --target direct")
    if target != "reachability" and depth_thresholds:
        raise ValueError(
            "--depth-thresholds is only supported for --target reachability"
        )

    if output:
        output_path = Path(output)
    else:
        article_suffix = "_articles" if article_only else ""
        if target == "reachability":
            output_path = (
                DEFAULT_OUTPUT_DIR
                / f"source_aggregate_{output_token}_{mode}{article_suffix}.json"
            )
        elif target == "direct":
            output_path = (
                DEFAULT_OUTPUT_DIR
                / f"source_direct_{output_token}_{mode}{article_suffix}.json"
            )
        else:
            par_variant = _par_variant_suffix(resolved_par_prop, par_prop_for_mode(prefix))
            output_path = (
                DEFAULT_OUTPUT_DIR
                / f"source_par_{output_token}_{mode}{par_variant}{article_suffix}.json"
            )

    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    print(f"[config] Target: {target}, mode: {mode}")
    print(f"[config] Seed mode: {seed_mode}, prefix: {prefix}")
    print(f"[config] Label: {label}")
    print(f"[config] Hops property: {resolved_hops_prop}")
    if target == "par":
        print(f"[config] PAR property: {resolved_par_prop}")
    print(f"[config] Article only: {article_only}")
    if depth_thresholds:
        print(f"[config] Depth thresholds: {depth_thresholds}")
    print(f"[config] Output: {output_path}")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        if target == "direct":
            with driver.session(database=NEO4J_DATABASE) as session:
                results = compute_source_direct(
                    session,
                    label,
                    rel_type,
                    seed_prop,
                    min_papers=min_papers,
                    article_only=article_only,
                )
            save_reachability_results(
                results,
                output_path,
                mode=f"direct-{mode}",
            )
            return output_path

        if target == "reachability":
            def _run_reachability_partition(partition_id: int, part_path: Path) -> Path:
                with driver.session(database=NEO4J_DATABASE) as session:
                    results = compute_reachability_source_aggregation(
                        session,
                        label,
                        resolved_hops_prop,
                        min_papers=min_papers,
                        num_partitions=num_partitions,
                        partition_id=partition_id,
                        depth_thresholds=depth_thresholds,
                        article_only=article_only,
                    )
                save_reachability_results(results, part_path, mode=f"aggregate-{mode}")
                return part_path

            if num_partitions > 1:
                output_dir = output_path.parent
                merged_name = output_path.stem
                with ThreadPoolExecutor(max_workers=num_partitions) as executor:
                    futures = {
                        executor.submit(
                            _run_reachability_partition,
                            pid,
                            output_dir
                            / f"{merged_name}_{REACH_PART_PREFIX}{pid}.json",
                        ): pid
                        for pid in range(num_partitions)
                    }
                    for future in as_completed(futures):
                        future.result()
                merged_path = merge_reachability_partitions(
                    output_dir,
                    merged_name,
                    num_partitions,
                )
                print(f"[source] Merged output written to: {merged_path}")
            else:
                _run_reachability_partition(partition_id, output_path)
            return output_path

        # target == "par"
        def _run_par_partition(partition_id: int, part_path: Path) -> Path:
            with driver.session(database=NEO4J_DATABASE) as session:
                results = compute_par_source_aggregation(
                    session,
                    label,
                    resolved_par_prop,
                    min_papers=min_papers,
                    num_partitions=num_partitions,
                    partition_id=partition_id,
                    article_only=article_only,
                )
            save_par_results(results, part_path)
            return part_path

        if num_partitions > 1:
            output_dir = output_path.parent
            merged_name = output_path.stem
            with ThreadPoolExecutor(max_workers=num_partitions) as executor:
                futures = {
                    executor.submit(
                        _run_par_partition,
                        pid,
                        output_dir / f"{merged_name}_{PAR_PART_PREFIX}{pid}.json",
                    ): pid
                    for pid in range(num_partitions)
                }
                for future in as_completed(futures):
                    future.result()
            merged_path = merge_par_partitions(
                output_dir,
                merged_name,
                num_partitions,
            )
            print(f"[par-agg] Merged output written to: {merged_path}")
        else:
            _run_par_partition(partition_id, output_path)
        return output_path
    finally:
        driver.close()


def main() -> None:
    args = _parse_args()
    depth_thresholds = _parse_depth_thresholds(args.depth_thresholds)
    run_source_aggregation(
        target=args.target,
        mode=args.mode,
        seed_mode=args.seed_mode,
        hops_prop=args.hops_prop,
        par_prop=args.par_prop,
        min_papers=args.min_papers,
        num_partitions=args.num_partitions,
        partition_id=args.partition_id,
        depth_thresholds=depth_thresholds,
        output=args.output,
        article_only=args.article_only,
    )


if __name__ == "__main__":
    main()
