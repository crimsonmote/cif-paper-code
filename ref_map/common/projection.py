"""Shared GDS projection helpers."""

from __future__ import annotations

from ref_map.common.neo4j_tokens import validate_token


def _q_token(token: str) -> str:
    return f"`{token}`"


def project_gds_graph(
    session,
    graph_name: str,
    label: str,
    rel_type: str,
    hops_prop: str,
    min_year: int | None = None,
    max_year: int | None = None,
) -> None:
    """
    Project GDS graph with ONLY reachable nodes (two-step: project + filter).

    Native projection doesn't support inline filtering, so we:
    1. Project all nodes
    2. Filter to reachable nodes using gds.graph.filter()
    """
    label = validate_token(label, kind="node label")
    rel_type = validate_token(rel_type, kind="relationship type")
    label_ref = _q_token(label)
    rel_ref = _q_token(rel_type)
    prop_ref = _q_token(hops_prop)

    print(f"[reach-count-gds] Projecting GDS graph '{graph_name}'...")

    debug_query = f"""
    MATCH (n:{label_ref})
    WITH
        count(n) AS total,
        sum(CASE WHEN n.{prop_ref} IS NOT NULL THEN 1 ELSE 0 END) AS with_prop,
        sum(CASE WHEN n.{prop_ref} >= 0 THEN 1 ELSE 0 END) AS reachable
    RETURN total, with_prop, reachable
    """
    debug_result = session.run(debug_query).single()
    print(
        f"[reach-count-gds] Total nodes in DB: {debug_result['total']:,}, "
        f"with {hops_prop}: {debug_result['with_prop']:,}, "
        f"reachable ({hops_prop} >= 0): {debug_result['reachable']:,}"
    )
    if debug_result["reachable"] == 0:
        raise ValueError(
            f"No reachable nodes found! Please run node_reachability.py first to compute {hops_prop}"
        )

    temp_graph_name = f"{graph_name}_temp"
    for gname in [graph_name, temp_graph_name]:
        try:
            session.run(
                "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName",
                {"graph_name": gname},
            ).consume()
            print(f"[reach-count-gds] Dropped existing graph '{gname}'")
        except Exception:
            pass

    if (min_year is not None) or (max_year is not None):
        year_msg_parts: list[str] = []
        if min_year is not None:
            year_msg_parts.append(f"publication_year >= {min_year}")
        if max_year is not None:
            year_msg_parts.append(f"publication_year <= {max_year}")
        print(
            "[reach-count-gds] Using selected cypher projection "
            f"({hops_prop} >= 0 + {' AND '.join(year_msg_parts)})"
        )
        try:
            session.run(
                "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName",
                {"graph_name": graph_name},
            ).consume()
        except Exception:
            pass
        year_clauses: list[str] = []
        if min_year is not None:
            year_clauses.append(f"n.publication_year >= {min_year}")
        if max_year is not None:
            year_clauses.append(f"n.publication_year <= {max_year}")
        year_filter_n = " AND ".join(year_clauses) if year_clauses else "true"

        year_clauses_m: list[str] = []
        if min_year is not None:
            year_clauses_m.append(f"m.publication_year >= {min_year}")
        if max_year is not None:
            year_clauses_m.append(f"m.publication_year <= {max_year}")
        year_filter_m = " AND ".join(year_clauses_m) if year_clauses_m else "true"
        node_query = f"""
        MATCH (n:{label_ref})
        WHERE n.{prop_ref} >= 0
          AND n.publication_year IS NOT NULL
          AND {year_filter_n}
        RETURN id(n) AS id
        """
        rel_query = f"""
        MATCH (n:{label_ref})-[:{rel_ref}]->(m:{label_ref})
        WHERE n.{prop_ref} >= 0
          AND m.{prop_ref} >= 0
          AND n.publication_year IS NOT NULL
          AND m.publication_year IS NOT NULL
          AND {year_filter_n}
          AND {year_filter_m}
        RETURN id(n) AS source, id(m) AS target
        """
        result = session.run(
            """
            CALL gds.graph.project.cypher(
                $graph_name,
                $node_query,
                $rel_query,
                {validateRelationships: false}
            )
            YIELD nodeCount, relationshipCount, projectMillis
            RETURN nodeCount, relationshipCount, projectMillis
            """,
            {
                "graph_name": graph_name,
                "node_query": node_query,
                "rel_query": rel_query,
            },
        ).single()
        print(
            f"[reach-count-gds] Projected {result['nodeCount']:,} nodes, "
            f"{result['relationshipCount']:,} rels"
        )
        print(
            f"[reach-count-gds] Projection took {result['projectMillis']/1000:.1f} seconds"
        )
        if result["nodeCount"] == 0:
            raise ValueError("Year-filtered projection resulted in 0 nodes.")
        return

    print(f"[reach-count-gds] Step 1: Project all {label} nodes...")

    query = f"""
    CALL gds.graph.project(
        $temp_graph_name,
        {{
            {label_ref}: {{
                properties: {{
                    {prop_ref}: {{
                        property: '{hops_prop}',
                        defaultValue: -1
                    }}
                }}
            }}
        }},
        {{
            {rel_ref}: {{
                orientation: 'NATURAL'
            }}
        }}
    )
    YIELD nodeCount, relationshipCount, projectMillis
    RETURN nodeCount, relationshipCount, projectMillis
    """

    result = session.run(query, {"temp_graph_name": temp_graph_name}).single()
    print(
        f"[reach-count-gds] Projected {result['nodeCount']:,} nodes, "
        f"{result['relationshipCount']:,} rels"
    )
    print(f"[reach-count-gds] Projection took {result['projectMillis']/1000:.1f} seconds")

    print(f"[reach-count-gds] Step 2: Filtering to reachable nodes ({hops_prop} >= 0)...")
    filter_query = f"""
    CALL gds.graph.filter(
        $graph_name,
        $temp_graph_name,
        'n.{hops_prop} >= 0',
        '*'
    )
    YIELD nodeCount, relationshipCount
    RETURN nodeCount, relationshipCount
    """

    result = session.run(
        filter_query,
        {
            "graph_name": graph_name,
            "temp_graph_name": temp_graph_name,
        },
    ).single()

    print(
        f"[reach-count-gds] Filtered to {result['nodeCount']:,} reachable nodes, "
        f"{result['relationshipCount']:,} rels"
    )

    if result["nodeCount"] == 0:
        raise ValueError(
            f"Filter resulted in 0 nodes! This suggests {hops_prop} property values may not be set correctly."
        )

    try:
        session.run(
            "CALL gds.graph.drop($graph_name) YIELD graphName RETURN graphName",
            {"graph_name": temp_graph_name},
        ).consume()
        print("[reach-count-gds] Cleaned up temp graph")
    except Exception:
        pass


