"""PAR source aggregation core logic."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ref_map.common.mongo_enrichment import enrich_source_names
from ref_map.common.neo4j_tokens import validate_property_key, validate_token

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"
PARTITION_ID_PREFIX = "part"
SOURCE_SCOPES = ("all", "articles")


def _validate_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    resolved = tuple(dict.fromkeys(scopes))
    if not resolved:
        raise ValueError("At least one source aggregation scope is required")
    invalid = [scope for scope in resolved if scope not in SOURCE_SCOPES]
    if invalid:
        raise ValueError(f"Unknown source aggregation scopes: {invalid}")
    return resolved


def compute_multi_property_source_aggregation(
    session,
    *,
    label: str,
    score_properties: Mapping[str, str],
    scopes: Sequence[str] = ("all",),
    min_papers: int = 1,
    num_partitions: int = 1,
    partition_id: int = 0,
    enrich_names: bool = True,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Aggregate several PAR properties and scopes in one ``Work`` scan.

    The returned structure is ``scope -> property key -> source rows``. Only
    metrics needed downstream are emitted: paper count, scored-paper count,
    total PAR, and PAR averaged over every paper (missing scores count as zero).
    """
    label = validate_token(label, kind="node label")
    scopes = _validate_scopes(scopes)
    if not score_properties:
        raise ValueError("At least one PAR property is required")
    if min_papers < 1:
        raise ValueError("min_papers must be >= 1")
    if num_partitions < 1:
        raise ValueError("num_partitions must be >= 1")
    if not 0 <= partition_id < num_partitions:
        raise ValueError("partition_id must be in [0, num_partitions)")

    property_items = [
        (str(key), validate_property_key(prop))
        for key, prop in score_properties.items()
    ]
    if any(not key for key, _ in property_items):
        raise ValueError("PAR property keys must be non-empty")

    score_expressions = []
    for index, (_, prop) in enumerate(property_items):
        score_expressions.append(
            f"""
            CASE
                WHEN valueType(w.`{prop}`) IN [
                    'INTEGER', 'FLOAT', 'INTEGER NOT NULL', 'FLOAT NOT NULL'
                ]
                THEN toFloat(w.`{prop}`)
                ELSE null
            END AS score_{index}
            """.strip()
        )

    aggregates: list[str] = []
    returns: list[str] = []
    for scope in scopes:
        condition = "true" if scope == "all" else "is_article"
        aggregates.append(
            (
                "count(*)" if scope == "all" else
                "sum(CASE WHEN is_article THEN 1 ELSE 0 END)"
            )
            + f" AS {scope}_papers"
        )
        returns.append(f"{scope}_papers")
        for index, _ in enumerate(property_items):
            aggregates.extend(
                [
                    f"sum(CASE WHEN {condition} AND score_{index} IS NOT NULL "
                    f"THEN 1 ELSE 0 END) AS {scope}_scored_{index}",
                    f"sum(CASE WHEN {condition} THEN coalesce(score_{index}, 0.0) "
                    f"ELSE 0.0 END) AS {scope}_total_{index}",
                ]
            )
            returns.extend(
                [f"{scope}_scored_{index}", f"{scope}_total_{index}"]
            )

    article_filter = (
        "AND toLower(coalesce(w.type, '')) = 'article'"
        if scopes == ("articles",)
        else ""
    )
    query = f"""
    MATCH (w:`{label}`)
    WHERE w.primary_source_id IS NOT NULL
      {article_filter}
      AND (
        $num_partitions = 1
        OR (
          CASE
            WHEN w.primary_source_id =~ '^[A-Za-z]\\d+$'
            THEN toInteger(substring(w.primary_source_id, 1))
            ELSE 0
          END
        ) % $num_partitions = $partition_id
      )
    WITH w.primary_source_id AS source_id,
         w.primary_source_name AS source_name,
         w.primary_source_type AS source_type,
         toLower(coalesce(w.type, '')) = 'article' AS is_article,
         {", ".join(score_expressions)}
    WITH source_id,
         max(source_name) AS source_name,
         max(source_type) AS source_type,
         {", ".join(aggregates)}
    RETURN source_id, source_name, source_type, {", ".join(returns)}
    ORDER BY source_id
    """

    print(
        "[par-agg] One-pass aggregation: "
        f"properties={len(property_items)}, scopes={','.join(scopes)}, "
        f"partition={partition_id}/{num_partitions}"
    )
    raw: dict[str, dict[str, list[dict[str, Any]]]] = {
        scope: {key: [] for key, _ in property_items} for scope in scopes
    }
    source_meta: dict[str, dict[str, Any]] = {}
    for record in session.run(
        query,
        {
            "num_partitions": num_partitions,
            "partition_id": partition_id,
        },
    ):
        source_id = record["source_id"]
        source_meta[source_id] = {
            "source_id": source_id,
            "source_name": record["source_name"],
            "source_type": record["source_type"],
        }
        for scope in scopes:
            total_papers = int(record[f"{scope}_papers"] or 0)
            if total_papers < min_papers:
                continue
            for index, (key, _) in enumerate(property_items):
                total_score = float(record[f"{scope}_total_{index}"] or 0.0)
                raw[scope][key].append(
                    {
                        "source_id": source_id,
                        "total_papers": total_papers,
                        "scored_papers": int(
                            record[f"{scope}_scored_{index}"] or 0
                        ),
                        "total_score": round(total_score, 6),
                        "avg_score_all": round(total_score / total_papers, 6),
                    }
                )

    if enrich_names:
        enrich_source_names(list(source_meta.values()))
    for scope_rows in raw.values():
        for rows in scope_rows.values():
            for row in rows:
                meta = source_meta[row["source_id"]]
                row["source_name"] = meta.get("source_name")
                row["source_type"] = meta.get("source_type")
    return raw


def compute_source_aggregation(
    session,
    label: str,
    score_prop: str,
    min_papers: int = 1,
    num_partitions: int = 1,
    partition_id: int = 0,
    article_only: bool = False,
) -> list[dict[str, Any]]:
    """Aggregate one PAR property through the canonical multi-property core."""
    scope = "articles" if article_only else "all"
    result = compute_multi_property_source_aggregation(
        session,
        label=label,
        score_properties={"score": score_prop},
        scopes=(scope,),
        min_papers=min_papers,
        num_partitions=num_partitions,
        partition_id=partition_id,
    )
    return result[scope]["score"]


def save_results(results: list[dict[str, Any]], output_path: Path) -> None:
    """Save PAR source aggregation results."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"[par-agg] Results written to: {output_path}")


def merge_partition_outputs(
    output_dir: Path,
    merged_name: str,
    num_partitions: int,
) -> Path:
    """Merge PAR partition outputs into a single JSON."""
    merged: list[dict[str, Any]] = []
    for partition_id in range(num_partitions):
        part_name = f"{merged_name}_{PARTITION_ID_PREFIX}{partition_id}.json"
        part_path = output_dir / part_name
        if not part_path.exists():
            raise FileNotFoundError(f"Missing partition output: {part_path}")
        with part_path.open("r", encoding="utf-8") as f:
            merged.extend(json.load(f))

    merged_path = output_dir / f"{merged_name}.json"
    with merged_path.open("w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    return merged_path


def property_has_values(session, label: str, prop: str) -> bool:
    """Check whether a property has any non-null values on the label."""
    prop_ref = f"`{prop}`"
    query = f"""
    MATCH (w:{label})
    WHERE w.{prop_ref} IS NOT NULL
    RETURN count(w) AS c
    """
    res = session.run(query).single()
    return bool(res and res["c"] > 0)
