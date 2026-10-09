"""Exact, restart-safe patching of sparse boolean properties on ``:Work``."""

from __future__ import annotations

from collections.abc import Sequence
import sys
from pathlib import Path
from typing import Any

from tqdm import tqdm

from ref_map.common.batching import chunked
from ref_map.common.config import NEO4J_DATABASE
from ref_map.common.neo4j_apoc import (
    apoc_available,
    run_apoc_query_iterate,
    run_apoc_rows_iterate,
)
from ref_map.common.neo4j_tokens import validate_property_key, validate_property_token
from ref_map.common.openalex_work_ids import (
    detect_work_id_format,
    neo4j_work_id_expression,
    normalize_work_id,
)

DEFAULT_BATCH_SIZE = 10_000


def load_work_ids(path: str | Path) -> list[str]:
    """Load, normalize, deduplicate, and sort OpenAlex Work IDs."""
    source = Path(path)
    ids: set[str] = set()
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            raw = line.strip()
            if not raw:
                continue
            work_id = normalize_work_id(raw)
            if work_id is None:
                raise ValueError(
                    f"Invalid OpenAlex Work ID at {source}:{line_number}: "
                    f"{raw!r}"
                )
            ids.add(work_id)
    if not ids:
        raise ValueError(f"No Work IDs found in {source}")
    return sorted(ids, key=lambda value: int(value[1:]))


def _property_counts(session, prop: str) -> tuple[int, int]:
    record = session.run(
        f"""
        MATCH (w:Work)
        WHERE w.`{prop}` IS NOT NULL
        RETURN count(w) AS present,
               sum(CASE WHEN w.`{prop}` = true THEN 1 ELSE 0 END) AS true_count
        """
    ).single()
    return (
        int(record["present"] if record else 0),
        int(record["true_count"] if record and record["true_count"] else 0),
    )


def _inspect_input_membership(
    driver,
    work_ids: Sequence[str],
    *,
    prop: str,
    id_format: str,
    batch_size: int,
) -> tuple[int, int, list[str]]:
    matched = 0
    already_true = 0
    missing: list[str] = []
    query = f"""
    UNWIND $work_ids AS input_id
    OPTIONAL MATCH (w:Work {{id: {neo4j_work_id_expression(id_format, 'input_id')}}})
    RETURN input_id,
           count(w) AS matched,
           sum(CASE WHEN w.`{prop}` = true THEN 1 ELSE 0 END) AS already_true
    """
    for batch in chunked(work_ids, batch_size):
        with driver.session(database=NEO4J_DATABASE) as session:
            for record in session.run(query, {"work_ids": list(batch)}):
                row_matched = int(record["matched"] or 0)
                if row_matched != 1:
                    missing.append(record["input_id"])
                    continue
                matched += 1
                already_true += int(record["already_true"] or 0)
    return matched, already_true, missing


def clear_work_properties(
    driver,
    *,
    props: Sequence[str],
    batch_size: int = DEFAULT_BATCH_SIZE,
    use_apoc: bool = True,
) -> int:
    """Remove one or more Work properties in bounded transactions."""
    clean_props = tuple(dict.fromkeys(validate_property_key(prop) for prop in props))
    if not clean_props:
        return 0
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    predicate = " OR ".join(f"w.`{prop}` IS NOT NULL" for prop in clean_props)
    remove_clause = ", ".join(f"w.`{prop}`" for prop in clean_props)
    with driver.session(database=NEO4J_DATABASE) as session:
        record = session.run(
            f"MATCH (w:Work) WHERE {predicate} RETURN count(w) AS present"
        ).single()
        present = int(record["present"] if record else 0)
        if use_apoc and not apoc_available(session):
            print("[sparse-patch] APOC unavailable for property cleanup.")
            use_apoc = False
    if present == 0:
        return 0
    if use_apoc:
        with driver.session(database=NEO4J_DATABASE) as session:
            run_apoc_query_iterate(
                session,
                source_query=(
                    f"MATCH (w:Work) WHERE {predicate} "
                    "RETURN elementId(w) AS element_id"
                ),
                action_query=(
                    "MATCH (w:Work) WHERE elementId(w) = element_id "
                    f"REMOVE {remove_clause}"
                ),
                batch_size=batch_size,
                parallel=False,
                retries=2,
            )
    else:
        with driver.session(database=NEO4J_DATABASE) as session:
            while True:
                record = session.run(
                    f"""
                    MATCH (w:Work)
                    WHERE {predicate}
                    WITH w LIMIT $batch_size
                    REMOVE {remove_clause}
                    RETURN count(w) AS changed
                    """,
                    {"batch_size": batch_size},
                ).single()
                if not record or int(record["changed"] or 0) == 0:
                    break
    return present


