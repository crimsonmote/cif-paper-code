"""Seed-specificity test (SM S4.6, Tables S22-S24).

Compares CIF computed from the industry-absorbed seed set with CIF computed from
three stratum-matched seed samples, all four runs on one shared subgraph, at the
venue level (Data S1: cif_matched_samples, with works and types from
cif_all_documents).

Sections of analysis/results/M5_PLACEBO_RESULTS.md:
  1. Reconciliation of the shared-subgraph CIF run with the published CIF.
  2. Spearman correlation against the floor (venues nonzero in >= 1 run).
  3. Per-pair tables: Pearson, Pearson on asinh, Spearman, and top-20/100/1000
     overlaps, for all six pairs, over all venues, over venues nonzero in >= 1 run,
     over the curated set (venues with JIF or SJR), over the venues every run
     reaches, and among each pair's top-2,000 venues.
  4. Tail decomposition: Pearson without the top 100 venues, the top-100 share of
     the covariance, and the median size of each ranking's top 100.

Normalized CIF is summed CIF / works at full precision.
"""
import csv
import json
import os

import numpy as np
from scipy.stats import pearsonr, spearmanr

import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from venue_data import RESULTS_DIR, load_data_s1  # noqa: E402

TABLE = os.path.join(_HERE, "regression_output", "venue_analysis_table.csv")
OUT_MD = os.path.join(RESULTS_DIR, "M5_PLACEBO_RESULTS.md")

# run -> field of Data S1 cif_matched_samples
ARMS = {"true": "cif_union_graph", "r1": "sample_1", "r2": "sample_2", "r3": "sample_3"}
PLACEBOS = ["r1", "r2", "r3"]
PAIRS = [("CIF vs null 1", "true", "r1"), ("CIF vs null 2", "true", "r2"),
         ("CIF vs null 3", "true", "r3"), ("null 1 vs null 2", "r1", "r2"),
         ("null 1 vs null 3", "r1", "r3"), ("null 2 vs null 3", "r2", "r3")]
VENUE_TYPES = {"j", "c"}
NORM_SCALE = 1e4


_BASE = None
_RUNS = None


def load_arm(run):
    """Summed CIF, normalized CIF and works per journal/conference for one run."""
    global _BASE, _RUNS
    if _BASE is None:
        _BASE = {r["source_id"]: r for r in load_data_s1("cif_all_documents")}
        _RUNS = {r["source_id"]: r for r in load_data_s1("cif_matched_samples")}
    agg, norm, papers = {}, {}, {}
    for sid, b in _BASE.items():
        if b.get("source_type") not in VENUE_TYPES:
            continue
        ts = float(_RUNS.get(sid, {}).get(run) or 0.0)
        tp = b.get("total_papers") or 0
        agg[sid] = ts
        norm[sid] = ts / tp if tp else 0.0
        papers[sid] = tp
    return agg, norm, papers


def fnum(v):
    if v in (None, "", "NA"):
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if np.isfinite(x) else None


def pair_tables(A, N, mask, w):
    """Correlations + top-k overlaps for all six pairs within `mask`."""
    idx = np.where(mask)[0]
    w("| pair | P agg | Pa agg | rho agg | P norm | Pa norm | rho norm |")
    w("|---|--:|--:|--:|--:|--:|--:|")
    for lbl, a, b in PAIRS:
        cells = []
        for S, is_norm in ((A, False), (N, True)):
            x, y = S[a][idx], S[b][idx]
            xa = np.arcsinh(x * NORM_SCALE) if is_norm else np.arcsinh(x)
            ya = np.arcsinh(y * NORM_SCALE) if is_norm else np.arcsinh(y)
            cells += [f"{pearsonr(x, y)[0]:.4f}", f"{pearsonr(xa, ya)[0]:.4f}",
                      f"{spearmanr(x, y).correlation:.4f}"]
        w(f"| {lbl} | " + " | ".join(cells) + " |")
    w("")

    def ov(x, y, k):
        return len(set(idx[np.argsort(-x[idx])[:k]]) &
                   set(idx[np.argsort(-y[idx])[:k]]))
    w("| pair | top-20 agg | top-100 agg | top-1000 agg | "
      "top-20 norm | top-100 norm | top-1000 norm |")
    w("|---|--:|--:|--:|--:|--:|--:|")
    for lbl, a, b in PAIRS:
        cells = [str(ov(A[a], A[b], k)) for k in (20, 100, 1000)]
        cells += [str(ov(N[a], N[b], k)) for k in (20, 100, 1000)]
        w(f"| {lbl} | " + " | ".join(cells) + " |")
    w("")


