from __future__ import annotations

import csv
import io
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from common.mongo import get_mongo_client, handle_pymongo_errors
from ref_map.common.batching import chunked
from ref_map.export.shared import DB_NAME, OPENALEX_PREFIX, SOURCES_COLLECTION_NAME

# Small, derived reference files that live in the repo.
DEFAULT_REFERENCE_DIR = Path(__file__).resolve().parent / "reference"

# Bulk and licensed external reference data lives in data/, which is gitignored:
# Clarivate JCR is not redistributable and Scimago is large. See data/README.md.
# Override the location with the SC_DATA_DIR environment variable.
_REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("SC_DATA_DIR", _REPO_ROOT / "data"))
DEFAULT_JCR_CSV = DATA_DIR / "clarivate" / "jcr_journal_results_2025_comprehensive.csv"
DEFAULT_SCIMAGO_CSV = DATA_DIR / "scimago" / "scimagojr_2025.csv"


def strip_openalex_source_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v:
        return None
    if v.startswith(OPENALEX_PREFIX):
        v = v[len(OPENALEX_PREFIX) :]
    return v or None


def full_source_id(source_id: str) -> str:
    return source_id if source_id.startswith(OPENALEX_PREFIX) else f"{OPENALEX_PREFIX}{source_id}"


def normalize_issn(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.upper().replace("-", "").replace(" ", "").strip()
    return v if len(v) == 8 else None


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_scimago_number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(".", "").replace(",", ".").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_csv_dicts(path_or_buf: Any, *, delimiter: str = ",", skiprows: int = 0) -> list[dict[str, Any]]:
    close_after = False
    if isinstance(path_or_buf, Path):
        f = path_or_buf.open("r", encoding="utf-8-sig", newline="")
        close_after = True
    else:
        f = path_or_buf
    try:
        for _ in range(skiprows):
            next(f, None)
        reader = csv.DictReader(f, delimiter=delimiter)
        return list(reader)
    finally:
        if close_after:
            f.close()


def _read_jcr_rows(path: Path) -> tuple[list[dict[str, Any]], str, str | None]:
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r'"([^"]*)"%', r'"\1%"', text)
    lines = text.splitlines()
    header_idx = None
    for idx, line in enumerate(lines):
        if "Journal name" in line and "ISSN" in line and "JIF" in line:
            header_idx = idx
            break
    if header_idx is None:
        raise ValueError(f"Could not find JCR header row in {path}")

    rows = read_csv_dicts(io.StringIO("\n".join(lines[header_idx:])))
    if not rows:
        return [], "", None

    columns = set(rows[0])
    jif_col = next(
        (col for col in rows[0] if re.fullmatch(r"20\d{2} JIF", col or "")),
        None,
    )
    if not jif_col and "JIF" in columns:
        jif_col = "JIF"
    if not jif_col:
        raise ValueError(f"JCR CSV missing JIF column: {path}")

    jci_col = next(
        (col for col in rows[0] if re.fullmatch(r"20\d{2} JCI", col or "")),
        None,
    )
    if not jci_col and "JCI" in columns:
        jci_col = "JCI"
    return rows, jif_col, jci_col


def load_jcr_issn_map(path: Path = DEFAULT_JCR_CSV) -> dict[str, dict[str, Any]]:
    if not path.exists():
        print(f"[external-metrics] JCR file not found; skipping: {path}")
        return {}

    rows, jif_col, jci_col = _read_jcr_rows(path)
    required = {"Journal name", "ISSN", "eISSN"}
    missing = required - set(rows[0] if rows else [])
    if missing:
        raise ValueError(f"JCR CSV missing columns: {sorted(missing)}")
    year_match = re.match(r"(20\d{2})\s+JIF", jif_col)
    jcr_year = int(year_match.group(1)) if year_match else None

    issn_to_row: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row.get("Journal name")
        if not isinstance(name, str) or name.startswith("Copyright"):
            continue
        jif = parse_number(row.get(jif_col))
        if jif is None:
            continue
        jci = parse_number(row.get(jci_col)) if jci_col else None
        payload = {
            "jcr_journal_name": name.strip(),
            "jcr_year": jcr_year,
            "jcr_jif": jif,
            "jcr_jci": jci,
            # Backward-compatible aliases used by existing plots/tables.
            "jcr_2024_jif": jif,
            "jcr_2024_jci": jci,
            "jcr_jif_quartile": row.get("JIF Quartile"),
            "jcr_category": row.get("Category"),
            "jcr_edition": row.get("Edition"),
        }
        for issn in (normalize_issn(row.get("ISSN")), normalize_issn(row.get("eISSN"))):
            if not issn:
                continue
            current = issn_to_row.get(issn)
            if current is None or jif > float(current.get("jcr_jif") or 0.0):
                issn_to_row[issn] = payload
    return issn_to_row


