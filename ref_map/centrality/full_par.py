"""Full-pass seed-personalized ArticleRank entrypoint."""

from __future__ import annotations

import argparse
import os

from neo4j import GraphDatabase

from ref_map.centrality.par_core import (
    compose_write_prop,
    damping_suffix,
    ensure_index,
    get_seed_neo4j_ids,
    run_article_rank_on_graph,
)
from ref_map.common.config import (
    NEO4J_DATABASE,
    add_seed_mode_argument,
    hops_prop_for_mode,
    par_prop_for_mode,
    prefix_for_mode,
    resolve_seed_mode,
    seed_prop_for_mode,
)
from ref_map.common.graph_utils import drop_gds_graph
from ref_map.common.projection import project_gds_graph
from ref_map.export.neo4j_export import _resolve_neo4j_config


def _parse_damping_factors(raw: str | None) -> list[float]:
    if not raw:
        return []
    values: list[float] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            val = float(chunk)
        except ValueError as exc:
            raise ValueError(f"Invalid damping factor: {chunk}") from exc
        if not (0.0 < val < 1.0):
            raise ValueError(f"Damping factor must be in (0,1): {val}")
        values.append(val)
    return values


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Full-pass seed-personalized ArticleRank using GDS."
    )
    parser.add_argument(
        "--mode",
        choices=["test", "full"],
        required=True,
        help="Run mode: test or full",
    )
    parser.add_argument(
        "--write-prop",
        default=None,
        help="Property base name to write (default: derived from the seed registry)",
    )
    add_seed_mode_argument(parser)
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Max iterations for ArticleRank (default: GDS default)",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=None,
        help="Convergence tolerance for ArticleRank (default: GDS default)",
    )
    parser.add_argument(
        "--damping-factor",
        type=float,
        default=None,
        help="Single damping factor for ArticleRank (default: GDS default)",
    )
    parser.add_argument(
        "--damping-factors",
        default=None,
        help="Comma-separated damping factors to run as a grid (e.g., 0.85,0.90,0.95).",
    )
    parser.add_argument(
        "--scale",
        action="store_true",
        help="Apply StdScore scaling to the written property (deprecated; use --scaler)",
    )
    parser.add_argument(
        "--scaler",
        choices=["MinMax", "Max", "Mean", "Log", "Center", "StdScore"],
        default=None,
        help="Apply a GDS scaler to the written property (default: none)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    seed_mode = resolve_seed_mode(args.seed_mode)
    seed_prop = seed_prop_for_mode(seed_mode)
    prefix = prefix_for_mode(seed_mode)
    damping_factors = _parse_damping_factors(args.damping_factors)

    if args.write_prop is None:
        args.write_prop = par_prop_for_mode(prefix)

    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    print(f"[config] Mode: {args.mode}")
    print(f"[config] Seed mode: {seed_mode}, seed prop: {seed_prop}, prefix: {prefix}")
    print(f"[config] Write property (base): {args.write_prop}")
    if damping_factors:
        print(
            f"[config] Damping factors: {', '.join(damping_suffix(v) for v in damping_factors)}"
        )

    if args.mode == "test":
        label = os.environ.get("ANALYTICS_LABEL", "WorkTest")
        rel_type = os.environ.get("ANALYTICS_REL_TYPE", "CITES_TEST")
        default_hops = hops_prop_for_mode(prefix, is_test=True)
        hops_prop = os.environ.get("ANALYTICS_HOPS_PROP", default_hops)
    else:
        label = "Work"
        rel_type = "CITES"
        default_hops = hops_prop_for_mode(prefix, is_test=False)
        hops_prop = os.environ.get("ANALYTICS_HOPS_PROP", default_hops)
    graph_name = f"par_{prefix}_{label.lower()}"
    print(f"[config] Hops property: {hops_prop}")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        scaler = args.scaler
        if args.scale and scaler is None:
            scaler = "StdScore"
        elif args.scale and scaler is not None and scaler != "StdScore":
            raise ValueError("--scale cannot be used with --scaler (unless StdScore)")

        if damping_factors and args.damping_factor is not None:
            print(
                "[warn] --damping-factor ignored because --damping-factors was provided."
            )

        with driver.session(database=NEO4J_DATABASE) as session:
            print("[config] Snapshot mode: full pass")
            print(
                "[config] Base write property: "
                f"{compose_write_prop(args.write_prop)}"
            )

            project_gds_graph(
                session,
                graph_name,
                label,
                rel_type,
                hops_prop,
            )
            try:
                source_ids = get_seed_neo4j_ids(
                    session,
                    label,
                    hops_prop,
                    seed_prop,
                )
                print(f"[par] Seeds in graph: {len(source_ids):,}")
                if not source_ids:
                    raise ValueError("No seed nodes found for personalization.")

                run_dampings: list[float | None]
                if damping_factors:
                    run_dampings = damping_factors
                elif args.damping_factor is not None:
                    run_dampings = [args.damping_factor]
                else:
                    run_dampings = [None]

                for dmp in run_dampings:
                    write_prop = compose_write_prop(
                        args.write_prop,
                        lag_years=None,
                        damping_factor=dmp,
                    )
                    print(f"[par] Ensuring index on :{label}({write_prop})...")
                    ensure_index(session, label, write_prop)
                    if dmp is not None:
                        print(f"[par] Damping factor: {damping_suffix(dmp)}")

                    run_article_rank_on_graph(
                        session,
                        graph_name,
                        write_prop,
                        source_ids,
                        max_iterations=args.max_iterations,
                        tolerance=args.tolerance,
                        damping_factor=dmp,
                        scaler=scaler,
                    )
            finally:
                drop_gds_graph(session, graph_name)
    finally:
        driver.close()


if __name__ == "__main__":
    main()
