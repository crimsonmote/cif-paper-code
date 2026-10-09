"""
Top-CIF papers stratified by OpenAlex primary field, restricted to the
top-10 fields by seed-paper count.

Pulls more candidates per field than we need (N_PER_FIELD_PULL) to allow
dedup + manual curation of the same OpenAlex data-quality issues as
S3.1 (Deleted Work, reference-parsing collisions, non-article types,
duplicate titles). The final SM table uses top-5 per field.

Outputs:
    figs/top_cif_per_field_raw.csv      raw candidates, IDs only
    figs/top_cif_per_field_enriched.csv candidates with display names + cby
    figs/top_cif_per_field_report.txt   per-field flag counts
"""
import os
import csv
import sys
import re
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from common.mongo import get_mongo_client
from ref_map.export.neo4j_export import _resolve_neo4j_config

# Top-10 fields by seed count, from seed_field_composition.py
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

N_PER_FIELD_PULL = 25   # room for dedup + manual removal; final SM = top-5
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")
CIF_PROP = "startup_par"

# Credentials from NEO4J_URI / NEO4J_USER / NEO4J_PASS or the OS keyring.
URI, USER, PASS = _resolve_neo4j_config()


def resolve_field_ids(target_ids):
    """Field IDs stored on Work nodes may be bare ('fields/22') or full
    ('https://openalex.org/fields/22'). Return the set of forms present."""
    drv = GraphDatabase.driver(URI, auth=(USER, PASS))
    with drv.session(database="neo4j") as s:
        r = s.run(
            "MATCH (w:Work) WHERE w.primary_field_id IS NOT NULL "
            "RETURN w.primary_field_id AS f LIMIT 1"
        ).single()
    drv.close()
    if not r:
        return target_ids
    sample = r["f"]
    if sample.startswith("http"):
        return [f"https://openalex.org/{i}" for i in target_ids]
    return target_ids


def pull_top_per_field(field_id):
    drv = GraphDatabase.driver(URI, auth=(USER, PASS))
    q = f"""
    MATCH (w:Work)
    WHERE w.{CIF_PROP} IS NOT NULL
      AND w.type = 'article'
      AND w.title IS NOT NULL
      AND w.title <> 'Deleted Work'
      AND w.primary_field_id = $fid
    RETURN w.{CIF_PROP} AS cif, w.id AS id, w.title AS title, w.type AS type,
           w.publication_year AS year, w.doi AS doi,
           w.primary_source_id AS src_id, w.primary_source_type AS src_type,
           w.first_author_id AS fa_id, w.last_author_id AS la_id,
           w.primary_field_id AS field_id, w.primary_subfield_id AS subfield_id,
           w.primary_domain_id AS domain_id, w.primary_topic_id AS topic_id,
           w.matt_marx_paper_patent_startup AS is_seed
    ORDER BY w.{CIF_PROP} DESC LIMIT $n
    """
    with drv.session(database="neo4j") as s:
        rows = [dict(r) for r in s.run(q, fid=field_id, n=N_PER_FIELD_PULL)]
    drv.close()
    return rows


def dedup_by_title(rows):
    """Collapse near-duplicate OpenAlex entries (same normalized title + year)."""
    def norm(s):
        return re.sub(r"\W+", "", (s or "").lower())
    seen = {}
    for r in rows:
        key = (norm(r["title"])[:80], r["year"])
        if key not in seen or r["cif"] > seen[key]["cif"]:
            seen[key] = r
    return sorted(seen.values(), key=lambda r: r["cif"], reverse=True)


