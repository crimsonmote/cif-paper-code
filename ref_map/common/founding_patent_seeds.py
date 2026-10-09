"""Derive canonical Founding Patents seed sets from the raw linkage table."""

from __future__ import annotations

import csv
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

from ref_map.common.config import (
    CANONICAL_FOUNDING_PATENT_MODES,
    SEED_SPECS,
)

# Raw inputs live under $SC_DATA_DIR (default: <repo>/data); see the README.
DATA_DIR = Path(os.environ.get("SC_DATA_DIR", Path(__file__).resolve().parents[2] / "data"))
# Built by `python -m ref_map.export.build_seed_linkage` from the Founding Patents
# and Reliance on Science releases.
DEFAULT_FOUNDING_PATENT_TRIPLETS = Path(
    os.environ.get(
        "MATT_MARX_FOUNDING_PATENT_TRIPLETS",
        DATA_DIR / "founding_patents" / "_patent_paper_startup_triplets.csv",
    )
)
# University flags: Founding Patents release of 2 December 2023
# (doi:10.5281/zenodo.10250493), file _patent_ocpb.csv.
DEFAULT_FOUNDING_PATENT_ASSIGNEES = Path(
    os.environ.get(
        "MATT_MARX_FOUNDING_PATENT_ASSIGNEES",
        DATA_DIR / "founding_patents" / "_patent_ocpb.csv",
    )
)
YOUNG_FIRM_MAX_AGE = 3
MIN_FOUNDING_MATCH_SCORE = 8.0


@dataclass(frozen=True)
class YoungFirmCandidate:
    founding_year: int
    application_day_offset: int


@dataclass(frozen=True)
class PaperSeedRecord:
    corporate: bool
    university: bool
    young_firm_candidates: tuple[YoungFirmCandidate, ...]


def _float_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def _int_or_none(value: Any) -> int | None:
    parsed = _float_or_none(value)
    return int(parsed) if parsed is not None else None


def _is_zero(value: Any) -> bool:
    parsed = _float_or_none(value)
    return parsed == 0.0


def _is_one(value: Any) -> bool:
    parsed = _float_or_none(value)
    return parsed == 1.0


def _parse_publication_date(value: Any) -> date | None:
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        value = to_native()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def is_young_firm_candidate(
    candidate: YoungFirmCandidate,
    publication_date: Any,
    *,
    max_age: int = YOUNG_FIRM_MAX_AGE,
) -> bool:
    published = _parse_publication_date(publication_date)
    if published is None:
        return False
    application_date = published + timedelta(days=candidate.application_day_offset)
    age = application_date.year - candidate.founding_year
    return 0 <= age <= max_age


class FoundingPatentSeedIndex:
    """Paper-level seed membership using an any-qualifying-link rule."""

    def __init__(self, records: Mapping[str, PaperSeedRecord]) -> None:
        self._records = dict(records)

    def __len__(self) -> int:
        return len(self._records)

    def paper_ids(self) -> Iterable[str]:
        return self._records.keys()

    def record_for(self, paper_id: str) -> PaperSeedRecord | None:
        return self._records.get(paper_id)

    def flags_for_work(
        self,
        paper_id: str,
        publication_date: Any,
        *,
        max_age: int = YOUNG_FIRM_MAX_AGE,
    ) -> dict[str, bool]:
        record = self.record_for(paper_id)
        if record is None:
            return {mode: False for mode in CANONICAL_FOUNDING_PATENT_MODES}
        young = any(
            is_young_firm_candidate(candidate, publication_date, max_age=max_age)
            for candidate in record.young_firm_candidates
        )
        return {
            "all-assignees": True,
            "corporate": record.corporate,
            "young-firm": young,
            "young-firm-university": young or record.university,
        }

    def property_values_for_work(
        self,
        paper_id: str,
        publication_date: Any,
        *,
        sparse: bool = True,
        max_age: int = YOUNG_FIRM_MAX_AGE,
    ) -> dict[str, bool | None]:
        flags = self.flags_for_work(paper_id, publication_date, max_age=max_age)
        return {
            SEED_SPECS[mode].property_name: (True if value else None)
            if sparse
            else value
            for mode, value in flags.items()
        }


