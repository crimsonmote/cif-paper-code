"""Patch canonical Founding Patents seed properties onto existing Work nodes."""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase
from tqdm import tqdm

from ref_map.common.config import (
    CANONICAL_FOUNDING_PATENT_INDEX_NAMES,
    CANONICAL_FOUNDING_PATENT_PROPERTIES,
    NEO4J_DATABASE,
)
from ref_map.common.batching import chunked
from ref_map.common.founding_patent_seeds import (
    DEFAULT_FOUNDING_PATENT_ASSIGNEES,
    DEFAULT_FOUNDING_PATENT_TRIPLETS,
    MIN_FOUNDING_MATCH_SCORE,
    YOUNG_FIRM_MAX_AGE,
    FoundingPatentSeedIndex,
    load_founding_patent_seed_index,
)
from ref_map.common.openalex_work_ids import (
    detect_work_id_format,
    neo4j_work_id_expression,
)
from ref_map.common.neo4j_apoc import apoc_available, run_apoc_rows_iterate
from ref_map.export.neo4j_export import _ensure_constraint
from ref_map.export.shared import _resolve_neo4j_config
from ref_map.export.sparse_work_properties import clear_work_properties

DEFAULT_BATCH_SIZE = 10_000


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Patch all canonical Founding Patents seed properties onto an "
            "existing Neo4j graph."
        )
    )
    parser.add_argument(
        "--triplets",
        default=str(DEFAULT_FOUNDING_PATENT_TRIPLETS),
        help="Path to _patent_paper_startup_triplets.csv.",
    )
    parser.add_argument(
        "--assignees",
        default=str(DEFAULT_FOUNDING_PATENT_ASSIGNEES),
        help=(
            "Path to _patent_ocpb.csv, used to distinguish universities from "
            "hospitals and government assignees."
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Work IDs per bounded transaction (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--young-firm-max-age",
        type=int,
        default=YOUNG_FIRM_MAX_AGE,
        help=(
            "Maximum firm age at patent application for young-firm membership "
            f"(default: {YOUNG_FIRM_MAX_AGE})."
        ),
    )
    parser.add_argument(
        "--min-founding-score",
        type=float,
        default=MIN_FOUNDING_MATCH_SCORE,
        help=(
            "Minimum Founding Patents organization-match score used by the "
            f"young-firm definition (default: {MIN_FOUNDING_MATCH_SCORE:g})."
        ),
    )
    parser.add_argument(
        "--clear-stale",
        action="store_true",
        help=(
            "Before patching, remove canonical seed properties currently set "
            "on any Work. This is only needed after changing the source table "
            "or definition."
        ),
    )
    parser.add_argument(
        "--no-apoc",
        action="store_true",
        help="Disable APOC batching and use bounded UNWIND transactions.",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip post-write index, count, and seed-subset verification.",
    )
    return parser.parse_args()


def _ensure_seed_indexes(session) -> None:
    for index_name, prop in zip(
        CANONICAL_FOUNDING_PATENT_INDEX_NAMES,
        CANONICAL_FOUNDING_PATENT_PROPERTIES,
        strict=True,
    ):
        session.run(
            f"CREATE INDEX {index_name} IF NOT EXISTS "
            f"FOR (w:Work) ON (w.`{prop}`)"
        ).consume()


def _verify_seed_patch(
    session,
    expected: dict[str, int],
) -> dict[str, int]:
    all_prop, corporate_prop, young_prop, young_university_prop = (
        CANONICAL_FOUNDING_PATENT_PROPERTIES
    )
    index_names = CANONICAL_FOUNDING_PATENT_INDEX_NAMES

    session.run("CALL db.awaitIndexes(300)").consume()
    index_rows = list(
        session.run(
            """
            SHOW INDEXES YIELD name, state
            WHERE name IN $index_names
            RETURN name, state
            """,
            {"index_names": list(index_names)},
        )
    )
    index_states = {row["name"]: row["state"] for row in index_rows}
    missing_or_offline = {
        name: index_states.get(name, "MISSING")
        for name in index_names
        if index_states.get(name) != "ONLINE"
    }
    if missing_or_offline:
        raise RuntimeError(f"Canonical seed indexes are not online: {missing_or_offline}")

    def _count(predicate: str) -> int:
        record = session.run(
            f"MATCH (w:Work) WHERE {predicate} RETURN count(w) AS count"
        ).single()
        return int(record["count"] if record else 0)

    actual = {
        "all_assignees": _count(f"w.`{all_prop}` = true"),
        "corporate": _count(f"w.`{corporate_prop}` = true"),
        "young_firm": _count(f"w.`{young_prop}` = true"),
        "young_firm_university": _count(f"w.`{young_university_prop}` = true"),
    }
    present = {
        "all_assignees": _count(f"w.`{all_prop}` IS NOT NULL"),
        "corporate": _count(f"w.`{corporate_prop}` IS NOT NULL"),
        "young_firm": _count(f"w.`{young_prop}` IS NOT NULL"),
        "young_firm_university": _count(
            f"w.`{young_university_prop}` IS NOT NULL"
        ),
    }
    subset_record = session.run(
        f"""
        CALL {{
            MATCH (w:Work)
            WHERE w.`{corporate_prop}` = true
              AND coalesce(w.`{all_prop}`, false) <> true
            RETURN count(w) AS corporate_without_all
        }}
        CALL {{
            MATCH (w:Work)
            WHERE w.`{young_prop}` = true
              AND coalesce(w.`{corporate_prop}`, false) <> true
            RETURN count(w) AS young_without_corporate
        }}
        CALL {{
            MATCH (w:Work)
            WHERE w.`{young_university_prop}` = true
              AND coalesce(w.`{all_prop}`, false) <> true
            RETURN count(w) AS young_university_without_all
        }}
        CALL {{
            MATCH (w:Work)
            WHERE w.`{young_prop}` = true
              AND coalesce(w.`{young_university_prop}`, false) <> true
            RETURN count(w) AS young_without_young_university
        }}
        RETURN corporate_without_all,
               young_without_corporate,
               young_university_without_all,
               young_without_young_university
        """
    ).single()
    subset_violations = {
        "corporate_without_all": int(
            subset_record["corporate_without_all"] if subset_record else 0
        ),
        "young_without_corporate": int(
            subset_record["young_without_corporate"] if subset_record else 0
        ),
        "young_university_without_all": int(
            subset_record["young_university_without_all"] if subset_record else 0
        ),
        "young_without_young_university": int(
            subset_record["young_without_young_university"] if subset_record else 0
        ),
    }

    expected_counts = {
        "all_assignees": int(expected["matched_works"]),
        "corporate": int(expected["corporate"]),
        "young_firm": int(expected["young_firm"]),
        "young_firm_university": int(expected["young_firm_university"]),
    }
    if actual != expected_counts:
        raise RuntimeError(
            "Canonical seed count verification failed: "
            f"expected={expected_counts}, actual={actual}. "
            "Rerun with --clear-stale if definitions or source data changed."
        )
    if present != actual:
        raise RuntimeError(
            "Canonical seed sparsity verification failed: "
            f"non-null={present}, true={actual}. Canonical properties must "
            "contain only true values and be absent otherwise."
        )
    if any(subset_violations.values()):
        raise RuntimeError(
            f"Canonical seed subset verification failed: {subset_violations}"
        )

    return {
        **{f"verified_{key}": value for key, value in actual.items()},
        **subset_violations,
        "indexes_online": len(index_states),
    }


def _read_publication_dates(
    session,
    paper_ids: list[str],
    id_format: str,
) -> dict[str, Any]:
    query = f"""
    UNWIND $paper_ids AS paper_id
    MATCH (w:Work {{id: {neo4j_work_id_expression(id_format, 'paper_id')}}})
    RETURN paper_id, w.publication_date AS publication_date
    """
    return {
        record["paper_id"]: record["publication_date"]
        for record in session.run(query, {"paper_ids": paper_ids})
    }


def _shape_patch_rows(
    seed_index: FoundingPatentSeedIndex,
    paper_ids: list[str],
    publication_dates: dict[str, Any],
    *,
    young_firm_max_age: int,
) -> list[dict[str, Any]]:
    all_prop, corporate_prop, young_prop, young_university_prop = (
        CANONICAL_FOUNDING_PATENT_PROPERTIES
    )
    rows: list[dict[str, Any]] = []
    for paper_id in paper_ids:
        if paper_id not in publication_dates:
            continue
        values = seed_index.property_values_for_work(
            paper_id,
            publication_dates[paper_id],
            sparse=True,
            max_age=young_firm_max_age,
        )
        rows.append(
            {
                "id": paper_id,
                "all_assignees": values[all_prop],
                "corporate": values[corporate_prop],
                "young_firm": values[young_prop],
                "young_firm_university": values[young_university_prop],
            }
        )
    return rows


def _write_patch_rows(
    session,
    rows: list[dict[str, Any]],
    *,
    id_format: str,
    batch_size: int,
    use_apoc: bool,
) -> None:
    if not rows:
        return
    all_prop, corporate_prop, young_prop, young_university_prop = (
        CANONICAL_FOUNDING_PATENT_PROPERTIES
    )
    source_query = "UNWIND $rows AS row RETURN row"
    action_query = f"""
    MATCH (w:Work {{id: {neo4j_work_id_expression(id_format, 'row.id')}}})
    SET w.`{all_prop}` = row.all_assignees,
        w.`{corporate_prop}` = row.corporate,
        w.`{young_prop}` = row.young_firm,
        w.`{young_university_prop}` = row.young_firm_university
    """
    if use_apoc:
        run_apoc_rows_iterate(
            session,
            source_query,
            action_query,
            rows,
            batch_size,
            parallel=True,
        )
    else:
        session.run(
            f"UNWIND $rows AS row {action_query}",
            {"rows": rows},
        ).consume()


def patch_founding_patent_seeds(
    driver,
    *,
    triplets: str | Path,
    assignees: str | Path = DEFAULT_FOUNDING_PATENT_ASSIGNEES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    young_firm_max_age: int = YOUNG_FIRM_MAX_AGE,
    min_founding_score: float = MIN_FOUNDING_MATCH_SCORE,
    clear_stale: bool = False,
    use_apoc: bool = True,
    verify: bool = True,
) -> dict[str, int]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if young_firm_max_age < 0:
        raise ValueError("young_firm_max_age must be nonnegative")

    seed_index = load_founding_patent_seed_index(
        triplets,
        assignees_path=assignees,
        min_founding_score=min_founding_score,
    )
    paper_ids = sorted(seed_index.paper_ids())
    id_format = detect_work_id_format(driver)

    with driver.session(database=NEO4J_DATABASE) as session:
        _ensure_seed_indexes(session)
        apoc_ready = apoc_available(session)
        if use_apoc and not apoc_ready:
            print("[seed-patch] APOC unavailable; using bounded UNWIND transactions.")
            use_apoc = False
        print(f"[seed-patch] Source papers: {len(paper_ids):,}")
        print(f"[seed-patch] Work ID format: {id_format}")
        print(f"[seed-patch] APOC batching: {'enabled' if use_apoc else 'disabled'}")
        print(
            f"[seed-patch] Outer batches: {math.ceil(len(paper_ids) / batch_size):,} "
            f"of up to {batch_size:,} papers"
        )
    if clear_stale:
        cleared = clear_work_properties(
            driver,
            props=CANONICAL_FOUNDING_PATENT_PROPERTIES,
            batch_size=batch_size,
            use_apoc=use_apoc,
        )
        print(f"[seed-patch] Cleared canonical properties from {cleared:,} works.")

    stats = {
        "source_papers": len(paper_ids),
        "matched_works": 0,
        "corporate": 0,
        "young_firm": 0,
        "young_firm_university": 0,
    }
    with tqdm(
        total=len(paper_ids),
        desc="Patching seed sets",
        unit="paper",
        dynamic_ncols=True,
        file=sys.stdout,
    ) as progress:
        for paper_id_batch in chunked(paper_ids, batch_size):
            with driver.session(database=NEO4J_DATABASE) as session:
                publication_dates = _read_publication_dates(
                    session,
                    paper_id_batch,
                    id_format,
                )
                rows = _shape_patch_rows(
                    seed_index,
                    paper_id_batch,
                    publication_dates,
                    young_firm_max_age=young_firm_max_age,
                )
                _write_patch_rows(
                    session,
                    rows,
                    id_format=id_format,
                    batch_size=batch_size,
                    use_apoc=use_apoc,
                )
            stats["matched_works"] += len(rows)
            stats["corporate"] += sum(row["corporate"] is True for row in rows)
            stats["young_firm"] += sum(row["young_firm"] is True for row in rows)
            stats["young_firm_university"] += sum(
                row["young_firm_university"] is True for row in rows
            )
            progress.update(len(paper_id_batch))
            progress.set_postfix(
                matched=stats["matched_works"],
                corporate=stats["corporate"],
                young=stats["young_firm"],
                young_univ=stats["young_firm_university"],
                refresh=True,
            )

    stats["missing_source_works"] = stats["source_papers"] - stats["matched_works"]
    if verify:
        print("[seed-patch] Verifying indexes, property counts, and subset invariants...")
        with driver.session(database=NEO4J_DATABASE) as session:
            stats.update(_verify_seed_patch(session, stats))
        print("[seed-patch] Verification passed.")
    return stats


def main() -> None:
    args = _parse_args()
    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    print(f"[config] Triplets: {args.triplets}")
    print(f"[config] Assignee classifications: {args.assignees}")
    print(
        "[config] Young-firm definition: strict corporate, "
        f"age <= {args.young_firm_max_age}, "
        f"founding score >= {args.min_founding_score:g}"
    )
    print(f"[config] APOC requested: {not args.no_apoc}")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    started = time.time()
    try:
        _ensure_constraint(driver)
        stats = patch_founding_patent_seeds(
            driver,
            triplets=args.triplets,
            assignees=args.assignees,
            batch_size=args.batch_size,
            young_firm_max_age=args.young_firm_max_age,
            min_founding_score=args.min_founding_score,
            clear_stale=args.clear_stale,
            use_apoc=not args.no_apoc,
            verify=not args.no_verify,
        )
    finally:
        driver.close()

    print(
        "[done] Founding Patents seed patch complete: "
        f"source={stats['source_papers']:,}, matched={stats['matched_works']:,}, "
        f"corporate={stats['corporate']:,}, young_firm={stats['young_firm']:,}, "
        f"young_firm_university={stats['young_firm_university']:,}, "
        f"missing_source_works={stats['missing_source_works']:,}, "
        f"elapsed={time.time() - started:.1f}s"
    )


if __name__ == "__main__":
    main()
