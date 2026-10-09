"""Assignee-class seed definitions (SM S5, Tables S25, S26 and S29).

Compares CIF under four seed definitions (all assignees, corporate, young firm,
young firm + university), venue by venue. Summed CIF per definition comes from
Data S1 (cif_seed_definitions; the all-assignee definition is cif_all_documents).

Statistics (venues = journals + conferences; a venue a definition does not reach
is scored zero):
  1. Pairwise correlations for every pair of definitions: Pearson r, Pearson on
     asinh scores and Spearman rho, for aggregate and normalized CIF, over all
     venues, over venues nonzero in >= 1 of the pair, and over venues nonzero in
     both; top-20/top-100 aggregate overlaps.
  2. Prestige association per definition: aggregate and normalized CIF against
     JIF and SJR, over all metric-carrying venues and over those also nonzero.
  3. Coverage: venues with a zero score under each definition.

Writes analysis/results/SEED_UNIVERSE_RESULTS.md. Seed sizes (papers): all
assignees 190,240; corporate 99,903; young firm 6,152; young firm + university
93,395.
"""
import csv
import json
import os
import sys

import numpy as np
from scipy.stats import pearsonr, spearmanr

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from venue_data import DATA_PATH, RESULTS_DIR, load_data_s1, num as cnum

OUT = os.path.join(RESULTS_DIR, "SEED_UNIVERSE_RESULTS.md")

UNIVERSES = [
    ("all_assignees", "All assignees", 190240),
    ("corporate", "Corporate", 99903),
    ("young_firm", "Young firm", 6152),
    ("young_firm_university", "Young firm + university", 93395),
]


_BASE = None
_DEFS = None


def load_variant(key):
    """(summed CIF, normalized CIF) per journal/conference under one definition."""
    global _BASE, _DEFS
    if _BASE is None:
        _BASE = {r["source_id"]: r for r in load_data_s1("cif_all_documents")}
        _DEFS = {r["source_id"]: r for r in load_data_s1("cif_seed_definitions")}
    out = {}
    for sid, b in _BASE.items():
        if b.get("source_type") not in ("j", "c"):
            continue
        ts = float((b.get("total_score") if key == "all_assignees"
                    else _DEFS.get(sid, {}).get(key)) or 0.0)
        tp = float(b.get("total_papers") or 0)
        out[sid] = (ts, (ts / tp) if tp else 0.0)
    return out


def load_published():
    scores, jif, sjr = {}, {}, {}
    with open(DATA_PATH) as f:
        for r in csv.DictReader(f):
            if r.get("source_type") not in ("j", "c"):
                continue
            sid = r.get("source_id")
            if not sid:
                continue
            scores[sid] = (cnum(r, "all_par_total_score") or 0.0,
                           cnum(r, "all_par_avg_score_all") or 0.0)
            j = cnum(r, "jcr_2024_jif")
            s = cnum(r, "scimago_sjr")
            if j is not None:
                jif[sid] = j
            if s is not None:
                sjr[sid] = s
    return scores, jif, sjr


def stats(a, b, idx, mode="union"):
    """Pairwise stats for score index idx (0 = aggregate, 1 = normalized)
    over every j/c venue ("all"), venues nonzero in >= 1 arm ("union"), or
    venues nonzero in both arms ("inter")."""
    if mode == "all":
        sids = sorted(set(a) | set(b))
    elif mode == "union":
        sids = [s for s in set(a) | set(b)
                if a.get(s, (0, 0))[idx] or b.get(s, (0, 0))[idx]]
    else:
        sids = [s for s in set(a) & set(b)
                if a.get(s, (0, 0))[idx] and b.get(s, (0, 0))[idx]]
    x = np.array([a.get(s, (0, 0))[idx] for s in sids])
    y = np.array([b.get(s, (0, 0))[idx] for s in sids])
    return (len(sids), pearsonr(x, y)[0],
            pearsonr(np.arcsinh(x * 1e4), np.arcsinh(y * 1e4))[0],
            spearmanr(x, y)[0])


def top_overlap(a, b, k, idx=0):
    ta = set(sorted(a, key=lambda s: -a[s][idx])[:k])
    tb = set(sorted(b, key=lambda s: -b[s][idx])[:k])
    return len(ta & tb)


def prestige(arm, metric, nonzero_only, idx=1):
    """idx 0 = aggregate score, 1 = per-work normalized score."""
    xs, ys = [], []
    for sid, m in metric.items():
        v = arm.get(sid, (0.0, 0.0))[idx]
        if nonzero_only and v == 0.0:
            continue
        xs.append(v)
        ys.append(m)
    x, y = np.array(xs), np.array(ys)
    return (len(x), pearsonr(x, y)[0],
            pearsonr(np.arcsinh(x * 1e4), np.arcsinh(y))[0],
            spearmanr(x, y)[0])


