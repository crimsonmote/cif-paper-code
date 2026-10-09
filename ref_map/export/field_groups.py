from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from common.mongo import handle_pymongo_errors
from ref_map.common.config import (
    CANONICAL_FOUNDING_PATENT_INDEX_NAMES,
    CANONICAL_FOUNDING_PATENT_PROPERTIES,
)
from ref_map.common.founding_patent_seeds import (
    DEFAULT_FOUNDING_PATENT_TRIPLETS,
    cached_founding_patent_seed_index,
)
from ref_map.export.shared import (
    FUNDERS_COLLECTION_NAME,
    _ensure_list,
    _extract_publication_year,
    _fill_nones,
    _map_author_position,
    _map_inst_type,
    _map_source_type,
    _strip_openalex,
    _strip_openalex_list,
    _to_json_or_none,
)

ALL_GROUPS = ("core", "grants", "grant_enrichment", "topics", "display_fields")
BASE_EXPORT_GROUPS = ("core", "grants", "grant_enrichment", "topics")

GROUP_PROJECTIONS: dict[str, dict[str, int]] = {
    "core": {
        "id": 1,
        "title": 1,
        "doi": 1,
        "publication_date": 1,
        "publication_year": 1,
        "type": 1,
        "referenced_works": 1,
        "authorships": 1,
        "primary_location": 1,
        "locations": 1,
        "matt_marx_paper_patent_v2": 1,
        "matt_marx_paper_patent_startup": 1,
        **{prop: 1 for prop in CANONICAL_FOUNDING_PATENT_PROPERTIES},
        "matt_marx_cited_by_patent": 1,
        "matt_marx_paper_patent_info": 1,
        "matt_marx_paper_patent_startup_info": 1,
        "matt_marx_paper_patent_citation_info": 1,
        "corresponding_author_ids": 1,
        "corresponding_institution_ids": 1,
    },
    "grants": {
        "id": 1,
        "grants": 1,
    },
    "grant_enrichment": {
        "id": 1,
        "grants": 1,
    },
    "topics": {
        "id": 1,
        "primary_topic": 1,
        "topics": 1,
    },
    "display_fields": {
        "id": 1,
        "authorships": 1,
        "primary_location": 1,
        "locations": 1,
        "grants": 1,
        "primary_topic": 1,
        "topics": 1,
    },
}

GROUP_ASSIGNMENTS: dict[str, list[str]] = {
    "core": [
        "w.doi = row.doi",
        "w.title = row.title",
        "w.publication_date = row.publication_date",
        "w.publication_year = row.publication_year",
        "w.type = row.type",
        "w.author_ids = row.author_ids",
        "w.author_positions = row.author_positions",
        "w.inst_ids = row.inst_ids",
        "w.inst_types = row.inst_types",
        "w.primary_source_id = row.primary_source_id",
        "w.primary_source_type = row.primary_source_type",
        "w.location_source_ids = row.location_source_ids",
        "w.location_source_types = row.location_source_types",
        "w.matt_marx_paper_patent_v2 = row.matt_marx_paper_patent_v2",
        "w.matt_marx_paper_patent_startup = row.matt_marx_paper_patent_startup",
        *[
            f"w.`{prop}` = row.`{prop}`"
            for prop in CANONICAL_FOUNDING_PATENT_PROPERTIES
        ],
        "w.matt_marx_cited_by_patent = row.matt_marx_cited_by_patent",
        "w.matt_marx_paper_patent_info = row.matt_marx_paper_patent_info_json",
        "w.matt_marx_paper_patent_startup_info = row.matt_marx_paper_patent_startup_info_json",
        "w.matt_marx_paper_patent_citation_info = row.matt_marx_paper_patent_citation_info_json",
        "w.corresponding_author_ids = row.corresponding_author_ids",
        "w.corresponding_institution_ids = row.corresponding_institution_ids",
    ],
    "grants": [
        "w.funder_ids = row.funder_ids",
    ],
    "grant_enrichment": [
        "w.funder_derived_classes = row.funder_derived_classes",
        "w.funder_crossref_types = row.funder_crossref_types",
        "w.funder_crossref_subtypes = row.funder_crossref_subtypes",
        "w.has_private_funder = row.has_private_funder",
        "w.has_public_funder = row.has_public_funder",
    ],
    "topics": [
        "w.primary_topic_id = row.primary_topic_id",
        "w.primary_subfield_id = row.primary_subfield_id",
        "w.primary_field_id = row.primary_field_id",
        "w.primary_domain_id = row.primary_domain_id",
        "w.topic_ids = row.topic_ids",
        "w.topic_subfield_ids = row.topic_subfield_ids",
        "w.topic_field_ids = row.topic_field_ids",
        "w.topic_domain_ids = row.topic_domain_ids",
    ],
    "display_fields": [
        "w.author_names = row.author_names",
        "w.inst_names = row.inst_names",
        "w.primary_source_name = row.primary_source_name",
        "w.location_source_names = row.location_source_names",
        "w.funder_names = row.funder_names",
        "w.primary_topic_name = row.primary_topic_name",
        "w.primary_subfield_name = row.primary_subfield_name",
        "w.primary_field_name = row.primary_field_name",
        "w.primary_domain_name = row.primary_domain_name",
        "w.topic_names = row.topic_names",
        "w.topic_subfield_names = row.topic_subfield_names",
        "w.topic_field_names = row.topic_field_names",
        "w.topic_domain_names = row.topic_domain_names",
    ],
}

