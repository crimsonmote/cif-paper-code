"""Measurement-robustness check: Table 1B Shapley shares with the
normalized-ranking publication-volume floor applied as a sample
restriction (S4.5.2 extension).

The deviance itself imposes no floor (volume is a covariate); this asks
whether the decomposition depends on the small venues the ranking floor
excludes. all_ basis outcomes; floors 1,000 and 500 articles; mean
criterion, point estimates only.
"""
import csv
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import panelB_bootstrap as pb
from venue_data import DATA_PATH


def main():
    ta = {}
    with open(DATA_PATH) as f:
        for r in csv.DictReader(f):
            v = pb.fnum(r.get("total_papers"))
            if r.get("source_id") and v is not None:
                ta[r["source_id"]] = v

    for floor in (1000, 500):
        print(f"--- all_ basis, venues with >= {floor} works ---")
        print(f"{'outcome':<9} {'metric':<4} {'n':>6}  "
              f"field volume  medyr    age metric  total")
        for okey in ("cif_norm", "cif_agg"):
            for mlabel, mcol in pb.METRICS:
                rows = []
                with open(pb.TABLE) as f:
                    for r in csv.DictReader(f):
                        if ta.get(r["source_id"], 0) < floor:
                            continue
                        m = pb.fnum(r[mcol])
                        y = pb.fnum(r[okey])
                        ls = pb.fnum(r["log_size"])
                        my = pb.fnum(r["median_year"])
                        fy = pb.fnum(r["first_year"])
                        ly = pb.fnum(r["last_year"])
                        fld = r["primary_field"]
                        if None in (m, y, ls, my, fy, ly) or fld in ("", "NA"):
                            continue
                        rows.append((fld, ls, my, ly - fy, m, y))
                Z, cols = pb.design(rows)
                r2s = pb.all_subset_r2(Z.T @ Z, cols)
                sh = pb.shapley(r2s)
                tot = r2s[frozenset(pb.BLOCKS)]
                print(f"{okey:<9} {mlabel:<4} {len(rows):>6}  "
                      + " ".join(f"{100*sh[b]:>6.1f}" for b in pb.BLOCKS)
                      + f" {100*tot:>6.1f}")


if __name__ == "__main__":
    main()
