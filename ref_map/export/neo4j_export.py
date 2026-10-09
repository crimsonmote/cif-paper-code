import argparse
import time
from collections.abc import Iterator, Mapping
from typing import Any, Optional

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import TransientError
from tqdm import tqdm

from common.mongo import get_mongo_client, handle_pymongo_errors
from ref_map.common.batching import chunked
from ref_map.common.neo4j_apoc import run_apoc_rows_iterate
from ref_map.export.field_groups import (
    build_group_context,
    default_export_groups,
    ensure_indexes_for_groups,
    projection_for_groups,
    shape_work_for_groups,
    write_assignments_for_groups,
)
from ref_map.export.shared import (
    BATCH_SIZE,
    COLLECTION_NAME,
    DB_NAME,
    FAST_COUNT,
    NEO4J_DATABASE,
    SKIP_COUNT,
    _ensure_list,
    _log_error,
    _resolve_neo4j_config,
    _strip_openalex,
    _strip_openalex_list,
)


def _shape_work(
    doc: Mapping[str, Any],
    groups: list[str],
    context: dict[str, Any] | None = None,
) -> Optional[dict[str, Any]]:
    return shape_work_for_groups(doc, groups, context)

def _shape_refs(doc: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    work_id = _strip_openalex(doc.get("id"))
    if not work_id:
        return None
    refs = _strip_openalex_list([ref for ref in _ensure_list(doc.get("referenced_works")) if ref])
    if not refs:
        return None
    return {"id": work_id, "refs": refs}


def _iter_work_rows(groups: list[str], batch_size: int = BATCH_SIZE) -> Iterator[list[dict[str, Any]]]:
    mongo = get_mongo_client()
    coll = mongo[DB_NAME][COLLECTION_NAME]
    projection = projection_for_groups(groups)
    with handle_pymongo_errors():
        cursor = coll.find({}, projection, batch_size=batch_size)
        for batch in chunked(cursor, batch_size):
            context = build_group_context(mongo[DB_NAME], batch, groups)
            shaped = [_shape_work(doc, groups, context) for doc in batch]
            shaped = [s for s in shaped if s is not None]
            if shaped:
                yield shaped


def _iter_ref_rows(batch_size: int = BATCH_SIZE) -> Iterator[tuple[list[dict[str, Any]], int]]:
    mongo = get_mongo_client()
    coll = mongo[DB_NAME][COLLECTION_NAME]
    projection = {"id": 1, "referenced_works": 1}
    with handle_pymongo_errors():
        cursor = coll.find({}, projection, batch_size=batch_size)
        for batch in chunked(cursor, batch_size):
            scanned = len(batch)
            shaped = [_shape_refs(doc) for doc in batch]
            shaped = [s for s in shaped if s is not None]
            yield shaped, scanned


def _count_works(fast: bool = True) -> int:
    mongo = get_mongo_client()
    coll = mongo[DB_NAME][COLLECTION_NAME]
    with handle_pymongo_errors():
        if fast:
            # Approximate count, O(1) and much faster on large collections.
            return coll.estimated_document_count()
        return coll.count_documents({})


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export OpenAlex works from Mongo to Neo4j.")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Override batch size for Neo4j writes (defaults to env NEO4J_BATCH_SIZE or 2000).",
    )
    parser.add_argument(
        "--reset-graph",
        action="store_true",
        help="Delete all nodes/relationships in the target Neo4j database before export (batched, safer than a single DETACH DELETE).",
    )
    parser.add_argument(
        "--reset-batch-size",
        type=int,
        default=10000,
        help="Batch size for graph reset deletes (default: 10000).",
    )
    parser.add_argument(
        "--no-display-fields",
        action="store_true",
        help="Skip name/display-only fields during export to save storage.",
    )
    return parser.parse_args()


def _ensure_constraint(driver: Driver) -> None:
    cypher = """
    CREATE CONSTRAINT work_id IF NOT EXISTS
    FOR (w:Work) REQUIRE w.id IS UNIQUE
    """
    print(
        f"[neo4j] Ensuring uniqueness constraint on Work.id in database '{NEO4J_DATABASE}'..."
    )
    with driver.session(database=NEO4J_DATABASE) as session:
        session.run(cypher)
    print("[neo4j] Constraint ensured.")


def _write_nodes_batch(
    session,
    rows: list[dict[str, Any]],
    groups: list[str],
    batch_size: int,
    parallel: bool = True,
) -> None:
    source_query = "UNWIND $rows AS row RETURN row"
    assignments = ",\n          ".join(write_assignments_for_groups(groups))
    action_query = f"""
    MERGE (w:Work {{id: row.id}})
      SET {assignments}
    """
    run_apoc_rows_iterate(
        session, source_query, action_query, rows, batch_size, parallel=parallel
    )


def _write_rels_batch(
    session, rows: list[dict[str, Any]], batch_size: int, parallel: bool = True
) -> None:
    source_query = """
    UNWIND $rows AS row
    UNWIND row.refs AS refId
    RETURN row.id AS src, refId AS dst
    """
    action_query = """
    MATCH (w:Work {id: src})
    MATCH (t:Work {id: dst})
    MERGE (w)-[:CITES]->(t)
    """
    run_apoc_rows_iterate(
        session, source_query, action_query, rows, batch_size, parallel=parallel
    )


