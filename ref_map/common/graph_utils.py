"""Shared graph and admin utilities (GDS helpers + maintenance operations)."""

from __future__ import annotations

from typing import Any

from tqdm import tqdm


def forward_bfs_gds(
    session,
    graph_name: str,
    seed_neo4j_id: int,
    max_depth: int = -1,
):
    """
    Run forward BFS from a seed using GDS.

    Returns Neo4j internal IDs, excluding the seed itself.
    """
    config = {"sourceNode": seed_neo4j_id}
    if max_depth > 0:
        config["maxDepth"] = max_depth

    query = """
    CALL gds.bfs.stream($graph_name, $config)
    YIELD nodeIds
    UNWIND nodeIds AS nodeId
    WITH nodeId WHERE nodeId <> $source_id
    RETURN nodeId AS neo4j_id
    """

    return session.run(
        query,
        {
            "graph_name": graph_name,
            "config": config,
            "source_id": seed_neo4j_id,
        },
    )


def forward_bfs_gds_with_depth(
    session, graph_name: str, seed_neo4j_id: int, max_depth: int = -1
):
    """Run forward BFS from a seed using GDS and return depth per node."""
    config = {"sourceNode": seed_neo4j_id}
    if max_depth > 0:
        config["maxDepth"] = max_depth

    query = """
    CALL gds.bfs.stream($graph_name, $config)
    YIELD nodeId, depth
    WITH nodeId, depth WHERE nodeId <> $source_id
    RETURN nodeId AS neo4j_id, depth AS depth
    """

    return session.run(
        query,
        {
            "graph_name": graph_name,
            "config": config,
            "source_id": seed_neo4j_id,
        },
    )


def gds_bfs_supports_depth(session, graph_name: str, seed_neo4j_id: int) -> bool:
    """Detect whether gds.bfs.stream yields depth in this GDS version."""
    try:
        res = session.run(
            """
            CALL gds.bfs.stream($graph_name, {sourceNode: $source_id, maxDepth: 1})
            YIELD nodeId, depth
            RETURN count(*) AS c
            """,
            {"graph_name": graph_name, "source_id": seed_neo4j_id},
        ).single()
        return res is not None
    except Exception:
        return False


def get_seed_id_mapping(
    session,
    label: str,
    seed_ids: list[str] | None = None,
    seed_prop: str = "matt_marx_paper_patent_v2",
) -> dict[str, int]:
    """Get mapping from custom ID to Neo4j internal ID for seed nodes."""
    print("[gds] Building seed ID mapping...")
    if seed_ids:
        query = f"""
        MATCH (w:{label})
        WHERE w.id IN $ids
        RETURN w.id AS custom_id, id(w) AS neo4j_id
        """
        params = {"ids": seed_ids}
    else:
        query = f"""
        MATCH (w:{label})
        WHERE w.{seed_prop} = true
        RETURN w.id AS custom_id, id(w) AS neo4j_id
        """
        params = {}

    mapping: dict[str, int] = {}
    for record in session.run(query, params):
        mapping[record["custom_id"]] = record["neo4j_id"]

    print(f"[gds] Mapped {len(mapping):,} seed IDs")
    return mapping


def drop_gds_graph(session, graph_name: str) -> None:
    """Drop GDS graph projection."""
    try:
        session.run(
            "CALL gds.graph.drop($graph_name) YIELD graphName RETURN graphName",
            {"graph_name": graph_name},
        ).consume()
        print(f"[gds] Dropped GDS graph '{graph_name}'")
    except Exception as exc:
        print(f"[gds] Could not drop graph: {exc}")


