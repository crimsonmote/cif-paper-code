"""Build three citation-profile-matched placebo seed sets for M5."""

from __future__ import annotations

import argparse
from bisect import bisect_left
import csv
from dataclasses import dataclass
import gzip
import hashlib
import heapq
import json
from pathlib import Path
import time
from typing import Any

from tqdm import tqdm

from common.mongo import get_mongo_client

OPENALEX_PREFIX = "https://openalex.org/"
TRUE_SEED_PROP = "matt_marx_paper_patent_startup"
QUOTA_COLUMN = "k_paper_patent_startup"
BREAKS = (0.50, 0.75, 0.90, 0.95, 0.99, 1.00)
BIN_LABELS = ("p0-50", "p50-75", "p75-90", "p90-95", "p95-99", "p99-100")
DRAW_SEEDS = (20260802, 20260803, 20260804)
DEFAULT_HISTOGRAM = Path(
    "analysis/regression_output/paper_absorption_hist.csv.gz"
)
DEFAULT_HISTOGRAM_META = Path(
    "analysis/regression_output/paper_absorption_meta.json"
)
DEFAULT_OUTPUT_DIR = Path("ref_map/output/m5_placebo")
MASK64 = (1 << 64) - 1

StratumKey = tuple[int, int]
CellKey = tuple[int, int, int]


def _citation_bin(percentile: float) -> int:
    """Match R ``cut(..., include.lowest=TRUE, right=TRUE)`` boundaries."""
    return min(bisect_left(BREAKS, percentile), len(BIN_LABELS) - 1)


@dataclass(frozen=True)
class CitationBinLookup:
    citation_values: tuple[int, ...]
    cumulative_ends: tuple[int, ...]
    bins: tuple[int, ...]
    total: int

    def locate(self, citations: int) -> tuple[int, bool]:
        """Return a bin and whether this citation value existed in the frame."""
        index = bisect_left(self.citation_values, citations)
        if index < len(self.citation_values) and self.citation_values[index] == citations:
            return self.bins[index], True
        cumulative_below = self.cumulative_ends[index - 1] if index else 0
        percentile = cumulative_below / self.total if self.total else 0.0
        return _citation_bin(percentile), False


@dataclass(frozen=True)
class HistogramDesign:
    lookups: dict[StratumKey, CitationBinLookup]
    quotas: dict[CellKey, int]
    expected_control_pools: dict[CellKey, int]
    histogram_rows: int
    histogram_docs: int
    seed_total: int
    sha256: str


def _flush_stratum(
    key: StratumKey,
    rows: list[tuple[int, int, int]],
    *,
    lookups: dict[StratumKey, CitationBinLookup],
    quotas: dict[CellKey, int],
    pools: dict[CellKey, int],
) -> None:
    if not rows:
        return
    rows.sort(key=lambda row: row[0])
    total = sum(row[1] for row in rows)
    cumulative = 0
    values: list[int] = []
    ends: list[int] = []
    bins: list[int] = []
    for citations, count, seeds in rows:
        midfrac = (cumulative + 0.5 * count) / total if total else 0.0
        bin_index = _citation_bin(midfrac)
        cell = (key[0], key[1], bin_index)
        quotas[cell] = quotas.get(cell, 0) + seeds
        pools[cell] = pools.get(cell, 0) + count - seeds
        cumulative += count
        values.append(citations)
        ends.append(cumulative)
        bins.append(bin_index)
    lookups[key] = CitationBinLookup(
        citation_values=tuple(values),
        cumulative_ends=tuple(ends),
        bins=tuple(bins),
        total=total,
    )