def main():
    pub, jif, sjr = load_published()
    arms = {"published": pub}
    for key, _, _ in UNIVERSES:
        arms[key] = load_variant(key)

    L = ["# Assignee-class seed definitions",
         "",
         "CIF under the published seed set and the four assignee-class definitions",
         "(one common graph, d = 0.85). Venues = journals and conferences; a venue",
         "a definition does not reach scores zero.",
         "Seed sizes: " + "; ".join(f"{k} {n:,}" for k, _, n in UNIVERSES) + ".",
         ""]

    L += ["## Coverage (zero-score j/c venues per arm)", ""]
    frame = set().union(*arms.values())
    for name, arm in arms.items():
        nz = sum(1 for s in frame if arm.get(s, (0, 0))[0])
        L.append(f"- {name}: nonzero {nz:,} of {len(frame):,} "
                 f"(zero or absent: {len(frame)-nz:,})")
    L.append("")

    L += ["## Pairwise correlations — ALL j/c venues (SM Table "
          "tab:universe-agreement)", "",
          "| pair | n | P agg | Pa agg | rho agg | P norm | Pa norm | "
          "rho norm | top-20 agg | top-100 agg |",
          "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    names = ["published"] + [k for k, _, _ in UNIVERSES]
    for i, na in enumerate(names):
        for nb in names[i + 1:]:
            a, b = arms[na], arms[nb]
            n_a, p_a, pa_a, s_a = stats(a, b, 0, "all")
            _, p_n, pa_n, s_n = stats(a, b, 1, "all")
            L.append(f"| {na} vs {nb} | {n_a:,} | {p_a:.3f} | {pa_a:.3f} | "
                     f"{s_a:.3f} | {p_n:.3f} | {pa_n:.3f} | {s_n:.3f} | "
                     f"{top_overlap(a, b, 20)} | {top_overlap(a, b, 100)} |")
    L.append("")

    L += ["## Pairwise agreement — venues nonzero in >= 1 arm", "",
          "| pair | n | P agg | Pa agg | rho agg | P norm | Pa norm | "
          "rho norm | top-20 agg | top-100 agg |",
          "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    names = ["published"] + [k for k, _, _ in UNIVERSES]
    for i, na in enumerate(names):
        for nb in names[i + 1:]:
            a, b = arms[na], arms[nb]
            n_a, p_a, pa_a, s_a = stats(a, b, 0)
            _, p_n, pa_n, s_n = stats(a, b, 1)
            o20 = top_overlap(a, b, 20)
            o100 = top_overlap(a, b, 100)
            L.append(f"| {na} vs {nb} | {n_a:,} | {p_a:.3f} | {pa_a:.3f} | "
                     f"{s_a:.3f} | {p_n:.3f} | {pa_n:.3f} | {s_n:.3f} | "
                     f"{o20} | {o100} |")
    L.append("")

    L += ["## Pairwise agreement — venues nonzero in BOTH arms", "",
          "| pair | n agg | Pa agg | rho agg | n norm | Pa norm | rho norm |",
          "|---|--:|--:|--:|--:|--:|--:|"]
    for i, na in enumerate(names):
        for nb in names[i + 1:]:
            a, b = arms[na], arms[nb]
            n_a, _, pa_a, s_a = stats(a, b, 0, "inter")
            n_n, _, pa_n, s_n = stats(a, b, 1, "inter")
            L.append(f"| {na} vs {nb} | {n_a:,} | {pa_a:.3f} | {s_a:.3f} | "
                     f"{n_n:,} | {pa_n:.3f} | {s_n:.3f} |")
    L.append("")

    for idx, what in ((0, "aggregate"), (1, "normalized")):
      for conv, nz in [("all metric-carrying venues", False),
                       ("metric-carrying and nonzero in arm", True)]:
        L += [f"## Prestige association, {what} score ({conv})", "",
              "| arm | n (JIF) | r JIF | Pa JIF | rho JIF | n (SJR) | r SJR | "
              "Pa SJR | rho SJR |",
              "|---|--:|--:|--:|--:|--:|--:|--:|--:|"]
        for name in names:
            nj, rj, paj, sj = prestige(arms[name], jif, nz, idx)
            ns, rs, pas, ss = prestige(arms[name], sjr, nz, idx)
            L.append(f"| {name} | {nj:,} | {rj:.3f} | {paj:.3f} | {sj:.3f} | "
                     f"{ns:,} | {rs:.3f} | {pas:.3f} | {ss:.3f} |")
        L.append("")

    text = "\n".join(L) + "\n"
    os.makedirs(RESULTS_DIR, exist_ok=True)
    open(OUT, "w").write(text)
    print(text)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
