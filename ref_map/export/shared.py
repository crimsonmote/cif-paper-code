import json
import os
from pathlib import Path
from typing import Any, Optional

import keyring

BATCH_SIZE = int(os.environ.get("NEO4J_BATCH_SIZE", "2000"))
NEO4J_DATABASE = os.environ.get("NEO4J_DATABASE", "neo4j")
DB_NAME = os.environ.get("OPENALEX_DB", "openalex")
COLLECTION_NAME = os.environ.get("OPENALEX_COLLECTION", "works")
FUNDERS_COLLECTION_NAME = os.environ.get("OPENALEX_FUNDERS_COLLECTION", "funders")
SOURCES_COLLECTION_NAME = os.environ.get("OPENALEX_SOURCES_COLLECTION", "sources")
FAST_COUNT = os.environ.get("NEO4J_FAST_COUNT", "true").lower() == "true"
SKIP_COUNT = os.environ.get("NEO4J_SKIP_COUNT", "false").lower() == "true"
LOG_PATH = Path(__file__).resolve().parents[2] / "log" / "error.log"
OPENALEX_PREFIX = "https://openalex.org/"


def _resolve_neo4j_config() -> tuple[str, str, str]:
    uri = os.environ.get("NEO4J_URI") or keyring.get_password("neo4j", "uri")
    user = os.environ.get("NEO4J_USER") or keyring.get_password("neo4j", "user")
    password = os.environ.get("NEO4J_PASS") or keyring.get_password("neo4j", "pass")
    if not all([uri, user, password]):
        raise RuntimeError(
            "Missing Neo4j credentials. Set NEO4J_URI/USER/PASS or store them in keyring."
        )
    return uri, user, password


def _to_json_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return json.dumps(value)
    except Exception:
        return json.dumps(str(value))


def _ensure_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _fill_nones(seq: list[Any], replacement: Any = "") -> list[Any]:
    return [replacement if v is None else v for v in seq]


def _strip_openalex(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    v = value.strip()
    if v.startswith(OPENALEX_PREFIX):
        v = v[len(OPENALEX_PREFIX) :]
    if v.startswith("works/"):
        v = v[len("works/") :]
    return v


def _strip_openalex_list(seq: list[Any]) -> list[Any]:
    return [_strip_openalex(v) for v in seq if v is not None]


def _extract_publication_year(publication_year: Any, publication_date: Any) -> int | None:
    if isinstance(publication_year, int):
        return publication_year
    if isinstance(publication_year, str) and publication_year.isdigit():
        return int(publication_year)
    if isinstance(publication_date, str) and len(publication_date) >= 4:
        prefix = publication_date[:4]
        if prefix.isdigit():
            return int(prefix)
    return None


def _map_author_position(pos: Any) -> str:
    if not isinstance(pos, str):
        return ""
    pl = pos.lower()
    if pl == "first":
        return "f"
    if pl == "middle":
        return "m"
    if pl == "last":
        return "l"
    return pos


def _map_inst_type(inst_type: Any) -> str:
    if not isinstance(inst_type, str):
        return ""
    t = inst_type.lower()
    mapping = {
        "education": "e",
        "healthcare": "h",
        "company": "c",
        "archive": "a",
        "nonprofit": "n",
        "government": "g",
        "facility": "f",
        "other": "o",
    }
    return mapping.get(t, inst_type)


def _map_source_type(src_type: Any) -> str:
    if not isinstance(src_type, str):
        return src_type
    t = src_type.lower()
    mapping = {
        "journal": "j",
        "repository": "r",
        "conference": "c",
        "ebook platform": "e",
        "book series": "b",
        "metadata": "m",
        "other": "o",
    }
    return mapping.get(t, src_type)


def _log_error(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as log_file:
        log_file.write(message + "\n")

