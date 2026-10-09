"""Run true-seed and three matched-placebo ArticleRank arms on one graph."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Any

from neo4j import GraphDatabase

from ref_map.centrality.par_core import (
    damping_suffix,
    ensure_index,
    get_seed_neo4j_ids,
    run_article_rank_on_graph,
)
from ref_map.common.config import NEO4J_DATABASE
from ref_map.common.gds_run_guard import (
    assert_gds_graph_exists,
    assert_no_active_gds_jobs,
    exclusive_run_lock,
)
from ref_map.common.graph_utils import drop_gds_graph
from ref_map.common.neo4j_tokens import (
    validate_property_key,
    validate_property_token,
)
from ref_map.common.projection import project_gds_graph
from ref_map.export.shared import _resolve_neo4j_config
from ref_map.export.sparse_work_properties import clear_work_properties

DEFAULT_DAMPING_FACTOR = 0.85
DEFAULT_MEMBER_PROP = "m5_union_member"
DEFAULT_GRAPH_PREFIX = "m5_union_par_work"
DEFAULT_RUN_LOCK = Path("/tmp/ref_map_m5_placebo_par_suite.lock")
DEFAULT_META_PATH = Path("ref_map/output/m5_placebo/m5_par_suite_meta.json")


@dataclass(frozen=True)
class ParArm:
    name: str
    seed_prop: str
    output_base: str


ARMS = (
    ParArm("true", "matt_marx_paper_patent_startup", "m5u_startup_par"),
    ParArm("placebo_r1", "placebo_seed_r1", "m5u_placebo_par_r1"),
    ParArm("placebo_r2", "placebo_seed_r2", "m5u_placebo_par_r2"),
    ParArm("placebo_r3", "placebo_seed_r3", "m5u_placebo_par_r3"),
)


def output_property(arm: ParArm, damping_factor: float) -> str:
    if not 0.0 < damping_factor < 1.0:
        raise ValueError("damping_factor must be in (0, 1)")
    return f"{arm.output_base}_{damping_suffix(damping_factor)}"


def expected_output_properties(damping_factor: float = DEFAULT_DAMPING_FACTOR) -> tuple[str, ...]:
    return tuple(output_property(arm, damping_factor) for arm in ARMS)


def _count(session, query: str) -> int:
    record = session.run(query).single()
    return int(record["count"] if record else 0)


def _completed_output_count(session, *, member_prop: str, output_prop: str) -> int:
    member = validate_property_token(member_prop)
    output = validate_property_key(output_prop)
    return _count(
        session,
        f"MATCH (w:Work) WHERE w.`{member}` >= 0 "
        f"AND w.`{output}` IS NOT NULL RETURN count(w) AS count",
    )


def _output_scope_counts(
    session,
    *,
    member_prop: str,
    output_prop: str,
) -> tuple[int, int]:
    member = validate_property_token(member_prop)
    output = validate_property_key(output_prop)
    record = session.run(
        f"""
        MATCH (w:Work)
        WHERE w.`{output}` IS NOT NULL
        RETURN count(w) AS present,
               sum(CASE WHEN w.`{member}` >= 0 THEN 0 ELSE 1 END) AS outside
        """
    ).single()
    return (
        int(record["present"] if record else 0),
        int(record["outside"] if record and record["outside"] else 0),
    )


def verify_m5_contract(
    session,
    *,
    member_prop: str,
    arms: tuple[ParArm, ...] = ARMS,
) -> dict[str, int]:
    member = validate_property_token(member_prop)
    member_count = _count(
        session,
        f"MATCH (w:Work) WHERE w.`{member}` >= 0 RETURN count(w) AS count",
    )
    if member_count == 0:
        raise RuntimeError(f"M5 membership property {member!r} is empty")
    stats = {"union_nodes": member_count}
    for arm in arms:
        seed_prop = validate_property_token(arm.seed_prop)
        seed_count = _count(
            session,
            f"MATCH (w:Work) WHERE w.`{seed_prop}` = true "
            "RETURN count(w) AS count",
        )
        outside = _count(
            session,
            f"MATCH (w:Work) WHERE w.`{seed_prop}` = true "
            f"AND coalesce(w.`{member}`, -1) < 0 RETURN count(w) AS count",
        )
        if seed_count == 0:
            raise RuntimeError(f"M5 arm {arm.name!r} has no seed nodes")
        if outside:
            raise RuntimeError(
                f"M5 arm {arm.name!r} has {outside:,} seeds outside "
                f"{member!r}; the shared projection would be invalid."
            )
        stats[f"{arm.name}_seeds"] = seed_count
        stats[f"{arm.name}_outside_union"] = outside
    return stats


def _write_meta(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def run_m5_placebo_par_suite(
    *,
    damping_factor: float = DEFAULT_DAMPING_FACTOR,
    member_prop: str = DEFAULT_MEMBER_PROP,
    graph_name: str | None = None,
    max_iterations: int | None = None,
    tolerance: float | None = None,
    resume: bool = False,
    allow_concurrent_gds: bool = False,
    clear_stale_outputs: bool = False,
    meta_path: str | Path = DEFAULT_META_PATH,
) -> dict[str, Any]:
    member_prop = validate_property_token(member_prop)
    graph_name = validate_property_token(
        graph_name or f"{DEFAULT_GRAPH_PREFIX}_{os.getpid()}"
    )
    output_props = expected_output_properties(damping_factor)
    uri, user, password = _resolve_neo4j_config()
    destination = Path(meta_path)
    started = time.monotonic()
    report: dict[str, Any] = {
        "status": "running",
        "database": NEO4J_DATABASE,
        "member_property": member_prop,
        "graph_name": graph_name,
        "damping_factor": damping_factor,
        "scaler": None,
        "arms": {},
    }
    _write_meta(destination, report)
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    print(f"[config] M5 membership: {member_prop}")
    print(f"[config] Shared GDS graph: {graph_name}")
    print(f"[config] Damping factor: {damping_factor:.2f}; scaler: none")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            if not allow_concurrent_gds:
                print("[m5-par] Checking for other active GDS jobs...")
                assert_no_active_gds_jobs(session)
            stats = verify_m5_contract(session, member_prop=member_prop)
            report["contract"] = stats
            union_count = stats["union_nodes"]
            pending: list[tuple[ParArm, str]] = []
            for arm, prop in zip(ARMS, output_props, strict=True):
                present, outside = _output_scope_counts(
                    session,
                    member_prop=member_prop,
                    output_prop=prop,
                )
                if outside:
                    if not clear_stale_outputs:
                        raise RuntimeError(
                            f"Output property {prop!r} has {outside:,} values "
                            f"outside the current {member_prop!r} universe. "
                            "Pass --clear-stale-outputs to remove the old "
                            "property in bounded batches before recomputing."
                        )
                    cleared = clear_work_properties(
                        driver,
                        props=(prop,),
                        use_apoc=True,
                    )
                    print(
                        f"[m5-par] Cleared {cleared:,} stale {prop} values "
                        "before recomputation."
                    )
                    present = 0
                completed = _completed_output_count(
                    session,
                    member_prop=member_prop,
                    output_prop=prop,
                )
                report["arms"][arm.name] = {
                    "seed_property": arm.seed_prop,
                    "output_property": prop,
                    "present_before": present,
                    "outside_union_before": outside,
                }
                if resume and completed == union_count:
                    print(
                        f"[m5-par] Resume: skipping {arm.name}; {prop} is "
                        f"complete on {completed:,} union nodes."
                    )
                    report["arms"][arm.name].update(
                        {
                            "status": "skipped_complete",
                            "node_properties_written": completed,
                        }
                    )
                else:
                    if resume and completed:
                        print(
                            f"[m5-par] Resume: {arm.name} is incomplete "
                            f"({completed:,}/{union_count:,}); recomputing."
                        )
                    pending.append((arm, prop))
            _write_meta(destination, report)
            if not pending:
                report["status"] = "complete"
                report["seconds"] = round(time.monotonic() - started, 3)
                _write_meta(destination, report)
                return report

            project_gds_graph(session, graph_name, "Work", "CITES", member_prop)
            try:
                assert_gds_graph_exists(
                    session,
                    graph_name,
                    stage="immediately after projection",
                )
                for index, (arm, prop) in enumerate(pending, start=1):
                    assert_gds_graph_exists(
                        session,
                        graph_name,
                        stage=f"before M5 arm {arm.name!r}",
                    )
                    source_ids = get_seed_neo4j_ids(
                        session,
                        "Work",
                        member_prop,
                        arm.seed_prop,
                    )
                    expected_seeds = stats[f"{arm.name}_seeds"]
                    if len(source_ids) != expected_seeds:
                        raise RuntimeError(
                            f"M5 arm {arm.name!r} source retrieval returned "
                            f"{len(source_ids):,}/{expected_seeds:,} seeds"
                        )
                    print(
                        f"[m5-par] Arm {index}/{len(pending)}: {arm.name}, "
                        f"seeds={len(source_ids):,}, write={prop}"
                    )
                    ensure_index(session, "Work", prop)
                    arm_started = time.monotonic()
                    result = run_article_rank_on_graph(
                        session,
                        graph_name,
                        prop,
                        source_ids,
                        max_iterations=max_iterations,
                        tolerance=tolerance,
                        damping_factor=damping_factor,
                        scaler=None,
                    )
                    completed = _completed_output_count(
                        session,
                        member_prop=member_prop,
                        output_prop=prop,
                    )
                    if completed != union_count:
                        raise RuntimeError(
                            f"M5 arm {arm.name!r} wrote {completed:,}/"
                            f"{union_count:,} union-node properties"
                        )
                    report["arms"][arm.name].update(
                        {
                            "seed_count": len(source_ids),
                            "status": "complete",
                            **result,
                            "verified_output_count": completed,
                            "seconds": round(time.monotonic() - arm_started, 3),
                        }
                    )
                    _write_meta(destination, report)
            finally:
                drop_gds_graph(session, graph_name)
        report["status"] = "complete"
        report["seconds"] = round(time.monotonic() - started, 3)
        _write_meta(destination, report)
        print(f"[m5-par] Complete in {report['seconds']:.1f}s; meta: {destination}")
        return report
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report["seconds"] = round(time.monotonic() - started, 3)
        _write_meta(destination, report)
        raise
    finally:
        driver.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run four M5 ArticleRank arms on one union projection."
    )
    parser.add_argument("--damping-factor", type=float, default=DEFAULT_DAMPING_FACTOR)
    parser.add_argument("--member-prop", default=DEFAULT_MEMBER_PROP)
    parser.add_argument("--graph-name", default=None)
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--tolerance", type=float, default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--allow-concurrent-gds", action="store_true")
    parser.add_argument(
        "--clear-stale-outputs",
        action="store_true",
        help="Clear M5 output values outside the current union before rerunning.",
    )
    parser.add_argument("--meta", default=str(DEFAULT_META_PATH))
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _print_plan(args: argparse.Namespace) -> None:
    graph_name = args.graph_name or f"{DEFAULT_GRAPH_PREFIX}_<pid>"
    print(f"[m5-par] 01. Verify {args.member_prop} and all four seed sets")
    print(f"[m5-par] 02. Project one shared graph: {graph_name}")
    for index, (arm, prop) in enumerate(
        zip(ARMS, expected_output_properties(args.damping_factor), strict=True),
        start=3,
    ):
        print(f"[m5-par] {index:02d}. {arm.seed_prop} -> {prop}")
    print(f"[m5-par] {len(ARMS) + 3:02d}. Drop shared graph")


def main() -> None:
    args = _parse_args()
    _print_plan(args)
    if args.dry_run:
        print("[m5-par] Dry run only; Neo4j was not modified.")
        return
    with exclusive_run_lock(DEFAULT_RUN_LOCK, run_name="M5 placebo PAR suite"):
        run_m5_placebo_par_suite(
            damping_factor=args.damping_factor,
            member_prop=args.member_prop,
            graph_name=args.graph_name,
            max_iterations=args.max_iterations,
            tolerance=args.tolerance,
            resume=args.resume,
            allow_concurrent_gds=args.allow_concurrent_gds,
            clear_stale_outputs=args.clear_stale_outputs,
            meta_path=args.meta,
        )


if __name__ == "__main__":
    main()
