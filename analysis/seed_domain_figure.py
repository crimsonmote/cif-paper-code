"""Horizontal bar chart of the seed set's top OpenAlex primary-topic fields (SM fig. S1).

Reads figs/seed_field_composition.csv, written by seed_field_composition.py,
which counts seed papers by OpenAlex primary field. Papers without a primary
topic (an empty field row) are left out, so the bars and "Other fields" sum to
the seed papers that carry a field.
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Top-10 fields + aggregated "Other" bar. Total seed with primary_topic = 190,173.
import csv

TOP_N = 10
COMPOSITION_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "figs", "seed_field_composition.csv")


def load_fields(path=COMPOSITION_CSV, top_n=TOP_N):
    """Top fields by seed count plus an 'Other fields' remainder."""
    rows = [r for r in csv.DictReader(open(path)) if r.get("field_name")]
    rows.sort(key=lambda r: -int(r["n"]))
    shown = [(r["field_name"].replace(" and ", " & "), int(r["n"])) for r in rows[:top_n]]
    total = sum(int(r["n"]) for r in rows)
    return shown + [("Other fields", total - sum(n for _, n in shown))], total


FIELDS, TOTAL_WITH_TOPIC = load_fields()

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")
BAR = "#1F5C8B"

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
    "font.size": 11,
    "figure.dpi": 300,
})

fig, ax = plt.subplots(figsize=(8, 5))
labels = [name for name, _ in FIELDS][::-1]
counts_k = [v / 1000 for _, v in FIELDS][::-1]
bars = ax.barh(labels, counts_k, color=BAR, edgecolor="none", height=0.7)

# Value annotations at end of each bar
for bar, v in zip(bars, counts_k):
    ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height() / 2,
            f"{v:.1f}k", va="center", ha="left", fontsize=10, color="#333333")

ax.set_xlabel("Number of seed papers (thousands)")
ax.spines["top"].set_visible(False)
ax.spines["right"].set_visible(False)
ax.set_xlim(0, max(counts_k) * 1.15)
ax.tick_params(axis="y", length=0)

fig.tight_layout()
os.makedirs(OUTDIR, exist_ok=True)
out_pdf = os.path.join(OUTDIR, "seed_domain_distribution.pdf")
fig.savefig(out_pdf, bbox_inches="tight")
print(f"Wrote {out_pdf}")
