"""Minimum-citation-hop distribution figure (SM fig. S5).

Reads the hop histogram written by ref_map/reachability/hop_histogram.py:
works counted by their minimum citation distance from the seed set, plus the
total number of works in the network.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")

import json

HIST_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ref_map",
                         "reachability", "output", "hop_histogram.json")
LAST_BAR = 15   # hops >= LAST_BAR share one bar, labelled "15+"


def load_histogram(path=HIST_PATH):
    h = json.load(open(path))
    hist = {int(k): v for k, v in h["histogram"].items()}
    hops = {k: hist.get(k, 0) for k in range(LAST_BAR)}
    tail = sum(v for k, v in hist.items() if k >= LAST_BAR)
    return hops, tail, h["total_works"], h["reachable_works"]


HOPS, HOP_15_PLUS, TOTAL, REACHABLE = load_histogram()
UNREACHABLE = TOTAL - REACHABLE

DARK_BLUE, LIGHT_BLUE, GRAY = "#1F5C8B", "#6699BF", "#CBDDEC"  # ring remainder: tint, not grey

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 12,
    "figure.dpi": 300,
})

fig, ax = plt.subplots(figsize=(9, 5))

xs = list(HOPS.keys()) + [15]
labels = [str(h) for h in HOPS.keys()] + ["15+"]
counts = [HOPS[h] / 1e6 for h in HOPS.keys()] + [HOP_15_PLUS / 1e6]

peak_idx = counts.index(max(counts))
colors = [DARK_BLUE if abs(i - peak_idx) <= 1 else LIGHT_BLUE for i in range(len(counts))]

bars = ax.bar(xs, counts, color=colors, edgecolor="none", width=0.85)
bars[-1].set_hatch("//")
bars[-1].set_edgecolor(LIGHT_BLUE)

peak_h, peak_v = xs[peak_idx], counts[peak_idx]
ax.annotate(
    f"Peak: {peak_h} hops ({peak_v:.1f}M)",
    xy=(peak_h, peak_v),
    xytext=(peak_h + 3.5, peak_v + 0.4),
    fontsize=11,
    color="#333333",
    arrowprops=dict(arrowstyle="->", color="#333333", lw=0.9, shrinkA=2, shrinkB=3),
)

ax.set_xticks(xs)
ax.set_xticklabels(labels)
ax.set_xlabel("Minimum citation hops from industry-absorbed papers")
ax.set_ylabel("Number of papers (millions)")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.set_ylim(0, max(counts) * 1.12)

inset = fig.add_axes([0.58, 0.55, 0.28, 0.32])
sizes = [REACHABLE, UNREACHABLE]
inset.pie(
    sizes,
    colors=[DARK_BLUE, GRAY],
    startangle=90,
    counterclock=False,
    wedgeprops={"width": 0.36, "edgecolor": "white", "linewidth": 1.5},
)
inset.text(0, 0.10, f"{100 * REACHABLE / TOTAL:.1f}%",
           ha="center", va="center", fontsize=15, fontweight="bold", color=DARK_BLUE)
inset.text(0, -0.10, "reachable", ha="center", va="center", fontsize=10, color="#555555")
inset.text(0, -0.85, f"{REACHABLE/1e6:.1f}M of {TOTAL/1e6:.1f}M works",
           ha="center", va="center", fontsize=9, color="#666666")
inset.set_aspect("equal")
inset.set_axis_off()

fig.tight_layout()
os.makedirs(OUTDIR, exist_ok=True)
out_pdf = os.path.join(OUTDIR, "min_hop_distribution.pdf")
fig.savefig(out_pdf, bbox_inches="tight")
print(f"Wrote {out_pdf}")
