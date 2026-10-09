"""
Stage 1 of the venue-level regression pipeline (ANCOVA + quantile regression).

Assembles a persistent, reproducible analysis table: one row per
journal/conference venue, with

  outcome    : CIF normalized (primary), CIF aggregate (secondary)
  controls   : field (argmax primary field + full field-share vector),
               log size, publication-year vintage stats,
               four prestige metrics (JIF, SJR, OpenAlex-h, Scimago-h)

Design decisions:
  - NO CIF>0 filter. CIF=0 is a valid value (a venue with no papers upstream
    of the seed); ~18% of JIF venues have CIF=0. This matches the paper's
    Table 1 basis (correlations.py::paired = both present, no >0 filter).
  - Vintage = median publication year (primary); first_year / p05_year kept
    for the age robustness spec. Computed unweighted over ALL the venue's
    papers (matches the CIF normalization denominator).
  - Field from the source's OpenAlex topic counts summed by parent field
    (source-level); yields both the argmax primary field and the full
    field-share vector.

Data sources:
  - Venue table (venue_data.load_rows): CIF, prestige metrics, size.
  - Data S1 venue_controls: per-venue publication-year statistics and field
    counts (collected by ref_map/aggregation/venue_controls.py).

Outputs (regression_output/):
  - venue_analysis_table.csv        the analysis dataset
  - venue_analysis_dictionary.md    column definitions
"""
import os
import sys
import csv

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from venue_data import (load_rows, load_data_s1, num, venues_only, FIELD_NAMES,
                    COL_CIF_AGG, COL_CIF_NORM, COL_JIF, COL_SJR, COL_NAME, COL_TYPE)

OUTDIR = os.path.join(_HERE, "regression_output")

COL_SIZE = "total_papers"   # all-types paper count, matches all_par CIF basis
YEAR_FIELDS = ("n_yr", "first_year", "p05_year", "mean_year", "median_year", "last_year")


def load_controls():
    """Year statistics and field counts per source, from Data S1."""
    year, fdist = {}, {}
    for r in load_data_s1("venue_controls"):
        if r.get("n_yr") is not None:
            year[r["source_id"]] = {k: r[k] for k in YEAR_FIELDS}
        if r.get("field_counts"):
            fdist[r["source_id"]] = r["field_counts"]
    return year, fdist