def patch_sparse_boolean_property(
    driver,
    *,
    work_ids: Sequence[str],
    prop: str,
    index_name: str | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    use_apoc: bool = True,
    force: bool = False,
    progress_label: str | None = None,
) -> dict[str, Any]:
    """Make one sparse property exactly equal to the supplied Work-ID set.

    A count match alone is deliberately insufficient: every supplied ID is
    checked before an existing property is accepted as complete.
    """
    prop = validate_property_token(prop)
    index_name = validate_property_token(index_name or f"work_{prop}_idx")
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    normalized = sorted(set(work_ids), key=lambda value: int(value[1:]))
    if len(normalized) != len(work_ids):
        raise ValueError(f"Input for {prop} contains duplicate Work IDs")
    if not normalized:
        raise ValueError(f"Input for {prop} is empty")

    id_format = detect_work_id_format(driver)
    with driver.session(database=NEO4J_DATABASE) as session:
        session.run(
            f"CREATE INDEX {index_name} IF NOT EXISTS "
            f"FOR (w:Work) ON (w.`{prop}`)"
        ).consume()
        apoc_ready = apoc_available(session)
        if use_apoc and not apoc_ready:
            print(f"[sparse-patch] APOC unavailable for {prop}; using UNWIND.")
            use_apoc = False
        present_before, true_before = _property_counts(session, prop)

    print(
        f"[sparse-patch] Preflighting {len(normalized):,} IDs for {prop} "
        f"(stored={present_before:,}, true={true_before:,})"
    )
    matched, already_true, missing = _inspect_input_membership(
        driver,
        normalized,
        prop=prop,
        id_format=id_format,
        batch_size=batch_size,
    )
    if missing:
        preview = ", ".join(missing[:10])
        raise RuntimeError(
            f"{len(missing):,} input IDs for {prop} have no matching Work "
            f"node (first: {preview}). No property changes were made."
        )

    exact_before = (
        present_before == len(normalized)
        and true_before == len(normalized)
        and already_true == len(normalized)
    )
    if exact_before:
        print(f"[sparse-patch] {prop} already has the exact requested membership.")
        return {
            "property": prop,
            "input_ids": len(normalized),
            "matched_ids": matched,
            "missing_ids": 0,
            "present_before": present_before,
            "true_before": true_before,
            "cleared": 0,
            "written": 0,
            "idempotent_skip": True,
            "apoc": use_apoc,
        }
    if present_before and not force:
        raise RuntimeError(
            f"Property {prop!r} already has non-exact membership "
            f"(present={present_before:,}, true={true_before:,}, requested "
            f"IDs already true={already_true:,}/{len(normalized):,}). Pass "
            "--force to clear stale values and replace it exactly."
        )

    cleared = 0
    if present_before:
        cleared = clear_work_properties(
            driver,
            props=(prop,),
            batch_size=batch_size,
            use_apoc=use_apoc,
        )
        print(f"[sparse-patch] Cleared {prop} from {cleared:,} Work nodes.")

    source_query = "UNWIND $rows AS row RETURN row"
    action_query = f"""
    MATCH (w:Work {{id: {neo4j_work_id_expression(id_format, 'row.id')}}})
    SET w.`{prop}` = true
    """
    with tqdm(
        total=len(normalized),
        desc=progress_label or f"Patching {prop}",
        unit="paper",
        dynamic_ncols=True,
        file=sys.stdout,
    ) as progress:
        for batch in chunked(normalized, batch_size):
            rows = [{"id": value} for value in batch]
            with driver.session(database=NEO4J_DATABASE) as session:
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
            progress.update(len(batch))

    with driver.session(database=NEO4J_DATABASE) as session:
        session.run("CALL db.awaitIndexes(300)").consume()
        present_after, true_after = _property_counts(session, prop)
    if present_after != len(normalized) or true_after != len(normalized):
        raise RuntimeError(
            f"Post-patch verification failed for {prop}: "
            f"present={present_after:,}, true={true_after:,}, "
            f"expected={len(normalized):,}"
        )
    _, verified_true, verified_missing = _inspect_input_membership(
        driver,
        normalized,
        prop=prop,
        id_format=id_format,
        batch_size=batch_size,
    )
    if verified_missing or verified_true != len(normalized):
        raise RuntimeError(
            f"Exact-membership verification failed for {prop}: "
            f"input true={verified_true:,}/{len(normalized):,}"
        )
    return {
        "property": prop,
        "input_ids": len(normalized),
        "matched_ids": matched,
        "missing_ids": 0,
        "present_before": present_before,
        "true_before": true_before,
        "cleared": cleared,
        "written": len(normalized),
        "present_after": present_after,
        "true_after": true_after,
        "idempotent_skip": False,
        "apoc": use_apoc,
    }
