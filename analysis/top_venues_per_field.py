"""
Top venues by CIF, stratified by the top-10 OpenAlex fields.

Strategy: instead of a per-field aggregate over all Work nodes (slow —
requires touching every Work in a field), pull top-N Works globally by
CIF using the `startup_par` index, then aggregate in Python by
(primary_field_id, primary_source_id). For top-10 venue-per-field lists,
this is a very good approximation: the top venues in each field derive
their aggregate CIF from the high-CIF tail, which is fully captured by
the global top-N. Absolute CIF sums are lower bounds on the true
aggregate; ranks are stable.

Outputs:
    figs/top_venues_per_field_raw.csv       ranked per-field aggregates
    figs/top_venues_per_field_enriched.csv  with source display_name + type
"""
import os
import csv
import sys
from collections import defaultdict
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from common.mongo import get_mongo_client
from ref_map.export.neo4j_export import _resolve_neo4j_config

TOP10_FIELDS = [
    "fields/22",  # Engineering
    "fields/27",  # Medicine
    "fields/13",  # Biochemistry, Genetics and Molecular Biology
    "fields/17",  # Computer Science
    "fields/25",  # Materials Science
    "fields/31",  # Physics and Astronomy
    "fields/16",  # Chemistry
    "fields/24",  # Immunology and Microbiology
    "fields/28",  # Neuroscience
    "fields/11",  # Agricultural and Biological Sciences
]

N_TOP_WORKS = 2_000_000       # global top-N Works by startup_par
MIN_PAPERS_PER_SOURCE = 5     # per-field min for ranking
N_PER_FIELD = 25              # keep more than needed; SM uses top-10
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")
CIF_PROP = "startup_par"

# Credentials from NEO4J_URI / NEO4J_USER / NEO4J_PASS or the OS keyring.
URI, USER, PASS = _resolve_neo4j_config()


def pull_top_works():
    """Pull top-N Works by CIF using the startup_par index. Returns rows
    with (field_id, source_id, source_type, cif)."""
    drv = GraphDatabase.driver(URI, auth=(USER, PASS))
    q = f"""
    MATCH (w:Work)
    WHERE w.{CIF_PROP} IS NOT NULL
      AND w.type = 'article'
      AND w.primary_source_id IS NOT NULL
      AND w.primary_field_id IS NOT NULL
    RETURN w.{CIF_PROP} AS cif,
           w.primary_field_id AS field_id,
           w.primary_source_id AS src_id,
           w.primary_source_type AS src_type
    ORDER BY w.{CIF_PROP} DESC LIMIT $n
    """
    rows = []
    with drv.session(database="neo4j") as s:
        for r in s.run(q, n=N_TOP_WORKS):
            rows.append((r["field_id"], r["src_id"], r["src_type"], r["cif"]))
    drv.close()
    return rows


def aggregate_by_field_source(rows):
    """Group by (field_id, src_id), compute n_papers, cif_sum, cif_mean."""
    agg = defaultdict(lambda: {"src_type": None, "n": 0, "sum": 0.0})
    for field_id, src_id, src_type, cif in rows:
        d = agg[(field_id, src_id)]
        d["src_type"] = src_type
        d["n"] += 1
        d["sum"] += cif
    return agg


def top_per_field(agg, field_id):
    rows = []
    for (fid, src_id), d in agg.items():
        if fid != field_id:
            continue
        if d["n"] < MIN_PAPERS_PER_SOURCE:
            continue
        rows.append({
            "src_id": src_id,
            "src_type": d["src_type"],
            "n_papers": d["n"],
            "cif_sum": d["sum"],
            "cif_mean": d["sum"] / d["n"],
        })
    rows.sort(key=lambda r: r["cif_sum"], reverse=True)
    return rows[:N_PER_FIELD]


