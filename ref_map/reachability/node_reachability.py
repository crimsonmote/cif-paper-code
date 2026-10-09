"""CLI entry point for node reachability computation."""

from __future__ import annotations

import argparse
import os

from neo4j import GraphDatabase

from ref_map.export.neo4j_export import _resolve_neo4j_config
from ref_map.common.config import (
    NEO4J_DATABASE,
    add_seed_mode_argument,
    hops_prop_for_mode,
    prefix_for_mode,
    reach_prop_for_mode,
    reachability_graph_for_mode,
    resolve_seed_mode,
    seed_prop_for_mode,
)
from ref_map.reachability import compute as reach_compute
from ref_map.reachability.compute import (
    BATCH_SIZE,
    compute_reachability_cypher,
    compute_reachability_gds,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute node reachability to seed-linked papers in citation graphs."
    )
    parser.add_argument(
        "--mode",
        choices=[
            "cypher-test",
            "cypher-full",
            "gds-test",
            "gds-full",
        ],
        required=True,
        help=(
            "Reachability mode: cypher (iterative BFS, recommended) "
            "or gds (Delta-Stepping, slow streaming)"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Batch size (default: {BATCH_SIZE})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help=(
            "Number of parallel workers for Cypher mode (default: 1=sequential, "
            "recommended: 8-16 for full graph)"
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug output (validation, distribution stats)",
    )
    add_seed_mode_argument(parser)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    if args.debug:
        print("[config] Debug mode enabled")
    if args.workers > 1 and args.mode.startswith("cypher"):
        print(f"[config] Parallel mode: {args.workers} workers")

    driver = GraphDatabase.driver(uri, auth=(user, password))

    try:
        seed_mode = resolve_seed_mode(args.seed_mode)
        seed_prop = seed_prop_for_mode(seed_mode)
        prefix = prefix_for_mode(seed_mode)
        if "test" in args.mode:
            label = os.environ.get("ANALYTICS_LABEL", "WorkTest")
            rel_type = os.environ.get("ANALYTICS_REL_TYPE", "CITES_TEST")
            default_reach = reach_prop_for_mode(prefix, is_test=True)
            default_hops = hops_prop_for_mode(prefix, is_test=True)
            reach_prop = os.environ.get("ANALYTICS_REACH_PROP", default_reach)
            hops_prop = os.environ.get("ANALYTICS_HOPS_PROP", default_hops)
        else:  # full
            label = "Work"
            rel_type = "CITES"
            default_reach = reach_prop_for_mode(prefix, is_test=False)
            default_hops = hops_prop_for_mode(prefix, is_test=False)
            reach_prop = os.environ.get("ANALYTICS_REACH_PROP", default_reach)
            hops_prop = os.environ.get("ANALYTICS_HOPS_PROP", default_hops)

        default_graph, default_anchor_label, default_anchor_name = (
            reachability_graph_for_mode(prefix)
        )
        graph_name = os.environ.get("ANALYTICS_GDS_GRAPH", default_graph)
        anchor_label = os.environ.get("ANALYTICS_ANCHOR_LABEL", default_anchor_label)
        anchor_name = os.environ.get("ANALYTICS_ANCHOR_NAME", default_anchor_name)
        print(
            "[config] Seed mode: "
            f"{seed_mode}, seed prop: {seed_prop}, prefix: {prefix}"
        )
        print(
            "[config] Props: "
            f"reach={reach_prop}, hops={hops_prop}, gds_graph={graph_name}"
        )

        reach_compute.DEBUG = args.debug

        if args.mode.startswith("cypher"):
            compute_reachability_cypher(
                driver,
                label,
                rel_type,
                reach_prop,
                hops_prop,
                args.batch_size,
                num_workers=args.workers,
                seed_prop=seed_prop,
            )
        elif args.mode.startswith("gds"):
            if args.workers > 1:
                print("[config] WARNING: --workers flag is ignored in GDS mode")
            compute_reachability_gds(
                driver,
                label,
                rel_type,
                reach_prop,
                hops_prop,
                graph_name,
                anchor_label,
                anchor_name,
                seed_prop,
                batch_size=args.batch_size,
            )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
