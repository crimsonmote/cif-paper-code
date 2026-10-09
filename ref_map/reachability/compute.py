"""
Node reachability computation for citation graphs.
Computes minimum hops to reach seed-linked papers using GDS or pure Cypher.
"""

import os
import time
import warnings
from collections.abc import Sequence
from typing import Any
import logging

from neo4j.exceptions import TransientError
from tqdm import tqdm

from ref_map.common.config import NEO4J_DATABASE
from ref_map.common.neo4j_tokens import validate_property_token

# Suppress warnings for cleaner output
warnings.filterwarnings("ignore")
logging.getLogger("neo4j").setLevel(logging.ERROR)

# Configuration
BATCH_SIZE = int(os.environ.get("ANALYTICS_BATCH_SIZE", "5000"))

# Global debug flag
DEBUG = False


def _debug(msg: str) -> None:
    """Print debug message if DEBUG mode is enabled."""
    if DEBUG:
        print(msg)


def _run_single(session, query: str, params: dict[str, Any]) -> dict[str, Any]:
    res = session.run(query, params).single()
    return dict(res) if res else {}


def _count_label(session, label: str) -> int:
    res = _run_single(
        session,
        f"MATCH (w:{label}) RETURN count(w) AS c",
        {},
    )
    return int(res.get("c", 0))

def _ensure_reachability_indexes(
    session, label: str, reach_prop: str | None, hops_prop: str
) -> None:
    """Create indexes on reachability properties if they don't exist."""
    # Index on hops property - critical for performance during BFS iterations
    index_name_hops = f"{label.lower()}_{hops_prop}"
    try:
        session.run(
            f"""
            CREATE INDEX {index_name_hops} IF NOT EXISTS
            FOR (n:{label}) ON (n.{hops_prop})
            """
        )
        print(f"[reach] Ensured index on :{label}({hops_prop})")
    except Exception as e:
        _debug(f"[reach] Index creation note: {e}")

    # Index on reachability property (optional; skip in single-property mode).
    if reach_prop:
        index_name_reach = f"{label.lower()}_{reach_prop}"
        try:
            session.run(
                f"""
                CREATE INDEX {index_name_reach} IF NOT EXISTS
                FOR (n:{label}) ON (n.{reach_prop})
                """
            )
            print(f"[reach] Ensured index on :{label}({reach_prop})")
        except Exception as e:
            _debug(f"[reach] Index creation note: {e}")


