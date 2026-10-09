"""Shared safety helpers for long-running GDS suites."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


@contextmanager
def exclusive_run_lock(path: Path, *, run_name: str):
    """Prevent concurrent local runs that could race on graph or properties."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "unknown"
            raise RuntimeError(
                f"Another {run_name} run is already active "
                f"(lock owner PID: {owner}). Wait for it to finish."
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def active_gds_jobs(session) -> list[dict[str, object]]:
    """Return currently running or pending GDS jobs."""
    return session.run(
        """
        CALL gds.listProgress()
        YIELD jobId, taskName, progress, status
        WHERE status IN ['RUNNING', 'PENDING']
        RETURN jobId, taskName, progress, status
        ORDER BY jobId
        """
    ).data()


def assert_no_active_gds_jobs(session) -> None:
    """Refuse to start another large GDS operation while one is active."""
    jobs = active_gds_jobs(session)
    if not jobs:
        return
    details = "; ".join(
        f"{job.get('jobId')} ({job.get('taskName')}, "
        f"{job.get('status')}, {job.get('progress')})"
        for job in jobs
    )
    raise RuntimeError(
        "Another GDS job is active: "
        f"{details}. Wait for it to finish, or explicitly allow concurrent "
        "GDS work only when the overlap is intentional."
    )


def assert_gds_graph_exists(session, graph_name: str, *, stage: str) -> None:
    """Detect catalog races that drop a suite's temporary graph."""
    record = session.run(
        """
        CALL gds.graph.exists($graph_name)
        YIELD exists
        RETURN exists
        """,
        {"graph_name": graph_name},
    ).single()
    if not record or not record["exists"]:
        raise RuntimeError(
            f"Temporary GDS graph {graph_name!r} disappeared {stage}. "
            "Another process may have dropped or replaced the catalog graph."
        )
