"""Reachability/direct source aggregation core logic."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from tqdm import tqdm

from ref_map.common.mongo_enrichment import enrich_source_names

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "output"
PARTITION_ID_PREFIX = "part"


def compute_source_aggregation(
    session,
    label: str,
    hops_prop: str,
    min_papers: int = 1,
    num_partitions: int = 1,
    partition_id: int = 0,
    depth_thresholds: list[int] | None = None,
    article_only: bool = False,
) -> list[dict[str, Any]]:
    """Aggregate node-level reachability to source-level metrics."""
    print(f"[source-agg] Computing source-level aggregation on :{label}")
    print(f"[source-agg] Minimum papers per source: {min_papers}")
    print(f"[source-agg] Article only: {article_only}")
    if num_partitions > 1:
        print(
            f"[source-agg] Partitioning by source_id: {partition_id}/{num_partitions}"
        )

    depth_thresholds = sorted({d for d in (depth_thresholds or []) if d >= 0})
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
    WHERE w.primary_source_id IS NOT NULL
      AND ($article_only = false OR toLower(coalesce(w.type, '')) = 'article')
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
         CASE
             WHEN w.{hops_prop} IS NULL THEN null
             WHEN valueType(w.{hops_prop}) STARTS WITH 'LIST' THEN null
             WHEN valueType(w.{hops_prop}) IN ['INTEGER', 'FLOAT', 'INTEGER NOT NULL', 'FLOAT NOT NULL']
             THEN toFloat(w.{hops_prop})
             ELSE null
         END AS hop_num
    WITH source_id,
         max(source_name) AS source_name,
         max(source_type) AS source_type,
         count(*) AS total_papers,
         sum(CASE WHEN source_type = 'j' THEN 1 ELSE 0 END) AS journal_papers,
         sum(CASE WHEN source_type = 'c' THEN 1 ELSE 0 END) AS conference_papers,
         sum(CASE WHEN hop_num IS NOT NULL THEN 1 ELSE 0 END) AS reachable_papers,
         avg(hop_num) AS avg_min_hops,
         percentileCont(hop_num, 0.5) AS median_min_hops
         {depth_select}
    WHERE total_papers >= $min_papers
    RETURN
        source_id,
        source_name,
        source_type,
        total_papers,
        journal_papers,
        conference_papers,
        reachable_papers,
        total_papers - reachable_papers AS unreachable_papers,
        toFloat(reachable_papers) / total_papers AS reachability_rate,
        avg_min_hops,
        median_min_hops
        {depth_return}
    ORDER BY reachable_papers DESC
    """

    results: list[dict[str, Any]] = []
    print("[source-agg] Executing aggregation query...")

    with tqdm(desc="Processing sources", unit="source") as pbar:
        for record in session.run(
            query,
            {
                "min_papers": min_papers,
                "num_partitions": num_partitions,
                "partition_id": partition_id,
                "article_only": article_only,
            },
        ):
            row: dict[str, Any] = {
                "source_id": record["source_id"],
                "source_name": record["source_name"],
                "source_type": record["source_type"],
                "total_papers": record["total_papers"],
                "journal_papers": record["journal_papers"],
                "conference_papers": record["conference_papers"],
                "reachable_papers": record["reachable_papers"],
                "unreachable_papers": record["unreachable_papers"],
                "reachability_rate": round(record["reachability_rate"], 4),
                "avg_min_hops": (
                    round(record["avg_min_hops"], 2)
                    if record["avg_min_hops"] is not None
                    else None
                ),
                "median_min_hops": (
                    round(record["median_min_hops"], 2)
                    if record["median_min_hops"] is not None
                    else None
                ),
            }

            for d in depth_thresholds:
                row[f"reachable_papers_d{d}"] = record.get(f"reachable_papers_d{d}")
                row[f"unreachable_papers_d{d}"] = record.get(
                    f"unreachable_papers_d{d}"
                )
                rate_key = f"reachability_rate_d{d}"
                rate_val = record.get(rate_key)
                row[rate_key] = round(rate_val, 4) if rate_val is not None else None

            results.append(row)
            pbar.update(1)

    enrich_source_names(results)
    return results


