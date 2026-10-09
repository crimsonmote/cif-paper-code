"""
correlations.py
---------------
Correlations between commercialization measures (CIF and direct startup-enabling
output) and volume-normalized prestige metrics (JIF, SJR), with 95% confidence
intervals by three methods:

  - Pearson  : Fisher z-transformation           SE = 1/sqrt(n-3)
  - Spearman : Fisher z + Bonett-Wright SE        SE = sqrt((1 + rho^2/2)/(n-3))
  - Bootstrap: percentile method (assumption-free cross-check), 10,000 resamples

Two samples are reported:
  - Full population (all journals + conferences with the given metric)
  - Top-N by JIF (elite venues; default N=1000)

Run:  python analysis/correlations.py
Requires: numpy, scipy, and common.py + the CSV in the same directory.
"""
import numpy as np
from scipy.stats import pearsonr, spearmanr, norm
from venue_data import (load_rows, num, venues_only,
                    COL_CIF_AGG, COL_CIF_NORM, COL_CNT_AGG, COL_CNT_NORM,
                    COL_JIF, COL_SJR)

TOP_N = 1000
N_BOOT = 10000
SEED = 2024

MEASURES = [
    ("Startup-enabling count (aggregate)",  COL_CNT_AGG),
    ("CIF (aggregate)",                     COL_CIF_AGG),
    ("Startup-enabling share (normalized)", COL_CNT_NORM),
    ("CIF (normalized)",                    COL_CIF_NORM),
]
METRICS = [("JIF", COL_JIF), ("SJR", COL_SJR)]


def paired(subset, mkey, metric_key):
    """Return arrays (x, y) for rows where both columns are present."""
    xs, ys = [], []
    for r in subset:
        a, b = num(r, mkey), num(r, metric_key)
        if a is not None and b is not None:
            xs.append(a); ys.append(b)
    return np.array(xs), np.array(ys)


def fisher_ci(r, n, spearman=False, alpha=0.05):
    """95% CI for a correlation via Fisher z. Bonett-Wright SE if spearman."""
    if n < 5 or abs(r) >= 1:
        return (np.nan, np.nan)
    z = np.arctanh(r)
    se = np.sqrt((1 + r**2 / 2.0) / (n - 3)) if spearman else 1.0 / np.sqrt(n - 3)
    zc = norm.ppf(1 - alpha / 2)
    return (np.tanh(z - zc * se), np.tanh(z + zc * se))


def bootstrap_ci(xs, ys, spearman=False, n_boot=N_BOOT, alpha=0.05, rng=None):
    """Percentile bootstrap CI (assumption-free)."""
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


def correlate(subset, mkey, metric_key, do_bootstrap=False, rng=None):
    xs, ys = paired(subset, mkey, metric_key)
    n = len(xs)
    out = {"n": n}
    pr = pearsonr(xs, ys)[0]
    sr = spearmanr(xs, ys).correlation
    out["pearson"]  = (pr, fisher_ci(pr, n, spearman=False))
    out["spearman"] = (sr, fisher_ci(sr, n, spearman=True))
    if do_bootstrap:
        out["pearson_boot"]  = bootstrap_ci(xs, ys, spearman=False, rng=rng)
        out["spearman_boot"] = bootstrap_ci(xs, ys, spearman=True,  rng=rng)
    return out


def print_block(subset, title, do_bootstrap=False):
    rng = np.random.default_rng(SEED)
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)
    for mlabel, mkey in MEASURES:
        for plabel, pkey in METRICS:
            c = correlate(subset, mkey, pkey, do_bootstrap=do_bootstrap, rng=rng)
            pr, (plo, phi) = c["pearson"]
            sr, (slo, shi) = c["spearman"]
            line = (f"  {mlabel:<36} {plabel}  "
                    f"P={pr:6.3f} [{plo:6.3f},{phi:6.3f}]   "
                    f"S={sr:6.3f} [{slo:6.3f},{shi:6.3f}]   n={c['n']}")
            print(line)
            if do_bootstrap:
                pblo, pbhi = c["pearson_boot"]; sblo, sbhi = c["spearman_boot"]
                print(f"      {'':<36} {plabel}  "
                      f"P_boot=[{pblo:6.3f},{pbhi:6.3f}]   "
                      f"S_boot=[{sblo:6.3f},{sbhi:6.3f}]")


def main():
    rows = load_rows()
    venues = venues_only(rows)
    elite = sorted((r for r in venues if num(r, COL_JIF) is not None),
                   key=lambda r: num(r, COL_JIF), reverse=True)[:TOP_N]

    # Full population: analytic CIs only (n is large; bootstrap agrees trivially)
    print_block(venues, "FULL POPULATION (journals + conferences)", do_bootstrap=False)
    # Elite panel: include bootstrap cross-check (smaller n; where CIs do work)
    print_block(elite, f"TOP {TOP_N} BY JIF (with bootstrap cross-check)", do_bootstrap=True)


if __name__ == "__main__":
    main()
