"""
Field-level composition of the industry-absorbed (seed) papers.

Pulls counts by `primary_field_id` for nodes with
`matt_marx_paper_patent_startup = true` from Neo4j, joins field
`display_name` from MongoDB, and writes a ranked table. Motivates the
choice of top-K fields for S3.3.

Outputs:
    figs/seed_field_composition.csv  ranked fields with count and share
"""
import os
import csv
import sys
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from common.mongo import get_mongo_client
from ref_map.export.neo4j_export import _resolve_neo4j_config

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")

# Credentials from NEO4J_URI / NEO4J_USER / NEO4J_PASS or the OS keyring.
URI, USER, PASS = _resolve_neo4j_config()


def pull_seed_field_counts():
    drv = GraphDatabase.driver(URI, auth=(USER, PASS))
    q = """
    MATCH (w:Work)
    WHERE w.matt_marx_paper_patent_startup = true
    RETURN w.primary_field_id AS field_id,
           w.primary_domain_id AS domain_id,
           count(*) AS n
    ORDER BY n DESC
    """
    with drv.session(database="neo4j") as s:
        rows = [dict(r) for r in s.run(q)]
    drv.close()
    return rows


def enrich_names(rows):
    c = get_mongo_client()
    db = c["openalex"]

    def _lookup(coll, ids):
        ids = [i for i in ids if i]
        if not ids:
            return {}
        or_ids = list(ids) + [f"https://openalex.org/{i}" for i in ids]
        docs = list(db[coll].find(
            {"id": {"$in": or_ids}},
            {"id": 1, "display_name": 1, "_id": 0},
        ))
        by_full = {d["id"]: d.get("display_name") for d in docs}
        by_bare = {d["id"].split("openalex.org/", 1)[-1]: d.get("display_name") for d in docs}
        return {i: (by_full.get(i) or by_bare.get(i)) for i in ids}

    fields = list({r["field_id"] for r in rows})
    domains = list({r["domain_id"] for r in rows})
    fnames = _lookup("fields", fields)
    dnames = _lookup("domains", domains)
    for r in rows:
        r["field_name"] = fnames.get(r["field_id"])
        r["domain_name"] = dnames.get(r["domain_id"])
    return rows


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    print("pulling seed field counts from Neo4j...")
    rows = pull_seed_field_counts()
    total = sum(r["n"] for r in rows)
    print(f"  {total} seed papers across {len(rows)} field groupings (incl null)")

    rows = enrich_names(rows)
    for r in rows:
        r["share"] = r["n"] / total if total else 0.0

    path = os.path.join(OUTDIR, "seed_field_composition.csv")
    fields = ["rank", "field_id", "field_name", "domain_id", "domain_name",
              "n", "share", "cum_share"]
    cum = 0.0
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for i, r in enumerate(rows, 1):
            cum += r["share"]
            w.writerow({
                "rank": i,
                "field_id": r["field_id"],
                "field_name": r["field_name"],
                "domain_id": r["domain_id"],
                "domain_name": r["domain_name"],
                "n": r["n"],
                "share": f"{r['share']:.4f}",
                "cum_share": f"{cum:.4f}",
            })

    print(f"\ntop-15 fields by seed count:")
    cum = 0.0
    print(f"  {'rank':>4}  {'n':>8}  {'share':>6}  {'cum':>6}  field  (domain)")
    for i, r in enumerate(rows[:15], 1):
        cum += r["share"]
        fn = r["field_name"] or "(null)"
        dn = r["domain_name"] or ""
        print(f"  {i:>4}  {r['n']:>8}  {r['share']*100:>5.1f}%  {cum*100:>5.1f}%  {fn}  ({dn})")

    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
