# Author: Xiaochen Kev Gao
import pandas as pd
import os
import json
import re
import tracemalloc
import asyncio
from functools import lru_cache
from decimal import Decimal
from bson import decimal128
from bson.codec_options import TypeCodec, TypeRegistry, CodecOptions
from pymongo import (
    AsyncMongoClient,
    ASCENDING,
    DESCENDING,
    HASHED,
    TEXT,
    DeleteOne,
)
from common.mongo import _resolve_mongo_uri, handle_pymongo_errors
from common.openalex_abstracts import decode_abstract_inverted_index
from ref_map.common.config import CANONICAL_FOUNDING_PATENT_PROPERTIES
from ref_map.common.founding_patent_seeds import cached_founding_patent_seed_index

tracemalloc.start()


# Raw inputs live under $SC_DATA_DIR (default: <repo>/data); see the README.
_DATA_DIR = os.environ.get(
    "SC_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
# Extracted OpenAlex snapshot (the analysis uses the release of 21 August 2025).
oa_decompress_folder = os.environ.get(
    "OPENALEX_SNAPSHOT_DIR", os.path.join(_DATA_DIR, "openalex")) + os.sep


class DecimalCodec(TypeCodec):
    python_type = Decimal  # the Python type acted upon by this type codec
    bson_type = decimal128.Decimal128  # the BSON type acted upon by this type codec

    def transform_python(self, value):
        # Function that transforms a custom type value into a type that BSON can encode.
        return decimal128.Decimal128(value)

    def transform_bson(self, value):
        # Function that transforms a vanilla BSON type value into our custom type.
        return value.to_decimal()


decimal_codec = DecimalCodec()
type_registry = TypeRegistry([decimal_codec])
codec_options = CodecOptions(type_registry=type_registry)


# Connection string from MONGO_URI or the OS keyring (see common/mongo.py).
client = AsyncMongoClient(_resolve_mongo_uri())
# Database name
db = client.get_database("openalex", codec_options=codec_options)


def strip_oaid(url):
    """
    Strip a URL of any suffixes.

    Given a URL, use a regular expression to search for a pattern that matches any
    suffixes. If a match is found, strip the suffix from the URL and return it.
    Otherwise, return None.

    Parameters
    ----------
    url : str
        The URL to strip of its suffix.

    Returns
    -------
    stripped_part : str or None
        The stripped suffix if a match is found, otherwise None.
    """
    pattern = r".*?(/[^/]+)$"
    match = re.search(pattern, url)
    if match:
        stripped_part = match.group(1).strip("/")
        return stripped_part
    return None


@lru_cache(maxsize=1)
def _load_matt_marx_lookups():
    """Load immutable paper/patent lookups once per ingest process."""
    print("Loading Matt Marx datasets...")
    pat_pap_startup = pd.read_csv(
        os.path.join(_DATA_DIR, "founding_patents", "_patent_paper_startup_triplets.csv")
    )
    pat_pap = pd.read_csv(
        os.path.join(_DATA_DIR, "reliance_on_science", "_patent_paper_pairs.csv")
    )
    pat_cit = pd.read_csv(
        os.path.join(_DATA_DIR, "reliance_on_science", "_pcs_oa.csv")
    )

    # Keep one deterministic representative only for legacy descriptive fields.
    # Boolean seed membership is derived separately with any-match semantics.
    patent_info = {}
    for row in pat_pap.to_dict("records"):
        paper_id = row.pop("paperid", None)
        if paper_id:
            patent_info.setdefault(paper_id, row)

    legacy_founding_info = {}
    for row in pat_pap_startup.to_dict("records"):
        paper_id = row.pop("paperid", None)
        row.pop("patent_id", None)
        row.pop("us_patent_id", None)
        if paper_id:
            legacy_founding_info.setdefault(paper_id, row)

    patent_citations = {}
    for row in pat_cit.to_dict("records"):
        mag_id = row.pop("oaid", None)
        if mag_id is not None:
            patent_citations.setdefault(mag_id, []).append(row)

    return (
        patent_info,
        legacy_founding_info,
        patent_citations,
        cached_founding_patent_seed_index(),
    )


def process_works(f_dictlist):
    """
    Process works by iterating over each entry in the list and performing the following operations:

    1. If the entry contains an "abstract_inverted_index" key, it will be converted to text and stored in the "abstract" key.
    2. If the entry contains an "id" key, it will be stripped of any suffixes and checked against the id_to_pat_pap and id_to_pat_pap_startup dictionaries.
       If the id exists in either dictionary, the corresponding information will be stored in the "matt_marx_paper_patent_v2" and "matt_marx_paper_patent_info" keys, or "matt_marx_paper_patent_startup" and "matt_marx_paper_patent_startup_info" respectively.
    """
    (
        id_to_pat_pap,
        id_to_pat_pap_startup,
        id_to_pat_cit,
        founding_seed_index,
    ) = _load_matt_marx_lookups()

    for ind, entry in enumerate(f_dictlist):
        if "abstract_inverted_index" in entry:
            if entry["abstract_inverted_index"] is not None:
                f_dictlist[ind]["abstract"] = decode_abstract_inverted_index(
                    entry["abstract_inverted_index"]
                )
            f_dictlist[ind].pop("abstract_inverted_index", None)

        if "id" in entry:
            openalex_id = strip_oaid(entry["id"])
            if openalex_id in id_to_pat_pap:
                f_dictlist[ind]["matt_marx_paper_patent_v2"] = True
                f_dictlist[ind]["matt_marx_paper_patent_info"] = id_to_pat_pap[
                    openalex_id
                ]
            else:
                f_dictlist[ind]["matt_marx_paper_patent_v2"] = False

            if openalex_id in id_to_pat_pap_startup:
                f_dictlist[ind]["matt_marx_paper_patent_startup"] = True
                f_dictlist[ind]["matt_marx_paper_patent_startup_info"] = (
                    id_to_pat_pap_startup[openalex_id]
                )
            else:
                f_dictlist[ind]["matt_marx_paper_patent_startup"] = False

            canonical_seed_values = founding_seed_index.property_values_for_work(
                openalex_id,
                entry.get("publication_date"),
                sparse=True,
            )
            for prop, value in canonical_seed_values.items():
                if value:
                    f_dictlist[ind][prop] = True

        if "ids" in entry and "mag" in entry["ids"]:
            mag_id = entry["ids"]["mag"]
            if mag_id in id_to_pat_cit:
                f_dictlist[ind]["matt_marx_cited_by_patent"] = True
                f_dictlist[ind]["matt_marx_paper_patent_citation_info"] = (
                    id_to_pat_cit[mag_id]
                )
            else:
                f_dictlist[ind]["matt_marx_cited_by_patent"] = False

    return f_dictlist


async def fresh_ingest():
    """
    Fresh ingest data from OpenAlex datasets into MongoDB.

    This function walks through all the subdirectories of oa_decompress_folder,
    reads all the JSON files in each subdirectory, processes them according to
    their domain, and then inserts them into the corresponding MongoDB collection.

    For each JSON file, it checks if the file belongs to the "works"
    domain. If it does, it processes the file using the process_works
    function. Finally, it inserts the processed data into the MongoDB
    collection.

    :param None: None
    :return None: None
    """
    for dirpath, dirs, files in os.walk(oa_decompress_folder):
        for file in files:
            if file.endswith(".json"):
                file_path = os.path.join(dirpath, file)
                domain = os.path.relpath(dirpath, oa_decompress_folder)
                domain = domain.strip("/")

                with open(file_path, "r") as f:
                    print("Reading " + domain + "/" + file + "...", end="", flush=True)
                    flines = f.readlines()
                    # f_dictlist = [ast.literal_eval(line) for line in f]
                    f_dictlist = [json.loads(line) for line in flines]
                    print("Done!")

                if domain == "works":
                    print(
                        "Processing " + domain + "/" + file + "...", end="", flush=True
                    )
                    f_dictlist = process_works(f_dictlist)
                    print("Done!")

                if len(f_dictlist) > 0:
                    print(
                        "Ingesting "
                        + file
                        + " into MongoDB collection "
                        + "["
                        + domain
                        + "]"
                        + "...",
                        end="",
                        flush=True,
                    )
                    await db[domain].insert_many(f_dictlist)
                    print("Done!")
                f_dictlist = None


async def convert_field_to_string(collection, field_name):
    """
    Converts a specific field to a string type for all documents in a collection.

    This function is efficient because it only targets documents where the
    specified field is not already a string.

    Args:
        collection: The PyMongo Collection object to update.
        field_name: The name of the field to convert to a string.

    Returns:
        The result object from update_many() on success, or None on failure.
    """
    # Filter for documents where the field is not already a string
    filter_query = {field_name: {"$not": {"$type": "string"}}}

    # Use an aggregation pipeline with $toString to perform the conversion
    update_pipeline = [{"$set": {field_name: {"$toString": f"${field_name}"}}}]

    # Execute the bulk update operation
    result1 = await collection.update_many(filter_query, update_pipeline)
    result2 = await collection.update_many(
        {field_name: None}, {"$unset": {field_name: ""}}
    )
    return (result1, result2) if result1 or result2 else None


async def create_mongo_index(
    domain,
    index_field,
    index_type=ASCENDING,
    unique=False,
    sparse=True,
    to_convert=False,
    **kwargs,
):
    """
    Create an index on a specified field in a specified MongoDB collection.

    Args:
        domain: The name of the MongoDB collection to create the index on.
        index_field: The name of the field to create the index on.
        index_type: The type of index to create (ASCENDING, DESCENDING, HASHED, TEXT, etc.).
        unique: If True, the index will be created as a unique index. If False, the index will not be unique.
        sparse: If True, the index will be created as a sparse index. If False, the index will not be sparse.

    Returns:
        0 if the index was created successfully, 1 if the index was created after converting the field to a string, -1 if an error occurred.
    """
    print(
        f"Starting to create an index on field '{index_field}' in collection '{domain}' with type '{index_type}'..."
    )
    with handle_pymongo_errors():
        if isinstance(index_field, list):
            indexes = list(zip(index_field, index_type))
            await db[domain].create_index(
                indexes, unique=unique, sparse=sparse, background=False, **kwargs
            )
        else:
            await db[domain].create_index(
                [(index_field, index_type)],
                unique=unique,
                sparse=sparse,
                background=False,
                **kwargs,
            )
        print(
            f"✅ Success to create an index on field '{index_field}' in collection '{domain}' with type '{index_type}'!"
        )
        return 0
    if to_convert:
        with handle_pymongo_errors():
            print(
                f"Attempting to convert field '{index_field}' in collection '{domain}' to string..."
            )
            if isinstance(index_field, list):
                for field in index_field:
                    await convert_field_to_string(db[domain], field)
            else:
                await convert_field_to_string(db[domain], index_field)
            print("✅ Success! Documents modified.")
            if isinstance(index_field, list):
                indexes = list(zip(index_field, index_type))
                await db[domain].create_index(
                    indexes, unique=unique, sparse=sparse, background=False, **kwargs
                )
            else:
                await db[domain].create_index(
                    [(index_field, index_type)],
                    unique=unique,
                    sparse=sparse,
                    background=False,
                    **kwargs,
                )
            print(
                f"✅ Success to create an index on field '{index_field}' in collection '{domain}' with type '{index_type}' after converting the field to string!"
            )
            return 1

    print(
        f"❌ Failed to create an index on field '{index_field}' in collection '{domain}' with type '{index_type}'!"
    )
    return -1


async def create_all_indexes(check_dups=False, check_language_override=True):
    """
    Creates various indexes on the MongoDB collections for faster querying.

    Args:
        check_dups: If True, checks and deletes duplicate documents in all collections.
    """
    collections = [
        "domains",
        # "topics",
        # "fields",
        # "subfields",
        # "keywords",
        "works",
        "authors",
        "sources",
        "institutions",
        "publishers",
        "funders",
    ]

    index_tasks = []
    for collection in collections:
        if check_dups:
            print(
                "Checking and deleting duplicate documents for [" + collection + "]...",
                end="",
                flush=True,
            )
            pipeline = [
                {
                    "$group": {
                        "_id": "$id",
                        "count": {"$count": {}},
                        "dups": {"$push": "$_id"},
                    }
                },
                {"$match": {"count": {"$gte": 2}}},
            ]

            dup_ops = []
            for document in collection.aggregate(pipeline, allowDiskUse=True):
                it = iter(document["dups"])
                next(it)
                for id in it:
                    dup_ops.append(DeleteOne({"_id": id}))
            db[collection].bulk_write(dup_ops)
            print("Done!")

        index_tasks.append(
            create_mongo_index(collection, "id", ASCENDING, unique=True, sparse=False)
        )
        if collection != "works" and collection != "sources":
            index_tasks.append(
                create_mongo_index(
                    collection, "display_name", TEXT, unique=False, sparse=False
                )
            )

    index_tasks.append(
        create_mongo_index(
            "topics",
            "subfield.id",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "topics",
            "field.id",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "topics",
            "domain.id",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "subfields",
            "topics.id",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "subfields",
            "field.id",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "subfields",
            "domain.id",
            ASCENDING,
        )
    )

    index_tasks.append(
        create_mongo_index(
            "fields",
            "subfields.id",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "fields",
            "domain.id",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "domains",
            "fields.id",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "sources",
            "abbreviated_title",
            TEXT,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "authors",
            "orcid",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "authors",
            "cited_by_count",
            DESCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "works",
            "cited_by_count",
            DESCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "works",
            "doi",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "works",
            "ids.mag",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "works",
            "published_date",
            HASHED,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "works",
            "topics.id",
            ASCENDING,
        )
    )
    # index_tasks.append(
    #     create_mongo_index(
    #         "works",
    #         "topics.display_name",
    #         TEXT,
    #     )
    # )
    index_tasks.append(
        create_mongo_index(
            "works",
            "keywords.id",
            ASCENDING,
        )
    )
    # index_tasks.append(
    #     create_mongo_index(
    #         "works",
    #         "keywords.display_name",
    #         TEXT,
    #     )
    # )
    index_tasks.append(
        create_mongo_index(
            "works",
            "referenced_works",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "works",
            "matt_marx_paper_patent_v2",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "works",
            "matt_marx_paper_patent_startup",
            ASCENDING,
        )
    )
    for canonical_seed_prop in CANONICAL_FOUNDING_PATENT_PROPERTIES:
        index_tasks.append(
            create_mongo_index(
                "works",
                canonical_seed_prop,
                ASCENDING,
            )
        )
    index_tasks.append(
        create_mongo_index(
            "works",
            "matt_marx_cited_by_patent",
            ASCENDING,
        )
    )
    index_tasks.append(
        create_mongo_index(
            "works",
            "authorships.author.id",
            ASCENDING,
        )
    )
    # index_tasks.append(
    #     create_mongo_index(
    #         "works",
    #         "authorships.author.display_name",
    #         TEXT,
    #     )
    # )
    index_tasks.append(
        create_mongo_index(
            "works",
            "authorships.institutions.id",
            ASCENDING,
        )
    )
    # index_tasks.append(
    #     create_mongo_index(
    #         "works",
    #         "authorships.institutions.display_name",
    #         TEXT,
    #     )
    # )
    valid_language_codes = [
        "da",  # Danish
        "nl",  # Dutch
        "en",  # English
        "fi",  # Finnish
        "fr",  # French
        "de",  # German
        "hu",  # Hungarian
        "it",  # Italian
        "nb",  # Norwegian
        "pt",  # Portuguese
        "ro",  # Romanian
        "ru",  # Russian
        "es",  # Spanish
        "sv",  # Swedish
        "tr",  # Turkish
    ]
    index_tasks.append(
        create_mongo_index(
            "works",
            ["title", "abstract"],
            [TEXT, TEXT],
            sparse=False,
            partialFilterExpression={"language": {"$in": valid_language_codes}},
        )
    )

    if check_language_override:
        print(
            "Checking and fixing language override field for [works]...",
            end="",
            flush=True,
        )
        await db["works"].update_many({"language": None}, {"$unset": {"language": ""}})
        print("Done!")

    await asyncio.gather(*index_tasks)


async def process_merged_ids():
    """
    Process merged IDs from the merged_ids folder.

    This function will remove merged entities from OpenAlex collections.
    It works by iterating over the merged_ids folder, reading the CSV files,
    and finding the corresponding documents in the OpenAlex database. It will then
    delete the documents from the database.

    Args:
        None

    Returns:
        None
    """
    print("Processing merged IDs...")
    to_remove = {}
    with os.scandir(oa_decompress_folder + "merged_ids") as entries:
        for entry in entries:
            domain = entry.name.split("_")[0]
            if domain not in to_remove:
                to_remove[domain] = []
            collection = db[domain]
            df = pd.read_csv(entry.path)

            for _, row in df.iterrows():
                full_id = "https://openalex.org/" + row["id"]
                full_merged_into_id = "https://openalex.org/" + row["merge_into_id"]
                if await collection.find_one({"id": full_merged_into_id}, {"_id": 1}) is None:
                    continue
                async for id_result in collection.find({"id": full_id}, {"_id": 1}):
                    to_remove[domain].append(id_result["_id"])

    for domain, id_list in to_remove.items():
        if len(id_list) > 0:
            print(f"Removing {len(id_list)} merged entities from {domain}...")
            collection = db[domain]
            await collection.delete_many({"_id": {"$in": id_list}})
            print("Done!")
        else:
            print(f"No merged entities to remove from {domain}.")


STEPS = ("ingest", "merged", "indexes")


async def main(steps=STEPS):
    """Load the snapshot into MongoDB, drop merged entities, and build indexes."""
    if "ingest" in steps:
        await fresh_ingest()
    if "merged" in steps:
        await process_merged_ids()
    if "indexes" in steps:
        await create_all_indexes(check_language_override=False)
    await client.close()


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Ingest the OpenAlex snapshot into MongoDB.")
    ap.add_argument("--steps", default=",".join(STEPS),
                    help=f"Comma-separated subset of {', '.join(STEPS)} (default: all, in order).")
    asyncio.run(main(tuple(x.strip() for x in ap.parse_args().steps.split(","))))
