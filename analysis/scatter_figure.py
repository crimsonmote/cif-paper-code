"""
scatter_figure.py
-----------------
Four-panel scatter: each commercialization measure vs JIF, across all
journals + conferences. Panel D (CIF normalized) is annotated with the two
overperformer lenses. Both axes use an inverse-hyperbolic-sine (asinh) scale.
A standard-deviation (SD) line marks perfect correlation (r=1) between the two
standardized measures, computed in log space.

Outputs (to ./figs/): four individual PDFs + a combined 2x2 PDF.

Run:  python analysis/scatter_figure.py
Requires: numpy, matplotlib, common.py + the CSV.
"""
import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
import matplotlib.ticker as mticker
from venue_data import (load_rows, num, venues_only,
                    COL_CIF_AGG, COL_CIF_NORM, COL_CNT_AGG, COL_CNT_NORM,
                    COL_JIF, COL_NAME)

OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "figs")
# Okabe-Ito palette: safe under all common color-vision deficiencies.
# BLUE = "#0072B2" (Okabe-Ito blue), RED = "#D55E00" (Okabe-Ito vermillion).
# CLOUD is a light, editorial cool gray for the scatter background.
# SD_LINE is near-black so the reference is clearly separable from the cloud.
BLUE, RED, CLOUD = "#0072B2", "#D55E00", "#BAC1CA"
SD_LINE = "#2A2A2A"
SEED = 42

# Panel definitions: key -> (column, y-scale, y-label, jitter?, annotate?)
PANELS = {
    "A": (COL_CNT_AGG,  1.0,  "Industry-absorbed paper count",             True,  False),
    "B": (COL_CNT_NORM, 100.0,"Industry-absorbed share (%)",               False, False),
    "C": (COL_CIF_AGG,  1.0,  "CIF (aggregate)",                           False, False),
    "D": (COL_CIF_NORM, 1e4,  r"CIF (normalized, $10^{-4}$)",       False, True),
}
FILENAMES = {  # match \includegraphics paths in the manuscript
    "A": "all_startup_paper_count_vs_jif",
    "B": "all_startup_share_vs_jif",
    "C": "all_par_total_score_vs_jif",
    "D": "all_par_avg_score_vs_jif",
}

# Overperformer venues to highlight in panel D
MAG = ["Journal of Medicinal Chemistry", "ACM Transactions on Graphics", "Cell",
       "Bioorganic & Medicinal Chemistry Letters", "Magnetic Resonance in Medicine",
       "ACM Transactions on Database Systems"]
RANK = ["Chemical engineering progress", "Materials Evaluation",
        "Journal of Imaging Science and Technology", "Journal of Medical Devices"]
# Manual label positions (data coords) tuned for the all_* CIF scale.
# Format: name -> (label_x, label_y, h-align, color, short-label)
LABELS = {
    # Magnitude overperformers: one label column right of the SD line, ordered
    # by marker height so the leader lines do not cross labels.
    "ACM Transactions on Graphics":             (60, 240, "left", BLUE, "ACM Trans. Graphics"),
    "Journal of Medicinal Chemistry":           (60, 170, "left", BLUE, "J. Med. Chem."),
    "Cell":                                     (60, 120, "left", BLUE, "Cell"),
    "Bioorganic & Medicinal Chemistry Letters": (60,  85, "left", BLUE, "Bioorg. Med. Chem. Lett."),
    "Magnetic Resonance in Medicine":           (60,  60, "left", BLUE, "Magn. Reson. Med."),
    "ACM Transactions on Database Systems":     (60,  42, "left", BLUE, "ACM Trans. Database Syst."),
    # Rank-gap overperformers: labels to the right of their markers.
    "Journal of Medical Devices":               (1.9,  46, "left", RED,  "J. Medical Devices"),
    "Journal of Imaging Science and Technology":(2.0,  19, "left", RED,  "J. Imaging Sci. Technol."),
    "Materials Evaluation":                     (1.7,10.5, "left", RED,  "Materials Evaluation"),
    "Chemical engineering progress":            (1.1, 5.5, "left", RED,  "Chem. Eng. Progress"),
}

rng = np.random.default_rng(SEED)


def series(venues, key, scale):
    """(name, jif, measure*scale) for rows with JIF and a positive measure."""
    out = []
    for r in venues:
        jif, m = num(r, COL_JIF), num(r, key)
        if jif is None or m is None or m <= 0:
            continue
        out.append({"name": r[COL_NAME], "jif": jif, "m": m * scale})
    return out


def sd_line(ax, jif, m):
    """SD (r=1) reference line through the centroid in log space, clipped to data."""
    mask = (jif > 0) & (m > 0)
    lx, ly = np.log10(jif[mask]), np.log10(m[mask])
    cx, cy, sx, sy = lx.mean(), ly.mean(), lx.std(), ly.std()
    slope = sy / sx
    xs = np.linspace(jif[mask].min(), jif[mask].max(), 400)
    ys = 10 ** (cy + slope * (np.log10(xs) - cx))
    keep = ys <= m.max() * 1.05
    ax.plot(xs[keep], ys[keep], "--", color=SD_LINE, lw=0.9, alpha=0.9, zorder=3)


def set_xticks(ax):
    ax.set_xticks([0, 1, 10, 100])
    def fmt(v, _):
        if abs(v) < 1e-9: return "0"
        for e in (0, 1, 2):
            if abs(v - 10**e) < 1e-9: return rf"$10^{e}$"
        return f"{v:g}"
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt))