def top_view(A, N, mask, w, k=2000):
    """Tight top-venue view: per pair, overlap of the two arms' top-k (within
    mask), plus Spearman/asinh-Pearson within the UNION of the two top-k sets
    and Spearman within the INTERSECTION (ordering agreement among shared
    top venues)."""
    idx = np.where(mask)[0]

    def triple(S, a, b, members, is_norm):
        sc = NORM_SCALE if is_norm else 1.0
        x, y = S[a][members], S[b][members]
        if len(members) < 3:
            return ["nan"] * 3
        return [f"{pearsonr(x, y)[0]:.3f}",
                f"{pearsonr(np.arcsinh(x * sc), np.arcsinh(y * sc))[0]:.3f}",
                f"{spearmanr(x, y).correlation:.3f}"]

    sets = {}
    for lbl, a, b in PAIRS:
        per = {}
        for key, S in (("agg", A), ("norm", N)):
            ta = set(idx[np.argsort(-S[a][idx])[:k]])
            tb = set(idx[np.argsort(-S[b][idx])[:k]])
            per[key] = (np.array(sorted(ta | tb)), np.array(sorted(ta & tb)))
        sets[lbl] = per

    w(f"**Union of the two arms' top-{k} sets:**")
    w("")
    w(f"| pair | n∪ agg | P agg | Pa agg | rho agg | "
      f"n∪ norm | P norm | Pa norm | rho norm |")
    w("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for lbl, a, b in PAIRS:
        cells = []
        for key, S, is_norm in (("agg", A, False), ("norm", N, True)):
            union, _ = sets[lbl][key]
            cells += [str(len(union))] + triple(S, a, b, union, is_norm)
        w(f"| {lbl} | " + " | ".join(cells) + " |")
    w("")
    w(f"**Intersection of the two arms' top-{k} sets:**")
    w("")
    w(f"| pair | n∩ agg | P agg | Pa agg | rho agg | "
      f"n∩ norm | P norm | Pa norm | rho norm |")
    w("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for lbl, a, b in PAIRS:
        cells = []
        for key, S, is_norm in (("agg", A, False), ("norm", N, True)):
            _, inter = sets[lbl][key]
            cells += [str(len(inter))] + triple(S, a, b, inter, is_norm)
        w(f"| {lbl} | " + " | ".join(cells) + " |")
    w("")


def tail_decomposition(A, N, size, mask, w):
    idx = np.where(mask)[0]
    w("| pair | P full | P drop-top-100 | top-100 cov share |")
    w("|---|--:|--:|--:|")
    for lbl, a, b in (("AGG CIF vs null 1", "true", "r1"),
                      ("AGG null 1 vs null 2", "r1", "r2"),
                      ("NORM CIF vs null 1", "true", "r1"),
                      ("NORM null 1 vs null 2", "r1", "r2")):
        S = A if lbl.startswith("AGG") else N
        x, y = S[a][idx], S[b][idx]
        r_full = pearsonr(x, y)[0]
        top = np.argsort(-(np.abs(x) + np.abs(y)))[:100]
        keep = np.ones(len(x), bool)
        keep[top] = False
        r_drop = pearsonr(x[keep], y[keep])[0]
        xc, yc = x - x.mean(), y - y.mean()
        share = (xc[top] * yc[top]).sum() / (xc * yc).sum()
        w(f"| {lbl} | {r_full:.4f} | {r_drop:.4f} | {share:.1%} |")
    top_a = idx[np.argsort(-A["true"][idx])[:100]]
    top_n = idx[np.argsort(-N["true"][idx])[:100]]
    w("")
    w(f"Median venue size (papers): top-100 by aggregate = "
      f"{int(np.median(size[top_a])):,}; top-100 by normalized = "
      f"{int(np.median(size[top_n])):,}; universe median = "
      f"{int(np.median(size[idx])):,}.")
    w("")


def main():
    arms = {name: load_arm(f) for name, f in ARMS.items()}
    sids = sorted(set().union(*(a[0].keys() for a in arms.values())))
    A = {n: np.array([arms[n][0].get(s, 0.0) for s in sids]) for n in arms}
    N = {n: np.array([arms[n][1].get(s, 0.0) for s in sids]) for n in arms}
    size = np.array([max(arms[n][2].get(s, 0) for n in arms) for s in sids])

    nz = np.zeros(len(sids), dtype=bool)
    for n in arms:
        nz |= A[n] > 0

    pub_agg, pub_norm, meta = {}, {}, {}
    with open(TABLE) as f:
        for r in csv.DictReader(f):
            pub_agg[r["source_id"]] = fnum(r["cif_agg"])
            pub_norm[r["source_id"]] = fnum(r["cif_norm"])
            meta[r["source_id"]] = {
                "name": r["source_name"],
                "jif": fnum(r["jif"]), "sjr": fnum(r["sjr"]),
                "oa_h": fnum(r["oa_h"]), "scimago_h": fnum(r["scimago_h"]),
            }
    curated = np.array([s in meta and (meta[s]["jif"] is not None or
                                       meta[s]["sjr"] is not None)
                        for s in sids])

    L = []
    w = L.append
    w("# Seed-specificity test: CIF against stratum-matched seed samples")
    w("")
    w(f"Universe: {len(sids):,} j/c sources; nonzero in >=1 arm: "
      f"{int(nz.sum()):,}. Curated universe (JIF or SJR available): "
      f"{int(curated.sum()):,}; curated & nonzero: "
      f"{int((curated & nz).sum()):,}. Per-arm zeros (nonzero set): "
      + ", ".join(f"{n}={int((A[n][nz]==0).sum()):,}" for n in ARMS))
    w("")

    # ---- 1. reconciliation ----
    join = [i for i, s in enumerate(sids)
            if s in pub_agg and pub_agg[s] is not None]
    ja = np.array(join)
    rec_a = spearmanr(A["true"][ja],
                      np.array([pub_agg[sids[i]] for i in join])).correlation
    rec_n = spearmanr(N["true"][ja],
                      np.array([pub_norm[sids[i]] for i in join])).correlation
    w("## 1. Reconciliation: union-graph true arm vs published CIF")
    w("")
    w(f"n = {len(join):,}. Spearman aggregate = {rec_a:.4f}; normalized = "
      f"{rec_n:.4f}. (Union graph differs from the published closure only "
      "through the mean-out-degree normalizer.)")
    w("")

    # ---- 2. primary endpoint ----
    def rho(x, y, m):
        return spearmanr(x[m], y[m]).correlation
    w("## 2. Primary endpoint : Spearman agreement vs floor")
    w("")
    for label, S in (("aggregate", A), ("normalized", N)):
        tv = [rho(S["true"], S[p], nz) for p in PLACEBOS]
        fl = [rho(S[a], S[b], nz) for a, b in
              (("r1", "r2"), ("r1", "r3"), ("r2", "r3"))]
        w(f"- {label}: CIF-vs-placebo mean {np.mean(tv):.4f} "
          f"[{min(tv):.4f}, {max(tv):.4f}]; floor {np.mean(fl):.4f} "
          f"[{min(fl):.4f}, {max(fl):.4f}]")
    w("")

    # ---- 3. per-pair tables, both universes ----
    w("## 3a. Per-pair tables — FULL universe (nonzero in >=1 arm)")
    w("")
    pair_tables(A, N, nz, w)
    w("## 3b. Per-pair tables — CURATED universe (JIF or SJR; nonzero in >=1 arm)")
    w("")
    pair_tables(A, N, nz & curated, w)

    # ---- 3e. all venues, no nonzero filter (source of SM Table S22) ----
    # Departs from M5_NULL_MODEL_SPEC.md sec. 9 ("nonzero in >=1 arm"): the SM
    # reports all journals/conferences, venues a run does not reach scored 0,
    # matching the main-paper convention of keeping zero-CIF venues.
    allv = np.ones(len(sids), dtype=bool)
    w("## 3e. Per-pair tables — ALL venues (no nonzero filter; SM Table S22)")
    w("")
    w(f"n = {len(sids):,}; scored zero by every arm: {int((~nz).sum()):,}; "
      f"curated (JIF or SJR): {int(curated.sum()):,}. Departs from "
      "M5_NULL_MODEL_SPEC.md sec. 9 (nonzero filter) to match the main-paper "
      "convention of keeping zero-CIF venues.")
    w("")
    pair_tables(A, N, allv, w)
    w("## 3f. Per-pair tables — CURATED venues, no nonzero filter (SM Table S22)")
    w("")
    w(f"n = {int(curated.sum()):,}; scored zero by every arm: "
      f"{int((curated & ~nz).sum()):,}.")
    w("")
    pair_tables(A, N, curated, w)

    # ---- 3g. venues every run reaches (SM S4.6 text) ----
    # The 108,065 venues no run reaches are tied at zero in every run and raise
    # every rank correlation over the full list. Restricting to the venues all
    # four runs score above zero removes those ties.
    reach_all = np.ones(len(sids), dtype=bool)
    for n in arms:
        reach_all &= A[n] > 0
    w("## 3g. Venues reached by all four runs (SM S4.6 text)")
    w("")
    w(f"n = {int(reach_all.sum()):,} venues scored above zero by every run.")
    w("")
    w("| pair | P agg | rho agg | rho norm |")
    w("|---|--:|--:|--:|")
    idx = np.where(reach_all)[0]
    for lbl, a, b in PAIRS:
        w(f"| {lbl} | {pearsonr(A[a][idx], A[b][idx])[0]:.4f} | "
          f"{spearmanr(A[a][idx], A[b][idx]).correlation:.4f} | "
          f"{spearmanr(N[a][idx], N[b][idx]).correlation:.4f} |")
    w("")

    # ---- 3c/3d. tight top-2000 view, both universes ----
    w("## 3c. Top-2000 view — FULL universe")
    w("")
    top_view(A, N, nz, w, k=2000)
    w("## 3d. Top-2000 view — CURATED universe")
    w("")
    top_view(A, N, nz & curated, w, k=2000)

    # ---- 4. tail decomposition, both universes ----
    w("## 4a. Tail decomposition — FULL universe")
    w("")
    tail_decomposition(A, N, size, nz, w)
    w("## 4b. Tail decomposition — CURATED universe")
    w("")
    tail_decomposition(A, N, size, nz & curated, w)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(OUT_MD, "w") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))
    print(f"\nwrote {OUT_MD}")


if __name__ == "__main__":
    main()
