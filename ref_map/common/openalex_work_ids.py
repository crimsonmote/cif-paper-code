"""Shared OpenAlex Work-ID normalization and Neo4j storage helpers."""

from __future__ import annotations

import re
from typing import Any

from ref_map.common.config import NEO4J_DATABASE

OPENALEX_PREFIX = "https://openalex.org/"
_WORK_ID = re.compile(r"^W\d+$")


def normalize_work_id(value: Any) -> str | None:
    """Return a bare ``W...`` identifier, or ``None`` when invalid."""
    if not isinstance(value, str):
        return None
    work_id = value.strip()
    if work_id.startswith(OPENALEX_PREFIX):
        work_id = work_id[len(OPENALEX_PREFIX) :]
    if work_id.startswith("works/"):
        work_id = work_id[len("works/") :]
    return work_id if _WORK_ID.fullmatch(work_id) else None


def neo4j_work_id_expression(id_format: str, expression: str) -> str:
    """Build the Cypher expression matching a bare ID to stored ``Work.id``."""
    if id_format == "full_url":
        return f"'{OPENALEX_PREFIX}' + {expression}"
    if id_format != "stripped":
        raise ValueError(f"Unsupported Neo4j Work.id format: {id_format!r}")
    return expression


def detect_work_id_format(driver) -> str:
    """Detect whether Neo4j stores bare Work IDs or full OpenAlex URLs."""
    query = """
    MATCH (w:Work)
    WITH w LIMIT 10000
    RETURN
      sum(CASE WHEN w.id STARTS WITH 'https://openalex.org/' THEN 1 ELSE 0 END) AS full_url,
      sum(CASE WHEN w.id STARTS WITH 'W' THEN 1 ELSE 0 END) AS stripped
    """
    with driver.session(database=NEO4J_DATABASE) as session:
        record = session.run(query).single()
    if not record:
        return "stripped"
    full_url = int(record["full_url"] or 0)
    stripped = int(record["stripped"] or 0)
    return "full_url" if full_url > stripped else "stripped"