GROUP_PROPERTIES: dict[str, list[str]] = {
    "core": [
        "doi",
        "title",
        "publication_date",
        "publication_year",
        "type",
        "author_ids",
        "author_positions",
        "inst_ids",
        "inst_types",
        "primary_source_id",
        "primary_source_type",
        "location_source_ids",
        "location_source_types",
        "matt_marx_paper_patent_v2",
        "matt_marx_paper_patent_startup",
        *CANONICAL_FOUNDING_PATENT_PROPERTIES,
        "matt_marx_cited_by_patent",
        "matt_marx_paper_patent_info",
        "matt_marx_paper_patent_startup_info",
        "matt_marx_paper_patent_citation_info",
        "corresponding_author_ids",
        "corresponding_institution_ids",
    ],
    "grants": [
        "funder_ids",
    ],
    "grant_enrichment": [
        "funder_derived_classes",
        "funder_crossref_types",
        "funder_crossref_subtypes",
        "has_private_funder",
        "has_public_funder",
    ],
    "topics": [
        "primary_topic_id",
        "primary_subfield_id",
        "primary_field_id",
        "primary_domain_id",
        "topic_ids",
        "topic_subfield_ids",
        "topic_field_ids",
        "topic_domain_ids",
    ],
    "display_fields": [
        "author_names",
        "inst_names",
        "primary_source_name",
        "location_source_names",
        "funder_names",
        "primary_topic_name",
        "primary_subfield_name",
        "primary_field_name",
        "primary_domain_name",
        "topic_names",
        "topic_subfield_names",
        "topic_field_names",
        "topic_domain_names",
    ],
}

GROUP_INDEX_CYPHERS: dict[str, list[str]] = {
    "core": [
        f"CREATE INDEX {index_name} IF NOT EXISTS FOR (w:Work) ON (w.`{prop}`)"
        for index_name, prop in zip(
            CANONICAL_FOUNDING_PATENT_INDEX_NAMES,
            CANONICAL_FOUNDING_PATENT_PROPERTIES,
            strict=True,
        )
    ],
    "grant_enrichment": [
        "CREATE INDEX work_has_public_funder IF NOT EXISTS FOR (w:Work) ON (w.has_public_funder)",
        "CREATE INDEX work_has_private_funder IF NOT EXISTS FOR (w:Work) ON (w.has_private_funder)",
    ],
    "topics": [
        "CREATE INDEX work_primary_topic_id IF NOT EXISTS FOR (w:Work) ON (w.primary_topic_id)",
        "CREATE INDEX work_primary_subfield_id IF NOT EXISTS FOR (w:Work) ON (w.primary_subfield_id)",
        "CREATE INDEX work_primary_field_id IF NOT EXISTS FOR (w:Work) ON (w.primary_field_id)",
        "CREATE INDEX work_primary_domain_id IF NOT EXISTS FOR (w:Work) ON (w.primary_domain_id)",
    ],
}


def normalize_groups(groups: list[str] | None) -> list[str]:
    if not groups:
        return list(ALL_GROUPS)
    normalized: list[str] = []
    for group in groups:
        token = group.strip().lower()
        if not token:
            continue
        if token == "all":
            return list(ALL_GROUPS)
        if token not in GROUP_PROJECTIONS:
            raise ValueError(f"Unknown field group: {group}")
        if token not in normalized:
            normalized.append(token)
    return normalized