def load_founding_patent_seed_index(
    path: str | Path = DEFAULT_FOUNDING_PATENT_TRIPLETS,
    *,
    assignees_path: str | Path | None = DEFAULT_FOUNDING_PATENT_ASSIGNEES,
    min_founding_score: float = MIN_FOUNDING_MATCH_SCORE,
) -> FoundingPatentSeedIndex:
    csv_path = Path(path)
    assignee_university_flags: dict[str, bool] = {}
    if assignees_path is not None:
        resolved_assignees_path = Path(assignees_path)
        if not resolved_assignees_path.exists():
            raise FileNotFoundError(
                f"Founding Patents assignee table not found: {resolved_assignees_path}"
            )
        assignee_university_flags = _load_assignee_university_flags(
            resolved_assignees_path
        )
    records: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "corporate": False,
            "university": False,
            "young_firm_candidates": set(),
        }
    )
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "paperid",
            "univ_hospital_gov",
            "founding_year",
            "founding_score",
            "daysdiffcont",
        }
        if assignee_university_flags:
            required.add("assignee_id_20220630pv")
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(
                f"Founding Patents table is missing columns: {', '.join(sorted(missing))}"
            )

        for row in reader:
            paper_id = (row.get("paperid") or "").strip()
            if not paper_id:
                continue
            record = records[paper_id]
            assignee_id = (row.get("assignee_id_20220630pv") or "").strip()
            if (
                assignee_university_flags.get(assignee_id) is True
                and _is_one(row.get("univ_hospital_gov"))
            ):
                record["university"] = True

            corporate = _is_zero(row.get("univ_hospital_gov"))
            if not corporate:
                continue
            record["corporate"] = True

            founding_year = _int_or_none(row.get("founding_year"))
            founding_score = _float_or_none(row.get("founding_score"))
            day_offset = _int_or_none(row.get("daysdiffcont"))
            if (
                founding_year is None
                or founding_score is None
                or founding_score < min_founding_score
                or day_offset is None
            ):
                continue
            record["young_firm_candidates"].add(
                YoungFirmCandidate(
                    founding_year=founding_year,
                    application_day_offset=day_offset,
                )
            )

    return FoundingPatentSeedIndex(
        {
            paper_id: PaperSeedRecord(
                corporate=bool(record["corporate"]),
                university=bool(record["university"]),
                young_firm_candidates=tuple(
                    sorted(
                        record["young_firm_candidates"],
                        key=lambda item: (
                            item.founding_year,
                            item.application_day_offset,
                        ),
                    )
                ),
            )
            for paper_id, record in records.items()
        }
    )


def _load_assignee_university_flags(path: Path) -> dict[str, bool]:
    classifications: dict[str, bool] = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"june2022assignee_id", "university"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "Founding Patents assignee table is missing columns: "
                f"{', '.join(sorted(missing))}"
            )
        for row in reader:
            assignee_id = (row.get("june2022assignee_id") or "").strip()
            if not assignee_id:
                continue
            university = _is_one(row.get("university"))
            previous = classifications.get(assignee_id)
            if previous is not None and previous is not university:
                raise ValueError(
                    "Conflicting university classifications for Founding "
                    f"Patents assignee {assignee_id}"
                )
            classifications[assignee_id] = university
    return classifications


@lru_cache(maxsize=4)
def cached_founding_patent_seed_index(
    path: str = str(DEFAULT_FOUNDING_PATENT_TRIPLETS),
    assignees_path: str = str(DEFAULT_FOUNDING_PATENT_ASSIGNEES),
    min_founding_score: float = MIN_FOUNDING_MATCH_SCORE,
) -> FoundingPatentSeedIndex:
    return load_founding_patent_seed_index(
        path,
        assignees_path=assignees_path,
        min_founding_score=min_founding_score,
    )
