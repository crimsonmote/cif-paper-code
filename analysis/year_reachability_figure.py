"""Reachability rate by publication year, 1960-2015 (SM fig. S6).

The series is the share of papers published in each year that lie in the
citation lineage of at least one industry-absorbed paper, read from the
pipeline's per-year reachability aggregate (all document types).

The plot stops at END so that every cohort has at least five years to accrue
a paired US patent within the Founding Patents aperture (grants through 2021).
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs",
                   "year_reachability.pdf")
START, END = 1960, 2015

REACH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ref_map",
                          "aggregation", "output",
                          "year_aggregate_startup_reachability_full.json")
LINE, FILL = "#2E5E8C", "#3D6E9E"


def load_rate():
    """Percent of each publication-year cohort reachable from the seed set."""
    rate = {}
    for r in json.load(open(REACH_PATH)):
        y, n = r.get("publication_year"), r.get("total_papers") or 0
        if y is not None and n:
            rate[int(y)] = 100.0 * (r.get("reachable_papers") or 0) / n
    return rate

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 12,
    "figure.dpi": 300,
})


def main():
    RATE = load_rate()
    years = [y for y in sorted(RATE) if START <= y <= END]
    vals = [RATE[y] for y in years]

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.fill_between(years, 0, vals, color=FILL, alpha=0.18, linewidth=0)
    ax.plot(years, vals, color=LINE, linewidth=1.8)

    peak = max(years, key=lambda y: RATE[y])
    ax.annotate(f"Peak ({peak})",
                xy=(peak, RATE[peak]), xytext=(peak - 17, RATE[peak] + 3.2),
                arrowprops=dict(arrowstyle="-", color="0.3", linewidth=0.8),
                fontsize=11, color="0.2")

    ax.set_xlabel("Publication year")
    ax.set_ylabel("Reachability rate (%)")
    ax.set_xlim(START, END)
    ax.set_ylim(20, 50)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    fig.tight_layout()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    fig.savefig(OUT, bbox_inches="tight")
    print(f"wrote {os.path.normpath(OUT)} ({years[0]}-{years[-1]}; "
          f"{vals[0]:.1f}% to {vals[-1]:.1f}%, peak {RATE[peak]:.1f}% "
          f"in {peak})")


if __name__ == "__main__":
    main()