def suppress_zero_ytick(ax):
    """Prevent the asinh '0' y-tick label from stacking under '10^-1' near
    the axis origin. Keeps other tick labels intact."""
    def fmt(v, _):
        if abs(v) < 1e-9:
            return ""
        for e in (-1, 0, 1, 2, 3, 4, 5):
            if abs(v - 10**e) < 1e-9:
                return rf"$10^{{{e}}}$"
        return f"{v:g}"
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt))


def draw_panel(ax, venues, key, scale, ylab, jitter, annotate, big=False):
    data = series(venues, key, scale)
    jif = np.array([d["jif"] for d in data])
    m = np.array([d["m"] for d in data], dtype=float)
    mplot = m * rng.uniform(0.85, 1.15, size=len(m)) if jitter else m
    ax.scatter(jif, mplot, s=3.5, c=CLOUD, alpha=0.22, edgecolors="none", zorder=1)
    ax.set_xscale("asinh"); ax.set_yscale("asinh"); ax.set_ylim(bottom=0)
    sd_line(ax, jif, m)
    set_xticks(ax)
    suppress_zero_ytick(ax)
    ax.minorticks_off()  # Science: no minor tick marks
    ax.set_xlabel("Journal Impact Factor (2025)"); ax.set_ylabel(ylab)
    ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
    if annotate:
        look = {d["name"]: d for d in data}
        ax.set_xlim(left=0.045)
        for nm in MAG:
            d = look.get(nm)
            if d: ax.scatter([d["jif"]], [d["m"]], s=36 if big else 30, c=BLUE,
                             edgecolors="white", linewidths=0.6, zorder=5)
        for nm in RANK:
            d = look.get(nm)
            if d: ax.scatter([d["jif"]], [d["m"]], s=36 if big else 30, c=RED,
                             edgecolors="white", linewidths=0.6, zorder=5)
        fs = 8 if big else 7
        for nm, (lx, ly, ha, col, short) in LABELS.items():
            d = look.get(nm)
            if not d: continue
            ax.annotate(short, xy=(d["jif"], d["m"]), xytext=(lx, ly), fontsize=fs,
                        color="#222222", ha=ha,  # Science: avoid coloured type va="center", zorder=6,
                        arrowprops=dict(arrowstyle="-", color=col, lw=0.6,
                                        alpha=0.75, shrinkA=2, shrinkB=3))
        leg = [Line2D([0],[0], marker="o", color="w", markerfacecolor=BLUE,
                      markersize=7 if big else 6, label="Magnitude overperformers"),
               Line2D([0],[0], marker="o", color="w", markerfacecolor=RED,
                      markersize=7 if big else 6, label="Rank-gap overperformers"),
               Line2D([0],[0], linestyle="--", color="#444444", lw=1.0,
                      label="Major-axis (SD) reference line")]
        ax.legend(handles=leg, loc="lower right", fontsize=7 if big else 6.5, frameon=False)


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    rows = load_rows(); venues = venues_only(rows)

    # Science figure convention: sans-serif in figures even when body is serif.
    # Hairline axes and outward-only ticks match Science's published-figure look.
    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": ["Helvetica", "Arial", "DejaVu Sans"],
        "pdf.fonttype": 42,           # TrueType embedding (avoids Type 3 warnings)
        "ps.fonttype": 42,
        "axes.linewidth": 0.6,
        "xtick.major.size": 3,
        "ytick.major.size": 3,
        "xtick.major.width": 0.6,
        "ytick.major.width": 0.6,
        "xtick.direction": "out",
        "ytick.direction": "out",
        "figure.dpi": 300,
    })

    # Combined 2x2 layout, sized for Science double-column (~175 mm ≈ 6.9 in).
    plt.rcParams.update({"font.size": 10, "axes.labelsize": 10,
                         "xtick.labelsize": 8, "ytick.labelsize": 8})
    fig, axes = plt.subplots(2, 2, figsize=(6.9, 5.65))
    amap = {"A": axes[0, 0], "B": axes[0, 1], "C": axes[1, 0], "D": axes[1, 1]}
    for k, (col, sc, yl, jit, an) in PANELS.items():
        draw_panel(amap[k], venues, col, sc, yl, jit, an, big=False)
        amap[k].text(-0.15, 1.03, k, transform=amap[k].transAxes,
                     fontsize=10, fontweight="bold")
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "four_panel_scatter.pdf"), bbox_inches="tight")
    plt.close(fig)

    # Individual panels, sized for the 2x2 minipage layout in main_pa.tex
    # (each rendered at ~2.69 in wide = 0.95 * 0.45 * textwidth).
    plt.rcParams.update({"font.size": 10, "axes.labelsize": 10,
                         "xtick.labelsize": 8, "ytick.labelsize": 8})
    for k, (col, sc, yl, jit, an) in PANELS.items():
        fig, ax = plt.subplots(figsize=(3.5, 3.0) if an else (3.2, 2.7))
        draw_panel(ax, venues, col, sc, yl, jit, an, big=True)
        fig.tight_layout()
        fig.savefig(os.path.join(OUTDIR, FILENAMES[k] + ".pdf"), bbox_inches="tight")
        plt.close(fig)

    print(f"Wrote figures to ./{OUTDIR}/")


if __name__ == "__main__":
    main()
