"""CIF against prestige metrics including the cumulative h-indices (SM S4.3, Table S16).

Correlates aggregate and normalized CIF, and the industry-absorbed count and share,
with JIF, SJR, the OpenAlex journal h-index and the Scimago h-index:
  1. over the full population and within the top 1,000 and top 300 venues by each
     metric: Pearson r, Pearson on asinh, Spearman rho, with bootstrap intervals for
     the CIF rows;
  2. within each of the top-10 OpenAlex fields: median [min, max] across fields.

Writes analysis/results/HINDEX_COMPREHENSIVE_RESULTS.md.
"""
import sys
import os
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import numpy as np
from scipy.stats import pearsonr, spearmanr
from venue_data import (load_rows, num, venues_only,
                    COL_CIF_AGG, COL_CIF_NORM, COL_CNT_AGG, COL_CNT_NORM,
                    COL_JIF, COL_SJR, COL_NWORKS)
from within_field_correlations import (load_primary_field_by_source,
                                       TOP10_FIELDS, FIELD_NAMES)

OUT = os.path.join(_HERE, "results", "HINDEX_COMPREHENSIVE_RESULTS.md")
rng = np.random.default_rng(2024)

MEASURES = [
    ("Ind.-absorbed count (agg.)", COL_CNT_AGG,  False),
    ("CIF (aggregate)",            COL_CIF_AGG,  True),
    ("Ind.-absorbed share (norm.)",COL_CNT_NORM, False),
    ("CIF (normalized)",           COL_CIF_NORM, True),
]
METRICS = [("JIF", COL_JIF), ("SJR", COL_SJR),
           ("OpenAlex-h", "source_h_index"), ("Scimago-h", "scimago_h_index")]

def stats3(x, y):
    return (pearsonr(x, y)[0],
            pearsonr(np.arcsinh(x), np.arcsinh(y))[0],
            spearmanr(x, y).correlation)

def boot(x, y, nres=1000):
    idx = np.arange(len(x)); pr, pa, sp = [], [], []
    for _ in range(nres):
        s = rng.choice(idx, len(idx), replace=True)
        xs, ys = x[s], y[s]
        pr.append(pearsonr(xs, ys)[0])
        pa.append(pearsonr(np.arcsinh(xs), np.arcsinh(ys))[0])
        sp.append(spearmanr(xs, ys).correlation)
    q = lambda a: (np.percentile(a, 2.5), np.percentile(a, 97.5))
    return q(pr), q(pa), q(sp)

def pairs(rows, mkey, pkey):
    # Match the paper's correlations.py::paired — both present, NO >0 filter
    # (CIF=0 and metric=0 are valid; ~18% of JIF venues have CIF=0).
    xs, ys = [], []
    for r in rows:
        a, b = num(r, mkey), num(r, pkey)
        if a is not None and b is not None:
            xs.append(a); ys.append(b)
    return np.array(xs), np.array(ys)

def main():
    rows = venues_only(load_rows())
    field_by_source = load_primary_field_by_source()

    L = []
    def w(s=""): L.append(s)

    w("# CIF against prestige metrics, including the cumulative h-indices")
    w()
    w("Mirrors the paper Table 1 structure, extended to OpenAlex and Scimago "
      "h-index. Venues = journals + conferences. Measure and metric both > 0. "
      "P = Pearson raw; Pa = Pearson asinh; S = Spearman. Bootstrap 95% CIs "
      "(1,000 resamples) shown for the two CIF rows in the full and top-1000 "
      "panels.")
    w()

    # ---- Section 1: panels ----
    PANELS = [("Full population", None), ("Top-1000 by metric", 1000),
              ("Top-300 by metric", 300)]
    for panel_label, N in PANELS:
        w(f"## {panel_label}")
        w()
        w("| Measure | Metric | n | P | Pa | S | boot CI (P / Pa / S) |")
        w("|---|---|--:|--:|--:|--:|---|")
        for m_label, m_key, is_cif in MEASURES:
            for p_label, p_key in METRICS:
                x, y = pairs(rows, m_key, p_key)
                if N is not None:
                    order = np.argsort(-y)[:N]
                    x, y = x[order], y[order]
                if len(x) < 5:
                    continue
                pr, pa, sp = stats3(x, y)
                ci = ""
                if is_cif and N != 300:
                    (pl, pu), (al, au), (slw, su) = boot(x, y)
                    ci = (f"[{pl:+.2f},{pu:+.2f}] / [{al:+.2f},{au:+.2f}] / "
                          f"[{slw:+.2f},{su:+.2f}]")
                w(f"| {m_label} | {p_label} | {len(x)} | {pr:+.3f} | "
                  f"{pa:+.3f} | {sp:+.3f} | {ci} |")
        w()

    # ---- Section 2: within-field ----
    w("## Within-field (top-10 OpenAlex fields): median [min, max] across fields")
    w()
    rows_by_field = {}
    for r in rows:
        sid = r.get("source_id")
        fid = field_by_source.get(sid) if sid else None
        if fid in TOP10_FIELDS:
            rows_by_field.setdefault(fid, []).append(r)
    w("| Measure | Metric | #flds | P median [min,max] | Pa median [min,max] | S median [min,max] |")
    w("|---|---|--:|---|---|---|")
    for m_label, m_key, is_cif in MEASURES:
        if not is_cif:
            continue
        for p_label, p_key in METRICS:
            prs, pas, sps = [], [], []
            for fid in TOP10_FIELDS:
                sub = rows_by_field.get(fid, [])
                xs, ys = [], []
                for r in sub:
                    a, b = num(r, m_key), num(r, p_key)
                    if a is not None and b is not None:
                        xs.append(a); ys.append(b)
                if len(xs) < 5:
                    continue
                x, y = np.array(xs), np.array(ys)
                p, a, s = stats3(x, y)
                prs.append(p); pas.append(a); sps.append(s)
            def fmt(v):
                v = np.array(v)
                return f"{np.median(v):+.3f} [{v.min():+.3f},{v.max():+.3f}]"
            w(f"| {m_label} | {p_label} | {len(prs)} | {fmt(prs)} | {fmt(pas)} | {fmt(sps)} |")
    w()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write("\n".join(L) + "\n")
    print(f"wrote {OUT}")
    print("\n".join(L))

if __name__ == "__main__":
    main()
