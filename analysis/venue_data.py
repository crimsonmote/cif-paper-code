"""Shared data loading and constants for the analysis scripts.

Inputs:
  * ref_map/output/venue_basis.csv — one row per OpenAlex source, built by
    `python -m ref_map.aggregation.build_venue_basis` from Data S1, OpenAlex source
    metadata, and the JCR and Scimago exports.
  * data_s1/ — the Data S1 files (see data_s1/README.md), read with `load_data_s1`.

Analysis conventions:
  - CIF: all_par_total_score (aggregate), all_par_avg_score_all (normalized = summed / works)
  - Industry-absorbed count: true_c0_count (aggregate), true_c0_share (= c_0 / works)
  - Venues: source_type in {'j', 'c'} (journals and conferences)
  - Prestige: jcr_2024_jif (JIF), scimago_sjr (SJR)
"""
import csv
import gzip
import json
import lzma
from pathlib import Path

_HERE = Path(__file__).resolve().parent
REPO = _HERE.parent
DATA_PATH = str(REPO / "ref_map" / "output" / "venue_basis.csv")
DATA_S1_DIR = REPO / "data_s1"
RESULTS_DIR = _HERE / "results"

# Column names (single source of truth)
COL_CIF_AGG   = "all_par_total_score"        # CIF aggregate (sum over the venue's works)
COL_CIF_NORM  = "all_par_avg_score_all"      # CIF normalized (mean over all the venue's works)
COL_CNT_AGG   = "true_c0_count"              # industry-absorbed paper count (c_0)
COL_CNT_NORM  = "true_c0_share"              # industry-absorbed share (c_0 / works)
COL_JIF       = "jcr_2024_jif"               # Journal Impact Factor (2025 JCR release)
COL_SJR       = "scimago_sjr"                # Scimago Journal Rank
COL_TYPE      = "source_type"                # j journal, c conference, other OpenAlex types
COL_NAME      = "source_name"
COL_NWORKS    = "total_papers"       # venue size for pool filters: all document types,
                                     # the denominator of all_par_avg_score_all
COL_NARTICLES = "total_articles"     # article-only count; for the article-basis check only

VENUE_TYPES = {"j", "c"}                      # journals + conferences

# OpenAlex fields (https://openalex.org/fields/<n>).
FIELD_NAMES = {
    "11": "Agricultural and Biological Sciences", "12": "Arts and Humanities",
    "13": "Biochemistry, Genetics and Molecular Biology",
    "14": "Business, Management and Accounting", "15": "Chemical Engineering",
    "16": "Chemistry", "17": "Computer Science", "18": "Decision Sciences",
    "19": "Earth and Planetary Sciences", "20": "Economics, Econometrics and Finance",
    "21": "Energy", "22": "Engineering", "23": "Environmental Science",
    "24": "Immunology and Microbiology", "25": "Materials Science", "26": "Mathematics",
    "27": "Medicine", "28": "Neuroscience", "29": "Nursing",
    "30": "Pharmacology, Toxicology and Pharmaceutics", "31": "Physics and Astronomy",
    "32": "Psychology", "33": "Social Sciences", "34": "Veterinary", "35": "Dentistry",
    "36": "Health Professions",
}


def load_data_s1(name):
    """Records of one Data S1 file, e.g. load_data_s1("cif_matched_samples")."""
    for suffix, opener in ((".json.xz", lzma.open), (".json.gz", gzip.open), (".json", open)):
        path = DATA_S1_DIR / f"{name}{suffix}"
        if path.exists():
            with opener(path, "rt") as f:
                return json.load(f)
    raise FileNotFoundError(f"Data S1 file {name!r} not found in {DATA_S1_DIR}")


def load_rows(path=DATA_PATH):
    """All rows of the venue table as dicts, with the c_0 count and c_0 share added."""
    with open(path) as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        c0 = float(row.get("all_reachable_papers_d0") or 0)
        tp = float(row.get(COL_NWORKS) or 0)
        row["true_c0_count"] = str(int(c0))
        row["true_c0_share"] = str(c0 / tp) if tp else ""
    return rows


def num(row, key):
    """Parse a numeric cell; return None if blank/non-numeric."""
    v = row.get(key, "")
    if v in ("", None):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def venues_only(rows):
    """Filter to journals + conferences (source_type in {'j','c'})."""
    return [r for r in rows if r.get(COL_TYPE, "") in VENUE_TYPES]
