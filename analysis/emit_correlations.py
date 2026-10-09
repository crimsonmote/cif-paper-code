"""
Emit the S3.5 extended-correlation longtable.

For each threshold (full population; top 2000; top 1000; top 600; top 300
by JIF) and each (measure, prestige metric) pair, compute Pearson and
Spearman correlations with both analytic (Fisher-z / Bonett-Wright) and
bootstrap (10,000 resamples, percentile) 95% CIs.

Flags applied at print time (not in the printed table):
  * WIDE : max half-width of any CI > 0.15
  * ZERO : any CI crosses 0
  * DIFF : analytic vs bootstrap CIs differ by > 0.05 in either endpoint

The flags are surfaced in a short "review" section printed alongside the
table, not embedded in the caption.

Outputs:
    figs/correlation_table.tex           longtable body
    figs/correlation_flags.txt           review notes (WIDE/ZERO/DIFF)
"""
import os
import pickle
import numpy as np
from scipy.stats import pearsonr, spearmanr, norm

FIGS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")
os.makedirs(FIGS, exist_ok=True)
CACHE_PATH = os.path.join(FIGS, "correlation_results.pkl")

from venue_data import (load_rows, num, venues_only,
                    COL_CIF_AGG, COL_CIF_NORM, COL_CNT_AGG, COL_CNT_NORM,
                    COL_JIF, COL_SJR)

N_BOOT = 10_000
SEED = 2024
THRESHOLDS = [None, 2000, 1000, 600, 300]

MEASURES = [
    ("Industry-absorbed count (aggregate)",     COL_CNT_AGG),
    ("CIF (aggregate)",                         COL_CIF_AGG),
    ("Industry-absorbed share (normalized)",    COL_CNT_NORM),
    ("CIF (normalized)",                        COL_CIF_NORM),
]
METRICS = [("JIF", COL_JIF), ("SJR", COL_SJR)]

FLAG_WIDE = 0.15   # CI half-width > this triggers WIDE
FLAG_DIFF = 0.05   # |analytic endpoint - bootstrap endpoint| > this triggers DIFF


def paired(subset, mkey, metric_key):
    xs, ys = [], []
    for r in subset:
        a, b = num(r, mkey), num(r, metric_key)
        if a is not None and b is not None:
            xs.append(a); ys.append(b)
    return np.array(xs), np.array(ys)


def fisher_ci(r, n, spearman=False, alpha=0.05):
    if n < 5 or abs(r) >= 1:
        return (np.nan, np.nan)
    z = np.arctanh(r)
    se = np.sqrt((1 + r**2 / 2.0) / (n - 3)) if spearman else 1.0 / np.sqrt(n - 3)
    zc = norm.ppf(1 - alpha / 2)
    return (np.tanh(z - zc * se), np.tanh(z + zc * se))


