"""Shared PAR core helpers used by full-pass and lagged entrypoints."""

from __future__ import annotations

from typing import Any
import re


def damping_suffix(damping: float) -> str:
    return f"{damping:.2f}"


def compose_write_prop(
    base_prop: str,
    lag_years: int | None = None,
    damping_factor: float | None = None,
) -> str:
    """Compose output property name from base + optional lag/damping suffixes."""
    prop = re.sub(r"_lag\d+$", "", base_prop)
    if lag_years is not None:
        prop = f"{prop}_lag{lag_years}"
    if damping_factor is not None:
        prop = f"{prop}_{damping_suffix(damping_factor)}"
    return prop


def ensure_index(session, label: str, prop: str) -> None:
    """Ensure an index exists for the written property."""
    prop_ref = f"`{prop}`"
    session.run(
        f"CREATE INDEX IF NOT EXISTS FOR (w:{label}) ON (w.{prop_ref})"
    ).consume()


def get_seed_neo4j_ids(
    session,
    label: str,
    hops_prop: str,
    seed_prop: str,
    min_year: int | None = None,
    max_year: int | None = None,
) -> list[int]:
    """Collect Neo4j internal IDs of seed papers under optional year bounds."""
    query = f"""
    MATCH (w:{label})
    WHERE w.{seed_prop} = true
      AND w.{hops_prop} >= 0
      AND (
        $min_year IS NULL
        OR (w.publication_year IS NOT NULL AND w.publication_year >= $min_year)
      )
      AND (
        $max_year IS NULL
        OR (w.publication_year IS NOT NULL AND w.publication_year <= $max_year)
      )
    RETURN id(w) AS neo4j_id
    """
    ids: list[int] = []
    for record in session.run(
        query,
        {"min_year": min_year, "max_year": max_year},
    ):
        nid = record.get("neo4j_id")
        if nid is not None:
            ids.append(nid)
    return ids


def run_article_rank_on_graph(
    session,
    graph_name: str,
    write_prop: str,
    source_ids: list[int],
    max_iterations: int | None = None,
    tolerance: float | None = None,
    damping_factor: float | None = None,
    scaler: str | None = None,
) -> dict[str, int]:
    """Run GDS ArticleRank with personalized source nodes and write scores."""
    config: dict[str, Any] = {
        "sourceNodes": source_ids,
        "writeProperty": write_prop,
    }
    if max_iterations is not None:
        config["maxIterations"] = max_iterations
    if tolerance is not None:
        config["tolerance"] = tolerance
    if damping_factor is not None:
        config["dampingFactor"] = damping_factor
    if scaler:
        config["scaler"] = scaler

    print("[par] Running personalized ArticleRank...")
    result = session.run(
        """
        CALL gds.articleRank.write($graph_name, $config)
        YIELD nodePropertiesWritten, ranIterations
        RETURN nodePropertiesWritten, ranIterations
        """,
        {"graph_name": graph_name, "config": config},
    ).single()

    if result:
        print(
            f"[par] Wrote {result['nodePropertiesWritten']:,} properties "
            f"in {result['ranIterations']} iterations"
        )
        return {
            "node_properties_written": int(result["nodePropertiesWritten"]),
            "ran_iterations": int(result["ranIterations"]),
        }
    return {"node_properties_written": 0, "ran_iterations": 0}