def enrich_source_names(all_rows):
    c = get_mongo_client()
    db = c["openalex"]
    src_ids = list({r["src_id"] for r in all_rows if r["src_id"]})
    or_ids = list(src_ids) + [f"https://openalex.org/{i}" for i in src_ids]
    docs = list(db["sources"].find(
        {"id": {"$in": or_ids}},
        {"id": 1, "display_name": 1, "type": 1, "_id": 0},
    ))
    by_full = {d["id"]: d for d in docs}
    by_bare = {d["id"].split("openalex.org/", 1)[-1]: d for d in docs}
    for r in all_rows:
        d = by_full.get(r["src_id"]) or by_bare.get(r["src_id"]) or {}
        r["src_name"] = d.get("display_name")
        r["src_meta_type"] = d.get("type")
    return all_rows


def field_name_lookup(fids):
    c = get_mongo_client()
    db = c["openalex"]
    or_ids = list(fids) + [f"https://openalex.org/{i}" for i in fids]
    docs = list(db["fields"].find(
        {"id": {"$in": or_ids}},
        {"id": 1, "display_name": 1, "_id": 0},
    ))
    by_full = {d["id"]: d.get("display_name") for d in docs}
    by_bare = {d["id"].split("openalex.org/", 1)[-1]: d.get("display_name") for d in docs}
    return {i: (by_full.get(i) or by_bare.get(i)) for i in fids}


def main():
    os.makedirs(OUTDIR, exist_ok=True)

    # Detect stored field-id form (bare vs full URL)
    drv = GraphDatabase.driver(URI, auth=(USER, PASS))
    with drv.session(database="neo4j") as s:
        r = s.run("MATCH (w:Work) WHERE w.primary_field_id IS NOT NULL "
                  "RETURN w.primary_field_id AS f LIMIT 1").single()
    drv.close()
    prefix = "https://openalex.org/" if r and r["f"].startswith("http") else ""
    stored_ids = [f"{prefix}{i}" for i in TOP10_FIELDS]
    id_to_bare = dict(zip(stored_ids, TOP10_FIELDS))

    field_names = field_name_lookup(TOP10_FIELDS)

    print(f"pulling top-{N_TOP_WORKS} Works globally by CIF...", flush=True)
    raw = pull_top_works()
    print(f"  got {len(raw)} rows", flush=True)

    print(f"aggregating by (field, source)...", flush=True)
    agg = aggregate_by_field_source(raw)
    print(f"  {len(agg)} unique (field, source) pairs", flush=True)

    all_rows = []
    for stored_fid in stored_ids:
        bare = id_to_bare[stored_fid]
        rows = top_per_field(agg, stored_fid)
        for i, r in enumerate(rows, 1):
            r["field_id_query"] = bare
            r["field_name_query"] = field_names.get(bare)
            r["field_rank"] = i
        all_rows.extend(rows)
        print(f"  {bare:12s}  {field_names.get(bare, '?'):45s}  {len(rows)} venues", flush=True)

    all_rows = enrich_source_names(all_rows)

    raw_fields = ["field_id_query", "field_name_query", "field_rank",
                  "src_id", "src_type", "n_papers", "cif_sum", "cif_mean"]
    enriched_fields = raw_fields + ["src_name", "src_meta_type"]

    with open(os.path.join(OUTDIR, "top_venues_per_field_raw.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=raw_fields)
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k) for k in raw_fields})

    with open(os.path.join(OUTDIR, "top_venues_per_field_enriched.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=enriched_fields)
        w.writeheader()
        for r in all_rows:
            w.writerow({k: r.get(k) for k in enriched_fields})

    print("\ntop-10 venues per field preview:")
    by_field = defaultdict(list)
    for r in all_rows:
        by_field[r["field_id_query"]].append(r)
    for fid, rows in by_field.items():
        print(f"\n--- {field_names.get(fid)} ---")
        for r in rows[:10]:
            name = (r["src_name"] or "?")[:45]
            print(f"  {r['field_rank']:>2}  cif_sum={r['cif_sum']:>7.2f}  "
                  f"n={r['n_papers']:>6}  mean={r['cif_mean']:>7.5f}  {name}")

    print(f"\nwrote {OUTDIR}/top_venues_per_field_{{raw,enriched}}.csv")


if __name__ == "__main__":
    main()
