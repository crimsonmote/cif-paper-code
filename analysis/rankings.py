"""
rankings.py
-----------
Venue rankings and overperformer analysis on the venue table
(all_* columns, journals + conferences).

Produces:
  1. Aggregate CIF top venues
  2. Startup-enabling count top venues
  3. CIF-intensity (normalized) top venues  [>= MIN_WORKS]
  4. Overperformers, two complementary lenses (vs JIF, >= MIN_WORKS):
       (a) MAGNITUDE : CIF intensity minus linear-fit JIF prediction
       (b) RANK-GAP  : CIF percentile minus JIF percentile

Run:  python analysis/rankings.py
Requires: numpy, scipy, common.py + the CSV.
"""
import numpy as np
from scipy.stats import rankdata
from venue_data import (load_rows, num, venues_only,
                    COL_CIF_AGG, COL_CIF_NORM, COL_CNT_AGG,
                    COL_JIF, COL_NAME, COL_NWORKS)

MIN_WORKS = 1000   # venue size floor (all document types)
TOPK = 15


def top_by(subset, key, k=TOPK, scale=1.0):
    vals = [(r[COL_NAME], num(r, key) * scale)
            for r in subset if num(r, key) is not None]
    return sorted(vals, key=lambda x: x[1], reverse=True)[:k]


def overperformer_pool(subset):
    """Rows with JIF + CIF-intensity present, >= MIN_WORKS, CIF>0."""
    pool = []
    for r in subset:
        jif = num(r, COL_JIF); cif = num(r, COL_CIF_NORM)
        na = num(r, COL_NWORKS) or 0
        if jif is not None and cif is not None and na >= MIN_WORKS and cif > 0:
            pool.append({"name": r[COL_NAME], "jif": jif, "cif": cif * 1e4})
    return pool


def main():
    rows = load_rows()
    venues = venues_only(rows)

    print("=" * 80)
    print("AGGREGATE CIF — top venues")
    print("=" * 80)
    for nm, v in top_by(venues, COL_CIF_AGG):
        print(f"  {nm[:50]:<50} {v:.1f}")

    print("\n" + "=" * 80)
    print("STARTUP-ENABLING COUNT — top venues")
    print("=" * 80)
    for nm, v in top_by(venues, COL_CNT_AGG):
        print(f"  {nm[:50]:<50} {int(v)}")

    print("\n" + "=" * 80)
    print(f"CIF INTENSITY (x1e4) — top venues, >= {MIN_WORKS} works")
    print("=" * 80)
    intensity = [(r[COL_NAME], num(r, COL_CIF_NORM) * 1e4, num(r, COL_JIF))
                 for r in venues
                 if num(r, COL_CIF_NORM) is not None
                 and (num(r, COL_NWORKS) or 0) >= MIN_WORKS]
    for nm, v, jif in sorted(intensity, key=lambda x: x[1], reverse=True)[:TOPK]:
        js = f"{jif:.1f}" if jif is not None else "NA"
        print(f"  {nm[:46]:<46} CIF_int={v:6.1f}  JIF={js:>6}")

    # ---- Overperformers ----
    pool = overperformer_pool(venues)
    n = len(pool)
    jif = np.array([d["jif"] for d in pool])
    cif = np.array([d["cif"] for d in pool])
    coef = np.polyfit(jif, cif, 1)                    # linear fit CIF ~ JIF
    jif_pct = rankdata(jif) / n * 100
    cif_pct = rankdata(cif) / n * 100
    for i, d in enumerate(pool):
        d["resid"] = cif[i] - np.polyval(coef, jif[i])
        d["jif_pct"] = jif_pct[i]; d["cif_pct"] = cif_pct[i]
        d["gap"] = cif_pct[i] - jif_pct[i]

    print("\n" + "=" * 92)
    print(f"OVERPERFORMERS — MAGNITUDE (residual vs linear JIF fit), pool n={n}")
    print("=" * 92)
    print(f"  {'Venue':<46}{'JIF':>6}{'CIFi':>8}{'resid':>9}")
    for d in sorted(pool, key=lambda x: x["resid"], reverse=True)[:TOPK]:
        print(f"  {d['name'][:44]:<46}{d['jif']:>6.1f}{d['cif']:>8.1f}{d['resid']:>9.1f}")

    print("\n" + "=" * 92)
    print(f"OVERPERFORMERS — RANK-GAP (CIF pct minus JIF pct), pool n={n}")
    print("=" * 92)
    print(f"  {'Venue':<46}{'JIF':>6}{'JIF%':>6}{'CIF%':>6}{'gap':>7}")
    for d in sorted(pool, key=lambda x: x["gap"], reverse=True)[:TOPK]:
        print(f"  {d['name'][:44]:<46}{d['jif']:>6.1f}"
              f"{d['jif_pct']:>5.0f}%{d['cif_pct']:>5.0f}%{d['gap']:>+7.0f}")


if __name__ == "__main__":
    main()
