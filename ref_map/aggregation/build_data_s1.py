"""Build the Data S1 files released with the paper.

Every file covers all OpenAlex sources (one record per `source_id`) and carries
only the quantity it is named for, so files join on `source_id` without
duplicated columns. Normalized CIF is not stored: compute it as
`total_score / total_papers` from the base file.

Inputs are the venue-level outputs of the pipeline:

  base, all document types   aggregation/output/source_par_all_assignees_full_0_85.json
  base, articles only        aggregation/output/source_par_all_assignees_full_0_85_articles.json
  c_n counts                 aggregation/output/source_aggregate_startup_full.json
  seed definitions           aggregation/output/source_par_<definition>_full_0_85.json
  matched seed samples       output/source_par_m5u_<run>_full.json
  damping d = 0.95           aggregation/output/source_par_startup_full_0_95.json
  venue controls             aggregation/output/venue_controls.json (venue_controls.py)

Writes xz-compressed JSON (`<name>.json.xz`) and a README data dictionary.

Usage:
    python -m ref_map.aggregation.build_data_s1 [--out DIR] [--order-from FILE]

--out defaults to <repo>/data_s1. Records keep the order of the released files
(see the data dictionary below).
"""
from __future__ import annotations

import argparse
import lzma
import json
import os

from ref_map.aggregation.venue_controls import YEAR_FIELDS
from ref_map.common.mongo_enrichment import fetch_source_names

_REF_MAP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGG = os.path.join(_REF_MAP, "aggregation", "output")
OUT = os.path.join(_REF_MAP, "output")

BASE_FIELDS = ("source_id", "source_name", "source_type",
               "total_papers", "scored_papers", "total_score")
SEED_DEFINITIONS = ("corporate", "young_firm", "young_firm_university")
MATCHED_RUNS = {"cif_union_graph": "startup", "sample_1": "placebo_r1",
                "sample_2": "placebo_r2", "sample_3": "placebo_r3"}
CN_FIELDS = {"c_0": "reachable_papers_d0", "c_1": "reachable_papers_d1",
             "c_5": "reachable_papers_d5", "c_inf": "reachable_papers"}

DICTIONARY = """# Data S1

Venue-level outputs for every OpenAlex source (journals, conferences, and all other
source types; 243,257 records per file). All files join on `source_id`. Each file
holds only the quantity it is named for.

CIF is the Commercialization Influence Factor: paper-level Personalized ArticleRank
scores (damping d = 0.85 unless stated) summed over the works whose OpenAlex primary
source is the venue. Normalized CIF is `total_score / total_papers`, computed from
`cif_all_documents.json`.

Each file is xz-compressed JSON (`.json.xz`): a list of records. Read it with
`lzma.open(path, "rt")` in Python or `jsonlite::fromJSON(xzfile(path))` in R.

| File | Fields |
|---|---|
| `cif_all_documents.json` | `source_id`, `source_name`, `source_type` (`j` journal, `c` conference, other OpenAlex types), `total_papers` (works with the source as primary source), `scored_papers` (works with a nonzero score), `total_score` (summed CIF) |
| `cif_articles.json` | the same fields, counting only works typed `article`; sources with no articles are omitted |
| `cn_counts.json` | `source_id`, `c_0`, `c_1`, `c_5`, `c_inf`: number of the source's works within 0, 1, 5, or any number of citation hops upstream of an industry-absorbed paper (all document types; cumulative in the hop count) |
| `cif_seed_definitions.json` | `source_id`, summed CIF with the seed set restricted to each assignee-class definition: `corporate`, `young_firm`, `young_firm_university`. The all-assignee definition is the base file |
| `cif_matched_samples.json` | `source_id`, summed CIF from the four runs of the seed-specificity test, computed on one shared subgraph: `cif_union_graph` (the industry-absorbed seed set) and `sample_1`–`sample_3` (stratum-matched seed samples) |
| `cif_damping_095.json` | `source_id`, `total_score`: summed CIF at damping d = 0.95 |
| `venue_controls.json` | `source_id`; publication-year statistics of the source's works: `n_yr` (works with a year), `first_year`, `p05_year` (5th percentile), `mean_year`, `median_year`, `last_year`; and `field_counts`, the source's OpenAlex topic counts summed by parent field (keys are OpenAlex field numbers, `https://openalex.org/fields/<n>`; empty when the source lists no topics). These are the field and vintage controls of the analysis of deviance |

A source with no reachable works has zero in every score and count field.
Fifteen sources have no display name in OpenAlex; their `source_name` is empty.

Records are listed in the same source order in every file: the order used for the
paper's analyses. Top-N selections in the analysis scripts break ties on the ranking
metric by this order, so keep it to reproduce the reported numbers exactly.
"""


