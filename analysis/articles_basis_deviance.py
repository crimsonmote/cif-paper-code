"""Measurement-robustness check: Table 1B Shapley shares under the
articles-only aggregation basis (S4.5.1 extension).

Outcomes: asinh(articles_par_avg_score_all * 1e4) and
asinh(articles_par_total_score * 1e4); volume block = log(total_articles).
Field, median publication year, age span, and asinh(metric) as in the
frozen spec. Point estimates only (no bootstrap); mean criterion only.
"""
import csv
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import panelB_bootstrap as pb
from venue_data import DATA_PATH

ART_COLS = {
    "art_norm": "articles_par_avg_score_all",
    "art_agg": "articles_par_total_score",
}


def main():
    art = {}
    with open(DATA_PATH) as f:
        for r in csv.DictReader(f):
            sid = r.get("source_id")
            ta = pb.fnum(r.get("total_articles"))
            if not sid or ta is None or ta <= 0:
                continue
            art[sid] = (np.log(ta),
                        pb.fnum(r.get(ART_COLS["art_norm"])),
                        pb.fnum(r.get(ART_COLS["art_agg"])))

    base = {}
    with open(pb.TABLE) as f:
        for r in csv.DictReader(f):
            base[r["source_id"]] = r

    print(f"{'outcome':<9} {'metric':<4} {'n':>6}  "
          f"{'field':>6} {'volume':>6} {'medyr':>6} {'age':>6} "
          f"{'metric':>6} {'total':>6}")
    for okey in ("art_norm", "art_agg"):
        for mlabel, mcol in pb.METRICS:
            rows = []
            for sid, r in base.items():
                a = art.get(sid)
                if a is None:
                    continue
                log_ta, y_norm, y_agg = a
                y = y_norm if okey == "art_norm" else y_agg
                m = pb.fnum(r[mcol])
                my = pb.fnum(r["median_year"])
                fy, ly = pb.fnum(r["first_year"]), pb.fnum(r["last_year"])
                fld = r["primary_field"]
                if None in (m, y, my, fy, ly) or fld in ("", "NA"):
                    continue
                rows.append((fld, log_ta, my, ly - fy, m, y))
            Z, cols = pb.design(rows)
            G = Z.T @ Z
            r2s = pb.all_subset_r2(G, cols)
            sh = pb.shapley(r2s)
            tot = r2s[frozenset(pb.BLOCKS)]
            print(f"{okey:<9} {mlabel:<4} {len(rows):>6}  "
                  + " ".join(f"{100*sh[b]:>6.1f}" for b in pb.BLOCKS)
                  + f" {100*tot:>6.1f}")


if __name__ == "__main__":
    main()
