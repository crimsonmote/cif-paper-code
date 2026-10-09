"""SM figure: industry-absorption rate by within-stratum citation percentile.

Reads regression_output/paper_absorption_rates.csv (main_startup flag only)
and writes figs/paper_absorption_rate.pdf.
"""
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "regression_output", "paper_absorption_rates.csv")
OUT = os.path.join(HERE, "figs", "paper_absorption_rate.pdf")

LABELS = {
    "p0-50": "0–50",
    "p50-75": "50–75",
    "p75-90": "75–90",
    "p90-95": "90–95",
    "p95-99": "95–99",
    "p99-100": "99–100",
}

rows = [r for r in csv.DictReader(open(SRC)) if r["flag"] == "main_startup"]
order = list(LABELS)
rows.sort(key=lambda r: order.index(r["bin"]))
rates = [100 * float(r["rate"]) for r in rows]
ratios = [float(r["ratio_vs_bottom"]) for r in rows]

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 9,
    "axes.labelsize": 10,
})

fig, ax = plt.subplots(figsize=(4.8, 3.0))
x = range(len(rows))
ax.bar(x, rates, color="#0072B2", width=0.62)
for xi, (rate, ratio) in enumerate(zip(rates, ratios)):
    ax.annotate(f"{rate:.3f}%" if rate < 0.01 else f"{rate:.2f}%",
                (xi, rate), xytext=(0, 3), textcoords="offset points",
                ha="center", fontsize=8)
    if xi:
        ax.annotate(f"{ratio:.0f}×", (xi, rate / 2), ha="center",
                    fontsize=8, color="white", fontweight="bold")
ax.set_yscale("log")
ax.set_ylim(0.01, 3.0)
ax.set_xticks(list(x), [LABELS[r["bin"]] for r in rows])
ax.set_xlabel("Citation percentile among field-and-year peers")
ax.set_ylabel("Industry-absorption rate (%)")
ax.spines[["top", "right"]].set_visible(False)
fig.tight_layout()
os.makedirs(os.path.dirname(OUT), exist_ok=True)
fig.savefig(OUT, bbox_inches="tight")
print("wrote", OUT)
