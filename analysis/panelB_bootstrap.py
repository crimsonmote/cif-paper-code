"""
Bootstrap CIs for the main-text Table 1 Panel B (analysis of deviance,
Shapley shares):
order-free shares reported with venue-resample bootstrap 95% CIs
(relative-importance convention, cf. relaimpo/Groemping).

Spec (frozen): outcome asinh(cif_norm * 1e4); blocks field (factor),
log_size, median_year, age_span, asinh(metric); per-metric samples
(JIF n=21,637; SJR n=28,841). Point estimates must reproduce
regression_output/venue_regression_results.md (rescaled panel) to ~1e-3.

Method: multinomial-weight bootstrap on the cross-product matrix.
Z = [1, blocks..., y]; for each resample, G = Z' diag(w) Z with
w ~ Multinomial(n, uniform). R^2 of any block subset comes from the
normal equations on sub-blocks of G, so each resample costs one O(n p^2)
weighted cross-product plus 32 tiny solves — B=1000 runs in seconds.
Exactly equivalent to refitting OLS on the resampled rows.

Writes regression_output/panelB_share_cis.md (+ .csv).
"""
import csv
import itertools
import math
import os

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
TABLE = os.path.join(_HERE, "regression_output", "venue_analysis_table.csv")
OUT_MD = os.path.join(_HERE, "regression_output", "panelB_share_cis.md")
OUT_CSV = os.path.join(_HERE, "regression_output", "panelB_share_cis.csv")

METRICS = [("JIF", "jif"), ("SJR", "sjr")]
OUTCOMES = ["cif_norm", "cif_agg"]
BLOCKS = ["field", "volume", "medyear", "agespan", "metric"]
B = 1000
SEED = 20260802
SCALE = 1e4


def fnum(v):
    if v in (None, "", "NA"):
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return x if np.isfinite(x) else None


def load(metric_col, outcome_col):
    rows = []
    with open(TABLE) as f:
        for r in csv.DictReader(f):
            m = fnum(r[metric_col])
            y = fnum(r[outcome_col])
            ls = fnum(r["log_size"])
            my = fnum(r["median_year"])
            fy, ly = fnum(r["first_year"]), fnum(r["last_year"])
            fld = r["primary_field"]
            if None in (m, y, ls, my, fy, ly) or fld in ("", "NA"):
                continue
            rows.append((fld, ls, my, ly - fy, m, y))
    return rows


def design(rows):
    fields = sorted({r[0] for r in rows})
    fidx = {f: i for i, f in enumerate(fields)}
    n, k = len(rows), len(fields)
    Fd = np.zeros((n, k - 1))  # drop first level (R^2 invariant to coding)
    num = np.zeros((n, 4))
    y = np.zeros(n)
    for i, (fld, ls, my, sp, m, cy) in enumerate(rows):
        j = fidx[fld]
        if j > 0:
            Fd[i, j - 1] = 1.0
        num[i] = (ls, my, sp, np.arcsinh(m))
        y[i] = np.arcsinh(cy * SCALE)
    X = np.column_stack([np.ones(n), Fd, num])
    cols = {  # column indices per block (intercept = 0, always included)
        "field": list(range(1, k)),
        "volume": [k],
        "medyear": [k + 1],
        "agespan": [k + 2],
        "metric": [k + 3],
    }
    Z = np.column_stack([X, y])
    return Z, cols


def r2_from_G(G, col_sets):
    """R^2 for each block subset from the weighted cross-product matrix."""
    p = G.shape[0] - 1
    yy = G[p, p]
    sw = G[0, 0]
    ybar_term = G[0, p] ** 2 / sw
    tss = yy - ybar_term
    out = {}
    for key, cidx in col_sets.items():
        idx = [0] + cidx
        Gxx = G[np.ix_(idx, idx)]
        Gxy = G[idx, p]
        beta = np.linalg.lstsq(Gxx, Gxy, rcond=None)[0]
        rss = yy - beta @ Gxy
        out[key] = 0.0 if tss <= 0 else 1.0 - rss / tss
    return out


def shapley(v):
    phi = {}
    nb = len(BLOCKS)
    fact = [math.factorial(i) for i in range(nb + 1)]
    for b in BLOCKS:
        others = [x for x in BLOCKS if x != b]
        val = 0.0
        for m in range(len(others) + 1):
            for s in itertools.combinations(others, m):
                w = fact[len(s)] * fact[nb - len(s) - 1] / fact[nb]
                val += w * (v[frozenset(s + (b,))] - v[frozenset(s)])
        phi[b] = val
    return phi


def all_subset_r2(G, cols):
    col_sets = {}
    for m in range(len(BLOCKS) + 1):
        for s in itertools.combinations(BLOCKS, m):
            col_sets[frozenset(s)] = sum((cols[b] for b in s), [])
    return r2_from_G(G, col_sets)


def main():
    rng = np.random.default_rng(SEED)
    md = ["# Panel B/C Shapley shares — venue-resample bootstrap 95% CIs",
          "",
          f"B = {B}, multinomial weights, seed {SEED}. Outcome "
          f"asinh(outcome x {SCALE:.0f}); blocks field, log_size, "
          "median_year, age_span, asinh(metric). Shares in percent.", ""]
    csv_rows = [("outcome", "metric", "n", "block", "share_pct",
                 "lo95_pct", "hi95_pct")]
    for outcome, (label, colname) in [(oc, m) for oc in OUTCOMES
                                      for m in METRICS]:
        rows = load(colname, outcome)
        Z, cols = design(rows)
        n = Z.shape[0]
        G0 = Z.T @ Z
        r2s0 = all_subset_r2(G0, cols)
        point = shapley(r2s0)
        total = r2s0[frozenset(BLOCKS)]
        boots = {b: np.empty(B) for b in BLOCKS}
        boots["total"] = np.empty(B)
        for it in range(B):
            w = rng.multinomial(n, np.full(n, 1.0 / n)).astype(float)
            Gw = (Z * w[:, None]).T @ Z
            r2s = all_subset_r2(Gw, cols)
            ph = shapley(r2s)
            for b in BLOCKS:
                boots[b][it] = ph[b]
            boots["total"][it] = r2s[frozenset(BLOCKS)]
        md.append(f"## {outcome} — {label} (n = {n:,})")
        md.append("")
        md.append("| block | share % | 95% CI |")
        md.append("|---|--:|---|")
        for b in BLOCKS + ["total"]:
            pt = (total if b == "total" else point[b]) * 100
            lo, hi = np.percentile(boots[b], [2.5, 97.5]) * 100
            md.append(f"| {b} | {pt:.1f} | [{lo:.1f}, {hi:.1f}] |")
            csv_rows.append((outcome, label, n, b, f"{pt:.3f}",
                             f"{lo:.3f}", f"{hi:.3f}"))
        md.append("")
        print(f"{outcome} {label}: point shares "
              + ", ".join(f"{b}={point[b]*100:.1f}" for b in BLOCKS)
              + f", total={total*100:.1f}")
    with open(OUT_MD, "w") as f:
        f.write("\n".join(md) + "\n")
    with open(OUT_CSV, "w", newline="") as f:
        csv.writer(f).writerows(csv_rows)
    print("\n".join(md))


if __name__ == "__main__":
    main()
