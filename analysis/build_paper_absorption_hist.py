"""
Stage 1 of the paper-level absorption analysis (the c_0 / seed-set regression).

Streams the full Mongo works collection (~270M docs) ONCE and accumulates a
histogram over (primary field, publication year, cited_by_count):

    n   = number of papers in the cell
    k_* = number positive under each Matt Marx patent-linkage flag
          (all six SEED_SPECS properties + matt_marx_cited_by_patent)

Because every downstream predictor is discrete (field, cohort year,
within-stratum citation percentile), this histogram is a SUFFICIENT STATISTIC
for the paper-level logistic regression: the individual-level Bernoulli fit
over 270M papers collapses exactly into a grouped binomial GLM over the cells
(identical likelihood). Tallying all seven flags lets stage 2 (a) identify
the flag matching the manuscript's ~190k main seed by its total, and (b) run
the assignee-class variants (M1) with no extra pass.

Outcome flags come from patent->paper linkage; the predictor is
paper->paper citations. No CIF/PAR anywhere in this pipeline.

Output (regression_output/):
    paper_absorption_hist.csv.gz   long format: field,year,cites,n,k_<flag>...
    paper_absorption_meta.json     per-flag totals, missing-value and
                                   per-type coverage tallies, runtime
Read-only on Mongo; safe to run alongside Neo4j jobs. Re-running overwrites.

The histogram and meta file are the sampling frame for the stratum-matched seed
samples (ref_map/export/build_m5_placebo_seeds.py), which checks the file by
SHA-256 and stops if the database counts differ from it. Keep the column layout,
the (field, year, cites) keying and the k_ flag names stable.
"""
import os
import sys
import csv
import gzip
import json
import time
from collections import defaultdict, Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
OUTDIR = os.path.join(_HERE, "regression_output")
os.makedirs(OUTDIR, exist_ok=True)
HIST_OUT = os.path.join(OUTDIR, "paper_absorption_hist.csv.gz")
META_OUT = os.path.join(OUTDIR, "paper_absorption_meta.json")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from common import mongo as _mongo  # noqa: E402

FLAGS = [
    "matt_marx_founding_patent_seed_all",
    "matt_marx_founding_patent_seed_corporate",
    "matt_marx_founding_patent_seed_young_firm",
    "matt_marx_founding_patent_seed_young_firm_university",
    "matt_marx_paper_patent_v2",
    "matt_marx_paper_patent_startup",
    "matt_marx_cited_by_patent",
]
NF = len(FLAGS)


def field_num(doc):
    fid = ((doc.get("primary_topic") or {}).get("field") or {}).get("id")
    if not fid:
        return -1
    try:
        return int(str(fid).rsplit("/", 1)[-1])
    except ValueError:
        return -1


def main():
    db = _mongo.get_mongo_client("paper-absorption-hist")["openalex"]
    proj = {"_id": 0, "publication_year": 1, "cited_by_count": 1,
            "primary_topic.field.id": 1, "type": 1}
    for f in FLAGS:
        proj[f] = 1

    hist = defaultdict(lambda: [0] * (1 + NF))  # (field, year, cites) -> [n, k...]
    type_n = Counter()
    type_k_v2 = Counter()   # per-type positives under paper_patent_v2
    missing = Counter()
    totals = [0] * NF
    n_docs = 0
    t0 = time.time()

    cur = db.works.find({}, proj).batch_size(20000)
    for d in cur:
        n_docs += 1
        y = d.get("publication_year")
        c = d.get("cited_by_count")
        f = field_num(d)
        if y is None:
            missing["year"] += 1
            y = -1
        if c is None:
            missing["cites"] += 1
            c = -1
        if f == -1:
            missing["field"] += 1
        cell = hist[(f, int(y), int(c))]
        cell[0] += 1
        for i, fl in enumerate(FLAGS):
            if d.get(fl):
                cell[1 + i] += 1
                totals[i] += 1
        t = d.get("type") or "NA"
        type_n[t] += 1
        if d.get("matt_marx_paper_patent_v2"):
            type_k_v2[t] += 1
        if n_docs % 10_000_000 == 0:
            el = time.time() - t0
            print(f"{n_docs/1e6:.0f}M docs, {len(hist)/1e6:.1f}M cells, "
                  f"{n_docs/el/1000:.0f}k/s, {el/60:.1f} min elapsed", flush=True)

    print(f"scan done: {n_docs} docs, {len(hist)} cells, "
          f"{(time.time()-t0)/60:.1f} min", flush=True)

    with gzip.open(HIST_OUT, "wt", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "year", "cites", "n"] +
                   ["k_" + f.replace("matt_marx_", "") for f in FLAGS])
        for key in sorted(hist):
            w.writerow(list(key) + hist[key])

    meta = {
        "n_docs": n_docs,
        "n_cells": len(hist),
        "flag_totals": dict(zip(FLAGS, totals)),
        "missing": dict(missing),
        "type_counts": dict(type_n.most_common()),
        "type_k_paper_patent_v2": dict(type_k_v2.most_common()),
        "minutes": round((time.time() - t0) / 60, 1),
    }
    with open(META_OUT, "w") as fh:
        json.dump(meta, fh, indent=2)
    print(json.dumps(meta["flag_totals"], indent=2))
    print(f"wrote {HIST_OUT} and {META_OUT}", flush=True)


if __name__ == "__main__":
    main()
