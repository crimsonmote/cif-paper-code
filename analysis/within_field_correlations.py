"""
Within-field CIF-prestige correlations (SM S4.1, Tables S11 and S12).

For each of the top-10 fields by seed count (Section S3.3), compute
CIF-JIF and CIF-SJR correlations (raw Pearson, asinh Pearson, Spearman)
across venues whose primary field is that field. Report:
  1. Per-field correlations
  2. Median and [min, max] range across the 10 fields for each stat
  3. Variance decomposition: R^2 of log(CIF) ~ field membership

Field assignment is done via MongoDB `openalex.sources.topics[]` — each
source has an array of topics with `field.id` and a paper count; the
source's primary field is the field with the largest total count.  This
sidesteps a full Neo4j Work-node scan (which contends with the running
d=0.95 damping computation).
"""
import os
import sys
from collections import defaultdict
import numpy as np
from scipy.stats import pearsonr, spearmanr

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from venue_data import (load_rows, load_data_s1, num, venues_only,
                    COL_CIF_AGG, COL_CIF_NORM, COL_JIF, COL_SJR)

# Top-10 fields by seed count (from S3.3 / seed_field_composition.py output).
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
FIELD_NAMES = {
    "fields/22": "Engineering",
    "fields/27": "Medicine",
    "fields/13": "Biochemistry",
    "fields/17": "Computer Science",
    "fields/25": "Materials Science",
    "fields/31": "Physics",
    "fields/16": "Chemistry",
    "fields/24": "Immunology",
    "fields/28": "Neuroscience",
    "fields/11": "Agr. & Biol. Sci.",
}


def load_primary_field_by_source():
    """For each source, the field that dominates its topic-count distribution
    (Data S1 venue_controls: topic counts summed by parent field)."""
    primary_field = {}
    for r in load_data_s1("venue_controls"):
        fc = r.get("field_counts")
        if fc:
            primary_field[r["source_id"]] = "fields/" + max(fc.items(), key=lambda kv: kv[1])[0]
    print(f"  assigned a primary field to {len(primary_field):,} sources", flush=True)
    return primary_field


def show_field_correlations(rows_by_field, field_names_ordered):
    """Per-field Pearson raw / Pearson asinh / Spearman for
    CIF (agg & norm) vs JIF and vs SJR."""
    MEASURES = [
        ("CIF (aggregate)", COL_CIF_AGG),
        ("CIF (normalized)", COL_CIF_NORM),
    ]
    METRICS = [("JIF", COL_JIF), ("SJR", COL_SJR)]

    stats_out = {}
    for m_label, _ in MEASURES:
        for p_label, _ in METRICS:
            stats_out[(m_label, p_label)] = {
                "P_raw": [], "P_asinh": [], "Spearman": [],
                "n_per_field": [], "labels": [],
            }

    print()
    print("=== Per-field correlations ===")
    for fid, fname in field_names_ordered:
        subset = rows_by_field.get(fid, [])
        print(f"\n[{fname}]  n_venues in field = {len(subset)}")
        for m_label, m_key in MEASURES:
            for p_label, p_key in METRICS:
                xs, ys = [], []
                for r in subset:
                    a, b = num(r, m_key), num(r, p_key)
                    if a is not None and b is not None:
                        xs.append(a); ys.append(b)
                if len(xs) < 5:
                    print(f"    {m_label:<20} vs {p_label:<4}  n={len(xs)}  (skip)")
                    continue
                xs, ys = np.array(xs), np.array(ys)
                p_r = pearsonr(xs, ys)[0]
                p_a = pearsonr(np.arcsinh(xs), np.arcsinh(ys))[0]
                sp = spearmanr(xs, ys).correlation
                d = stats_out[(m_label, p_label)]
                d["P_raw"].append(p_r); d["P_asinh"].append(p_a)
                d["Spearman"].append(sp); d["n_per_field"].append(len(xs))
                d["labels"].append(fname)
                print(f"    {m_label:<20} vs {p_label:<4}  n={len(xs):>6}  "
                      f"P={p_r:+.3f}  P_asinh={p_a:+.3f}  Spearman={sp:+.3f}")

    print()
    print("=== Summary across top-10 fields (median [min, max]) ===")
    for m_label, _ in MEASURES:
        for p_label, _ in METRICS:
            vals = stats_out[(m_label, p_label)]
            print(f"\n  {m_label} vs {p_label} (# fields = {len(vals['P_raw'])})")
            for stat_key in ("P_raw", "P_asinh", "Spearman"):
                vv = np.array(vals[stat_key])
                if len(vv) == 0:
                    print(f"    {stat_key:<9} : (no data)")
                    continue
                print(f"    {stat_key:<9} : median {np.median(vv):+.3f}  "
                      f"min {vv.min():+.3f}  max {vv.max():+.3f}")

    return stats_out


def variance_decomposition(rows_by_field, field_names_ordered):
    """log(CIF) ~ field: between-field share of total variance."""
    print()
    print("=== Variance decomposition: log(CIF) ~ field ===")
    for m_label, m_key in [("aggregate CIF", COL_CIF_AGG),
                           ("normalized CIF", COL_CIF_NORM)]:
        all_x, all_group = [], []
        for fid, _fname in field_names_ordered:
            for r in rows_by_field.get(fid, []):
                v = num(r, m_key)
                if v is not None and v > 0:
                    all_x.append(np.log(v)); all_group.append(fid)
        if len(all_x) < 20:
            print(f"  {m_label}: insufficient data"); continue
        x = np.array(all_x); g = np.array(all_group)
        gm = x.mean()
        ss_total = np.sum((x - gm) ** 2)
        ss_between = 0.0
        for u in np.unique(g):
            m = g == u
            ss_between += m.sum() * (x[m].mean() - gm) ** 2
        r_sq = ss_between / ss_total if ss_total > 0 else 0.0
        print(f"  log({m_label:<15}) ~ field:  R^2 = {r_sq:.3f}  "
              f"({r_sq*100:.1f}% between-field; "
              f"{(1-r_sq)*100:.1f}% within-field), n = {len(x):,}")


def main():
    field_by_source = load_primary_field_by_source()

    print("\nLoading venues from CSV and joining primary field...")
    rows = load_rows()
    venues = venues_only(rows)
    rows_by_field = defaultdict(list)
    n_matched = 0
    for r in venues:
        sid = r.get("source_id")
        if not sid: continue
        fid = field_by_source.get(sid)
        if fid in TOP10_FIELDS:
            rows_by_field[fid].append(r)
            n_matched += 1
    print(f"  {n_matched:,} venues assigned to top-10 fields "
          f"(of {len(venues):,} journals+conferences)")

    field_names_ordered = [(f, FIELD_NAMES[f]) for f in TOP10_FIELDS]
    show_field_correlations(rows_by_field, field_names_ordered)
    variance_decomposition(rows_by_field, field_names_ordered)


if __name__ == "__main__":
    main()
