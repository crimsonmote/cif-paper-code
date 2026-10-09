"""Panel B h-index rows, mean criterion: the five-factor Shapley
decomposition with the OpenAlex journal h-index (`oa_h`) as the metric,
with venue-resample bootstrap CIs at Panel B standards (B = 1,000,
multinomial weights, seed 20260802).

Samples: JIF-restricted (n = 21,637), SJR-restricted (n = 28,841), and
the full venue universe (n = 194,629). Outcomes cif_norm and cif_agg.
Writes regression_output/panelB_hindex_share_cis.{md,csv}.
"""
import csv
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import panelB_bootstrap as pb

OUT_MD = os.path.join(_HERE, "regression_output", "panelB_hindex_share_cis.md")
OUT_CSV = os.path.join(_HERE, "regression_output", "panelB_hindex_share_cis.csv")
SAMPLES = [("JIF-sample", "jif"), ("SJR-sample", "sjr"), ("full", None)]


def load_h(outcome, cond_col):
    rows = []
    with open(pb.TABLE) as f:
        for r in csv.DictReader(f):
            if cond_col is not None and pb.fnum(r[cond_col]) is None:
                continue
            m = pb.fnum(r["oa_h"])
            y = pb.fnum(r[outcome])
            ls = pb.fnum(r["log_size"])
            my = pb.fnum(r["median_year"])
            fy, ly = pb.fnum(r["first_year"]), pb.fnum(r["last_year"])
            fld = r["primary_field"]
            if None in (m, y, ls, my, fy, ly) or fld in ("", "NA"):
                continue
            rows.append((fld, ls, my, ly - fy, m, y))
    return rows


def main():
    rng = np.random.default_rng(pb.SEED)
    md = ["# Panel B h-index rows (mean criterion) — bootstrap 95% CIs",
          "",
          f"Metric: OpenAlex journal h (`oa_h`). B = {pb.B}, multinomial "
          f"weights, seed {pb.SEED}. Outcome asinh(outcome x {pb.SCALE:.0f});"
          " blocks field, log_size, median_year, age_span, asinh(h).", ""]
    csv_rows = [("outcome", "sample", "n", "block", "share_pct",
                 "lo95_pct", "hi95_pct")]
    for outcome in pb.OUTCOMES:
        for slabel, cond in SAMPLES:
            rows = load_h(outcome, cond)
            Z, cols = pb.design(rows)
            n = Z.shape[0]
            G0 = Z.T @ Z
            r2s0 = pb.all_subset_r2(G0, cols)
            point = pb.shapley(r2s0)
            total = r2s0[frozenset(pb.BLOCKS)]
            boots = {b: np.empty(pb.B) for b in pb.BLOCKS}
            boots["total"] = np.empty(pb.B)
            for it in range(pb.B):
                w = rng.multinomial(n, np.full(n, 1.0 / n)).astype(float)
                Gw = (Z * w[:, None]).T @ Z
                r2s = pb.all_subset_r2(Gw, cols)
                ph = pb.shapley(r2s)
                for b in pb.BLOCKS:
                    boots[b][it] = ph[b]
                boots["total"][it] = r2s[frozenset(pb.BLOCKS)]
            md += [f"## {outcome} — h-index, {slabel} (n = {n:,})", "",
                   "| block | share % | 95% CI |", "|---|--:|---|"]
            for b in pb.BLOCKS + ["total"]:
                pt = (total if b == "total" else point[b]) * 100
                lo, hi = np.percentile(boots[b], [2.5, 97.5]) * 100
                md.append(f"| {b} | {pt:.1f} | [{lo:.1f}, {hi:.1f}] |")
                csv_rows.append((outcome, slabel, n, b, f"{pt:.3f}",
                                 f"{lo:.3f}", f"{hi:.3f}"))
            md.append("")
            print(f"{outcome} {slabel}: "
                  + ", ".join(f"{b}={point[b]*100:.1f}" for b in pb.BLOCKS)
                  + f", total={total*100:.1f}", flush=True)
    open(OUT_MD, "w").write("\n".join(md) + "\n")
    with open(OUT_CSV, "w", newline="") as f:
        csv.writer(f).writerows(csv_rows)
    print("wrote", OUT_MD)


if __name__ == "__main__":
    main()
