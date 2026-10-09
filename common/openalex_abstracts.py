"""OpenAlex abstract reconstruction utilities.

OpenAlex represents abstracts as an inverted index mapping each token to every
position at which it occurs. Reconstruction must assign tokens to their absolute
positions; inserting into a growing list shifts earlier tokens and corrupts the
result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def decode_abstract_inverted_index(
    inverted_index: Mapping[str, Sequence[int]] | None,
) -> str | None:
    """Reconstruct an OpenAlex abstract without dropping repeated tokens.

    ``None`` denotes an unavailable abstract. Malformed indexes are rejected so
    ingestion cannot silently store another corrupted representation.
    """
    if inverted_index is None:
        return None
    if not isinstance(inverted_index, Mapping):
        raise TypeError("abstract_inverted_index must be a mapping or None")
    if not inverted_index:
        return ""

    words_by_position: dict[int, str] = {}
    for word, positions in inverted_index.items():
        if not isinstance(word, str):
            raise TypeError("abstract tokens must be strings")
        if not isinstance(positions, Sequence) or isinstance(positions, (str, bytes)):
            raise TypeError(f"positions for token {word!r} must be a sequence")
        for position in positions:
            if isinstance(position, bool) or not isinstance(position, int):
                raise TypeError(f"abstract position must be an integer: {position!r}")
            if position < 0:
                raise ValueError(f"abstract position must be non-negative: {position}")
            if position in words_by_position:
                raise ValueError(
                    "duplicate abstract position "
                    f"{position}: {words_by_position[position]!r} and {word!r}"
                )
            words_by_position[position] = word

    final_position = max(words_by_position)
    missing = [position for position in range(final_position + 1) if position not in words_by_position]
    if missing:
        preview = ", ".join(str(position) for position in missing[:10])
        suffix = "..." if len(missing) > 10 else ""
        raise ValueError(f"abstract inverted index has missing positions: {preview}{suffix}")

    return " ".join(words_by_position[position] for position in range(final_position + 1))


def abstract_from_openalex_work(work: Mapping[str, Any]) -> str | None:
    """Decode the abstract field from an OpenAlex work payload."""
    return decode_abstract_inverted_index(work.get("abstract_inverted_index"))