def main():
    year, fdist = load_controls()

    rows = venues_only(load_rows())  # journals + conferences

    # Universe of field numbers (sorted numerically) for stable columns
    field_nums = sorted({fn for d in fdist.values() for fn in d.keys()},
                        key=lambda x: int(x) if x.isdigit() else 1_000_000)
    fnames = FIELD_NAMES
    print(f"{len(field_nums)} distinct primary fields", flush=True)

    fs_cols = [f"fs_{n}" for n in field_nums]
    base_cols = ["source_id", "source_name", "source_type",
                 "cif_norm", "cif_agg", "size", "log_size",
                 "n_yr", "first_year", "p05_year", "mean_year",
                 "median_year", "last_year",
                 "jif", "sjr", "oa_h", "scimago_h",
                 "primary_field", "primary_field_name", "primary_field_share"]
    cols = base_cols + fs_cols

    import math
    out_rows = []
    n_drop_no_year = n_drop_no_field = n_drop_no_size = 0
    for r in rows:
        sid = r.get("source_id") or ""
        if not sid:
            continue
        cif_n, cif_a = num(r, COL_CIF_NORM), num(r, COL_CIF_AGG)
        size = num(r, COL_SIZE)
        if cif_n is None or cif_a is None:
            continue
        if size is None or size <= 0:
            n_drop_no_size += 1; continue
        y = year.get(sid)
        if not y:
            n_drop_no_year += 1; continue
        fd = fdist.get(sid)
        if not fd:
            n_drop_no_field += 1; continue

        total_f = sum(fd.values())
        shares = {n: fd.get(n, 0) / total_f for n in field_nums}
        pf = max(fd.items(), key=lambda kv: kv[1])[0]

        row = {
            "source_id": sid,
            "source_name": r.get(COL_NAME),
            "source_type": r.get(COL_TYPE),
            "cif_norm": cif_n, "cif_agg": cif_a,
            "size": size, "log_size": math.log(size),
            "n_yr": y["n_yr"], "first_year": y["first_year"],
            "p05_year": y["p05_year"], "mean_year": y["mean_year"],
            "median_year": y["median_year"], "last_year": y["last_year"],
            "jif": num(r, COL_JIF), "sjr": num(r, COL_SJR),
            "oa_h": num(r, "source_h_index"),
            "scimago_h": num(r, "scimago_h_index"),
            "primary_field": pf, "primary_field_name": fnames.get(pf),
            "primary_field_share": shares[pf],
        }
        for n in field_nums:
            row[f"fs_{n}"] = shares[n]
        out_rows.append(row)

    os.makedirs(OUTDIR, exist_ok=True)
    table_path = os.path.join(OUTDIR, "venue_analysis_table.csv")
    with open(table_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in out_rows:
            w.writerow(row)

    # ---- data dictionary ----
    dict_path = os.path.join(OUTDIR, "venue_analysis_dictionary.md")
    with open(dict_path, "w") as f:
        f.write("# venue_analysis_table.csv — data dictionary\n\n")
        f.write(f"{len(out_rows):,} venues (journals + conferences). "
                "One row per venue. **No CIF>0 filter** (CIF=0 is valid).\n\n")
        f.write("| column | meaning |\n|---|---|\n")
        defs = [
            ("source_id/name/type", "OpenAlex source; type in {j,c}"),
            ("cif_norm", "normalized CIF (all_par_avg_score_all) — PRIMARY outcome; may be 0"),
            ("cif_agg", "aggregate CIF (all_par_total_score) — secondary outcome; may be 0"),
            ("size, log_size", "total_papers (all types) and its log — size control"),
            ("n_yr", "number of the venue's papers with a publication year"),
            ("first_year/p05_year", "earliest / 5th-pctile pub year — founding-age proxies (robustness)"),
            ("mean_year", "mean publication year"),
            ("median_year", "median publication year — PRIMARY vintage control"),
            ("last_year", "latest publication year"),
            ("jif/sjr/oa_h/scimago_h", "prestige metrics; one per model, own-sample; may be blank"),
            ("primary_field", "argmax field number (OpenAlex field id suffix) — PRIMARY field control (FE)"),
            ("primary_field_name", "its display name"),
            ("primary_field_share", "share of the venue's papers in the primary field"),
            ("fs_<n>", "share of the venue's papers in field <n> — full field-share vector (robustness)"),
        ]
        for a, b in defs:
            f.write(f"| {a} | {b} |\n")
        f.write("\nField numbers: ")
        f.write(", ".join(f"{n}={fnames.get(n)}" for n in field_nums))
        f.write("\n")

    print(f"\nwrote {table_path}  ({len(out_rows):,} venues, {len(cols)} cols)")
    print(f"wrote {dict_path}")
    print(f"dropped: no_size={n_drop_no_size}, no_year={n_drop_no_year}, "
          f"no_field={n_drop_no_field}")
    # quick coverage sanity
    def cov(k):
        return sum(1 for r in out_rows if r.get(k) is not None)
    print(f"coverage: jif={cov('jif'):,}  sjr={cov('sjr'):,}  "
          f"oa_h={cov('oa_h'):,}  scimago_h={cov('scimago_h'):,}")
    nz = sum(1 for r in out_rows if r["cif_norm"] == 0)
    print(f"cif_norm==0: {nz:,} of {len(out_rows):,} "
          f"({100*nz/len(out_rows):.1f}%)")


if __name__ == "__main__":
    main()
