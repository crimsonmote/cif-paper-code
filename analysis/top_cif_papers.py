"""
Pull the top-N papers by CIF (Neo4j `startup_par`) and enrich with display
names from MongoDB (sources, authors, fields/domains). Surfaces the
OpenAlex data-quality issues encountered along the way.

Outputs:
    figs/top_cif_papers_raw.csv       raw Neo4j hits, top-N with IDs only
    figs/top_cif_papers_enriched.csv  same rows with display names joined
    figs/top_cif_papers_report.txt    counts of data-quality flags
"""
import os
import csv
import sys
from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
from common.mongo import get_mongo_client
from ref_map.export.neo4j_export import _resolve_neo4j_config

N_TOP = 500  # pull more than we need; will dedup + filter down
OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")
CIF_PROP = "startup_par"

# Credentials from NEO4J_URI / NEO4J_USER / NEO4J_PASS or the OS keyring.
URI, USER, PASS = _resolve_neo4j_config()


def pull_top_from_neo4j():
    drv = GraphDatabase.driver(URI, auth=(USER, PASS))
    # Restrict to type='article' and drop empty/deleted titles at the DB.
    # (in-degree / cited_by_count is fetched from Mongo in enrich_from_mongo;
    # a Cypher OPTIONAL MATCH against 47k-in-degree nodes is too slow.)
    q = f"""
    MATCH (w:Work)
    WHERE w.{CIF_PROP} IS NOT NULL
      AND w.type = 'article'
      AND w.title IS NOT NULL
      AND w.title <> 'Deleted Work'
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
        rows = [dict(r) for r in s.run(q, n=N_TOP)]
    drv.close()
    return rows


def dedup_by_title(rows):
    """Collapse near-duplicate OpenAlex entries for the same paper
    (same normalized title + same year → keep the row with the highest CIF)."""
    import re
    def norm(s):
        return re.sub(r"\W+", "", (s or "").lower())
    seen = {}
    for r in rows:
        key = (norm(r["title"])[:80], r["year"])
        if key not in seen or r["cif"] > seen[key]["cif"]:
            seen[key] = r
    out = sorted(seen.values(), key=lambda r: r["cif"], reverse=True)
    return out


def enrich_from_mongo(rows):
    c = get_mongo_client()
    db = c["openalex"]

    def _lookup(collection, ids, name_field="display_name"):
        """Return {input_id -> display_name}. Handles both bare IDs (e.g.
        'S1234' or 'fields/12') and full-URL IDs from Mongo."""
        ids = [i for i in ids if i]
        if not ids:
            return {}
        or_ids = list(ids) + [f"https://openalex.org/{i}" for i in ids]
        # Fetch all matching docs in one pass; map back to whichever form the
        # caller passed in.
        docs = list(db[collection].find(
            {"id": {"$in": or_ids}},
            {"id": 1, name_field: 1, "_id": 0},
        ))
        by_full = {d["id"]: d.get(name_field) for d in docs}
        by_bare = {d["id"].split("openalex.org/", 1)[-1]: d.get(name_field) for d in docs}
        out = {}
        for i in ids:
            out[i] = by_full.get(i) or by_bare.get(i)
        return out

    src_ids = [r["src_id"] for r in rows if r["src_id"]]
    src_names = _lookup("sources", src_ids)

    author_ids = list({r["fa_id"] for r in rows if r["fa_id"]} |
                      {r["la_id"] for r in rows if r["la_id"]})
    author_names = _lookup("authors", author_ids)

    field_ids = list({r["field_id"] for r in rows if r["field_id"]})
    domain_ids = list({r["domain_id"] for r in rows if r["domain_id"]})
    # These IDs come back as "fields/12" style; the ID stored on Mongo is
    # "https://openalex.org/fields/12".
    field_names = _lookup("fields", field_ids)
    domain_names = _lookup("domains", domain_ids)

    # Fetch OpenAlex cited_by_count (= Neo4j in-degree, within a few) as an
    # anomaly-triage signal: cross-referencing against WoS/CrossRef for the
    # top-N papers, this equals a legitimate paper's true citation count, but
    # is inflated for parse-collision-contaminated nodes.
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


def data_quality_report(rows):
    lines = []
    n = len(rows)
    lines.append(f"Top-{n} papers by {CIF_PROP} pulled from Neo4j.\n")
    lines.append(f"  no title              : {sum(1 for r in rows if not r['title']):>4}")
    lines.append(f"  title = 'Deleted Work': {sum(1 for r in rows if r['title'] == 'Deleted Work'):>4}")
    lines.append(f"  no DOI                : {sum(1 for r in rows if not r['doi']):>4}")
    lines.append(f"  no primary source     : {sum(1 for r in rows if not r['src_id']):>4}")
    lines.append(f"  no first author       : {sum(1 for r in rows if not r['fa_id']):>4}")
    lines.append(f"  no field id           : {sum(1 for r in rows if not r['field_id']):>4}")
    lines.append(f"  is seed (industry-abs): {sum(1 for r in rows if r['is_seed']):>4}")
    src_types = {}
    for r in rows:
        src_types[r["src_type"]] = src_types.get(r["src_type"], 0) + 1
    lines.append(f"  by primary_source_type: {dict(sorted(src_types.items(), key=lambda x: -x[1]))}")
    types = {}
    for r in rows:
        types[r["type"]] = types.get(r["type"], 0) + 1
    lines.append(f"  by OpenAlex work type : {dict(sorted(types.items(), key=lambda x: -x[1]))}")
    return "\n".join(lines)


def write_csv(rows, path, fields):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fields})


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    print(f"pulling top-{N_TOP} article-type papers by {CIF_PROP} from Neo4j "
          f"(filtered: type='article', non-null non-'Deleted Work' title)...")
    rows = pull_top_from_neo4j()
    print(f"  got {len(rows)} rows")
    rows = dedup_by_title(rows)
    print(f"  after title/year dedup: {len(rows)} rows")

    raw_fields = ["cif", "id", "title", "type", "year", "doi",
                  "src_id", "src_type", "fa_id", "la_id",
                  "field_id", "subfield_id", "domain_id", "topic_id", "is_seed"]
    write_csv(rows, os.path.join(OUTDIR, "top_cif_papers_raw.csv"), raw_fields)

    print("enriching with display names from MongoDB...")
    rows = enrich_from_mongo(rows)

    enriched_fields = raw_fields + ["cited_by_count",
                                    "src_name", "fa_name", "la_name",
                                    "field_name", "domain_name"]
    write_csv(rows, os.path.join(OUTDIR, "top_cif_papers_enriched.csv"), enriched_fields)

    report = data_quality_report(rows)
    print()
    print(report)
    with open(os.path.join(OUTDIR, "top_cif_papers_report.txt"), "w") as f:
        f.write(report + "\n")

    print(f"\nwrote {OUTDIR}/top_cif_papers_raw.csv, _enriched.csv, _report.txt")


if __name__ == "__main__":
    main()
