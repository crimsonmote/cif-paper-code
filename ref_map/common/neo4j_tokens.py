"""Validation for identifiers interpolated into Cypher queries."""

from __future__ import annotations

import re

_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PROPERTY_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def validate_token(value: str, *, kind: str = "Neo4j token") -> str:
    token = (value or "").strip()
    if not _TOKEN.fullmatch(token):
        raise ValueError(f"Invalid {kind}: {value!r}")
    return token


def validate_property_token(value: str) -> str:
    return validate_token(value, kind="Neo4j property token")


def validate_property_key(value: str) -> str:
    """Validate a backtick-quoted property key, including damping decimals."""
    key = (value or "").strip()
    if not _PROPERTY_KEY.fullmatch(key):
        raise ValueError(f"Invalid Neo4j property key: {value!r}")
    return key