def _write_batch(
    driver: Driver,
    rows: list[dict[str, Any]],
    groups: list[str],
    batch_size: int,
    retries: int = 3,
) -> None:
    if not rows:
        return

    attempt = 0
    while attempt <= retries:
        try:
            with driver.session(database=NEO4J_DATABASE) as session:
                _write_nodes_batch(session, rows, groups, batch_size, parallel=True)
                _write_rels_batch(session, rows, batch_size, parallel=True)
            return
        except TransientError as exc:
            attempt += 1
            if attempt > retries:
                _log_error(f"TransientError after {retries} retries: {exc}")
                raise
            time.sleep(2**attempt)
        except Exception as exc:  # noqa: BLE001
            _log_error(f"Failed to write batch: {exc}")
            raise


def _write_nodes_only(
    driver: Driver,
    rows: list[dict[str, Any]],
    groups: list[str],
    batch_size: int,
    retries: int = 3,
) -> None:
    if not rows:
        return
    attempt = 0
    while attempt <= retries:
        try:
            with driver.session(database=NEO4J_DATABASE) as session:
                _write_nodes_batch(session, rows, groups, batch_size, parallel=True)
            return
        except TransientError as exc:
            attempt += 1
            if attempt > retries:
                _log_error(f"TransientError after {retries} retries: {exc}")
                raise
            time.sleep(2**attempt)
        except Exception as exc:  # noqa: BLE001
            _log_error(f"Failed to write node batch: {exc}")
            raise


def _write_rels_only(
    driver: Driver,
    rows: list[dict[str, Any]],
    batch_size: int,
    retries: int = 3,
) -> None:
    if not rows:
        return
    attempt = 0
    while attempt <= retries:
        try:
            with driver.session(database=NEO4J_DATABASE) as session:
                _write_rels_batch(session, rows, batch_size, parallel=True)
            return
        except TransientError as exc:
            attempt += 1
            if attempt > retries:
                _log_error(f"TransientError after {retries} retries: {exc}")
                raise
            time.sleep(2**attempt)
        except Exception as exc:  # noqa: BLE001
            _log_error(f"Failed to write relationship batch: {exc}")
            raise

def _reset_graph(driver: Driver, batch_size: int) -> None:
    """
    Safely wipe all nodes/relationships in batches to avoid memory spikes.
    """
    print("[reset] Counting nodes to delete...")
    with driver.session(database=NEO4J_DATABASE) as session:
        total_nodes = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
    print(f"[reset] Found {total_nodes} nodes. Deleting in batches of {batch_size}...")
    deleted_total = 0
    with tqdm(total=total_nodes, desc="Resetting graph", unit="node", leave=True) as pbar:
        while True:
            res = None
            with driver.session(database=NEO4J_DATABASE) as session:
                res = session.run(
                    """
                    MATCH (n)
                    WITH n LIMIT $limit
                    DETACH DELETE n
                    RETURN count(n) AS deleted
                    """,
                    {"limit": batch_size},
                ).single()
            deleted = res["deleted"] if res else 0
            deleted_total += deleted
            pbar.update(deleted)
            print(f"[reset] Deleted batch: {deleted}, total: {deleted_total}")
            if deleted == 0:
                break
    print("[reset] Graph cleared.")


def main() -> None:
    args = _parse_args()
    batch_size = args.batch_size or BATCH_SIZE
    export_groups = default_export_groups(include_display_fields=not args.no_display_fields)
    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Mongo db/collection: {DB_NAME}/{COLLECTION_NAME}")
    print(
        f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}, batch size: {batch_size}"
    )
    print(f"[config] Export groups: {', '.join(export_groups)}")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    if args.reset_graph:
        _reset_graph(driver, batch_size=args.reset_batch_size)
    _ensure_constraint(driver)
    with driver.session(database=NEO4J_DATABASE) as session:
        ensure_indexes_for_groups(session, export_groups)
    total_docs = None
    if not SKIP_COUNT:
        try:
            total_docs = _count_works(fast=FAST_COUNT)
            print(f"[config] Total works to export: ~{total_docs} (fast={FAST_COUNT})")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] Unable to count documents quickly: {exc}")
    else:
        print("[config] Skipping initial count (NEO4J_SKIP_COUNT=true)")
    total = 0
    start = time.time()
    print("[export] Phase 1/2: nodes")
    with tqdm(total=total_docs, desc="Exporting nodes", unit="doc") as pbar:
        for rows in _iter_work_rows(export_groups, batch_size):
            _write_nodes_only(driver, rows, export_groups, batch_size)
            total += len(rows)
            pbar.update(len(rows))
    elapsed_nodes = time.time() - start
    print(f"[done] wrote {total} nodes in {elapsed_nodes:.1f}s")

    rel_total = 0
    rel_start = time.time()
    print("[export] Phase 2/2: relationships")
    with tqdm(total=total_docs, desc="Exporting relationships", unit="doc") as pbar:
        for rows, scanned in _iter_ref_rows(batch_size):
            _write_rels_only(driver, rows, batch_size)
            rel_total += len(rows)
            pbar.update(scanned)
    elapsed_rels = time.time() - rel_start
    elapsed = time.time() - start
    print(f"[done] wrote relationships for {rel_total} docs in {elapsed_rels:.1f}s")
    print(f"[done] total export time: {elapsed:.1f}s")
    driver.close()


if __name__ == "__main__":
    main()