def project_gds_graph_from_labels(
    session,
    graph_name: str,
    node_labels: list[str],
    rel_type: str,
) -> None:
    """Project a graph using native node-label selection (union of labels)."""
    rel = validate_token(rel_type, kind="relationship type")
    rel_ref = _q_token(rel)

    cleaned_labels = [
        validate_token(label, kind="node label") for label in node_labels if label
    ]
    if not cleaned_labels:
        raise ValueError("No node labels provided for label-based projection.")

    try:
        session.run(
            "CALL gds.graph.drop($graph_name, false) YIELD graphName RETURN graphName",
            {"graph_name": graph_name},
        ).consume()
        print(f"[reach-count-gds] Dropped existing graph '{graph_name}'")
    except Exception:
        pass

    print(
        f"[reach-count-gds] Label projection: {len(cleaned_labels):,} labels "
        f"({cleaned_labels[0]}..{cleaned_labels[-1]})"
    )
    result = session.run(
        f"""
        CALL gds.graph.project(
            $graph_name,
            $node_labels,
            {{
                {rel_ref}: {{
                    orientation: 'NATURAL'
                }}
            }}
        )
        YIELD nodeCount, relationshipCount, projectMillis
        RETURN nodeCount, relationshipCount, projectMillis
        """,
        {"graph_name": graph_name, "node_labels": cleaned_labels},
    ).single()
    print(
        f"[reach-count-gds] Projected {result['nodeCount']:,} nodes, "
        f"{result['relationshipCount']:,} rels"
    )
    print(f"[reach-count-gds] Projection took {result['projectMillis']/1000:.1f} seconds")
    if result["nodeCount"] == 0:
        raise ValueError("Label-based projection resulted in 0 nodes.")
