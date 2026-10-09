from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from common.mongo import get_mongo_client, handle_pymongo_errors
from ref_map.common.batching import chunked
from ref_map.export.field_groups import projection_for_groups, shape_work_for_groups
from ref_map.export.shared import COLLECTION_NAME, DB_NAME, SOURCES_COLLECTION_NAME

OPENALEX_PREFIX = "https://openalex.org/"


def fetch_source_names(source_ids: Iterable[str], chunk_size: int = 5000) -> dict[str, str]:
    ids = sorted({source_id for source_id in source_ids if source_id})
    if not ids:
        return {}

    mongo = get_mongo_client()
    coll = mongo[DB_NAME][SOURCES_COLLECTION_NAME]
    projection = {"id": 1, "display_name": 1}
    result: dict[str, str] = {}

    # OpenAlex snapshots store the source identifier as a URL in `id`
    # ("https://openalex.org/S123"); the pipeline uses the short form ("S123").
    with handle_pymongo_errors():
        for batch in chunked(ids, chunk_size):
            urls = [OPENALEX_PREFIX + sid for sid in batch]
            for doc in coll.find({"id": {"$in": urls}}, projection):
                source_id = (doc.get("id") or "").removeprefix(OPENALEX_PREFIX)
                if source_id:
                    result[source_id] = doc.get("display_name") or ""
    return result


def enrich_source_names(rows: list[dict], source_id_key: str = "source_id") -> list[dict]:
    missing_ids = [
        row.get(source_id_key)
        for row in rows
        if row.get(source_id_key) and not row.get("source_name")
    ]
    if not missing_ids:
        return rows

    name_lookup = fetch_source_names(missing_ids)
    for row in rows:
        if not row.get("source_name"):
            row["source_name"] = name_lookup.get(row.get(source_id_key), row.get("source_name"))
    return rows


def fetch_work_display_fields(work_ids: Iterable[str], chunk_size: int = 5000) -> dict[str, dict[str, Any]]:
    ids = sorted({work_id for work_id in work_ids if work_id})
    if not ids:
        return {}

    mongo = get_mongo_client()
    coll = mongo[DB_NAME][COLLECTION_NAME]
    projection = projection_for_groups(["display_fields"])
    query_ids = [work_id if work_id.startswith("http") else f"https://openalex.org/{work_id}" for work_id in ids]
    result: dict[str, dict[str, Any]] = {}

    with handle_pymongo_errors():
        for batch in chunked(query_ids, chunk_size):
            for doc in coll.find({"id": {"$in": batch}}, projection):
                shaped = shape_work_for_groups(doc, ["display_fields"])
                if shaped is None:
                    continue
                work_id = shaped.get("id")
                if work_id:
                    result[work_id] = shaped
    return result
