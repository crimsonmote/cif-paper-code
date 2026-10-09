"""Build a compact JCR reference CSV from saved Clarivate journal pages."""

from __future__ import annotations

import argparse
import csv
import html
import re
from pathlib import Path
from typing import Any

from ref_map.aggregation.external_source_metrics import DATA_DIR

_CLARIVATE_DIR = DATA_DIR / "clarivate"
DEFAULT_INPUT_DIR = _CLARIVATE_DIR / "Journal Impact Data"
DEFAULT_OUTPUT_CSV = _CLARIVATE_DIR / "jcr_journal_results_2025_comprehensive.csv"

OUTPUT_COLUMNS = [
    "Journal name",
    "ISSN",
    "eISSN",
    "Category",
    "Edition",
    "Total Citations",
    "2025 JIF",
    "JIF Quartile",
    "2025 JCI",
    "% of Citable OA",
    "JIF Rank",
    "JCI Rank",
    "5 Year JIF",
    "Article Influence Score",
    "Eigenfactor Score",
    "Normalized Eigenfactor",
    "Citable Items",
    "Total Articles",
    "source_page",
]

COLUMN_MAP = {
    "journalName": "Journal name",
    "issn": "ISSN",
    "eissn": "eISSN",
    "category": "Category",
    "edition": "Edition",
    "totalCites": "Total Citations",
    "jif2019": "2025 JIF",
    "quartile": "JIF Quartile",
    "jci": "2025 JCI",
    "percentageOAGold": "% of Citable OA",
    "jifRank": "JIF Rank",
    "jciRank": "JCI Rank",
    "jif5Years": "5 Year JIF",
    "articleInfluenceScore": "Article Influence Score",
    "eigenFactor": "Eigenfactor Score",
    "normalizedEigenFactor": "Normalized Eigenfactor",
    "citableItems": "Citable Items",
    "totalArticles": "Total Articles",
}

ROW_RE = re.compile(r"<mat-row\b.*?</mat-row>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(
    r"<mat-cell\b(?P<attrs>[^>]*)>(?P<body>.*?)</mat-cell>",
    re.IGNORECASE | re.DOTALL,
)
CLASS_COL_RE = re.compile(r"\bcdk-column-([A-Za-z0-9_]+)\b")
TITLE_RE = re.compile(r'\btitle="([^"]*)"', re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")


def _clean_text(value: str) -> str:
    text = html.unescape(value)
    text = TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _cell_value(column: str, body: str) -> str:
    titles = [_clean_text(value) for value in TITLE_RE.findall(body)]
    titles = [value for value in titles if value and value != "\xa0"]
    if not titles:
        return _clean_text(body)

    if column in {"category", "edition"}:
        unique: list[str] = []
        for value in titles:
            if value not in unique:
                unique.append(value)
        return "; ".join(unique)

    return titles[0]


def parse_jcr_html(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    out: list[dict[str, Any]] = []
    for row_match in ROW_RE.finditer(text):
        row: dict[str, Any] = {column: "" for column in OUTPUT_COLUMNS}
        for cell_match in CELL_RE.finditer(row_match.group(0)):
            col_match = CLASS_COL_RE.search(cell_match.group("attrs"))
            if not col_match:
                continue
            raw_col = col_match.group(1)
            out_col = COLUMN_MAP.get(raw_col)
            if not out_col:
                continue
            row[out_col] = _cell_value(raw_col, cell_match.group("body"))

        name = str(row.get("Journal name") or "").strip()
        if not name:
            continue
        row["source_page"] = path.name
        out.append(row)
    return out


def build_reference(input_dir: Path, output_csv: Path) -> list[dict[str, Any]]:
    pages = sorted(input_dir.glob("J*.html"))
    if not pages:
        raise FileNotFoundError(f"No J*.html files found in {input_dir}")

    rows: list[dict[str, Any]] = []
    for page in pages:
        rows.extend(parse_jcr_html(page))

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    return rows


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Parse saved Clarivate/JCR journal HTML pages into a compact CSV."
    )
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help="Directory containing saved J*.html pages.",
    )
    parser.add_argument(
        "--output-csv",
        default=str(DEFAULT_OUTPUT_CSV),
        help="Output compact JCR reference CSV.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    rows = build_reference(Path(args.input_dir), Path(args.output_csv))
    unique_names = {row["Journal name"] for row in rows}
    issns = {
        value
        for row in rows
        for value in (row.get("ISSN"), row.get("eISSN"))
        if isinstance(value, str) and value and value != "N/A"
    }
    print(f"[jcr-html] Rows parsed: {len(rows):,}")
    print(f"[jcr-html] Unique journal names: {len(unique_names):,}")
    print(f"[jcr-html] Non-empty raw ISSN/eISSN values: {len(issns):,}")
    print(f"[jcr-html] Output: {args.output_csv}")


if __name__ == "__main__":
    main()