def default_export_groups(include_display_fields: bool = True) -> list[str]:
    if include_display_fields:
        return list(ALL_GROUPS)
    return list(BASE_EXPORT_GROUPS)


def projection_for_groups(groups: list[str]) -> dict[str, int]:
    projection: dict[str, int] = {}
    for group in groups:
        projection.update(GROUP_PROJECTIONS[group])
    return projection


def write_assignments_for_groups(groups: list[str]) -> list[str]:
    assignments: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for assignment in GROUP_ASSIGNMENTS[group]:
            if assignment in seen:
                continue
            seen.add(assignment)
            assignments.append(assignment)
    return assignments


def properties_for_groups(groups: list[str]) -> list[str]:
    properties: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for prop in GROUP_PROPERTIES[group]:
            if prop in seen:
                continue
            seen.add(prop)
            properties.append(prop)
    return properties


def ensure_indexes_for_groups(session, groups: list[str]) -> None:
    for group in groups:
        for cypher in GROUP_INDEX_CYPHERS.get(group, []):
            session.run(cypher)


def build_group_context(db, docs: list[Mapping[str, Any]], groups: list[str]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    if "core" in groups and DEFAULT_FOUNDING_PATENT_TRIPLETS.exists():
        context["founding_patent_seed_index"] = cached_founding_patent_seed_index()
    if "grant_enrichment" in groups:
        context["grant_enrichment"] = _fetch_grant_enrichment(db, docs)
    return context


def shape_work_for_groups(
    doc: Mapping[str, Any],
    groups: list[str],
    context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    work_id = _strip_openalex(doc.get("id"))
    if not work_id:
        return None

    result: dict[str, Any] = {"id": work_id}
    if "core" in groups:
        result.update(_shape_core_fields(doc, context or {}))
    if "grants" in groups:
        result.update(_shape_grants_fields(doc))
    if "grant_enrichment" in groups:
        result.update(_shape_grant_enrichment_fields(doc, context or {}))
    if "topics" in groups:
        result.update(_shape_topics_fields(doc))
    if "display_fields" in groups:
        result.update(_shape_display_fields(doc))
    return result


def _shape_core_fields(
    doc: Mapping[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    refs = _strip_openalex_list([ref for ref in _ensure_list(doc.get("referenced_works")) if ref])

    author_ids: list[Any] = []
    author_positions: list[Any] = []
    inst_ids: list[Any] = []
    inst_types: list[Any] = []

    for authorship in _ensure_list(doc.get("authorships")):
        if not isinstance(authorship, Mapping):
            continue
        author = authorship.get("author") or {}
        author_ids.append(_strip_openalex(author.get("id")))
        author_positions.append(_map_author_position(authorship.get("author_position")))
        for inst in _ensure_list(authorship.get("institutions")):
            if not isinstance(inst, Mapping):
                continue
            inst_ids.append(_strip_openalex(inst.get("id")))
            inst_types.append(_map_inst_type(inst.get("type")))

    primary_source = (doc.get("primary_location") or {}).get("source") or {}
    primary_source_id = _strip_openalex(primary_source.get("id"))
    primary_source_type = _map_source_type(primary_source.get("type"))

    location_source_ids: list[Any] = []
    location_source_types: list[Any] = []
    for location in _ensure_list(doc.get("locations")):
        if not isinstance(location, Mapping):
            continue
        src = location.get("source") or {}
        src_id = src.get("id")
        if not src_id:
            continue
        location_source_ids.append(_strip_openalex(src_id))
        location_source_types.append(_map_source_type(src.get("type")))

    work_id = _strip_openalex(doc.get("id"))
    canonical_seed_values = {
        prop: doc.get(prop) for prop in CANONICAL_FOUNDING_PATENT_PROPERTIES
    }
    seed_index = context.get("founding_patent_seed_index")
    if seed_index is not None and work_id:
        canonical_seed_values = seed_index.property_values_for_work(
            work_id,
            doc.get("publication_date"),
            sparse=True,
        )

    return {
        "title": doc.get("title"),
        "doi": doc.get("doi"),
        "publication_date": doc.get("publication_date"),
        "publication_year": _extract_publication_year(
            doc.get("publication_year"),
            doc.get("publication_date"),
        ),
        "type": doc.get("type"),
        "refs": refs,
        "author_ids": _fill_nones(author_ids),
        "author_positions": _fill_nones(author_positions),
        "inst_ids": _fill_nones(inst_ids),
        "inst_types": _fill_nones(inst_types),
        "primary_source_id": primary_source_id,
        "primary_source_type": primary_source_type,
        "location_source_ids": _fill_nones(location_source_ids),
        "location_source_types": _fill_nones(location_source_types),
        "matt_marx_paper_patent_v2": doc.get("matt_marx_paper_patent_v2"),
        "matt_marx_paper_patent_startup": doc.get("matt_marx_paper_patent_startup"),
        **canonical_seed_values,
        "matt_marx_cited_by_patent": doc.get("matt_marx_cited_by_patent"),
        "matt_marx_paper_patent_info_json": _to_json_or_none(doc.get("matt_marx_paper_patent_info")),
        "matt_marx_paper_patent_startup_info_json": _to_json_or_none(
            doc.get("matt_marx_paper_patent_startup_info")
        ),
        "matt_marx_paper_patent_citation_info_json": _to_json_or_none(
            doc.get("matt_marx_paper_patent_citation_info")
        ),
        "corresponding_author_ids": _fill_nones(
            _strip_openalex_list(doc.get("corresponding_author_ids") or [])
        ),
        "corresponding_institution_ids": _fill_nones(
            _strip_openalex_list(doc.get("corresponding_institution_ids") or [])
        ),
    }


def _shape_grants_fields(doc: Mapping[str, Any]) -> dict[str, Any]:
    funder_ids: list[Any] = []
    for grant in _iter_grants(doc):
        funder_id = _extract_grant_funder_id(grant)
        funder_ids.append(funder_id)

    return {
        "funder_ids": _fill_nones(funder_ids),
    }


def _shape_grant_enrichment_fields(
    doc: Mapping[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    enrichment = context.get("grant_enrichment", {})
    funder_derived_classes: list[Any] = []
    funder_crossref_types: list[Any] = []
    funder_crossref_subtypes: list[Any] = []
    for grant in _iter_grants(doc):
        funder_id = _extract_grant_funder_id(grant)
        info = enrichment.get(funder_id, {})
        funder_derived_classes.append(info.get("derived_funder_class") or "")
        funder_crossref_types.append(info.get("raw_crossref_type") or "")
        funder_crossref_subtypes.append(info.get("raw_crossref_subtype") or "")

    return {
        "funder_derived_classes": _fill_nones(funder_derived_classes),
        "funder_crossref_types": _fill_nones(funder_crossref_types),
        "funder_crossref_subtypes": _fill_nones(funder_crossref_subtypes),
        "has_private_funder": any(
            isinstance(v, str) and v.lower().startswith("private_")
            for v in funder_derived_classes
        ),
        "has_public_funder": any(
            isinstance(v, str) and v.lower().startswith("public_")
            for v in funder_derived_classes
        ),
    }


def _shape_topics_fields(doc: Mapping[str, Any]) -> dict[str, Any]:

    primary_topic = doc.get("primary_topic") or {}
    primary_topic_id = _strip_openalex(primary_topic.get("id"))
    primary_subfield = primary_topic.get("subfield") or {}
    primary_subfield_id = _strip_openalex(primary_subfield.get("id"))
    primary_field = primary_topic.get("field") or {}
    primary_field_id = _strip_openalex(primary_field.get("id"))
    primary_domain = primary_topic.get("domain") or {}
    primary_domain_id = _strip_openalex(primary_domain.get("id"))

    topic_ids: list[Any] = []
    topic_subfield_ids: list[Any] = []
    topic_field_ids: list[Any] = []
    topic_domain_ids: list[Any] = []
    for topic in _ensure_list(doc.get("topics")):
        if not isinstance(topic, Mapping):
            continue
        topic_ids.append(_strip_openalex(topic.get("id")))
        subfield = topic.get("subfield") or {}
        topic_subfield_ids.append(_strip_openalex(subfield.get("id")))
        field = topic.get("field") or {}
        topic_field_ids.append(_strip_openalex(field.get("id")))
        domain = topic.get("domain") or {}
        topic_domain_ids.append(_strip_openalex(domain.get("id")))

    return {
        "primary_topic_id": primary_topic_id,
        "primary_subfield_id": primary_subfield_id,
        "primary_field_id": primary_field_id,
        "primary_domain_id": primary_domain_id,
        "topic_ids": _fill_nones(topic_ids),
        "topic_subfield_ids": _fill_nones(topic_subfield_ids),
        "topic_field_ids": _fill_nones(topic_field_ids),
        "topic_domain_ids": _fill_nones(topic_domain_ids),
    }

def _shape_display_fields(doc: Mapping[str, Any]) -> dict[str, Any]:
    author_names: list[Any] = []
    inst_names: list[Any] = []
    for authorship in _ensure_list(doc.get("authorships")):
        if not isinstance(authorship, Mapping):
            continue
        author = authorship.get("author") or {}
        author_names.append(author.get("display_name"))
        for inst in _ensure_list(authorship.get("institutions")):
            if not isinstance(inst, Mapping):
                continue
            inst_names.append(inst.get("display_name"))

    primary_source = (doc.get("primary_location") or {}).get("source") or {}
    primary_source_name = primary_source.get("display_name")

    location_source_names: list[Any] = []
    for location in _ensure_list(doc.get("locations")):
        if not isinstance(location, Mapping):
            continue
        src = location.get("source") or {}
        if src.get("id"):
            location_source_names.append(src.get("display_name"))

    funder_names: list[Any] = []
    for grant in _iter_grants(doc):
        funder_names.append(grant.get("funder_display_name"))

    primary_topic = doc.get("primary_topic") or {}
    primary_topic_name = primary_topic.get("display_name")
    primary_subfield = primary_topic.get("subfield") or {}
    primary_subfield_name = primary_subfield.get("display_name")
    primary_field = primary_topic.get("field") or {}
    primary_field_name = primary_field.get("display_name")
    primary_domain = primary_topic.get("domain") or {}
    primary_domain_name = primary_domain.get("display_name")

    topic_names: list[Any] = []
    topic_subfield_names: list[Any] = []
    topic_field_names: list[Any] = []
    topic_domain_names: list[Any] = []
    for topic in _ensure_list(doc.get("topics")):
        if not isinstance(topic, Mapping):
            continue
        topic_names.append(topic.get("display_name"))
        subfield = topic.get("subfield") or {}
        topic_subfield_names.append(subfield.get("display_name"))
        field = topic.get("field") or {}
        topic_field_names.append(field.get("display_name"))
        domain = topic.get("domain") or {}
        topic_domain_names.append(domain.get("display_name"))

    return {
        "author_names": _fill_nones(author_names),
        "inst_names": _fill_nones(inst_names),
        "primary_source_name": primary_source_name,
        "location_source_names": _fill_nones(location_source_names),
        "funder_names": _fill_nones(funder_names),
        "primary_topic_name": primary_topic_name,
        "primary_subfield_name": primary_subfield_name,
        "primary_field_name": primary_field_name,
        "primary_domain_name": primary_domain_name,
        "topic_names": _fill_nones(topic_names),
        "topic_subfield_names": _fill_nones(topic_subfield_names),
        "topic_field_names": _fill_nones(topic_field_names),
        "topic_domain_names": _fill_nones(topic_domain_names),
    }


def _fetch_grant_enrichment(
    db,
    docs: list[Mapping[str, Any]],
) -> dict[str, dict[str, str]]:
    funder_ids: set[str] = set()
    for doc in docs:
        for grant in _iter_grants(doc):
            funder_id = _extract_grant_funder_id(grant)
            if funder_id:
                funder_ids.add(funder_id)

    if not funder_ids:
        return {}

    coll = db[FUNDERS_COLLECTION_NAME]
    projection = {
        "id": 1,
        "derived_funder_class": 1,
        "raw_crossref_type": 1,
        "raw_crossref_subtype": 1,
    }
    # Support both storage styles for funders.id in Mongo:
    # - full URL: https://openalex.org/F...
    # - stripped: F...
    query_ids_full = [f"https://openalex.org/{funder_id}" for funder_id in funder_ids]
    query_ids_stripped = list(funder_ids)
    query_ids = query_ids_full + query_ids_stripped
    result: dict[str, dict[str, str]] = {}
    with handle_pymongo_errors():
        for doc in coll.find({"id": {"$in": query_ids}}, projection):
            funder_id = _strip_openalex(doc.get("id"))
            if not funder_id:
                continue
            result[funder_id] = {
                "derived_funder_class": doc.get("derived_funder_class") or "",
                "raw_crossref_type": doc.get("raw_crossref_type") or "",
                "raw_crossref_subtype": doc.get("raw_crossref_subtype") or "",
            }
    return result


def _iter_grants(doc: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for grant in _ensure_list(doc.get("grants")):
        if isinstance(grant, Mapping):
            out.append(grant)
    return out


def _extract_grant_funder_id(grant: Mapping[str, Any]) -> str:
    return _strip_openalex(grant.get("funder")) or ""