def compute_source_direct(
    session,
    label: str,
    rel_type: str,
    seed_prop: str,
    min_papers: int = 1,
    article_only: bool = False,
) -> list[dict[str, Any]]:
    """Direct source-level BFS without node-level reachability properties."""
    print(f"[source-direct] Computing source-level BFS on :{label}")
    print(f"[source-direct] Minimum papers per source: {min_papers}")
    print(f"[source-direct] Article only: {article_only}")

    print("[source-direct] Step 1: Extracting sources...")
    sources_query = f"""
    MATCH (w:{label})
    WHERE w.primary_source_id IS NOT NULL
      AND ($article_only = false OR toLower(coalesce(w.type, '')) = 'article')
    WITH w.primary_source_id AS source_id,
         w.primary_source_name AS source_name,
         w.primary_source_type AS source_type,
         count(w) AS total_papers
    WHERE total_papers >= $min_papers
    RETURN source_id, source_name, source_type, total_papers
    """

    sources: dict[str, dict[str, Any]] = {}
    for record in session.run(
        sources_query, {"min_papers": min_papers, "article_only": article_only}
    ):
        source_id = record["source_id"]
        sources[source_id] = {
            "source_id": source_id,
            "source_name": record["source_name"],
            "source_type": record["source_type"],
            "total_papers": record["total_papers"],
            "reachable": False,
            "min_hops_to_source": None,
        }

    print(f"[source-direct] Found {len(sources)} sources")

    print("[source-direct] Step 2: Extracting source citation graph...")
    citation_query = f"""
    MATCH (citing:{label})-[:{rel_type}]->(cited:{label})
    WHERE citing.primary_source_id IS NOT NULL
      AND cited.primary_source_id IS NOT NULL
      AND ($article_only = false OR toLower(coalesce(citing.type, '')) = 'article')
      AND ($article_only = false OR toLower(coalesce(cited.type, '')) = 'article')
    WITH DISTINCT citing.primary_source_id AS from_source,
                  cited.primary_source_id AS to_source
    RETURN from_source, to_source
    """

    source_cited_by: dict[str, list[str]] = {}
    with tqdm(desc="Building citation graph", unit="edge") as pbar:
        for record in session.run(citation_query, {"article_only": article_only}):
            from_src = record["from_source"]
            to_src = record["to_source"]
            if from_src in sources and to_src in sources:
                if to_src not in source_cited_by:
                    source_cited_by[to_src] = []
                source_cited_by[to_src].append(from_src)
            pbar.update(1)

    print(
        f"[source-direct] Citation graph has {len(source_cited_by)} sources with inbound citations"
    )

    print("[source-direct] Step 3: Finding seed sources...")
    seed_sources_query = f"""
    MATCH (s:{label})
    WHERE s.{seed_prop} = true
      AND s.primary_source_id IS NOT NULL
      AND ($article_only = false OR toLower(coalesce(s.type, '')) = 'article')
    WITH DISTINCT s.primary_source_id AS source_id
    RETURN source_id
    """

    seed_sources: set[str] = set()
    for record in session.run(seed_sources_query, {"article_only": article_only}):
        src_id = record["source_id"]
        if src_id in sources:
            seed_sources.add(src_id)

    print(f"[source-direct] Found {len(seed_sources)} seed sources")

    print("[source-direct] Step 4: Running BFS on source graph...")
    queue: deque[tuple[str, int]] = deque()
    visited: set[str] = set()

    for src_id in seed_sources:
        sources[src_id]["reachable"] = True
        sources[src_id]["min_hops_to_source"] = 0
        visited.add(src_id)
        queue.append((src_id, 0))

    with tqdm(desc="BFS traversal", unit="source") as pbar:
        while queue:
            current_src, current_hop = queue.popleft()
            if current_src in source_cited_by:
                for citing_src in source_cited_by[current_src]:
                    if citing_src not in visited:
                        visited.add(citing_src)
                        sources[citing_src]["reachable"] = True
                        sources[citing_src]["min_hops_to_source"] = current_hop + 1
                        queue.append((citing_src, current_hop + 1))
                        pbar.update(1)

    reachable_count = sum(1 for s in sources.values() if s["reachable"])
    print(f"[source-direct] Reachable sources: {reachable_count}/{len(sources)}")

    results = list(sources.values())
    enrich_source_names(results)
    results.sort(
        key=lambda x: (
            not x["reachable"],
            (
                x["min_hops_to_source"]
                if x["min_hops_to_source"] is not None
                else float("inf")
            ),
            x["source_name"] or x["source_id"],
        )
    )
    return results


def save_results(
    results: list[dict[str, Any]],
    output_path: Path,
    mode: str,
) -> None:
    """Save source aggregation results."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

    print(f"\n[source] Results written to: {output_path}")
    print(f"[source] Total sources: {len(results)}")

    if not results:
        return

    if mode.startswith("aggregate"):
        total_papers_all = sum(r["total_papers"] for r in results)
        total_reachable_all = sum(r["reachable_papers"] for r in results)
        overall_rate = (
            total_reachable_all / total_papers_all if total_papers_all > 0 else 0
        )
        print(f"[source] Total papers across all sources: {total_papers_all:,}")
        print(f"[source] Total reachable papers: {total_reachable_all:,}")
        print(f"[source] Overall reachability rate: {overall_rate:.2%}")
    elif mode.startswith("direct"):
        reachable = [r for r in results if r["reachable"]]
        total_papers_reachable = sum(r["total_papers"] for r in reachable)
        total_papers_all = sum(r["total_papers"] for r in results)
        print(f"[source] Reachable sources: {len(reachable)}/{len(results)}")
        print(
            f"[source] Papers in reachable sources: {total_papers_reachable:,}/{total_papers_all:,}"
        )


def merge_partition_outputs(
    output_dir: Path,
    merged_name: str,
    num_partitions: int,
) -> Path:
    """Merge partition JSON outputs into a single file."""
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