def load_histogram_design(path: str | Path) -> HistogramDesign:
    """Load the exact grouped sampling frame without another corpus scan."""
    histogram = Path(path)
    digest = hashlib.sha256()
    with histogram.open("rb") as raw:
        for chunk in iter(lambda: raw.read(1024 * 1024), b""):
            digest.update(chunk)

    lookups: dict[StratumKey, CitationBinLookup] = {}
    quotas: dict[CellKey, int] = {}
    pools: dict[CellKey, int] = {}
    current_key: StratumKey | None = None
    current_rows: list[tuple[int, int, int]] = []
    row_count = 0
    doc_count = 0
    seed_total = 0
    with gzip.open(histogram, "rt", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or QUOTA_COLUMN not in reader.fieldnames:
            raise ValueError(f"Histogram {histogram} lacks {QUOTA_COLUMN!r}")
        for row in reader:
            row_count += 1
            field = int(row["field"])
            year = int(row["year"])
            citations = int(row["cites"])
            count = int(row["n"])
            seeds = int(row[QUOTA_COLUMN])
            key = (field, year)
            if current_key is not None and key != current_key:
                _flush_stratum(
                    current_key,
                    current_rows,
                    lookups=lookups,
                    quotas=quotas,
                    pools=pools,
                )
                current_rows = []
            current_key = key
            current_rows.append((citations, count, seeds))
            doc_count += count
            seed_total += seeds
    if current_key is not None:
        _flush_stratum(
            current_key,
            current_rows,
            lookups=lookups,
            quotas=quotas,
            pools=pools,
        )
    return HistogramDesign(
        lookups=lookups,
        quotas={key: value for key, value in quotas.items() if value > 0},
        expected_control_pools=pools,
        histogram_rows=row_count,
        histogram_docs=doc_count,
        seed_total=seed_total,
        sha256=digest.hexdigest(),
    )


def _field_number(doc: dict[str, Any]) -> int:
    field_id = ((doc.get("primary_topic") or {}).get("field") or {}).get("id")
    if not field_id:
        return -1
    try:
        return int(str(field_id).rsplit("/", 1)[-1])
    except ValueError:
        return -1


def _normalized_coordinates(doc: dict[str, Any]) -> tuple[int, int, int]:
    year = doc.get("publication_year")
    citations = doc.get("cited_by_count")
    return (
        _field_number(doc),
        int(year) if year is not None else -1,
        int(citations) if citations is not None else -1,
    )


def _bare_work_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    work_id = value[len(OPENALEX_PREFIX) :] if value.startswith(OPENALEX_PREFIX) else value
    if len(work_id) < 2 or work_id[0] != "W" or not work_id[1:].isdigit():
        return None
    return work_id


def splitmix64(value: int) -> int:
    """Fast deterministic 64-bit mixer used as a stream-order-free priority."""
    value = (value + 0x9E3779B97F4A7C15) & MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & MASK64
    return (value ^ (value >> 31)) & MASK64


def _offer(
    heap: list[tuple[int, int, str]],
    *,
    quota: int,
    priority: int,
    numeric_id: int,
    work_id: str,
) -> None:
    """Keep the bottom-k deterministic priorities in a max-heap encoding."""
    item = (-priority, -numeric_id, work_id)
    if len(heap) < quota:
        heapq.heappush(heap, item)
        return
    worst_key = (-heap[0][0], -heap[0][1])
    if (priority, numeric_id) < worst_key:
        heapq.heapreplace(heap, item)


def _cell_string(cell: CellKey) -> str:
    return f"field={cell[0]}|year={cell[1]}|bin={BIN_LABELS[cell[2]]}"


def _write_ids(path: Path, ids: list[str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("".join(f"{work_id}\n" for work_id in ids), encoding="utf-8")
    temporary.replace(path)


def _ids_sha256(ids: list[str]) -> str:
    digest = hashlib.sha256()
    for work_id in ids:
        digest.update(work_id.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_placebo_seed_sets(
    *,
    histogram: str | Path = DEFAULT_HISTOGRAM,
    histogram_meta: str | Path = DEFAULT_HISTOGRAM_META,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    database: str = "openalex",
    collection: str = "works",
    batch_size: int = 20_000,
    allow_frame_mismatch: bool = False,
) -> dict[str, Any]:
    started = time.monotonic()
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    design = load_histogram_design(histogram)
    print(
        f"[m5-sampler] Frame: {design.histogram_docs:,} papers, "
        f"{design.histogram_rows:,} histogram rows, "
        f"{design.seed_total:,} true seeds, {len(design.quotas):,} quota cells"
    )

    prior_meta: dict[str, Any] = {}
    meta_path = Path(histogram_meta)
    if meta_path.exists():
        prior_meta = json.loads(meta_path.read_text(encoding="utf-8"))

    heaps: list[dict[CellKey, list[tuple[int, int, str]]]] = [
        {} for _ in DRAW_SEEDS
    ]
    observed_pools: dict[CellKey, int] = {}
    observed_seed_quotas: dict[CellKey, int] = {}
    docs_streamed = 0
    true_seeds_seen = 0
    eligible_seen = 0
    invalid_ids = 0
    unknown_strata = 0
    unseen_citation_values = 0
    mongo = get_mongo_client(appname="m5-placebo-sampler")
    works = mongo[database][collection]
    projection = {
        "_id": 0,
        "id": 1,
        "publication_year": 1,
        "cited_by_count": 1,
        "primary_topic.field.id": 1,
        TRUE_SEED_PROP: 1,
    }
    cursor = works.find({}, projection).batch_size(batch_size)
    with tqdm(
        total=design.histogram_docs,
        desc="M5 placebo sampling",
        unit="paper",
        dynamic_ncols=True,
    ) as progress:
        for doc in cursor:
            docs_streamed += 1
            field, year, citations = _normalized_coordinates(doc)
            lookup = design.lookups.get((field, year))
            if lookup is None:
                unknown_strata += 1
                progress.update(1)
                continue
            bin_index, citation_seen = lookup.locate(citations)
            if not citation_seen:
                unseen_citation_values += 1
            cell = (field, year, bin_index)
            if doc.get(TRUE_SEED_PROP):
                true_seeds_seen += 1
                observed_seed_quotas[cell] = observed_seed_quotas.get(cell, 0) + 1
                progress.update(1)
                continue
            quota = design.quotas.get(cell, 0)
            if quota == 0:
                progress.update(1)
                continue
            work_id = _bare_work_id(doc.get("id"))
            if work_id is None:
                invalid_ids += 1
                progress.update(1)
                continue
            eligible_seen += 1
            observed_pools[cell] = observed_pools.get(cell, 0) + 1
            numeric_id = int(work_id[1:])
            for draw_index, draw_seed in enumerate(DRAW_SEEDS):
                heap = heaps[draw_index].setdefault(cell, [])
                _offer(
                    heap,
                    quota=quota,
                    priority=splitmix64(numeric_id ^ draw_seed),
                    numeric_id=numeric_id,
                    work_id=work_id,
                )
            progress.update(1)
            if docs_streamed % 10_000_000 == 0:
                progress.set_postfix(
                    seeds=true_seeds_seen,
                    eligible=eligible_seen,
                    refresh=True,
                )

    quota_differences = {
        cell: observed_seed_quotas.get(cell, 0) - quota
        for cell, quota in design.quotas.items()
        if observed_seed_quotas.get(cell, 0) != quota
    }
    for cell, observed in observed_seed_quotas.items():
        if cell not in design.quotas and observed:
            quota_differences[cell] = observed
    frame_issues: list[str] = []
    if docs_streamed != design.histogram_docs:
        frame_issues.append(
            f"Mongo streamed {docs_streamed:,} docs; histogram has "
            f"{design.histogram_docs:,}"
        )
    if true_seeds_seen != design.seed_total:
        frame_issues.append(
            f"Mongo has {true_seeds_seen:,} true seeds; histogram has "
            f"{design.seed_total:,}"
        )
    if quota_differences:
        frame_issues.append(
            f"{len(quota_differences):,} matched-profile cells have seed-quota drift"
        )
    if unknown_strata:
        frame_issues.append(f"{unknown_strata:,} Mongo docs have strata absent from frame")

    draw_ids: list[list[str]] = []
    draw_reports: dict[str, Any] = {}
    for draw_index, draw_seed in enumerate(DRAW_SEEDS):
        selected = sorted(
            (item[2] for heap in heaps[draw_index].values() for item in heap),
            key=lambda value: int(value[1:]),
        )
        draw_ids.append(selected)
        shortfalls = {
            cell: quota - len(heaps[draw_index].get(cell, []))
            for cell, quota in design.quotas.items()
            if len(heaps[draw_index].get(cell, [])) < quota
        }
        draw_reports[f"r{draw_index + 1}"] = {
            "seed": draw_seed,
            "selected": len(selected),
            "sha256": _ids_sha256(selected),
            "shortfall_total": sum(shortfalls.values()),
            "shortfalls": {
                _cell_string(cell): amount
                for cell, amount in sorted(shortfalls.items())
            },
        }

    overlaps = {
        "r1_r2": len(set(draw_ids[0]) & set(draw_ids[1])),
        "r1_r3": len(set(draw_ids[0]) & set(draw_ids[2])),
        "r2_r3": len(set(draw_ids[1]) & set(draw_ids[2])),
    }
    identical_draw_pairs = [
        name
        for name, left, right in (
            ("r1_r2", draw_ids[0], draw_ids[1]),
            ("r1_r3", draw_ids[0], draw_ids[2]),
            ("r2_r3", draw_ids[1], draw_ids[2]),
        )
        if left == right
    ]
    report: dict[str, Any] = {
        "status": "frame_mismatch" if frame_issues else "complete",
        "strata": "field",
        "true_seed_property": TRUE_SEED_PROP,
        "histogram": str(Path(histogram).resolve()),
        "histogram_sha256": design.sha256,
        "histogram_rows": design.histogram_rows,
        "histogram_docs": design.histogram_docs,
        "histogram_seed_total": design.seed_total,
        "histogram_meta_n_docs": prior_meta.get("n_docs"),
        "mongo_docs_streamed": docs_streamed,
        "mongo_true_seeds": true_seeds_seen,
        "eligible_candidates_in_quota_cells": eligible_seen,
        "expected_control_pool_in_quota_cells": sum(
            design.expected_control_pools.get(cell, 0) for cell in design.quotas
        ),
        "observed_control_pool_in_quota_cells": sum(observed_pools.values()),
        "invalid_work_ids": invalid_ids,
        "unknown_strata": unknown_strata,
        "unseen_citation_values": unseen_citation_values,
        "frame_issues": frame_issues,
        "quota_drift_cells": {
            _cell_string(cell): difference
            for cell, difference in sorted(quota_differences.items())
        },
        "sampling_algorithm": "splitmix64 bottom-k priority per matched cell",
        "stream_order_independent": True,
        "draws": draw_reports,
        "pairwise_overlaps": overlaps,
        "identical_draw_pairs": identical_draw_pairs,
        "seconds": round(time.monotonic() - started, 3),
    }
    audit_path = output / "m5_sampler_meta.json"
    audit_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if frame_issues and not allow_frame_mismatch:
        raise RuntimeError(
            "Mongo and the histogram sampling frame differ; placebo lists were "
            f"not written. See {audit_path}. Rebuild the histogram or inspect "
            "the recorded drift; --allow-frame-mismatch explicitly accepts the "
            "older histogram quotas."
        )
    if identical_draw_pairs:
        raise RuntimeError(
            "One or more independently keyed placebo draws are identical: "
            f"{identical_draw_pairs}. Lists were not written; see {audit_path}."
        )
    if frame_issues:
        report["status"] = "complete_with_accepted_frame_mismatch"
        audit_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    true_seed_leak = False
    # True seeds were skipped before offering candidates. Keep this explicit in
    # the audit so downstream checks need not infer it from the code path.
    for draw_index, selected in enumerate(draw_ids, start=1):
        _write_ids(output / f"placebo_seed_r{draw_index}.txt", selected)
    report["true_seed_leak_detected"] = true_seed_leak
    report["seconds"] = round(time.monotonic() - started, 3)
    audit_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[m5-sampler] Wrote three draws and audit report under {output}")
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build three field/year/citation-bin matched M5 placebo draws."
    )
    parser.add_argument("--strata", choices=["field", "subfield"], default="field")
    parser.add_argument("--histogram", default=str(DEFAULT_HISTOGRAM))
    parser.add_argument("--histogram-meta", default=str(DEFAULT_HISTOGRAM_META))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--db", default="openalex")
    parser.add_argument("--collection", default="works")
    parser.add_argument("--batch-size", type=int, default=20_000)
    parser.add_argument(
        "--allow-frame-mismatch",
        action="store_true",
        help="Explicitly retain histogram quotas if current Mongo audit differs.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.strata == "subfield":
        raise NotImplementedError(
            "Subfield matching requires a subfield-keyed absorption histogram."
        )
    build_placebo_seed_sets(
        histogram=args.histogram,
        histogram_meta=args.histogram_meta,
        output_dir=args.output_dir,
        database=args.db,
        collection=args.collection,
        batch_size=args.batch_size,
        allow_frame_mismatch=args.allow_frame_mismatch,
    )


if __name__ == "__main__":
    main()
