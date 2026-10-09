"""Assemble the venue table that the analysis scripts read.

One row per OpenAlex source, combining

  * Data S1: summed CIF for all document types and for articles, work and article
    counts, and the c_n counts (built by build_data_s1.py);
  * OpenAlex source metadata from the sources collection: the h-index
    (`summary_stats.h_index`) and the ISSNs used for matching;
  * Journal Impact Factor from a Journal Citation Reports export (licensed from
    Clarivate, not redistributable) and SJR and its h-index from the Scimago export,
    both matched to OpenAlex sources by ISSN (external_source_metrics.py).

Normalized CIF is recomputed at full precision as summed CIF / works.

Usage:
    python -m ref_map.aggregation.build_venue_basis \
        [--data-s1 DIR] [--jcr CSV] [--scimago CSV] [--out CSV]

--data-s1 defaults to <repo>/data_s1 (the released files).

The JCR and Scimago paths default to $SC_DATA_DIR/clarivate/... and
$SC_DATA_DIR/scimago/... (see external_source_metrics.py).
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import lzma
import os

from common.mongo import get_mongo_client, handle_pymongo_errors
from ref_map.aggregation.external_source_metrics import (
    DEFAULT_JCR_CSV,
    DEFAULT_SCIMAGO_CSV,
    apply_external_metrics_to_rows,
    fetch_source_issns,
    full_source_id,
)
from ref_map.common.batching import chunked
from ref_map.export.shared import DB_NAME, OPENALEX_PREFIX, SOURCES_COLLECTION_NAME

_REF_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DATA_S1 = os.path.join(os.path.dirname(_REF_MAP), "data_s1")
DEFAULT_OUT = os.path.join(_REF_MAP, "output", "venue_basis.csv")

COLUMNS = [
    "source_id", "source_name", "source_type",
    "total_papers", "total_articles",
    "all_par_total_score", "all_par_avg_score_all",
    "articles_par_total_score", "articles_par_avg_score_all",
    "all_reachable_papers_d0", "all_reachable_papers_d1", "all_reachable_papers_d5",
    "reachable_papers",
    "source_h_index", "jcr_2024_jif", "scimago_sjr", "scimago_h_index",
]


def _by_id(data_s1: str, name: str) -> dict[str, dict]:
    """Records of one Data S1 file keyed by source_id (`.json.xz`, `.json.gz` or `.json`)."""
    for path, opener in ((os.path.join(data_s1, f"{name}.json.xz"), lzma.open),
                         (os.path.join(data_s1, f"{name}.json.gz"), gzip.open),
                         (os.path.join(data_s1, f"{name}.json"), open)):
        if os.path.exists(path):
            with opener(path, "rt") as f:
                return {r["source_id"]: r for r in json.load(f)}
    raise FileNotFoundError(f"Data S1 file {name!r} not found in {data_s1}")


def fetch_h_index(source_ids: list[str], chunk_size: int = 5000) -> dict[str, int]:
    """OpenAlex h-index (`summary_stats.h_index`) for each source."""
    coll = get_mongo_client()[DB_NAME][SOURCES_COLLECTION_NAME]
    out: dict[str, int] = {}
    with handle_pymongo_errors():
        for batch in chunked(source_ids, chunk_size):
            for doc in coll.find({"id": {"$in": [full_source_id(s) for s in batch]}},
                                 {"id": 1, "summary_stats.h_index": 1}):
                sid = (doc.get("id") or "").removeprefix(OPENALEX_PREFIX)
                h = (doc.get("summary_stats") or {}).get("h_index")
                if sid and h is not None:
                    out[sid] = h
    return out


def build(data_s1: str, jcr_csv: str, scimago_csv: str, out_path: str) -> None:
    base = _by_id(data_s1, "cif_all_documents")
    art = _by_id(data_s1, "cif_articles")
    cn = _by_id(data_s1, "cn_counts")
    ids = list(base)
    h_index = fetch_h_index(ids)

    rows = []
    for sid in ids:
        b, a, c = base[sid], art.get(sid, {}), cn.get(sid, {})
        works, articles = b.get("total_papers") or 0, a.get("total_papers") or 0
        cif, cif_art = b.get("total_score") or 0.0, a.get("total_score") or 0.0
        rows.append({
            "source_id": sid, "source_name": b.get("source_name") or "",
            "source_type": b.get("source_type") or "",
            "total_papers": works, "total_articles": articles,
            "all_par_total_score": cif,
            "all_par_avg_score_all": cif / works if works else 0.0,
            "articles_par_total_score": cif_art,
            "articles_par_avg_score_all": cif_art / articles if articles else 0.0,
            "all_reachable_papers_d0": c.get("c_0", 0), "all_reachable_papers_d1": c.get("c_1", 0),
            "all_reachable_papers_d5": c.get("c_5", 0), "reachable_papers": c.get("c_inf", 0),
            "source_h_index": h_index.get(sid),
        })

    rows, n_jcr, n_sjr = apply_external_metrics_to_rows(
        rows, source_issns=fetch_source_issns(ids), jcr_csv=jcr_csv, scimago_csv=scimago_csv)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS})
    print(f"wrote {out_path}: {len(rows):,} sources; JIF matched {n_jcr:,}, SJR matched {n_sjr:,}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Assemble the venue table for the analyses.")
    ap.add_argument("--data-s1", default=DEFAULT_DATA_S1)
    ap.add_argument("--jcr", default=str(DEFAULT_JCR_CSV))
    ap.add_argument("--scimago", default=str(DEFAULT_SCIMAGO_CSV))
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args()
    build(a.data_s1, a.jcr, a.scimago, a.out)


if __name__ == "__main__":
    main()
