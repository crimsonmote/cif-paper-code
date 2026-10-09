"""Patch canonical Founding Patents seeds and run CIF on one common graph."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from neo4j import GraphDatabase

from ref_map.centrality.par_core import (
    compose_write_prop,
    ensure_index,
    get_seed_neo4j_ids,
    run_article_rank_on_graph,
)
from ref_map.common.config import (
    CANONICAL_FOUNDING_PATENT_MODES,
    NEO4J_DATABASE,
    par_prop_for_mode,
    prefix_for_mode,
    resolve_seed_mode,
    seed_prop_for_mode,
)
from ref_map.common.graph_utils import drop_gds_graph
from ref_map.common.gds_run_guard import (
    assert_gds_graph_exists,
    assert_no_active_gds_jobs,
    exclusive_run_lock,
)
from ref_map.common.projection import project_gds_graph
from ref_map.common.neo4j_tokens import (
    validate_property_key,
    validate_property_token,
)
from ref_map.export.neo4j_export import _resolve_neo4j_config

DEFAULT_DAMPING_FACTOR = 0.85
DEFAULT_COMMON_HOPS_PROP = "startup_min_hops"
DEFAULT_GRAPH_NAME_PREFIX = "par_canonical_seed_suite_work"
DEFAULT_RUN_LOCK = Path("/tmp/ref_map_founding_patent_cif_suite.lock")
LEGACY_BROAD_SEED_PROP = "matt_marx_paper_patent_startup"


def _parse_seed_modes(raw: str) -> tuple[str, ...]:
    modes: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        mode = resolve_seed_mode(token)
        if mode not in CANONICAL_FOUNDING_PATENT_MODES:
            choices = ", ".join(CANONICAL_FOUNDING_PATENT_MODES)
            raise ValueError(
                f"Suite mode {mode!r} is not a canonical Founding Patents "
                f"mode; choose from: {choices}"
            )
        if mode not in modes:
            modes.append(mode)
    if not modes:
        raise ValueError("At least one canonical seed mode is required")
    return tuple(modes)


def _default_graph_name(pid: int | None = None) -> str:
    return f"{DEFAULT_GRAPH_NAME_PREFIX}_{pid if pid is not None else os.getpid()}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Patch canonical Founding Patents seeds, project one common broad "
            "reachable graph, and run full-pass CIF for each seed universe."
        )
    )
    parser.add_argument(
        "--seed-modes",
        default=",".join(CANONICAL_FOUNDING_PATENT_MODES),
        help=(
            "Comma-separated canonical modes. Default: "
            + ",".join(CANONICAL_FOUNDING_PATENT_MODES)
        ),
    )
    parser.add_argument(
        "--damping-factor",
        type=float,
        default=DEFAULT_DAMPING_FACTOR,
        help=f"ArticleRank damping factor (default: {DEFAULT_DAMPING_FACTOR}).",
    )
    parser.add_argument(
        "--common-hops-prop",
        default=DEFAULT_COMMON_HOPS_PROP,
        help=(
            "Existing broad-seed hops property used to define the common graph "
            f"(default: {DEFAULT_COMMON_HOPS_PROP})."
        ),
    )
    parser.add_argument(
        "--graph-name",
        default=None,
        help=(
            "Temporary GDS graph name. Default: a process-unique name derived "
            f"from {DEFAULT_GRAPH_NAME_PREFIX!r}."
        ),
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=None,
        help="Override GDS ArticleRank max iterations.",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=None,
        help="Override GDS ArticleRank convergence tolerance.",
    )
    parser.add_argument(
        "--scaler",
        choices=["MinMax", "Max", "Mean", "Log", "Center", "StdScore"],
        default=None,
        help="Optional GDS score scaler (default: none).",
    )
    parser.add_argument(
        "--skip-patch",
        action="store_true",
        help="Skip the Founding Patents seed-property patch.",
    )
    parser.add_argument(
        "--allow-missing-common-seeds",
        action="store_true",
        help=(
            "Allow requested seeds outside the existing common closure to be "
            "omitted from personalization. Strict containment is the default."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Skip a seed mode when its output property is already present on "
            "every node in the common reachable closure."
        ),
    )
    parser.add_argument(
        "--allow-concurrent-gds",
        action="store_true",
        help=(
            "Allow projection while another GDS job is active. By default the "
            "suite stops to avoid catalog races and competing large writes."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print and validate the plan without modifying Neo4j.",
    )
    return parser.parse_args()


def expected_output_properties(
    seed_modes: tuple[str, ...],
    damping_factor: float,
) -> tuple[str, ...]:
    if not 0.0 < damping_factor < 1.0:
        raise ValueError("damping_factor must be in (0, 1)")
    return tuple(
        compose_write_prop(
            par_prop_for_mode(prefix_for_mode(seed_mode)),
            damping_factor=damping_factor,
        )
        for seed_mode in seed_modes
    )


def _patch_command() -> tuple[str, ...]:
    return (
        sys.executable,
        "-m",
        "ref_map.export.founding_patent_seed_patch",
    )


def _run_patch() -> None:
    command = _patch_command()
    print(f"[suite] Running: {shlex.join(command)}", flush=True)
    subprocess.run(command, check=True)


def _count(session, query: str) -> int:
    record = session.run(query).single()
    return int(record["count"] if record else 0)


def _completed_output_count(
    session,
    *,
    common_hops_prop: str,
    output_prop: str,
) -> int:
    hops_prop = validate_property_token(common_hops_prop)
    prop = validate_property_key(output_prop)
    return _count(
        session,
        f"MATCH (w:Work) WHERE w.`{hops_prop}` >= 0 "
        f"AND w.`{prop}` IS NOT NULL RETURN count(w) AS count",
    )


def verify_common_graph_contract(
    session,
    *,
    common_hops_prop: str,
    seed_modes: tuple[str, ...],
    allow_missing_common_seeds: bool = False,
) -> dict[str, int]:
    """Verify that the legacy broad closure contains every requested seed.

    The canonical and legacy broad seed sets need not be identical. If a new
    canonical seed is already reachable anywhere in the legacy closure, every
    node downstream of that seed is already in the closure as well. What would
    make reuse unsafe is a requested seed outside the common graph.
    """
    hops_prop = validate_property_token(common_hops_prop)
    canonical_broad_prop = seed_prop_for_mode("all-assignees")

    canonical_broad = _count(
        session,
        f"MATCH (w:Work) WHERE w.`{canonical_broad_prop}` = true "
        "RETURN count(w) AS count",
    )
    legacy_broad = _count(
        session,
        f"MATCH (w:Work) WHERE w.`{LEGACY_BROAD_SEED_PROP}` = true "
        "RETURN count(w) AS count",
    )
    canonical_only = _count(
        session,
        f"MATCH (w:Work) WHERE w.`{canonical_broad_prop}` = true "
        f"AND coalesce(w.`{LEGACY_BROAD_SEED_PROP}`, false) <> true "
        "RETURN count(w) AS count",
    )
    legacy_only = _count(
        session,
        f"MATCH (w:Work) WHERE w.`{LEGACY_BROAD_SEED_PROP}` = true "
        f"AND coalesce(w.`{canonical_broad_prop}`, false) <> true "
        "RETURN count(w) AS count",
    )
    if canonical_broad == 0:
        raise RuntimeError("Canonical all-assignees seed property is empty")

    reachable = _count(
        session,
        f"MATCH (w:Work) WHERE w.`{hops_prop}` >= 0 RETURN count(w) AS count",
    )
    if reachable == 0:
        raise RuntimeError(
            f"Common hops property {hops_prop!r} has no reachable Work nodes"
        )

    stats = {
        "canonical_broad_seeds": canonical_broad,
        "legacy_broad_seeds": legacy_broad,
        "canonical_only_seeds": canonical_only,
        "legacy_only_seeds": legacy_only,
        "common_reachable_nodes": reachable,
    }
    for seed_mode in seed_modes:
        seed_prop = seed_prop_for_mode(seed_mode)
        seed_count = _count(
            session,
            f"MATCH (w:Work) WHERE w.`{seed_prop}` = true "
            "RETURN count(w) AS count",
        )
        outside_common_graph = _count(
            session,
            f"MATCH (w:Work) WHERE w.`{seed_prop}` = true "
            f"AND coalesce(w.`{hops_prop}`, -1) < 0 "
            "RETURN count(w) AS count",
        )
        if seed_count == 0:
            raise RuntimeError(f"Seed mode {seed_mode!r} has no seed nodes")
        if outside_common_graph and not allow_missing_common_seeds:
            raise RuntimeError(
                f"Seed mode {seed_mode!r} has {outside_common_graph:,} seed "
                f"nodes outside the common {hops_prop!r} closure. Reusing "
                "that graph would omit those seeds and their downstream "
                "citation paths. Pass --allow-missing-common-seeds to accept "
                "and report this approximation explicitly."
            )
        stats[f"{seed_mode}_seeds"] = seed_count
        stats[f"{seed_mode}_missing_common_seeds"] = outside_common_graph
    return stats


def run_common_graph_cif(
    *,
    seed_modes: tuple[str, ...],
    damping_factor: float,
    common_hops_prop: str,
    graph_name: str,
    max_iterations: int | None,
    tolerance: float | None,
    scaler: str | None,
    allow_missing_common_seeds: bool,
    resume: bool = False,
    allow_concurrent_gds: bool = False,
) -> None:
    common_hops_prop = validate_property_token(common_hops_prop)
    graph_name = validate_property_token(graph_name)
    output_props = expected_output_properties(seed_modes, damping_factor)
    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    print(f"[config] Common hops property: {common_hops_prop}")
    print(f"[config] Common GDS graph: {graph_name}")
    print(f"[config] Damping factor: {damping_factor:.2f}")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            if not allow_concurrent_gds:
                print("[suite] Checking for other active GDS jobs...")
                assert_no_active_gds_jobs(session)
            print("[suite] Verifying common-graph seed contract...")
            stats = verify_common_graph_contract(
                session,
                common_hops_prop=common_hops_prop,
                seed_modes=seed_modes,
                allow_missing_common_seeds=allow_missing_common_seeds,
            )
            print(
                "[suite] Contract verified: "
                f"broad_seeds={stats['canonical_broad_seeds']:,}, "
                f"common_reachable={stats['common_reachable_nodes']:,}"
            )
            if (
                stats["canonical_only_seeds"]
                or stats["legacy_only_seeds"]
            ):
                any_missing = any(
                    stats[f"{seed_mode}_missing_common_seeds"]
                    for seed_mode in seed_modes
                )
                qualifier = (
                    "accepted approximation; omitted seeds are reported below"
                    if any_missing
                    else "all requested seeds are inside the common graph"
                )
                print(
                    f"[suite] Broad seed-set delta ({qualifier}): "
                    f"canonical_only={stats['canonical_only_seeds']:,}, "
                    f"legacy_only={stats['legacy_only_seeds']:,}"
                )
            for seed_mode in seed_modes:
                missing = stats[f"{seed_mode}_missing_common_seeds"]
                if missing:
                    total = stats[f"{seed_mode}_seeds"]
                    print(
                        "[suite] WARNING: common-graph approximation for "
                        f"{seed_mode}: omitting {missing:,}/{total:,} seeds "
                        f"({missing / total:.6%}) and any citation paths unique "
                        "to them."
                    )

            pending_runs = list(zip(seed_modes, output_props, strict=True))
            if resume:
                pending_runs = []
                expected_count = stats["common_reachable_nodes"]
                for seed_mode, output_prop in zip(
                    seed_modes,
                    output_props,
                    strict=True,
                ):
                    completed_count = _completed_output_count(
                        session,
                        common_hops_prop=common_hops_prop,
                        output_prop=output_prop,
                    )
                    if completed_count == expected_count:
                        print(
                            f"[suite] Resume: skipping {seed_mode}; "
                            f"{output_prop} is complete on "
                            f"{completed_count:,} common-graph nodes."
                        )
                    else:
                        print(
                            f"[suite] Resume: {seed_mode} is incomplete "
                            f"({completed_count:,}/{expected_count:,}); it "
                            "will be recomputed."
                        )
                        pending_runs.append((seed_mode, output_prop))
                if not pending_runs:
                    print("[suite] All requested CIF outputs are complete.")
                    return

            project_gds_graph(
                session,
                graph_name,
                "Work",
                "CITES",
                common_hops_prop,
            )
            try:
                assert_gds_graph_exists(
                    session,
                    graph_name,
                    stage="immediately after projection",
                )
                for index, (seed_mode, write_prop) in enumerate(
                    pending_runs,
                    start=1,
                ):
                    assert_gds_graph_exists(
                        session,
                        graph_name,
                        stage=f"before CIF mode {seed_mode!r}",
                    )
                    seed_prop = seed_prop_for_mode(seed_mode)
                    source_ids = get_seed_neo4j_ids(
                        session,
                        "Work",
                        common_hops_prop,
                        seed_prop,
                    )
                    print(
                        f"[suite] CIF {index}/{len(pending_runs)}: {seed_mode}, "
                        f"seeds={len(source_ids):,}, write={write_prop}"
                    )
                    if not source_ids:
                        raise RuntimeError(
                            f"No source nodes found for seed mode {seed_mode!r}"
                        )
                    ensure_index(session, "Work", write_prop)
                    run_article_rank_on_graph(
                        session,
                        graph_name,
                        write_prop,
                        source_ids,
                        max_iterations=max_iterations,
                        tolerance=tolerance,
                        damping_factor=damping_factor,
                        scaler=scaler,
                    )
            finally:
                drop_gds_graph(session, graph_name)
    finally:
        driver.close()


def _print_plan(
    *,
    seed_modes: tuple[str, ...],
    output_props: tuple[str, ...],
    common_hops_prop: str,
    graph_name: str,
    skip_patch: bool,
) -> None:
    step = 1
    if not skip_patch:
        print(f"[suite] {step:02d}. Patch canonical seed properties")
        print(f"[suite]     {shlex.join(_patch_command())}")
        step += 1
    print(
        f"[suite] {step:02d}. Verify and project one common graph from "
        f"{common_hops_prop}"
    )
    print(f"[suite]     GDS graph: {graph_name}")
    step += 1
    for seed_mode, output_prop in zip(seed_modes, output_props, strict=True):
        print(f"[suite] {step:02d}. CIF {seed_mode} -> {output_prop}")
        step += 1
    print(f"[suite] {step:02d}. Drop common GDS graph")


def main() -> None:
    args = _parse_args()
    seed_modes = _parse_seed_modes(args.seed_modes)
    common_hops_prop = validate_property_token(args.common_hops_prop)
    graph_name = validate_property_token(args.graph_name or _default_graph_name())
    output_props = expected_output_properties(seed_modes, args.damping_factor)
    _print_plan(
        seed_modes=seed_modes,
        output_props=output_props,
        common_hops_prop=common_hops_prop,
        graph_name=graph_name,
        skip_patch=args.skip_patch,
    )
    if args.dry_run:
        print("[suite] Dry run only; Neo4j was not modified.")
        return

    with exclusive_run_lock(
        DEFAULT_RUN_LOCK,
        run_name="founding-patent CIF suite",
    ):
        started = time.monotonic()
        if not args.skip_patch:
            _run_patch()
        run_common_graph_cif(
            seed_modes=seed_modes,
            damping_factor=args.damping_factor,
            common_hops_prop=common_hops_prop,
            graph_name=graph_name,
            max_iterations=args.max_iterations,
            tolerance=args.tolerance,
            scaler=args.scaler,
            allow_missing_common_seeds=args.allow_missing_common_seeds,
            resume=args.resume,
            allow_concurrent_gds=args.allow_concurrent_gds,
        )
        print(f"[suite] Complete in {time.monotonic() - started:.1f}s")


if __name__ == "__main__":
    main()