def bootstrap_ci(xs, ys, spearman=False, n_boot=N_BOOT, alpha=0.05, rng=None):
    if rng is None:
        rng = np.random.default_rng(SEED)
    n = len(xs)
    idx = np.arange(n)
    stats = np.empty(n_boot)
    for b in range(n_boot):
        s = rng.choice(idx, size=n, replace=True)
        xb, yb = xs[s], ys[s]
        if spearman:
            stats[b] = spearmanr(xb, yb).correlation
        else:
            xm, ym = xb - xb.mean(), yb - yb.mean()
            denom = np.sqrt((xm * xm).sum() * (ym * ym).sum())
            stats[b] = (xm * ym).sum() / denom if denom > 0 else np.nan
    lo, hi = np.nanpercentile(stats, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return (lo, hi)


def restrict_to_elite(venues, k):
    """Top-k venues by JIF (descending)."""
    withjif = [r for r in venues if num(r, COL_JIF) is not None]
    withjif.sort(key=lambda r: num(r, COL_JIF), reverse=True)
    return withjif[:k]


def compute_all():
    rows = load_rows()
    venues = venues_only(rows)
    results = []
    rng = np.random.default_rng(SEED)
    for thresh in THRESHOLDS:
        subset = venues if thresh is None else restrict_to_elite(venues, thresh)
        label = "Full" if thresh is None else f"Top {thresh}"
        for mlabel, mkey in MEASURES:
            for plabel, pkey in METRICS:
                xs, ys = paired(subset, mkey, pkey)
                n = len(xs)
                if n < 5:
                    continue
                pr = pearsonr(xs, ys)[0]
                sr = spearmanr(xs, ys).correlation
                pr_ana = fisher_ci(pr, n, spearman=False)
                sr_ana = fisher_ci(sr, n, spearman=True)
                pr_boot = bootstrap_ci(xs, ys, spearman=False, rng=rng)
                sr_boot = bootstrap_ci(xs, ys, spearman=True, rng=rng)
                results.append({
                    "thresh": label,
                    "measure": mlabel,
                    "metric": plabel,
                    "n": n,
                    "pearson": pr, "pearson_ana": pr_ana, "pearson_boot": pr_boot,
                    "spearman": sr, "spearman_ana": sr_ana, "spearman_boot": sr_boot,
                })
                print(f"  [{label:>7}] {mlabel:<40} {plabel}  "
                      f"P={pr:+.3f} [{pr_ana[0]:+.3f},{pr_ana[1]:+.3f}] "
                      f"boot[{pr_boot[0]:+.3f},{pr_boot[1]:+.3f}]  "
                      f"S={sr:+.3f} [{sr_ana[0]:+.3f},{sr_ana[1]:+.3f}] "
                      f"boot[{sr_boot[0]:+.3f},{sr_boot[1]:+.3f}]  n={n}",
                      flush=True)
    return results


def flags(row):
    """Return list of {WIDE, ZERO, DIFF} flags for a row."""
    out = []
    for kind in ("pearson", "spearman"):
        r = row[kind]
        ana = row[f"{kind}_ana"]
        boot = row[f"{kind}_boot"]
        half = max((ana[1] - ana[0]) / 2, (boot[1] - boot[0]) / 2)
        if half > FLAG_WIDE:
            out.append(f"WIDE-{kind[0].upper()}")
        if ana[0] < 0 < ana[1] or boot[0] < 0 < boot[1]:
            out.append(f"ZERO-{kind[0].upper()}")
        if abs(ana[0] - boot[0]) > FLAG_DIFF or abs(ana[1] - boot[1]) > FLAG_DIFF:
            out.append(f"DIFF-{kind[0].upper()}")
    return out


def emit_latex(results):
    """Longtable with block rows per (measure, metric); columns for each threshold's
    r and CI. To keep the table narrow, we group each threshold as
    "point [lo,hi]" for Pearson, and the same for Spearman on a separate row."""
    out = []
    out.append(r"% Autogenerated by emit_correlations.py")
    out.append(r"\scriptsize")
    out.append(r"\setlength{\tabcolsep}{3pt}" "\n" r"\begin{longtable}{@{}l l l r r r r r r@{}}")
    out.append(
        r"\caption[Correlations between commercialization measures and prestige metrics]{\textbf{Correlations between commercialization measures and prestige metrics.} Pearson and Spearman correlations between each commercialization measure and each prestige metric, "
        r"across the full venue population and four elite-venue thresholds ranked by JIF. "
        r"For every cell, 95\% confidence intervals are given by both the analytic method (Fisher-$z$ for Pearson; "
        r"Bonett--Wright for Spearman) and a percentile bootstrap with $10{,}000$ resamples; "
        r"the bootstrap interval is shown in brackets on a second row for each (measure, metric) pair. "
        r"Sample sizes $n$ vary within a threshold because a venue must have both the measure and the prestige metric present.}"
        r"\label{tab:correlation-battery}\\"
    )
    out.append(r"\toprule")
    out.append(r"Measure & Metric & Stat.\ & Full pop.\ & Top 2000 & Top 1000 & Top 600 & Top 300 \\")
    out.append(r"\midrule\endfirsthead")
    out.append(r"\multicolumn{8}{l}{\emph{Table~\ref{tab:correlation-battery} (continued)}}\\")
    out.append(r"\toprule Measure & Metric & Stat.\ & Full pop.\ & Top 2000 & Top 1000 & Top 600 & Top 300 \\ \midrule\endhead")
    out.append(r"\bottomrule\endfoot")

    # Index results by (measure, metric, thresh)
    idx = {}
    for r in results:
        idx[(r["measure"], r["metric"], r["thresh"])] = r
    threshs = ["Full", "Top 2000", "Top 1000", "Top 600", "Top 300"]

    def fmt(r_val, ci):
        if np.isnan(r_val):
            return "--"
        return f"{r_val:+.3f}"

    def fmt_ci(ci):
        if np.isnan(ci[0]):
            return "--"
        return f"[{ci[0]:+.3f},{ci[1]:+.3f}]"

    for mlabel, _mkey in MEASURES:
        out.append(r"\midrule")
        # measure block header
        out.append(rf"\multicolumn{{8}}{{l}}{{\textbf{{{mlabel}}}}}\\")
        out.append(r"\midrule")
        for plabel, _pkey in METRICS:
            row = [
                idx.get((mlabel, plabel, t)) for t in threshs
            ]
            # metric label + n row (spans measure+metric cols)
            ns = " & ".join(f"{r['n']:,}" if r else "--" for r in row)
            out.append(rf"\multicolumn{{2}}{{l}}{{\textit{{vs.\ {plabel}}}}} & $n$ & {ns} \\")
            # Pearson point + analytic
            pr_point = " & ".join(fmt(r['pearson'], r['pearson_ana']) if r else "--" for r in row)
            out.append(rf" &  & Pearson $r$ & {pr_point} \\")
            pr_ana = " & ".join(fmt_ci(r['pearson_ana']) if r else "--" for r in row)
            out.append(rf" &  & \quad Fisher CI & {pr_ana} \\")
            pr_boot = " & ".join(fmt_ci(r['pearson_boot']) if r else "--" for r in row)
            out.append(rf" &  & \quad boot.\ CI & {pr_boot} \\")
            # Spearman point + analytic + boot
            sr_point = " & ".join(fmt(r['spearman'], r['spearman_ana']) if r else "--" for r in row)
            out.append(rf" &  & Spearman $\rho$ & {sr_point} \\")
            sr_ana = " & ".join(fmt_ci(r['spearman_ana']) if r else "--" for r in row)
            out.append(rf" &  & \quad BW CI & {sr_ana} \\")
            sr_boot = " & ".join(fmt_ci(r['spearman_boot']) if r else "--" for r in row)
            out.append(rf" &  & \quad boot.\ CI & {sr_boot} \\")

    out.append(r"\end{longtable}")
    out.append(r"\normalsize\setlength{\tabcolsep}{6pt}")

    with open(os.path.join(FIGS, "correlation_table.tex"), "w") as f:
        f.write("\n".join(out) + "\n")
    print("wrote figs/correlation_table.tex")


def emit_flags(results):
    lines = []
    lines.append("Flag legend:")
    lines.append("  WIDE-P/S : Pearson or Spearman CI half-width > "
                 f"{FLAG_WIDE}")
    lines.append("  ZERO-P/S : any Pearson or Spearman CI crosses 0")
    lines.append("  DIFF-P/S : analytic and bootstrap CIs disagree by > "
                 f"{FLAG_DIFF} at either endpoint")
    lines.append("")
    any_flag = False
    for r in results:
        fl = flags(r)
        if not fl:
            continue
        any_flag = True
        lines.append(f"  [{r['thresh']:>7}] {r['measure']:<40} {r['metric']}  "
                     f"flags={','.join(fl)}")
    if not any_flag:
        lines.append("  (no flags triggered)")
    with open(os.path.join(FIGS, "correlation_flags.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote figs/correlation_flags.txt")
    print("\n".join(lines))


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(CACHE_PATH):
        print(f"loading cached results from {CACHE_PATH} "
              f"(delete to recompute)")
        with open(CACHE_PATH, "rb") as f:
            results = pickle.load(f)
    else:
        results = compute_all()
        with open(CACHE_PATH, "wb") as f:
            pickle.dump(results, f)
        print(f"cached results to {CACHE_PATH}")
    emit_latex(results)
    emit_flags(results)


if __name__ == "__main__":
    main()
