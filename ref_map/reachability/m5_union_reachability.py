"""Prepare the union citation closure used by all four M5 PAR arms."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time
from typing import Any

from neo4j import GraphDatabase

from ref_map.common.config import NEO4J_DATABASE
from ref_map.common.gds_run_guard import exclusive_run_lock
from ref_map.common.neo4j_apoc import apoc_available, run_apoc_query_iterate
from ref_map.common.neo4j_tokens import validate_property_token
from ref_map.export.shared import _resolve_neo4j_config
from ref_map.export.sparse_work_properties import (
    DEFAULT_BATCH_SIZE,
    clear_work_properties,
)
from ref_map.reachability.compute import compute_reachability_cypher

PLACEBO_SEED_PROPS = (
    "placebo_seed_r1",
    "placebo_seed_r2",
    "placebo_seed_r3",
)
TRUE_SEED_PROP = "matt_marx_paper_patent_startup"
DEFAULT_BASE_HOPS_PROP = "startup_min_hops"
DEFAULT_PLACEBO_HOPS_PROP = "placebo_union_min_hops"
DEFAULT_MEMBER_PROP = "m5_union_member"
DEFAULT_META_PATH = Path("ref_map/output/m5_placebo/m5_union_meta.json")
DEFAULT_RUN_LOCK = Path("/tmp/ref_map_m5_union_reachability.lock")


def _count(session, query: str, params: dict[str, Any] | None = None) -> int:
    record = session.run(query, params or {}).single()
    return int(record["count"] if record else 0)


def _property_present_count(session, prop: str) -> int:
    return _count(
        session,
        f"MATCH (w:Work) WHERE w.`{prop}` IS NOT NULL RETURN count(w) AS count",
    )


def _run_materialization_pass(
    session,
    *,
    source_prop: str,
    member_prop: str,
    batch_size: int,
    use_apoc: bool,
) -> int:
    source_query = (
        f"MATCH (w:Work) WHERE w.`{source_prop}` >= 0 "
        f"AND w.`{member_prop}` IS NULL "
        "RETURN elementId(w) AS element_id"
    )
    action_query = (
        "MATCH (w:Work) WHERE elementId(w) = element_id "
        f"SET w.`{member_prop}` = 0"
    )
    if use_apoc:
        summary = run_apoc_query_iterate(
            session,
            source_query=source_query,
            action_query=action_query,
            batch_size=batch_size,
            parallel=True,
            retries=3,
        )
        return int(summary.get("committedOperations") or summary.get("total") or 0)

    changed = 0
    while True:
        record = session.run(
            f"""
            MATCH (w:Work)
            WHERE w.`{source_prop}` >= 0
              AND w.`{member_prop}` IS NULL
            WITH w LIMIT $batch_size
            SET w.`{member_prop}` = 0
            RETURN count(w) AS count
            """,
            {"batch_size": batch_size},
        ).single()
        batch_changed = int(record["count"] if record else 0)
        changed += batch_changed
        if batch_changed == 0:
            break
    return changed


def _membership_differences(
    session,
    *,
    base_hops_prop: str,
    placebo_hops_prop: str,
    member_prop: str,
) -> tuple[int, int]:
    expected = (
        f"(coalesce(w.`{base_hops_prop}`, -1) >= 0 OR "
        f"coalesce(w.`{placebo_hops_prop}`, -1) >= 0)"
    )
    missing = _count(
        session,
        f"MATCH (w:Work) WHERE {expected} "
        f"AND coalesce(w.`{member_prop}`, -1) < 0 RETURN count(w) AS count",
    )
    extra = _count(
        session,
        f"MATCH (w:Work) WHERE w.`{member_prop}` >= 0 "
        f"AND NOT {expected} RETURN count(w) AS count",
    )
    return missing, extra


def prepare_m5_union_reachability(
    driver,
    *,
    workers: int = 8,
    reach_batch_size: int = 5_000,
    write_batch_size: int = DEFAULT_BATCH_SIZE,
    base_hops_prop: str = DEFAULT_BASE_HOPS_PROP,
    placebo_hops_prop: str = DEFAULT_PLACEBO_HOPS_PROP,
    member_prop: str = DEFAULT_MEMBER_PROP,
    max_union_ratio: float = 0.60,
    resume: bool = False,
    force: bool = False,
    use_apoc: bool = True,
    meta_path: str | Path = DEFAULT_META_PATH,
) -> dict[str, Any]:
    base_hops_prop = validate_property_token(base_hops_prop)
    placebo_hops_prop = validate_property_token(placebo_hops_prop)
    member_prop = validate_property_token(member_prop)
    if not 0.0 < max_union_ratio <= 1.0:
        raise ValueError("max_union_ratio must be in (0, 1]")
    started = time.monotonic()
    with driver.session(database=NEO4J_DATABASE) as session:
        total_nodes = _count(session, "MATCH (w:Work) RETURN count(w) AS count")
        base_count = _count(
            session,
            f"MATCH (w:Work) WHERE w.`{base_hops_prop}` >= 0 "
            "RETURN count(w) AS count",
        )
        if base_count == 0:
            raise RuntimeError(f"Base closure {base_hops_prop!r} is empty")
        seed_counts = {
            prop: _count(
                session,
                f"MATCH (w:Work) WHERE w.`{prop}` = true RETURN count(w) AS count",
            )
            for prop in PLACEBO_SEED_PROPS
        }
        if any(count == 0 for count in seed_counts.values()):
            raise RuntimeError(f"One or more placebo seed properties are empty: {seed_counts}")
        hops_present = _property_present_count(session, placebo_hops_prop)

    print(
        f"[m5-union] Base closure: {base_count:,}/{total_nodes:,}; "
        f"placebo seeds: {seed_counts}"
    )
    if hops_present:
        if force:
            cleared = clear_work_properties(
                driver,
                props=(placebo_hops_prop,),
                batch_size=write_batch_size,
                use_apoc=use_apoc,
            )
            print(f"[m5-union] Cleared {placebo_hops_prop} from {cleared:,} nodes.")
        elif not resume:
            raise RuntimeError(
                f"{placebo_hops_prop!r} already exists on {hops_present:,} nodes. "
                "Pass --resume to continue its BFS or --force to recompute it."
            )

    compute_reachability_cypher(
        driver,
        "Work",
        "CITES",
        "",
        placebo_hops_prop,
        reach_batch_size,
        num_workers=workers,
        seed_props=PLACEBO_SEED_PROPS,
    )

    with driver.session(database=NEO4J_DATABASE) as session:
        session.run("CALL db.awaitIndexes(300)").consume()
        placebo_count = _count(
            session,
            f"MATCH (w:Work) WHERE w.`{placebo_hops_prop}` >= 0 "
            "RETURN count(w) AS count",
        )
        member_present = _property_present_count(session, member_prop)
        exact_member = False
        if member_present:
            missing, extra = _membership_differences(
                session,
                base_hops_prop=base_hops_prop,
                placebo_hops_prop=placebo_hops_prop,
                member_prop=member_prop,
            )
            exact_member = missing == 0 and extra == 0
            if exact_member:
                print(f"[m5-union] {member_prop} already has exact union membership.")
            elif not force:
                raise RuntimeError(
                    f"Existing {member_prop!r} is stale (missing={missing:,}, "
                    f"extra={extra:,}). Pass --force to replace it."
                )

    if member_present and not exact_member:
        cleared = clear_work_properties(
            driver,
            props=(member_prop,),
            batch_size=write_batch_size,
            use_apoc=use_apoc,
        )
        print(f"[m5-union] Cleared stale membership from {cleared:,} nodes.")

    if not exact_member:
        with driver.session(database=NEO4J_DATABASE) as session:
            session.run(
                f"CREATE INDEX work_{member_prop}_idx IF NOT EXISTS "
                f"FOR (w:Work) ON (w.`{member_prop}`)"
            ).consume()
            apoc_ready = apoc_available(session)
            if use_apoc and not apoc_ready:
                print("[m5-union] APOC unavailable; using bounded transactions.")
                use_apoc = False
            base_rows = _run_materialization_pass(
                session,
                source_prop=base_hops_prop,
                member_prop=member_prop,
                batch_size=write_batch_size,
                use_apoc=use_apoc,
            )
            placebo_rows = _run_materialization_pass(
                session,
                source_prop=placebo_hops_prop,
                member_prop=member_prop,
                batch_size=write_batch_size,
                use_apoc=use_apoc,
            )
            print(
                f"[m5-union] Materialization passes: base={base_rows:,}, "
                f"placebo={placebo_rows:,}"
            )

    with driver.session(database=NEO4J_DATABASE) as session:
        session.run("CALL db.awaitIndexes(300)").consume()
        union_count = _count(
            session,
            f"MATCH (w:Work) WHERE w.`{member_prop}` >= 0 "
            "RETURN count(w) AS count",
        )
        missing, extra = _membership_differences(
            session,
            base_hops_prop=base_hops_prop,
            placebo_hops_prop=placebo_hops_prop,
            member_prop=member_prop,
        )
        seed_props = (TRUE_SEED_PROP, *PLACEBO_SEED_PROPS)
        seeds_outside = {
            prop: _count(
                session,
                f"MATCH (w:Work) WHERE w.`{prop}` = true "
                f"AND coalesce(w.`{member_prop}`, -1) < 0 "
                "RETURN count(w) AS count",
            )
            for prop in seed_props
        }
    ratio = union_count / total_nodes if total_nodes else 0.0
    report: dict[str, Any] = {
        "status": (
            "verification_failed"
            if missing or extra or any(seeds_outside.values())
            else "blocked_union_too_large"
            if ratio > max_union_ratio
            else "complete"
        ),
        "database": NEO4J_DATABASE,
        "total_work_nodes": total_nodes,
        "base_hops_property": base_hops_prop,
        "base_closure_nodes": base_count,
        "placebo_hops_property": placebo_hops_prop,
        "placebo_closure_nodes": placebo_count,
        "membership_property": member_prop,
        "union_nodes": union_count,
        "union_delta_over_base": union_count - base_count,
        "union_ratio": ratio,
        "max_union_ratio": max_union_ratio,
        "placebo_seed_counts": seed_counts,
        "seeds_outside_union": seeds_outside,
        "membership_missing": missing,
        "membership_extra": extra,
        "seconds": round(time.monotonic() - started, 3),
    }
    destination = Path(meta_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(
        f"[m5-union] Checkpoint: union={union_count:,} "
        f"({ratio:.2%} of Work), delta={union_count - base_count:+,}"
    )
    if missing or extra or any(seeds_outside.values()):
        raise RuntimeError(f"M5 union verification failed; see {destination}")
    if ratio > max_union_ratio:
        raise RuntimeError(
            f"M5 union is {ratio:.2%} of the full Work graph, above the "
            f"{max_union_ratio:.2%} safety threshold. Do not project it until "
            f"the expansion is reviewed. See {destination}."
        )
    print(f"[m5-union] Audit report: {destination}")
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute placebo union reachability and materialize m5_union_member."
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--reach-batch-size", type=int, default=5_000)
    parser.add_argument("--write-batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--base-hops-prop", default=DEFAULT_BASE_HOPS_PROP)
    parser.add_argument("--placebo-hops-prop", default=DEFAULT_PLACEBO_HOPS_PROP)
    parser.add_argument("--member-prop", default=DEFAULT_MEMBER_PROP)
    parser.add_argument("--max-union-ratio", type=float, default=0.60)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-apoc", action="store_true")
    parser.add_argument("--meta", default=str(DEFAULT_META_PATH))
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with exclusive_run_lock(
            DEFAULT_RUN_LOCK,
            run_name="M5 union reachability",
        ):
            prepare_m5_union_reachability(
                driver,
                workers=args.workers,
                reach_batch_size=args.reach_batch_size,
                write_batch_size=args.write_batch_size,
                base_hops_prop=args.base_hops_prop,
                placebo_hops_prop=args.placebo_hops_prop,
                member_prop=args.member_prop,
                max_union_ratio=args.max_union_ratio,
                resume=args.resume,
                force=args.force,
                use_apoc=not args.no_apoc,
                meta_path=args.meta,
            )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
