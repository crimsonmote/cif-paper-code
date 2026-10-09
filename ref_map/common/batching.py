"""Small batching primitives shared across data pipelines."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TypeVar

T = TypeVar("T")


def chunked(iterable: Iterable[T], size: int) -> Iterator[list[T]]:
    """Yield ``iterable`` in non-empty lists of at most ``size`` items."""
    if size <= 0:
        raise ValueError("size must be positive")
    batch: list[T] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
