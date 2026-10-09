"""Per-venue control variables for the analysis of deviance.

For every OpenAlex source this collects

  * publication-year statistics of the works whose primary source it is
    (count with a year, minimum, 5th percentile, mean, median, maximum), from the
    Neo4j Work nodes;
  * a field-composition vector: the source's OpenAlex topic counts
    (`sources.topics[].count`) summed by each topic's parent field, from MongoDB.

build_data_s1.py releases the result as Data S1 `venue_controls`, and
analysis/build_venue_analysis_table.py derives the primary field, field shares and
vintage controls from it.

Usage:
    python -m ref_map.aggregation.venue_controls [--out JSON]

Credentials come from NEO4J_URI / NEO4J_USER / NEO4J_PASS and MONGO_URI, or the OS keyring.
"""
from __future__ import annotations

import argparse
import json
import os

from neo4j import GraphDatabase

from common.mongo import get_mongo_client
from ref_map.common.config import NEO4J_DATABASE
from ref_map.export.shared import _resolve_neo4j_config

DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output",
                           "venue_controls.json")
YEAR_FIELDS = ("n_yr", "first_year", "p05_year", "mean_year", "median_year", "last_year")

YEAR_Q = """
MATCH (w:Work)
WHERE w.primary_source_id IS NOT NULL AND w.publication_year IS NOT NULL
WITH w.primary_source_id AS sid, w.publication_year AS y
RETURN sid,
       count(*)               AS n_yr,
       min(y)                 AS first_year,
       percentileCont(y,0.05) AS p05_year,
       avg(y)                 AS mean_year,
       percentileCont(y,0.5)  AS median_year,
       max(y)                 AS last_year
"""


def _bare(x) -> str | None:
    if x is None:
        return None
    x = str(x)
    return x.split("openalex.org/", 1)[-1] if "openalex.org/" in x else x


def _field_num(fid) -> str | None:
    """22, 'fields/22', or 'https://openalex.org/fields/22' -> '22'."""
    b = _bare(fid)
    if b is None:
        return None
    return b.split("/", 1)[-1] if "/" in b else b


def year_stats() -> dict[str, dict]:
    """Publication-year statistics per source (one full scan of the Work nodes)."""
    uri, user, password = _resolve_neo4j_config()
    driver = GraphDatabase.driver(uri, auth=(user, password))
    out = {}
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            for r in s.run(YEAR_Q):
                out[_bare(r["sid"])] = {k: r[k] for k in YEAR_FIELDS}
    finally:
        driver.close()
    return out


def field_counts() -> dict[str, dict[str, int]]:
    """Topic counts summed by parent field, per source, in topic order."""
    db = get_mongo_client()["openalex"]
    out = {}
    for d in db["sources"].find({}, {"id": 1, "topics": 1, "_id": 0}):
        sid = _bare(d.get("id"))
        if not sid:
            continue
        fc: dict[str, int] = {}
        for t in d.get("topics") or []:
            fn = _field_num((t.get("field") or {}).get("id"))
            if fn:
                fc[fn] = fc.get(fn, 0) + int(t.get("count") or 0)
        if fc:
            out[sid] = fc
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Collect per-venue year and field controls.")
    ap.add_argument("--out", default=DEFAULT_OUT)
    out = ap.parse_args().out
    print("scanning Neo4j for publication-year statistics (full Work scan)...", flush=True)
    years = year_stats()
    print(f"  {len(years):,} sources; reading field composition from MongoDB...", flush=True)
    fields = field_counts()
    print(f"  {len(fields):,} sources with topics", flush=True)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as f:
        json.dump({"year_stats": years, "field_counts": fields}, f)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
