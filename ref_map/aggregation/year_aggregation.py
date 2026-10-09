"""Year-level reachability and PAR aggregation by publication year."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from neo4j import GraphDatabase

from ref_map.common.config import (
    NEO4J_DATABASE,
    add_seed_mode_argument,
    hops_prop_for_mode,
    par_prop_for_mode,
    prefix_for_mode,
    resolve_seed_mode,
)
from ref_map.export.neo4j_export import _resolve_neo4j_config

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"


def _parse_depth_thresholds(raw: str) -> list[int]:
    if not raw:
        return []
    values: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        values.append(int(token))
    return sorted({v for v in values if v >= 0})


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate reachability and PAR metrics by publication year."
    )
    parser.add_argument(
        "--target",
        choices=["reachability", "par", "both"],
        default="both",
        help="Aggregation target (default: both).",
    )
    parser.add_argument(
        "--mode",
        choices=["test", "full"],
        required=True,
        help="Data mode.",
    )
    add_seed_mode_argument(
        parser,
        help_text="Seed universe used to resolve default hops and PAR properties.",
    )
    parser.add_argument(
        "--hops-prop",
        default=None,
        help="Override hops property.",
    )
    parser.add_argument(
        "--par-prop",
        default=None,
        help="Override PAR property.",
    )
    parser.add_argument(
        "--depth-thresholds",
        default="",
        help="Comma-separated depth thresholds (e.g. 0,1,2,5,10).",
    )
    parser.add_argument(
        "--article-only",
        action="store_true",
        help="Restrict aggregation input to works with type='article'.",
    )
    parser.add_argument(
        "--min-papers",
        type=int,
        default=1,
        help="Minimum papers required for a year bucket (default: 1).",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output JSON path (default under ref_map/aggregation/output).",
    )
    return parser.parse_args()


def compute_year_aggregation(
    session,
    *,
    label: str,
    hops_prop: str,
    par_prop: str,
    depth_thresholds: list[int],
    article_only: bool,
    min_papers: int,
) -> list[dict[str, Any]]:
    """Aggregate node-level reachability and PAR metrics to publication-year buckets."""
    hops_ref = f"`{hops_prop}`"
    par_ref = f"`{par_prop}`"

    depth_select = ""
    depth_return = ""
    for d in depth_thresholds:
        depth_select += (
            f", sum(CASE WHEN hop_num IS NOT NULL AND hop_num <= {d} THEN 1 ELSE 0 END) "
            f"AS reachable_papers_d{d}\n"
        )
        depth_return += (
            f", reachable_papers_d{d}\n"
            f", total_papers - reachable_papers_d{d} AS unreachable_papers_d{d}\n"
            f", toFloat(reachable_papers_d{d}) / total_papers AS reachability_rate_d{d}\n"
        )

    query = f"""
    MATCH (w:{label})
    WHERE w.publication_year IS NOT NULL
      AND ($article_only = false OR toLower(coalesce(w.type, '')) = 'article')
    WITH toInteger(w.publication_year) AS publication_year,
         CASE
             WHEN w.{hops_ref} IS NULL THEN null
             WHEN valueType(w.{hops_ref}) STARTS WITH 'LIST' THEN null
             WHEN valueType(w.{hops_ref}) IN ['INTEGER', 'FLOAT', 'INTEGER NOT NULL', 'FLOAT NOT NULL']
             THEN toFloat(w.{hops_ref})
             ELSE null
         END AS hop_num,
         CASE
             WHEN w.{par_ref} IS NULL THEN null
             WHEN valueType(w.{par_ref}) STARTS WITH 'LIST' THEN null
             WHEN valueType(w.{par_ref}) IN ['INTEGER', 'FLOAT', 'INTEGER NOT NULL', 'FLOAT NOT NULL']
             THEN toFloat(w.{par_ref})
             ELSE null
         END AS score_num
    WITH publication_year, hop_num, score_num, coalesce(score_num, 0.0) AS score_all
    WITH publication_year,
         count(*) AS total_papers,
         sum(CASE WHEN hop_num IS NOT NULL THEN 1 ELSE 0 END) AS reachable_papers,
         avg(hop_num) AS avg_min_hops,
         percentileCont(hop_num, 0.5) AS median_min_hops,
         sum(CASE WHEN score_num IS NOT NULL THEN 1 ELSE 0 END) AS scored_papers,
         sum(score_all) AS par_total_score,
         avg(score_all) AS par_avg_score_all,
         avg(score_num) AS par_avg_score_scored
         {depth_select}
    WHERE total_papers >= $min_papers
    RETURN
        publication_year,
        total_papers,
        reachable_papers,
        total_papers - reachable_papers AS unreachable_papers,
        toFloat(reachable_papers) / total_papers AS reachability_rate,
        avg_min_hops,
        median_min_hops,
        scored_papers,
        par_total_score,
        par_avg_score_all,
        par_avg_score_scored
        {depth_return}
    ORDER BY publication_year
    """

    rows: list[dict[str, Any]] = []
    for record in session.run(
        query,
        {
            "article_only": article_only,
            "min_papers": min_papers,
        },
    ):
        row: dict[str, Any] = {
            "publication_year": int(record["publication_year"]),
            "total_papers": int(record["total_papers"]),
            "reachable_papers": int(record["reachable_papers"]),
            "unreachable_papers": int(record["unreachable_papers"]),
            "reachability_rate": round(float(record["reachability_rate"]), 6),
            "avg_min_hops": (
                round(record["avg_min_hops"], 6)
                if record["avg_min_hops"] is not None
                else None
            ),
            "median_min_hops": (
                round(record["median_min_hops"], 6)
                if record["median_min_hops"] is not None
                else None
            ),
            "scored_papers": int(record["scored_papers"]),
            "par_total_score": round(float(record["par_total_score"] or 0.0), 6),
            "par_avg_score_all": (
                round(record["par_avg_score_all"], 6)
                if record["par_avg_score_all"] is not None
                else 0.0
            ),
            "par_avg_score_scored": (
                round(record["par_avg_score_scored"], 6)
                if record["par_avg_score_scored"] is not None
                else None
            ),
        }
        for d in depth_thresholds:
            reach_key = f"reachable_papers_d{d}"
            unreach_key = f"unreachable_papers_d{d}"
            rate_key = f"reachability_rate_d{d}"
            row[reach_key] = int(record.get(reach_key) or 0)
            row[unreach_key] = int(record.get(unreach_key) or 0)
            rate_val = record.get(rate_key)
            row[rate_key] = round(rate_val, 6) if rate_val is not None else None
        rows.append(row)
    return rows


def _filter_target_columns(
    rows: list[dict[str, Any]],
    *,
    target: str,
    depth_thresholds: list[int],
) -> list[dict[str, Any]]:
    if target == "both":
        return rows

    base_keys = {"publication_year", "total_papers"}
    reach_keys = {
        "reachable_papers",
        "unreachable_papers",
        "reachability_rate",
        "avg_min_hops",
        "median_min_hops",
    }
    for d in depth_thresholds:
        reach_keys.update(
            {
                f"reachable_papers_d{d}",
                f"unreachable_papers_d{d}",
                f"reachability_rate_d{d}",
            }
        )
    par_keys = {
        "scored_papers",
        "par_total_score",
        "par_avg_score_all",
        "par_avg_score_scored",
    }

    keep = base_keys | (reach_keys if target == "reachability" else par_keys)
    return [{k: row[k] for k in row if k in keep} for row in rows]


def main() -> None:
    args = _parse_args()
    seed_mode = resolve_seed_mode(args.seed_mode)
    prefix = prefix_for_mode(seed_mode)
    is_test = args.mode == "test"
    label = os.environ.get("ANALYTICS_LABEL", "WorkTest") if is_test else "Work"
    default_hops = hops_prop_for_mode(prefix, is_test=is_test)
    hops_prop = args.hops_prop or os.environ.get("ANALYTICS_HOPS_PROP", default_hops)
    default_par = par_prop_for_mode(prefix)
    par_prop = args.par_prop or default_par
    depth_thresholds = _parse_depth_thresholds(args.depth_thresholds)

    if args.output:
        output_path = Path(args.output)
    else:
        article_suffix = "_articles" if args.article_only else ""
        output_path = (
            DEFAULT_OUTPUT_DIR
            / f"year_aggregate_{prefix}_{args.target}_{args.mode}{article_suffix}.json"
        )

    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    print(f"[config] Mode: {args.mode}, label: {label}")
    print(f"[config] Seed mode: {seed_mode}, prefix: {prefix}")
    print(f"[config] Target: {args.target}")
    print(f"[config] Hops property: {hops_prop}")
    print(f"[config] PAR property: {par_prop}")
    print(f"[config] Article only: {args.article_only}")
    print(f"[config] Min papers per year: {args.min_papers}")
    if depth_thresholds:
        print(f"[config] Depth thresholds: {depth_thresholds}")
    print(f"[config] Output: {output_path}")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session(database=NEO4J_DATABASE) as session:
            rows = compute_year_aggregation(
                session,
                label=label,
                hops_prop=hops_prop,
                par_prop=par_prop,
                depth_thresholds=depth_thresholds,
                article_only=args.article_only,
                min_papers=args.min_papers,
            )
    finally:
        driver.close()

    rows = _filter_target_columns(
        rows,
        target=args.target,
        depth_thresholds=depth_thresholds,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)

    print(f"[year-agg] Results written to: {output_path}")
    print(f"[year-agg] Year buckets: {len(rows):,}")
    if rows:
        print(
            f"[year-agg] Year range: {rows[0]['publication_year']}..{rows[-1]['publication_year']}"
        )


if __name__ == "__main__":
    main()
