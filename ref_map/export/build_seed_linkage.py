"""Build the paper-patent-assignee linkage that defines the seed set.

Joins the Founding Patents assignee-at-filing file with the Reliance on Science
Patent-Paper Pairs on the US patent number. Each output row is one
(paper, patent, assignee) triplet: the Founding Patents columns, then the
Patent-Paper Pairs columns, then `us_patent_id` (the pair's patent number without
its "US-" prefix). `founding_patent_seeds.py` reads this file to flag seed papers.

Inputs (public releases):
  Founding Patents, release of 10 July 2024 (doi:10.5281/zenodo.12706672):
      ocpb_assigneeatfiling.csv
  Reliance on Science, release of 3 June 2024 (doi:10.5281/zenodo.11461587):
      _patent_paper_pairs.csv

Usage:
    python -m ref_map.export.build_seed_linkage \
        --assignees ocpb_assigneeatfiling.csv --pairs _patent_paper_pairs.csv \
        --out _patent_paper_startup_triplets.csv
"""
from __future__ import annotations

import argparse
import csv
from collections import defaultdict

PAIR_PREFIX = "US-"


def build(assignees_csv: str, pairs_csv: str, out_csv: str) -> int:
    pairs_by_patent: dict[str, list[list[str]]] = defaultdict(list)
    with open(pairs_csv, newline="") as f:
        reader = csv.reader(f)
        pair_cols = next(reader)
        patent_idx = pair_cols.index("patent")
        for row in reader:
            pairs_by_patent[row[patent_idx].removeprefix(PAIR_PREFIX)].append(row)

    n = 0
    with open(assignees_csv, newline="") as f, open(out_csv, "w", newline="") as g:
        reader = csv.reader(f)
        assignee_cols = next(reader)
        patent_id_idx = assignee_cols.index("patent_id")
        writer = csv.writer(g)
        writer.writerow(assignee_cols + pair_cols + ["us_patent_id"])
        for row in reader:
            pid = row[patent_id_idx]
            for pair in pairs_by_patent.get(pid, ()):
                writer.writerow(row + pair + [pid])
                n += 1
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Join Founding Patents with Patent-Paper Pairs.")
    ap.add_argument("--assignees", required=True, help="ocpb_assigneeatfiling.csv")
    ap.add_argument("--pairs", required=True, help="_patent_paper_pairs.csv")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    print(f"wrote {build(a.assignees, a.pairs, a.out):,} triplets to {a.out}")


if __name__ == "__main__":
    main()
