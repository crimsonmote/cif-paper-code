"""Patch the three M5 placebo seed sets onto existing Neo4j Work nodes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

from neo4j import GraphDatabase

from ref_map.common.config import NEO4J_DATABASE
from ref_map.export.shared import _resolve_neo4j_config
from ref_map.export.sparse_work_properties import (
    DEFAULT_BATCH_SIZE,
    load_work_ids,
    patch_sparse_boolean_property,
)

DEFAULT_INPUT_DIR = Path("ref_map/output/m5_placebo")
DRAW_PROPERTIES = {
    1: "placebo_seed_r1",
    2: "placebo_seed_r2",
    3: "placebo_seed_r3",
}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Write exact sparse placebo_seed_r1/r2/r3 memberships to Neo4j."
        )
    )
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help=f"Directory containing placebo seed lists (default: {DEFAULT_INPUT_DIR}).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"IDs per bounded outer transaction (default: {DEFAULT_BATCH_SIZE}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Clear non-exact existing memberships before replacing them.",
    )
    parser.add_argument(
        "--no-apoc",
        action="store_true",
        help="Use bounded UNWIND transactions instead of APOC batching.",
    )
    return parser.parse_args()


def patch_m5_placebo_seeds(
    driver,
    *,
    input_dir: str | Path = DEFAULT_INPUT_DIR,
    batch_size: int = DEFAULT_BATCH_SIZE,
    use_apoc: bool = True,
    force: bool = False,
) -> dict[str, object]:
    directory = Path(input_dir)
    started = time.monotonic()
    sampler_meta_path = directory / "m5_sampler_meta.json"
    if not sampler_meta_path.exists():
        raise FileNotFoundError(
            f"Missing sampler audit metadata: {sampler_meta_path}"
        )
    sampler_meta = json.loads(sampler_meta_path.read_text(encoding="utf-8"))
    if sampler_meta.get("status") not in {
        "complete",
        "complete_with_accepted_frame_mismatch",
    }:
        raise RuntimeError(
            f"Sampler status is {sampler_meta.get('status')!r}; refusing to "
            "patch potentially stale draw files."
        )
    loaded_draws: dict[int, list[str]] = {}
    for draw, prop in DRAW_PROPERTIES.items():
        path = directory / f"placebo_seed_r{draw}.txt"
        work_ids = load_work_ids(path)
        draw_meta = (sampler_meta.get("draws") or {}).get(f"r{draw}") or {}
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if int(draw_meta.get("selected", -1)) != len(work_ids):
            raise RuntimeError(
                f"Draw r{draw} count differs from sampler metadata: "
                f"file={len(work_ids):,}, meta={draw_meta.get('selected')}"
            )
        if draw_meta.get("sha256") != digest:
            raise RuntimeError(
                f"Draw r{draw} checksum differs from sampler metadata; "
                "refusing to patch a stale or edited list."
            )
        loaded_draws[draw] = work_ids

    meta_path = directory / "m5_patch_meta.json"
    reports: dict[str, object] = {}
    report: dict[str, object] = {
        "status": "running",
        "database": NEO4J_DATABASE,
        "input_dir": str(directory.resolve()),
        "sampler_meta": str(sampler_meta_path.resolve()),
        "force": force,
        "draws": reports,
    }
    meta_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    try:
        for draw, prop in DRAW_PROPERTIES.items():
            path = directory / f"placebo_seed_r{draw}.txt"
            work_ids = loaded_draws[draw]
            print(f"[m5-patch] Draw r{draw}: {len(work_ids):,} IDs from {path}")
            reports[prop] = patch_sparse_boolean_property(
                driver,
                work_ids=work_ids,
                prop=prop,
                index_name=f"work_{prop}_idx",
                batch_size=batch_size,
                use_apoc=use_apoc,
                force=force,
                progress_label=f"M5 placebo r{draw}",
            )
            meta_path.write_text(
                json.dumps(report, indent=2) + "\n",
                encoding="utf-8",
            )
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
        report["seconds"] = round(time.monotonic() - started, 3)
        meta_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        raise
    report["status"] = "complete"
    report["seconds"] = round(time.monotonic() - started, 3)
    meta_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"[m5-patch] Audit report: {meta_path}")
    return report


def main() -> None:
    args = _parse_args()
    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        patch_m5_placebo_seeds(
            driver,
            input_dir=args.input_dir,
            batch_size=args.batch_size,
            use_apoc=not args.no_apoc,
            force=args.force,
        )
    finally:
        driver.close()


if __name__ == "__main__":
    main()
