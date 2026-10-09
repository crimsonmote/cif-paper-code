"""Panel A h-index column: Pearson r and Spearman rho of the four
commercialization measures against the OpenAlex journal h-index
(`source_h_index` in the venue table), with 95% percentile-bootstrap CIs
at Panel A standards (10,000 paired-row resamples, seed 2024).

Sample per cell: journals + conferences with both the measure and the
h-index present (no >0 filter), matching correlations.py conventions.
Writes regression_output/panelA_hindex_cis.md.
"""
import os
import sys

import numpy as np
from scipy.stats import pearsonr, spearmanr, rankdata

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from venue_data import (load_rows, num, venues_only,
                    COL_CIF_AGG, COL_CIF_NORM, COL_CNT_AGG, COL_CNT_NORM)

OUT = os.path.join(_HERE, "regression_output", "panelA_hindex_cis.md")
H_COL = "source_h_index"
N_BOOT = 10_000
SEED = 2024

MEASURES = [
    ("Ind.-absorbed count (agg.)", COL_CNT_AGG),
    ("CIF (aggregate)", COL_CIF_AGG),
    ("Ind.-absorbed share (norm.)", COL_CNT_NORM),
    ("CIF (normalized)", COL_CIF_NORM),
]


def main():
    rows = venues_only(load_rows())
    rng = np.random.default_rng(SEED)
    L = ["# Panel A h-index column — r / rho with 95% percentile bootstrap CIs",
         "",
         f"Metric: OpenAlex journal h-index (`{H_COL}`). B = {N_BOOT:,}, "
         "paired-row resamples, seed 2024 (Panel A standards).", "",
         "| measure | n | r | r 95% CI | rho | rho 95% CI |",
         "|---|--:|--:|---|--:|---|"]
    for label, mkey in MEASURES:
        xs, ys = [], []
        for r in rows:
            a, b = num(r, mkey), num(r, H_COL)
            if a is not None and b is not None:
                xs.append(a)
                ys.append(b)
        x, y = np.array(xs), np.array(ys)
        n = len(x)
        r_pt = pearsonr(x, y)[0]
        s_pt = spearmanr(x, y)[0]
        rb = np.empty(N_BOOT)
        sb = np.empty(N_BOOT)
        for i in range(N_BOOT):
            idx = rng.integers(0, n, n)
            xi, yi = x[idx], y[idx]
            xc = xi - xi.mean()
            yc = yi - yi.mean()
            rb[i] = (xc @ yc) / np.sqrt((xc @ xc) * (yc @ yc))
            rx = rankdata(xi)
            ry = rankdata(yi)
            rxc = rx - rx.mean()
            ryc = ry - ry.mean()
            sb[i] = (rxc @ ryc) / np.sqrt((rxc @ rxc) * (ryc @ ryc))
        rlo, rhi = np.percentile(rb, [2.5, 97.5])
        slo, shi = np.percentile(sb, [2.5, 97.5])
        L.append(f"| {label} | {n:,} | {r_pt:+.3f} | [{rlo:+.2f},{rhi:+.2f}] "
                 f"| {s_pt:+.3f} | [{slo:+.2f},{shi:+.2f}] |")
        print(f"{label}: n={n:,} r={r_pt:+.3f} [{rlo:+.2f},{rhi:+.2f}] "
              f"rho={s_pt:+.3f} [{slo:+.2f},{shi:+.2f}]", flush=True)
    open(OUT, "w").write("\n".join(L) + "\n")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
