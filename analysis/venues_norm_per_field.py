"""
Top venues by NORMALIZED CIF, stratified by the top-10 OpenAlex fields.

Companion to top_venues_per_field.py, which ranks by aggregate CIF over a
global top-2M truncation. A normalized (per-paper) ranking cannot be built
from that truncation: the denominator would be the censored count, which is
what made the old "CIF mean" column uninterpretable. This script therefore
runs a full grouped aggregation over every article in the ten fields.

Definition, mirroring the venue-level convention in the canonical basis
(`articles_par_avg_score_all` = articles_par_total_score / total_articles,
i.e. the mean is taken over ALL of the venue's articles, including
unreachable ones scoring zero):

    normalized CIF (venue v, field F)
        = sum_{w: field(w)=F, source(w)=v} CIF(w)  /  #{w: field(w)=F, source(w)=v}

Unreachable works carry no `startup_par` property and count as CIF 0 in the
numerator while still counting in the denominator.

Runs one query per field so partial progress is checkpointed.

Outputs:
    figs/venues_norm_per_field_raw.csv       every (field, venue) group
    figs/venues_norm_per_field_enriched.csv  with source display_name + type
"""
import os
import csv
import sys
import time
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from common.mongo import get_mongo_client
from ref_map.export.neo4j_export import _resolve_neo4j_config

TOP10_FIELDS = [
    ("fields/22", "Engineering"),
    ("fields/27", "Medicine"),
    ("fields/13", "Biochemistry, Genetics and Molecular Biology"),
    ("fields/17", "Computer Science"),
    ("fields/25", "Materials Science"),
    ("fields/31", "Physics and Astronomy"),
    ("fields/16", "Chemistry"),
    ("fields/24", "Immunology and Microbiology"),
    ("fields/28", "Neuroscience"),
    ("fields/11", "Agricultural and Biological Sciences"),
]

CIF_PROP = "startup_par"
# Unit of analysis. "works" counts every Work the venue published in the field,
# matching the per-work denominator behind Table S3's normalized CIF
# (all_par_avg_score_all over total_papers). "articles" restricts to
# type='article'; that was the original basis and is kept for comparison.
BASIS = "works"
MIN_KEEP = 25          # keep any venue with >= this many articles in the field;
                       # publication-volume floors are applied at emit time.
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")
_SUFFIX = "" if BASIS == "articles" else f"_{BASIS}"
CKPT = os.path.join(OUTDIR, f"_venues_norm{_SUFFIX}_ckpt")
OUT_RAW = f"{OUTDIR}/venues_norm{_SUFFIX}_per_field_raw.csv"
OUT_ENR = f"{OUTDIR}/venues_norm{_SUFFIX}_per_field_enriched.csv"

# Credentials from NEO4J_URI / NEO4J_USER / NEO4J_PASS or the OS keyring.
URI, USER, PASS = _resolve_neo4j_config()

_TYPE_FILTER = "  AND w.type = 'article'\n" if BASIS == "articles" else ""

AGG_Q = f"""
MATCH (w:Work)
WHERE w.primary_field_id = $fid
{_TYPE_FILTER}  AND w.primary_source_id IS NOT NULL
WITH w.primary_source_id AS src_id,
     head(collect(w.primary_source_type)) AS src_type,
     count(*) AS n_articles,
     sum(coalesce(w.{CIF_PROP}, 0.0)) AS cif_sum
WHERE n_articles >= $min_keep
RETURN src_id, src_type, n_articles, cif_sum
"""


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def detect_prefix(drv):
    with drv.session(database="neo4j") as s:
        r = s.run("MATCH (w:Work) WHERE w.primary_field_id IS NOT NULL "
                  "RETURN w.primary_field_id AS f LIMIT 1").single()
    return "https://openalex.org/" if r and r["f"].startswith("http") else ""


def run_field(drv, stored_fid, bare, fname):
    ck = os.path.join(CKPT, f"{bare.replace('/', '_')}.csv")
    if os.path.exists(ck):
        rows = list(csv.DictReader(open(ck)))
        log(f"  {fname}: {len(rows):,} venues (checkpoint)")
        return rows
    t0 = time.time()
    rows = []
    with drv.session(database="neo4j") as s:
        for r in s.run(AGG_Q, fid=stored_fid, min_keep=MIN_KEEP):
            rows.append({
                "src_id": r["src_id"],
                "src_type": r["src_type"],
                "n_articles": r["n_articles"],
                "cif_sum": r["cif_sum"],
                "cif_norm": (r["cif_sum"] / r["n_articles"]) if r["n_articles"] else 0.0,
            })
    os.makedirs(CKPT, exist_ok=True)
    with open(ck, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["src_id", "src_type", "n_articles",
                                          "cif_sum", "cif_norm"])
        w.writeheader()
        w.writerows(rows)
    log(f"  {fname}: {len(rows):,} venues  ({time.time() - t0:.0f}s)")
    return rows


def enrich(all_rows):
    c = get_mongo_client()
    db = c["openalex"]
    src_ids = list({r["src_id"] for r in all_rows if r["src_id"]})
    or_ids = list(src_ids) + [f"https://openalex.org/{i}" for i in src_ids]
    docs = list(db["sources"].find({"id": {"$in": or_ids}},
                                   {"id": 1, "display_name": 1, "type": 1, "_id": 0}))
    by_full = {d["id"]: d for d in docs}
    by_bare = {d["id"].split("openalex.org/", 1)[-1]: d for d in docs}
    for r in all_rows:
        d = by_full.get(r["src_id"]) or by_bare.get(r["src_id"]) or {}
        r["src_name"] = d.get("display_name")
        r["src_meta_type"] = d.get("type")
    return all_rows


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(OUTDIR, exist_ok=True)
    drv = GraphDatabase.driver(URI, auth=(USER, PASS))
    prefix = detect_prefix(drv)
    log(f"basis={BASIS}  field-id prefix: {prefix!r}")

    all_rows = []
    for bare, fname in TOP10_FIELDS:
        rows = run_field(drv, f"{prefix}{bare}", bare, fname)
        for r in rows:
            r["field_id_query"] = bare
            r["field_name_query"] = fname
            r["n_articles"] = int(r["n_articles"])
            r["cif_sum"] = float(r["cif_sum"])
            r["cif_norm"] = float(r["cif_norm"])
        all_rows.extend(rows)
    drv.close()

    log(f"enriching {len({r['src_id'] for r in all_rows}):,} source ids from Mongo...")
    all_rows = enrich(all_rows)

    raw_f = ["field_id_query", "field_name_query", "src_id", "src_type",
             "n_articles", "cif_sum", "cif_norm"]
    enr_f = raw_f + ["src_name", "src_meta_type"]
    for path, fields in ((OUT_RAW, raw_f), (OUT_ENR, enr_f)):
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in all_rows:
                w.writerow({k: r.get(k) for k in fields})
        log(f"wrote {path}  ({len(all_rows):,} rows)")


if __name__ == "__main__":
    main()