def _run_single(session, query: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    res = session.run(query, params or {}).single()
    return dict(res) if res else {}


def _count_label(session, label: str) -> int:
    res = _run_single(
        session,
        f"MATCH (w:{label}) RETURN count(w) AS c",
        {},
    )
    return int(res.get("c", 0))


def build_test_nodes(session, year: str, label: str, batch_size: int) -> None:
    """Copy Work nodes from a specific year to test label."""
    print(f"[test] Building test nodes from year {year} as :{label}...")
    session.run(
        f"""
        MATCH (w:Work)
        WHERE w.publication_year = $year
        SET w:{label}
        """,
        {"year": year},
    )
    count = _count_label(session, label)
    print(f"[test] Created {count} test nodes with label :{label}")


def build_test_rels(
    session, label: str, rel_type: str, batch_size: int, use_apoc: bool = False
) -> None:
    """Create test citation relationships between test nodes."""
    print(f"[test] Building test relationships :{rel_type} for :{label}...")

    if use_apoc:
        print("[test] Using APOC periodic.iterate for optimized performance")
        result = session.run(
            f"""
            CALL apoc.periodic.iterate(
                "MATCH (a:{label})-[:CITES]->(b:{label}) RETURN a, b",
                "MERGE (a)-[:{rel_type}]->(b)",
                {{batchSize: $batchSize, parallel: true, retries: 3}}
            )
            YIELD batches, total, errorMessages
            RETURN batches, total, errorMessages
            """,
            {"batchSize": batch_size * 5},
        ).single()

        if result:
            print(
                f"[test] APOC completed: {result['batches']} batches, {result['total']} rels"
            )
            if result["errorMessages"]:
                print(f"[test] Errors: {result['errorMessages']}")
        return

    last_id = -1
    total = 0

    with tqdm(desc="Creating test relationships", unit="rel") as pbar:
        while True:
            res = _run_single(
                session,
                f"""
                MATCH (a:{label})-[:CITES]->(b:{label})
                WHERE id(a) > $last_id
                WITH a, b ORDER BY id(a) LIMIT $limit
                MERGE (a)-[:{rel_type}]->(b)
                RETURN count(*) AS created, max(id(a)) AS max_id
                """,
                {"last_id": last_id, "limit": batch_size},
            )
            created = int(res.get("created", 0) or 0)
            max_id = res.get("max_id")
            if created == 0 or max_id is None:
                break
            total += created
            pbar.update(created)
            last_id = max_id

    print(f"[test] Created {total} test relationships")


def expand_test_nodes(
    session,
    label: str,
    expand_rel: str,
    hops: int,
    direction: str,
    batch_size: int,
    frontier_label: str,
) -> None:
    """Expand test graph by including nodes N hops away."""
    print(f"[test] Expanding test nodes {hops} hop(s) in direction: {direction}...")

    session.run(f"MATCH (n:{label}) SET n:{frontier_label}")

    for hop in range(hops):
        frontier_next = f"{frontier_label}Next"

        if direction in ("out", "both"):
            session.run(
                f"""
                MATCH (n:{frontier_label})-[:{expand_rel}]->(m:Work)
                WHERE NOT m:{label} AND NOT m:{frontier_label}
                SET m:{frontier_next}
                """
            )

        if direction in ("in", "both"):
            session.run(
                f"""
                MATCH (n:{frontier_label})<-[:{expand_rel}]-(m:Work)
                WHERE NOT m:{label} AND NOT m:{frontier_label}
                SET m:{frontier_next}
                """
            )

        count = _count_label(session, frontier_next)
        print(f"[test] Hop {hop + 1}: found {count} new nodes")

        session.run(f"MATCH (n:{frontier_next}) SET n:{label}")
        session.run(
            f"""
            MATCH (n:{frontier_next})
            REMOVE n:{frontier_next}
            SET n:{frontier_label}
            """
        )

    session.run(
        f"""
        MATCH (f:{frontier_label})
        REMOVE f:{frontier_label}
        """
    )
    print("[test] Done expanding test nodes.")


def reset_reachability(
    session,
    label: str,
    reach_prop: str,
    hops_prop: str,
    batch_size: int = 10000,
    use_apoc: bool = False,
) -> None:
    """Fast batched removal of reachability properties."""
    print(f"[reset] Clearing {reach_prop} and {hops_prop} on :{label}...")

    if use_apoc:
        print("[reset] Using APOC periodic.iterate for optimized performance")
        count_with_props = _run_single(
            session,
            f"""
            MATCH (w:{label})
            WHERE w.{reach_prop} IS NOT NULL OR w.{hops_prop} IS NOT NULL
            RETURN count(w) AS c
            """,
            {},
        ).get("c", 0)

        print(f"[reset] Found {count_with_props} nodes with properties to clear")

        if count_with_props == 0:
            print("[reset] No nodes to reset")
            return

        result = session.run(
            f"""
            CALL apoc.periodic.iterate(
                "MATCH (w:{label})
                 WHERE w.{reach_prop} IS NOT NULL OR w.{hops_prop} IS NOT NULL
                 RETURN w",
                "REMOVE w.{reach_prop}, w.{hops_prop}",
                {{batchSize: $batchSize, parallel: true, retries: 3}}
            )
            YIELD batches, total, errorMessages
            RETURN batches, total, errorMessages
            """,
            {"batchSize": batch_size * 5},
        ).single()

        print(
            f"[reset] APOC completed: {result['batches']} batches, {result['total']} nodes"
        )
        if result["errorMessages"]:
            print(f"[reset] Errors: {result['errorMessages']}")
        return

    count_with_props = _run_single(
        session,
        f"""
        MATCH (w:{label})
        WHERE w.{reach_prop} IS NOT NULL OR w.{hops_prop} IS NOT NULL
        RETURN count(w) AS c
        """,
        {},
    ).get("c", 0)

    print(f"[reset] Found {count_with_props} nodes with properties to clear")

    last_id = -1
    total = 0

    with tqdm(desc="Resetting properties", unit="node", total=count_with_props) as pbar:
        while True:
            res = _run_single(
                session,
                f"""
                MATCH (w:{label})
                WHERE id(w) > $last_id
                  AND (w.{reach_prop} IS NOT NULL OR w.{hops_prop} IS NOT NULL)
                WITH w ORDER BY id(w) LIMIT $limit
                REMOVE w.{reach_prop}, w.{hops_prop}
                RETURN count(w) AS seen, max(id(w)) AS max_id
                """,
                {"last_id": last_id, "limit": batch_size},
            )
            seen = int(res.get("seen", 0) or 0)
            max_id = res.get("max_id")
            if seen == 0 or max_id is None:
                break

            total += seen
            pbar.update(seen)
            last_id = max_id

    print(f"[reset] Cleared {total} nodes")


def reset_reach_counts(
    session,
    label: str,
    backward_prop: str | None = "all_assignees_reach_count",
    forward_prop: str | None = "all_assignees_forward_reach_count",
    batch_size: int = 10000,
    use_apoc: bool = False,
    include_depth_suffixes: bool = True,
) -> None:
    """Reset reach count properties on nodes."""
    props_to_reset: list[str] = []
    if backward_prop:
        props_to_reset.append(backward_prop)
    if forward_prop:
        props_to_reset.append(forward_prop)

    if include_depth_suffixes and props_to_reset:
        try:
            prop_keys = [
                row["propertyKey"] for row in session.run("CALL db.propertyKeys()")
            ]
        except Exception:
            prop_keys = []

        for base_prop in list(props_to_reset):
            prefix = f"{base_prop}_"
            for key in prop_keys:
                if key == f"{base_prop}_all":
                    if key not in props_to_reset:
                        props_to_reset.append(key)
                    continue
                if not key.startswith(prefix):
                    continue
                suffix = key[len(prefix) :]
                if suffix.startswith("w") and suffix[1:].isdigit():
                    if key not in props_to_reset:
                        props_to_reset.append(key)
                    continue
                if suffix.startswith("w") and "_y" in suffix:
                    window_part, _, year_part = suffix.partition("_y")
                    if window_part[1:].isdigit() and year_part.isdigit():
                        if key not in props_to_reset:
                            props_to_reset.append(key)
                        continue
                if suffix.startswith("d") and suffix[1:].isdigit():
                    if key not in props_to_reset:
                        props_to_reset.append(key)
                    continue
                if suffix.isdigit():
                    if key not in props_to_reset:
                        props_to_reset.append(key)
                    continue
                if suffix.startswith("y") and suffix[1:].isdigit():
                    if key not in props_to_reset:
                        props_to_reset.append(key)
                    continue
                if "_d" in suffix:
                    _, _, depth_part = suffix.rpartition("_d")
                    if depth_part.isdigit():
                        if key not in props_to_reset:
                            props_to_reset.append(key)
                        continue
                if "_y" in suffix:
                    depth_part, _, year_part = suffix.partition("_y")
                    if depth_part.isdigit() and year_part.isdigit():
                        if key not in props_to_reset:
                            props_to_reset.append(key)

    if not props_to_reset:
        print("[reset-reach] No properties specified to reset")
        return

    props_str = ", ".join(props_to_reset)
    print(f"[reset-reach] Resetting {props_str} on :{label}...")

    if use_apoc:
        print("[reset-reach] Using APOC periodic.iterate for optimized performance")
        where_conditions = [f"w.{prop} IS NOT NULL" for prop in props_to_reset]
        where_clause = " OR ".join(where_conditions)
        count = _run_single(
            session,
            f"""
            MATCH (w:{label})
            WHERE {where_clause}
            RETURN count(w) AS c
            """,
            {},
        ).get("c", 0)

        print(f"[reset-reach] Found {count:,} nodes with reach count properties")

        if count == 0:
            print("[reset-reach] No nodes to reset")
            return

        remove_clauses = [f"w.{prop}" for prop in props_to_reset]
        remove_clause = ", ".join(remove_clauses)

        result = session.run(
            f"""
            CALL apoc.periodic.iterate(
                "MATCH (w:{label})
                 WHERE {where_clause}
                 RETURN w",
                "REMOVE {remove_clause}",
                {{batchSize: $batchSize, parallel: true, retries: 3}}
            )
            YIELD batches, total, errorMessages
            RETURN batches, total, errorMessages
            """,
            {"batchSize": batch_size * 5},
        ).single()

        print(
            f"[reset-reach] APOC completed: {result['batches']} batches, {result['total']} nodes"
        )
        if result["errorMessages"]:
            print(f"[reset-reach] Errors: {result['errorMessages']}")
        return

    where_conditions = [f"w.{prop} IS NOT NULL" for prop in props_to_reset]
    where_clause = " OR ".join(where_conditions)
    remove_clauses = [f"w.{prop}" for prop in props_to_reset]
    remove_clause = ", ".join(remove_clauses)
    last_id = -1
    total = 0

    with tqdm(desc="Resetting reach counts", unit="node") as pbar:
        while True:
            res = _run_single(
                session,
                f"""
                MATCH (w:{label})
                WHERE id(w) > $last_id
                  AND ({where_clause})
                WITH w ORDER BY id(w) LIMIT $limit
                REMOVE {remove_clause}
                RETURN count(w) AS seen, max(id(w)) AS max_id
                """,
                {"last_id": last_id, "limit": batch_size},
            )

            seen = int(res.get("seen", 0) or 0)
            max_id = res.get("max_id")

            if seen == 0 or max_id is None:
                break

            total += seen
            pbar.update(seen)
            last_id = max_id

    print(f"[reset-reach] Reset {total:,} nodes")


def cleanup_gds_and_anchors(
    session,
    graph_name: str,
    anchor_label: str,
    anchor_name: str | None = None,
) -> None:
    """Clean up GDS graphs and anchor nodes/relationships."""
    print("[cleanup] Starting GDS and anchor cleanup...")

    try:
        session.run(
            """
            CALL gds.graph.drop($graph, false)
            YIELD graphName
            RETURN graphName
            """,
            {"graph": graph_name},
        ).consume()
        print(f"[cleanup] Dropped GDS graph: {graph_name}")
    except Exception as exc:
        print(f"[cleanup] No GDS graph to drop (or error): {exc}")

    for relationship_type in ("ANALYTICS_SEED", "STARTUP_SEED"):
        try:
            result = session.run(
                f"""
                MATCH ()-[r:{relationship_type}]->()
                WITH r
                DELETE r
                RETURN count(r) AS deleted
                """
            ).single()
            deleted_seeds = result["deleted"] if result else 0
            if deleted_seeds:
                print(
                    f"[cleanup] Deleted {deleted_seeds} "
                    f"{relationship_type} relationships"
                )
        except Exception as exc:
            print(
                f"[cleanup] Error deleting {relationship_type} relationships: {exc}"
            )

    try:
        result = session.run(
            """
            MATCH ()-[r:BFS_TREE_TMP]->()
            WITH r
            DELETE r
            RETURN count(r) AS deleted
            """
        ).single()
        deleted_tmp = result["deleted"] if result else 0
        if deleted_tmp > 0:
            print(f"[cleanup] Deleted {deleted_tmp} BFS_TREE_TMP relationships")
    except Exception as exc:
        print(f"[cleanup] Error deleting BFS_TREE_TMP relationships: {exc}")

    try:
        if anchor_name:
            result = session.run(
                f"""
                MATCH (a:{anchor_label} {{name: $name}})
                DELETE a
                RETURN count(a) AS deleted
                """,
                {"name": anchor_name},
            ).single()
        else:
            result = session.run(
                f"""
                MATCH (a:{anchor_label})
                DELETE a
                RETURN count(a) AS deleted
                """
            ).single()
        deleted_anchors = result["deleted"] if result else 0
        print(f"[cleanup] Deleted {deleted_anchors} anchor nodes (:{anchor_label})")
    except Exception as exc:
        print(f"[cleanup] Error deleting anchor nodes: {exc}")

    print("[cleanup] Cleanup complete!")


def cleanup_reach_count_gds(session, labels: list[str]) -> None:
    """Drop reach-count GDS projections for the provided labels."""
    print("[cleanup] Dropping reach-count GDS graphs...")
    for label in labels:
        graph_name = f"reach_count_{label.lower()}"
        try:
            session.run(
                """
                CALL gds.graph.drop($graph, false)
                YIELD graphName
                RETURN graphName
                """,
                {"graph": graph_name},
            ).consume()
            print(f"[cleanup] Dropped GDS graph: {graph_name}")
        except Exception as exc:
            print(
                f"[cleanup] No GDS graph to drop for {graph_name} (or error): {exc}"
            )
    print("[cleanup] Reach-count GDS cleanup complete!")