def enrich_from_mongo(rows):
    c = get_mongo_client()
    db = c["openalex"]

    def _lookup(collection, ids, name_field="display_name"):
        ids = [i for i in ids if i]
        if not ids:
            return {}
        or_ids = list(ids) + [f"https://openalex.org/{i}" for i in ids]
        docs = list(db[collection].find(
            {"id": {"$in": or_ids}},
            {"id": 1, name_field: 1, "_id": 0},
        ))
        by_full = {d["id"]: d.get(name_field) for d in docs}
        by_bare = {d["id"].split("openalex.org/", 1)[-1]: d.get(name_field) for d in docs}
        return {i: (by_full.get(i) or by_bare.get(i)) for i in ids}

    src_ids = [r["src_id"] for r in rows if r["src_id"]]
    src_names = _lookup("sources", src_ids)

    author_ids = list({r["fa_id"] for r in rows if r["fa_id"]} |
                      {r["la_id"] for r in rows if r["la_id"]})
    author_names = _lookup("authors", author_ids)

    field_ids = list({r["field_id"] for r in rows if r["field_id"]})
    domain_ids = list({r["domain_id"] for r in rows if r["domain_id"]})
    field_names = _lookup("fields", field_ids)
    domain_names = _lookup("domains", domain_ids)

    work_ids = [r["id"] for r in rows if r["id"]]
    cby_map = {}
    for chunk_start in range(0, len(work_ids), 500):
        chunk = work_ids[chunk_start:chunk_start + 500]
        or_ids = chunk + [f"https://openalex.org/{i}" for i in chunk]
        for doc in db["works"].find(
            {"id": {"$in": or_ids}},
            {"id": 1, "cited_by_count": 1, "_id": 0},
        ):
            bare = doc["id"].split("openalex.org/", 1)[-1]
            cby_map[bare] = doc.get("cited_by_count")

    for r in rows:
        r["cited_by_count"] = cby_map.get(r["id"])
        r["src_name"] = src_names.get(r["src_id"])
        r["fa_name"] = author_names.get(r["fa_id"])
        r["la_name"] = author_names.get(r["la_id"])
        r["field_name"] = field_names.get(r["field_id"])
        r["domain_name"] = domain_names.get(r["domain_id"])
    return rows


def report_per_field(rows_by_field):
    lines = []
    for fid, rows in rows_by_field.items():
        fname = rows[0]["field_name"] if rows else fid
        lines.append(f"\n=== {fname} ({fid}) ===")
        lines.append(f"  candidates: {len(rows)}")
        lines.append(f"  cited_by max/min: {max((r['cited_by_count'] or 0) for r in rows)}"
                     f" / {min((r['cited_by_count'] or 0) for r in rows)}")
        lines.append(f"  is_seed count : {sum(1 for r in rows if r['is_seed'])}")
        types = {}
        for r in rows:
            types[r["type"]] = types.get(r["type"], 0) + 1
        lines.append(f"  by type       : {dict(sorted(types.items(), key=lambda x: -x[1]))}")
    return "\n".join(lines)


def write_csv(rows, path, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    resolved = resolve_field_ids(TOP10_FIELDS)
    print(f"pulling top-{N_PER_FIELD_PULL} per field for {len(resolved)} fields...")

    all_rows = []
    rows_by_field = {}
    for fid_orig, fid in zip(TOP10_FIELDS, resolved):
        rows = pull_top_per_field(fid)
        rows = dedup_by_title(rows)
        # Enrich per-field so the report can name them
        rows = enrich_from_mongo(rows)
        for i, r in enumerate(rows, 1):
            r["field_rank"] = i
            r["field_id_query"] = fid_orig
        rows_by_field[fid_orig] = rows
        all_rows.extend(rows)
        fn = rows[0]["field_name"] if rows else "?"
        print(f"  {fid_orig:12s}  {fn:45s}  {len(rows)} rows "
              f"(top CIF={rows[0]['cif']:.2f})" if rows else f"  {fid_orig}  no rows")

    raw_fields = ["field_id_query", "field_rank",
                  "cif", "id", "title", "type", "year", "doi",
                  "src_id", "src_type", "fa_id", "la_id",
                  "field_id", "subfield_id", "domain_id", "topic_id", "is_seed"]
    write_csv(all_rows, os.path.join(OUTDIR, "top_cif_per_field_raw.csv"), raw_fields)

    enriched_fields = raw_fields + ["cited_by_count",
                                    "src_name", "fa_name", "la_name",
                                    "field_name", "domain_name"]
    write_csv(all_rows, os.path.join(OUTDIR, "top_cif_per_field_enriched.csv"), enriched_fields)

    report = report_per_field(rows_by_field)
    print()
    print(report)
    with open(os.path.join(OUTDIR, "top_cif_per_field_report.txt"), "w") as f:
        f.write(report + "\n")

    print(f"\nwrote {OUTDIR}/top_cif_per_field_{{raw,enriched}}.csv + _report.txt")


if __name__ == "__main__":
    main()