def _load(path: str):
    with open(path) as f:
        return json.load(f)


def _by_id(path: str) -> dict[str, dict]:
    return {r["source_id"]: r for r in _load(path) if r.get("source_id")}


def _num(x) -> float:
    return float(x) if x not in (None, "") else 0.0


def _write(out_dir: str, name: str, records: list[dict]) -> None:
    path = os.path.join(out_dir, name + ".xz")
    with lzma.open(path, "wb", preset=9 | lzma.PRESET_EXTREME) as f:
        f.write(json.dumps(records, separators=(",", ":")).encode())
    print(f"  {name:28s} {len(records):>8,} records  {os.path.getsize(path) / 1e6:6.1f} MB")


def _with_names(records: list[dict], names: dict[str, str]) -> list[dict]:
    """Base-file fields, with display names from the OpenAlex sources collection."""
    out = []
    for r in records:
        if r.get("source_id"):
            row = {k: r.get(k) for k in BASE_FIELDS}
            row["source_name"] = names.get(r["source_id"], "")
            out.append(row)
    return out


def _source_order(order_from: str | None) -> dict[str, int]:
    """Position of each source in an existing Data S1 base file (empty if none)."""
    if not order_from or not os.path.exists(order_from):
        return {}
    with lzma.open(order_from, "rt") as f:
        return {r["source_id"]: i for i, r in enumerate(json.load(f))}


def build(out_dir: str, order_from: str | None = None) -> None:
    os.makedirs(out_dir, exist_ok=True)
    order = _source_order(order_from)
    base = _load(os.path.join(AGG, "source_par_all_assignees_full_0_85.json"))
    # Keep the released record order; sources it does not list follow in pipeline order.
    base.sort(key=lambda r: order.get(r.get("source_id"), len(order)))
    ids = [r["source_id"] for r in base if r.get("source_id")]
    names = fetch_source_names(ids)   # the score files carry no display names
    print(f"writing Data S1 to {out_dir} ({len(names):,} of {len(ids):,} sources named)")

    _write(out_dir, "cif_all_documents.json", _with_names(base, names))
    articles = _load(os.path.join(AGG, "source_par_all_assignees_full_0_85_articles.json"))
    articles.sort(key=lambda r: order.get(r.get("source_id"), len(order)))
    _write(out_dir, "cif_articles.json", _with_names(articles, names))

    cn = _by_id(os.path.join(AGG, "source_aggregate_startup_full.json"))
    _write(out_dir, "cn_counts.json",
           [{"source_id": s, **{k: int(_num(cn.get(s, {}).get(v))) for k, v in CN_FIELDS.items()}}
            for s in ids])

    seeds = {d: _by_id(os.path.join(AGG, f"source_par_{d}_full_0_85.json"))
             for d in SEED_DEFINITIONS}
    _write(out_dir, "cif_seed_definitions.json",
           [{"source_id": s, **{d: _num(seeds[d].get(s, {}).get("total_score"))
                                for d in SEED_DEFINITIONS}} for s in ids])

    runs = {k: _by_id(os.path.join(OUT, f"source_par_m5u_{v}_full.json"))
            for k, v in MATCHED_RUNS.items()}
    _write(out_dir, "cif_matched_samples.json",
           [{"source_id": s, **{k: _num(runs[k].get(s, {}).get("total_score"))
                                for k in MATCHED_RUNS}} for s in ids])

    d95 = _by_id(os.path.join(AGG, "source_par_startup_full_0_95.json"))
    _write(out_dir, "cif_damping_095.json",
           [{"source_id": s, "total_score": _num(d95.get(s, {}).get("total_score"))}
            for s in ids])

    controls = _load(os.path.join(AGG, "venue_controls.json"))
    years, fields = controls["year_stats"], controls["field_counts"]
    _write(out_dir, "venue_controls.json",
           [{"source_id": s, **{k: (years.get(s) or {}).get(k) for k in YEAR_FIELDS},
             "field_counts": fields.get(s, {})} for s in ids])

    with open(os.path.join(out_dir, "README.md"), "w") as f:
        f.write(DICTIONARY)
    print("  README.md (data dictionary)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Data S1 files.")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(_REF_MAP), "data_s1"))
    ap.add_argument("--order-from", default=None,
                    help="Data S1 base file whose record order to keep "
                         "(default: <out>/cif_all_documents.json.xz if present)")
    a = ap.parse_args()
    build(a.out, a.order_from or os.path.join(a.out, "cif_all_documents.json.xz"))


if __name__ == "__main__":
    main()