def _process_hop_partition(
    driver,
    label: str,
    rel_type: str,
    hops_prop: str,
    reach_prop: str,
    hop: int,
    partition_id: int,
    num_workers: int,
    max_retries: int = 10,
) -> int:
    """
    Process a single partition of nodes for a given hop.

    Correctness guarantee: Each node from hop-1 is assigned to exactly one partition
    based on id(w) % num_workers = partition_id, ensuring complete coverage without overlap.

    Handles deadlocks via retry with exponential backoff. Deadlocks can occur when multiple
    workers try to set properties on the same target node concurrently. This is safe because
    property writes are idempotent (same hop value).
    """
    def _do_work(tx):
        """Execute the partition query in a managed transaction."""
        result = tx.run(
            f"""
            MATCH (w:{label})
            WHERE w.{hops_prop} = $prev
              AND id(w) % $num_workers = $partition_id
            MATCH (w)-[:{rel_type}]->(p:{label})
            WHERE p.{hops_prop} IS NULL
            SET p.{hops_prop} = $next
            RETURN count(DISTINCT p) AS updated
            """,
            {
                "prev": hop - 1,
                "next": hop,
                "partition_id": partition_id,
                "num_workers": num_workers,
            },
        )
        rec = result.single()
        return int(rec["updated"]) if rec else 0

    for attempt in range(max_retries):
        try:
            with driver.session(database=NEO4J_DATABASE) as session:
                # Use execute_write to ensure transaction commits properly
                return session.execute_write(_do_work)

        except TransientError as e:
            error_msg = str(e).lower()
            is_deadlock = "deadlock" in error_msg

            if is_deadlock and attempt < max_retries - 1:
                # Exponential backoff with jitter (longer for high worker counts)
                base_wait = (2 ** attempt) * 0.2  # 0.2s, 0.4s, 0.8s, 1.6s, 3.2s, 6.4s...
                jitter = (partition_id * 0.02) + (attempt * 0.05)  # More jitter to reduce collisions
                wait_time = base_wait + jitter
                print(f"[reach] Partition {partition_id} deadlock, retrying in {wait_time:.2f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
            else:
                # Either not a deadlock or we've exhausted retries
                if is_deadlock:
                    print(f"[reach] Partition {partition_id} FAILED after {max_retries} deadlock retries")
                raise

    # Should never reach here (loop always returns or raises)
    return 0


def _verify_hop_coverage(session, label: str, hops_prop: str, hop: int, num_workers: int) -> None:
    """
    Verify that partitioning covers all nodes from previous hop.

    This validation ensures no nodes are missed due to partitioning errors.
    """
    # Count total nodes at hop-1
    total = _run_single(
        session,
        f"MATCH (w:{label}) WHERE w.{hops_prop} = $prev RETURN count(w) AS c",
        {"prev": hop - 1},
    ).get("c", 0)

    # Count nodes in each partition
    partition_sum = 0
    for partition_id in range(num_workers):
        count = _run_single(
            session,
            f"""
            MATCH (w:{label})
            WHERE w.{hops_prop} = $prev
              AND id(w) % $num_workers = $partition_id
            RETURN count(w) AS c
            """,
            {"prev": hop - 1, "partition_id": partition_id, "num_workers": num_workers},
        ).get("c", 0)
        partition_sum += count

    if total != partition_sum:
        raise RuntimeError(
            f"Partition coverage mismatch at hop {hop}: "
            f"total={total}, partition_sum={partition_sum}"
        )
    _debug(f"[reach] Partition coverage verified: {total} nodes across {num_workers} partitions")


def compute_reachability_cypher(
    driver,
    label: str,
    rel_type: str,
    reach_prop: str,
    hops_prop: str,
    batch_size: int,
    num_workers: int = 1,
    seed_prop: str = "matt_marx_paper_patent_v2",
    seed_props: Sequence[str] | None = None,
) -> None:
    """
    Pure Cypher implementation of reachability computation using iterative BFS.

    Direction: Computes DOWNSTREAM reachability (papers cited by patent seeds and their citation lineage).
    - Hop 0: Seed papers (patent-linked)
    - Hop 1: Papers cited BY seeds (papers in seed reference lists)
    - Hop 2: Papers cited BY papers that are cited by seeds
    - Pattern: (w)-[:CITES]->(p) means w cites p, so we traverse forward along citations

    Supports parallel execution via num_workers parameter:
    - num_workers=1: Sequential mode (default, backward compatible)
    - num_workers>1: Parallel mode with partition-based concurrency

    Correctness guarantees:
    1. Partitioning by id(w) % num_workers ensures disjoint, complete coverage
    2. Property writes are idempotent (same hop value even if multiple workers try)
    3. Synchronization between hops (all workers complete before next hop)
    4. Optional validation checks verify partition coverage

    Creates indexes for optimal performance. Works without GDS plugin.
    Reachability is derived from hops (hops IS NOT NULL).
    """
    resolved_seed_props = tuple(seed_props or (seed_prop,))
    if not resolved_seed_props:
        raise ValueError("At least one seed property is required")
    resolved_seed_props = tuple(
        validate_property_token(prop) for prop in resolved_seed_props
    )
    seed_predicate = " OR ".join(
        f"w.`{prop}` = true" for prop in resolved_seed_props
    )

    with driver.session(database=NEO4J_DATABASE) as session:
        total_nodes = _count_label(session, label)
        print(
            f"[reach] Computing reachability on :{label} via :{rel_type} "
            f"(total {total_nodes}, workers={num_workers})"
        )

        # Create indexes for performance
        _ensure_reachability_indexes(session, label, reach_prop, hops_prop)

        # Seed papers with hops=0 (reachability derived from hops)
        session.run(
            f"""
            MATCH (w:{label})
            WHERE {seed_predicate}
            SET w.{hops_prop} = 0
            """
        ).consume()  # CRITICAL: Must consume result to commit transaction

        initial = _run_single(
            session,
            f"MATCH (w:{label}) WHERE w.{hops_prop} = 0 RETURN count(w) AS c",
            {},
        ).get("c", 0)
        existing_reachable = _run_single(
            session,
            f"MATCH (w:{label}) WHERE w.{hops_prop} IS NOT NULL "
            "RETURN count(w) AS c",
            {},
        ).get("c", 0)
        print(f"[reach] Seeded {initial} patent nodes.")
        if existing_reachable > initial:
            print(
                f"[reach] Resuming from {existing_reachable:,} existing "
                f"{hops_prop} values."
            )

    # Iterative BFS with optional parallelization
    hop = 1
    updated_total = int(existing_reachable or 0)

    # Threshold for enabling parallelization (only parallelize large hops)
    PARALLEL_THRESHOLD = 100_000
    min_parallel = max(1, num_workers)

    def _select_workers(frontier_size: int) -> int:
        if min_parallel <= 1:
            return 1
        if frontier_size >= PARALLEL_THRESHOLD:
            return min_parallel
        if frontier_size >= PARALLEL_THRESHOLD // 10:
            return max(2, min_parallel // 2)
        if frontier_size >= PARALLEL_THRESHOLD // 100:
            return max(1, min_parallel // 4)
        return 1

    with tqdm(total=total_nodes, initial=updated_total, unit="node") as pbar:
        while True:
            # Check size of current hop frontier
            with driver.session(database=NEO4J_DATABASE) as session:
                hop_size = _run_single(
                    session,
                    f"MATCH (w:{label}) WHERE w.{hops_prop} = $prev RETURN count(w) AS c",
                    {"prev": hop - 1},
                ).get("c", 0)

            if hop_size == 0:
                break

            # Decide: adaptive parallelism based on hop size and --workers cap
            worker_count = _select_workers(hop_size)
            use_parallel = worker_count > 1

            if use_parallel:
                # Parallel processing with partitioning
                _debug(
                    f"[reach] hop {hop}: processing {hop_size} nodes in parallel ({worker_count} workers)"
                )

                # Optional: verify partition coverage before processing
                if DEBUG:
                    with driver.session(database=NEO4J_DATABASE) as session:
                        _verify_hop_coverage(session, label, hops_prop, hop, worker_count)

                # Process partitions in parallel
                from concurrent.futures import ThreadPoolExecutor, as_completed

                hop_updates = 0
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    futures = [
                        executor.submit(
                            _process_hop_partition,
                            driver,
                            label,
                            rel_type,
                            hops_prop,
                            reach_prop,
                            hop,
                            partition_id,
                            worker_count,
                        )
                        for partition_id in range(worker_count)
                    ]

                    for future in as_completed(futures):
                        partition_updates = future.result()
                        hop_updates += partition_updates
                        pbar.update(partition_updates)

            else:
                # Sequential processing (original batched approach)
                if num_workers > 1:
                    _debug(
                        f"[reach] hop {hop}: processing {hop_size} nodes sequentially (below adaptive threshold)"
                    )

                with driver.session(database=NEO4J_DATABASE) as session:
                    last_id = -1
                    hop_updates = 0
                    while True:
                        res = _run_single(
                            session,
                            f"""
                            MATCH (w:{label})
                            WHERE w.{hops_prop} = $prev
                            MATCH (w)-[:{rel_type}]->(p:{label})
                            WHERE p.{hops_prop} IS NULL AND id(p) > $last_id
                            WITH DISTINCT p ORDER BY id(p) LIMIT $limit
                            SET p.{hops_prop} = $next
                            RETURN count(p) AS seen, max(id(p)) AS max_id
                            """,
                            {"prev": hop - 1, "next": hop, "last_id": last_id, "limit": batch_size},
                        )
                        seen = int(res.get("seen", 0) or 0)
                        max_id = res.get("max_id")
                        if seen == 0 or max_id is None:
                            break
                        hop_updates += seen
                        pbar.update(seen)
                        last_id = max_id

            print(f"[reach] hop {hop}: updated {hop_updates} nodes")
            updated_total += hop_updates

            # A restart can encounter a fully completed hop followed by a
            # partially completed deeper hop. Continue over any existing next
            # frontier even when this pass added nothing at the current hop.
            with driver.session(database=NEO4J_DATABASE) as session:
                next_frontier = _run_single(
                    session,
                    f"MATCH (w:{label}) WHERE w.{hops_prop} = $hop "
                    "RETURN count(w) AS c",
                    {"hop": hop},
                ).get("c", 0)
            if next_frontier == 0:
                break
            hop += 1

    # Reachability derived from hops: hops IS NOT NULL = reachable.
    with driver.session(database=NEO4J_DATABASE) as session:
        unreachable_count = _run_single(
            session,
            f"""
            MATCH (w:{label})
            WHERE w.{hops_prop} IS NULL
            RETURN count(w) AS c
            """,
            {},
        ).get("c", 0)

    print(f"[reach] Reachable nodes: {updated_total}, Unreachable nodes: {unreachable_count}")

    # Final validation
    if DEBUG:
        with driver.session(database=NEO4J_DATABASE) as session:
            print("[reach] Running final validation...")
            validation = _run_single(
                session,
                f"""
                MATCH (w:{label})
                RETURN
                    count(w) AS total,
                    sum(CASE WHEN w.{hops_prop} IS NOT NULL THEN 1 ELSE 0 END) AS reachable,
                    sum(CASE WHEN w.{hops_prop} IS NULL THEN 1 ELSE 0 END) AS unreachable
                """,
                {},
            )
            _debug(f"[reach] Validation - total={validation.get('total')}, "
                  f"reachable={validation.get('reachable')}, "
                  f"unreachable={validation.get('unreachable')}")

            # All nodes should be processed (either reachable or unreachable)
            expected_total = validation.get("reachable", 0) + validation.get(
                "unreachable", 0
            )
            if expected_total != validation.get("total", 0):
                print(f"[reach] WARNING: Validation mismatch! total={validation.get('total')}, "
                      f"reachable+unreachable={expected_total}")
            else:
                print("[reach] ✓ All nodes accounted for successfully")

    print("[reach] Done.")


def _drop_gds_graph(session, graph_name: str) -> None:
    """Drop a GDS graph projection if it exists."""
    session.run(
        """
        CALL gds.graph.drop($graph, false)
        YIELD graphName
        RETURN graphName
        """,
        {"graph": graph_name},
    ).consume()


def _project_gds_graph(
    session,
    graph_name: str,
    label: str,
    rel_type: str,
    anchor_label: str,
) -> None:
    """Project a GDS graph with natural citation direction (downstream) and anchor seed edges."""
    session.run(
        """
        CALL gds.graph.project(
            $graph,
            $labels,
            {
                CITES_REV: {type: $rel_type, orientation: 'NATURAL'},
                ANALYTICS_SEED: {type: 'ANALYTICS_SEED', orientation: 'NATURAL'}
            }
        )
        """,
        {
            "graph": graph_name,
            "labels": [label, anchor_label],
            "rel_type": rel_type,
        },
    )


def _ensure_anchor(session, anchor_label: str, anchor_name: str) -> int:
    """Create or get anchor node."""
    res = _run_single(
        session,
        f"""
        MERGE (a:{anchor_label} {{name: $name}})
        RETURN id(a) AS id
        """,
        {"name": anchor_name},
    )
    return int(res.get("id", -1))


def _prepare_seed_edges(
    session,
    label: str,
    anchor_label: str,
    anchor_name: str,
    seed_prop: str,
) -> int:
    """Create anchor node and connect to all seed papers."""
    anchor_id = _ensure_anchor(session, anchor_label, anchor_name)
    session.run(
        f"""
        MATCH (a:{anchor_label} {{name: $name}})
        MATCH (w:{label})
        WHERE w.{seed_prop} = true
        MERGE (a)-[:ANALYTICS_SEED]->(w)
        """,
        {"name": anchor_name},
    )
    return anchor_id


def _write_reachable_batch(
    session, label: str, batch: list[dict], reach_prop: str, hops_prop: str
) -> None:
    """Batch write reachable nodes with their hop distances."""
    session.run(
        f"""
        UNWIND $batch AS row
        MATCH (w:{label} {{id: row.id}})
        SET w.{hops_prop} = row.hops
        """,
        {"batch": batch},
    )


def compute_reachability_gds(
    driver,
    label: str,
    rel_type: str,
    reach_prop: str,
    hops_prop: str,
    graph_name: str,
    anchor_label: str,
    anchor_name: str,
    seed_prop: str,
    batch_size: int = BATCH_SIZE,
) -> None:
    """
    Compute seed reachability using GDS Delta-Stepping shortest path.
    Uses an anchor to find minimum hops to the nearest selected seed paper.
    """
    print(
        f"[reach:gds] Computing reachability on :{label} via :{rel_type} using GDS Delta-Stepping"
    )

    with driver.session(database=NEO4J_DATABASE) as session:
        # 1. Create anchor and connect to all seed papers
        print("[reach:gds] Creating anchor node and seed edges...")
        anchor_id = _prepare_seed_edges(
            session, label, anchor_label, anchor_name, seed_prop
        )
        if anchor_id < 0:
            raise RuntimeError("Failed to create patent anchor node.")

        if DEBUG:
            debug_seed_count = _run_single(
                session,
                f"""
                MATCH (a:{anchor_label})-[r:ANALYTICS_SEED]->()
                RETURN count(r) AS c
                """,
                {},
            ).get("c", 0)
            _debug(
                f"[reach:gds] DEBUG: Created {debug_seed_count} "
                "ANALYTICS_SEED relationships"
            )

        # Count total nodes and seed papers
        total_nodes = _count_label(session, label)
        seed_count = _run_single(
            session,
            f"""
            MATCH (:{anchor_label} {{name: $name}})-[:ANALYTICS_SEED]->(w:{label})
            RETURN count(w) AS c
            """,
            {"name": anchor_name},
        ).get("c", 0)
        print(f"[reach:gds] Total nodes: {total_nodes}, seeded {seed_count} papers")

        # 2. Project GDS graph
        print("[reach:gds] Projecting graph into GDS...")
        try:
            _drop_gds_graph(session, graph_name)
        except Exception:
            pass
        _project_gds_graph(session, graph_name, label, rel_type, anchor_label)

        if DEBUG:
            gds_stats = _run_single(
                session,
                """
                CALL gds.graph.list($graph)
                YIELD nodeCount, relationshipCount
                RETURN nodeCount, relationshipCount
                """,
                {"graph": graph_name}
            )
            _debug(f"[reach:gds] DEBUG: GDS graph has {gds_stats.get('nodeCount', 0)} nodes, {gds_stats.get('relationshipCount', 0)} relationships")

        # 3. Run Delta-Stepping single-source shortest path
        print(f"[reach:gds] Running Delta-Stepping from anchor (id={anchor_id})...")
        print("[reach:gds] NOTE: Streaming 26M+ results is SLOW. Consider using Cypher mode instead.")

        results = session.run(
            """
            CALL gds.allShortestPaths.delta.stream($graph, {
                sourceNode: $source,
                delta: 2.0
            })
            YIELD targetNode, totalCost
            WITH gds.util.asNode(targetNode) AS work_node, totalCost
            WHERE totalCost > 0
            RETURN work_node.id AS work_id, toInteger(totalCost - 1) AS hops
            """,
            {"graph": graph_name, "source": anchor_id},
        )

        # 4. Batch write reachable nodes
        print("[reach:gds] Writing reachable nodes with distances...")
        batch = []
        reachable_count = 0
        result_count = 0

        with tqdm(desc="Writing reachable", unit="node") as pbar:
            for record in results:
                result_count += 1
                if DEBUG and result_count <= 5:
                    _debug(f"[reach:gds] DEBUG: result {result_count}: work_id={record['work_id']}, hops={record['hops']}")

                batch.append({
                    "id": record["work_id"],
                    "hops": record["hops"],
                })

                if len(batch) >= batch_size:
                    _write_reachable_batch(session, label, batch, reach_prop, hops_prop)
                    reachable_count += len(batch)
                    pbar.update(len(batch))
                    batch = []

            if batch:
                _write_reachable_batch(session, label, batch, reach_prop, hops_prop)
                reachable_count += len(batch)
                pbar.update(len(batch))

        _debug(f"[reach:gds] DEBUG: Delta-Stepping returned {result_count} total results")
        print(f"[reach:gds] Marked {reachable_count} nodes as reachable")

        # 5. Mark unreachable nodes
        print("[reach:gds] Counting unreachable nodes...")
        unreachable_count = 0
        last_id = -1
        expected_unreachable = total_nodes - reachable_count

        with tqdm(
            desc="Writing unreachable",
            unit="node",
            total=expected_unreachable
        ) as pbar:
            while True:
                res = _run_single(
                    session,
                    f"""
                    MATCH (w:{label})
                    WHERE w.{hops_prop} IS NULL AND id(w) > $last_id
                    WITH w ORDER BY id(w) LIMIT $limit
                    RETURN count(w) AS seen, max(id(w)) AS max_id
                    """,
                    {"last_id": last_id, "limit": batch_size},
                )

                seen = res.get("seen", 0) if res else 0
                max_id = res.get("max_id") if res else None

                if seen == 0 or max_id is None:
                    break

                unreachable_count += seen
                pbar.update(seen)
                last_id = max_id

        print(f"[reach:gds] Counted {unreachable_count} unreachable nodes")
        print(
            f"[reach:gds] Total: {reachable_count + unreachable_count} nodes processed"
        )

        if DEBUG:
            _debug("[reach:gds] DEBUG: Final validation...")
            validation = _run_single(
                session,
                f"""
                MATCH (w:{label})
                RETURN
                    count(w) AS total,
                    sum(CASE WHEN w.{hops_prop} IS NOT NULL THEN 1 ELSE 0 END) AS reachable,
                    sum(CASE WHEN w.{hops_prop} IS NULL THEN 1 ELSE 0 END) AS unreachable
                """,
                {}
            )
            _debug(f"[reach:gds] DEBUG: Validation - total={validation.get('total')}, "
                  f"reachable={validation.get('reachable')}, "
                  f"unreachable={validation.get('unreachable')}")

            hops_dist = session.run(
                f"""
                MATCH (w:{label})
                WHERE w.{hops_prop} IS NOT NULL
                RETURN w.{hops_prop} AS hops, count(*) AS count
                ORDER BY hops
                LIMIT 10
                """
            )
            _debug("[reach:gds] DEBUG: Hops distribution (first 10 levels):")
            for rec in hops_dist:
                _debug(f"[reach:gds] DEBUG:   hops={rec['hops']} -> {rec['count']} nodes")

        # 6. Cleanup
        print("[reach:gds] Cleaning up anchor and GDS graph...")
        _drop_gds_graph(session, graph_name)

        session.run(
            f"""
            MATCH (a:{anchor_label} {{name: $name}})-[r:ANALYTICS_SEED]->()
            DELETE r
            """,
            {"name": anchor_name},
        )
        session.run(
            f"""
            MATCH (a:{anchor_label} {{name: $name}})
            DELETE a
            """,
            {"name": anchor_name},
        )

    print("[reach:gds] Done.")
