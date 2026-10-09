"""One-scan orchestration for aggregating multiple source PAR properties."""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import time
from typing import Any

from neo4j import GraphDatabase

from ref_map.aggregation.source_par import (
    PARTITION_ID_PREFIX,
    compute_multi_property_source_aggregation,
    merge_partition_outputs,
    save_results,
)
from ref_map.common.config import NEO4J_DATABASE
from ref_map.common.neo4j_tokens import validate_property_key, validate_token
from ref_map.export.shared import _resolve_neo4j_config


def _valid_existing_output(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, list) and bool(payload)


def _assert_properties_present(session, *, label: str, properties: Mapping[str, str]) -> None:
    label = validate_token(label, kind="node label")
    missing: list[str] = []
    for prop in properties.values():
        prop = validate_property_key(prop)
        record = session.run(
            f"MATCH (w:`{label}`) WHERE w.`{prop}` IS NOT NULL "
            "RETURN 1 AS found LIMIT 1"
        ).single()
        if record is None:
            missing.append(prop)
    if missing:
        raise RuntimeError(
            "PAR properties have no values in Neo4j: " + ", ".join(missing)
        )


def aggregate_source_par_suite(
    *,
    score_properties: Mapping[str, str],
    output_paths: Mapping[str, Mapping[str, str | Path]],
    label: str = "Work",
    min_papers: int = 1,
    num_partitions: int = 1,
    resume: bool = False,
) -> dict[str, Any]:
    """Aggregate requested property/scope outputs with one scan per partition."""
    properties = dict(score_properties)
    outputs = {
        scope: {key: Path(path) for key, path in paths.items()}
        for scope, paths in output_paths.items()
    }
    if not properties:
        raise ValueError("At least one PAR property is required")
    if not outputs:
        raise ValueError("At least one output scope is required")
    for scope, paths in outputs.items():
        unknown = set(paths) - set(properties)
        if unknown:
            raise ValueError(f"Scope {scope!r} has unknown property keys: {unknown}")
        if not paths:
            raise ValueError(f"Scope {scope!r} has no output paths")
    if num_partitions < 1:
        raise ValueError("num_partitions must be >= 1")

    report: dict[str, Any] = {
        "label": label,
        "min_papers": min_papers,
        "num_partitions": num_partitions,
        "outputs": {},
    }
    pending_pairs: set[tuple[str, str]] = set()
    for scope, paths in outputs.items():
        for key, path in paths.items():
            status = "pending"
            if resume and _valid_existing_output(path):
                status = "skipped_existing"
            else:
                pending_pairs.add((scope, key))
            report["outputs"][f"{scope}:{key}"] = {
                "property": properties[key],
                "output": str(path),
                "status": status,
            }
    if not pending_pairs:
        report["status"] = "skipped_all_existing"
        report["seconds"] = 0.0
        return report

    pending_scopes = tuple(scope for scope in outputs if any(s == scope for s, _ in pending_pairs))
    pending_keys = tuple(key for key in properties if any(k == key for _, k in pending_pairs))
    pending_properties = {key: properties[key] for key in pending_keys}
    uri, user, password = _resolve_neo4j_config()
    print(f"[config] Neo4j URI: {uri}, database: {NEO4J_DATABASE}")
    print(f"[par-suite] Properties: {pending_properties}")
    print(f"[par-suite] Scopes: {pending_scopes}")
    print(f"[par-suite] Pending outputs: {len(pending_pairs)}")
    print(f"[par-suite] Partitions: {num_partitions}")
    started = time.monotonic()
    driver = GraphDatabase.driver(uri, auth=(user, password))

    with driver.session(database=NEO4J_DATABASE) as session:
        _assert_properties_present(
            session,
            label=label,
            properties=pending_properties,
        )

    def run_partition(partition_id: int) -> None:
        with driver.session(database=NEO4J_DATABASE) as session:
            result = compute_multi_property_source_aggregation(
                session,
                label=label,
                score_properties=pending_properties,
                scopes=pending_scopes,
                min_papers=min_papers,
                num_partitions=num_partitions,
                partition_id=partition_id,
            )
        for scope, key in pending_pairs:
            output = outputs[scope][key]
            destination = (
                output.with_name(
                    f"{output.stem}_{PARTITION_ID_PREFIX}{partition_id}.json"
                )
                if num_partitions > 1
                else output
            )
            save_results(result[scope][key], destination)

    try:
        if num_partitions == 1:
            run_partition(0)
        else:
            with ThreadPoolExecutor(max_workers=num_partitions) as executor:
                futures = {
                    executor.submit(run_partition, partition_id): partition_id
                    for partition_id in range(num_partitions)
                }
                for future in as_completed(futures):
                    future.result()
            for scope, key in pending_pairs:
                output = outputs[scope][key]
                merged = merge_partition_outputs(
                    output.parent,
                    output.stem,
                    num_partitions,
                )
                if merged != output:
                    raise RuntimeError(
                        f"Unexpected merged output path {merged}; expected {output}"
                    )
    finally:
        driver.close()

    seconds = round(time.monotonic() - started, 3)
    for scope, key in pending_pairs:
        report["outputs"][f"{scope}:{key}"]["status"] = "complete"
    report["status"] = "complete"
    report["seconds"] = seconds
    return report