def load_scimago_issn_map(path: Path = DEFAULT_SCIMAGO_CSV) -> dict[str, dict[str, Any]]:
    if not path.exists():
        print(f"[external-metrics] Scimago file not found; skipping: {path}")
        return {}

    rows = read_csv_dicts(path, delimiter=";")
    required = {"Title", "Issn", "SJR"}
    missing = required - set(rows[0] if rows else [])
    if missing:
        raise ValueError(f"Scimago CSV missing columns: {sorted(missing)}")

    issn_to_row: dict[str, dict[str, Any]] = {}
    for row in rows:
        title = row.get("Title")
        sjr = parse_scimago_number(row.get("SJR"))
        if not isinstance(title, str) or sjr is None:
            continue
        payload = {
            "scimago_title": title.strip(),
            "scimago_sjr": sjr,
            "scimago_quartile": row.get("SJR Best Quartile"),
            "scimago_h_index": parse_number(row.get("H index")),
            "scimago_category": row.get("Categories"),
            "scimago_publisher": row.get("Publisher"),
        }
        for part in str(row.get("Issn") or "").replace(",", ";").split(";"):
            issn = normalize_issn(part)
            if not issn:
                continue
            current = issn_to_row.get(issn)
            if current is None or sjr > float(current.get("scimago_sjr") or 0.0):
                issn_to_row[issn] = payload
    return issn_to_row


def fetch_source_issns(
    source_ids: Iterable[str],
    *,
    db_name: str = DB_NAME,
    coll_name: str = SOURCES_COLLECTION_NAME,
    chunk_size: int = 5000,
) -> dict[str, list[str]]:
    ids = sorted({sid for value in source_ids if (sid := strip_openalex_source_id(value))})
    if not ids:
        return {}

    mongo = get_mongo_client(appname="external-source-metrics")
    coll = mongo[db_name][coll_name]
    projection = {"id": 1, "openalex_id": 1, "issn": 1, "issn_l": 1}
    out: dict[str, list[str]] = {}
    with handle_pymongo_errors():
        for batch in chunked(ids, chunk_size):
            full_ids = [full_source_id(sid) for sid in batch]
            cursor = coll.find(
                {"$or": [{"id": {"$in": full_ids}}, {"openalex_id": {"$in": batch}}]},
                projection,
            )
            for doc in cursor:
                sid = strip_openalex_source_id(doc.get("openalex_id")) or strip_openalex_source_id(
                    doc.get("id")
                )
                if not sid:
                    continue
                issns = set()
                for value in doc.get("issn") or []:
                    norm = normalize_issn(value)
                    if norm:
                        issns.add(norm)
                norm_l = normalize_issn(doc.get("issn_l"))
                if norm_l:
                    issns.add(norm_l)
                out[sid] = sorted(issns)
    return out


def apply_external_metrics_to_rows(
    rows: list[dict[str, Any]],
    *,
    source_issns: dict[str, list[str]],
    jcr_csv: str | Path | None = DEFAULT_JCR_CSV,
    scimago_csv: str | Path | None = DEFAULT_SCIMAGO_CSV,
) -> tuple[list[dict[str, Any]], int, int]:
    jcr_by_issn = load_jcr_issn_map(Path(jcr_csv)) if jcr_csv else {}
    scimago_by_issn = load_scimago_issn_map(Path(scimago_csv)) if scimago_csv else {}
    if not jcr_by_issn and not scimago_by_issn:
        return rows, 0, 0

    jcr_matches = 0
    scimago_matches = 0
    for row in rows:
        sid = strip_openalex_source_id(row.get("source_id"))
        issns = source_issns.get(sid or "", [])
        if issns:
            row["source_issns"] = "|".join(issns)

        jcr_matches_for_row = [jcr_by_issn[issn] for issn in issns if issn in jcr_by_issn]
        if jcr_matches_for_row:
            match = max(jcr_matches_for_row, key=lambda item: float(item["jcr_jif"]))
            row.update(match)
            jcr_matches += 1

        scimago_matches_for_row = [
            scimago_by_issn[issn] for issn in issns if issn in scimago_by_issn
        ]
        if scimago_matches_for_row:
            match = max(scimago_matches_for_row, key=lambda item: float(item["scimago_sjr"]))
            row.update(match)
            scimago_matches += 1

    return rows, jcr_matches, scimago_matches
