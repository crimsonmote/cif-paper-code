"""Audited wrappers for generic APOC batching operations."""

from __future__ import annotations

from typing import Any


def apoc_available(session) -> bool:
    """Return whether APOC procedures are callable in the current database."""
    try:
        record = session.run("RETURN apoc.version() AS version").single()
        return bool(record and record.get("version"))
    except Exception:
        return False


def run_apoc_query_iterate(
    session,
    *,
    source_query: str,
    action_query: str,
    batch_size: int,
    parallel: bool = False,
    retries: int = 3,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ``apoc.periodic.iterate`` and reject partial write failures."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    result = session.run(
        """
        CALL apoc.periodic.iterate(
            $source,
            $action,
            $config
        )
        YIELD total, batches, committedOperations,
              failedOperations, failedBatches, errorMessages
        RETURN total, batches, committedOperations,
               failedOperations, failedBatches, errorMessages
        """,
        {
            "source": source_query,
            "action": action_query,
            "config": {
                "batchSize": int(batch_size),
                "parallel": bool(parallel),
                "retries": int(retries),
                "params": params or {},
            },
        },
    ).single()
    if result is None:
        raise RuntimeError("APOC periodic iterate returned no result")

    summary = dict(result)
    if (
        int(summary.get("failedOperations") or 0) > 0
        or int(summary.get("failedBatches") or 0) > 0
    ):
        raise RuntimeError(
            "APOC periodic iterate failed: "
            f"failedOperations={summary.get('failedOperations')}, "
            f"failedBatches={summary.get('failedBatches')}, "
            f"errors={summary.get('errorMessages')}"
        )
    return summary


def run_apoc_rows_iterate(
    session,
    source_query: str,
    action_query: str,
    rows: list[dict[str, Any]],
    batch_size: int,
    parallel: bool = True,
    retries: int = 3,
) -> dict[str, Any]:
    """Run an APOC iterate operation whose source unwinds supplied rows."""
    return run_apoc_query_iterate(
        session,
        source_query=source_query,
        action_query=action_query,
        batch_size=batch_size,
        parallel=parallel,
        retries=retries,
        params={"rows": rows},
    )
