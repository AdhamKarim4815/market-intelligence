"""Job records and the lock that stops two runs colliding.

The lock is a row whose primary key is the job name, so taking it is a
single insert and the conflict *is* the answer. A select-then-insert would
have a window between the two in which both processes see it free — small,
and hit in practice by a cron job and a manual run started in the same
second.

A crashed process cannot release its own lock, so the lock carries a
heartbeat and a stale one can be taken over. The timeout is deliberately
generous: stealing a lock from a job that is merely slow is worse than
waiting, because two concurrent refreshes both writing prices is exactly
what the lock exists to prevent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from akmarkets.db.base import new_id, utc_now
from akmarkets.db.models import JobLockRow, JobRunRow

# How long a lock survives without a heartbeat before another process may
# take it. Longer than any stage should take; short enough that a killed
# GitHub Actions run does not block tonight's schedule too.
STALE_AFTER = timedelta(minutes=30)


class JobBusy(RuntimeError):
    """Another run holds this job's lock and is still alive."""


@dataclass(frozen=True)
class JobHandle:
    run_id: str
    job: str
    source: str


def current_source() -> str:
    """Where this process is running, for the record and for diagnosis.

    When two runs collide the first question is always "who else is running
    this", and an answer of "unknown" is useless.
    """
    if os.environ.get("GITHUB_ACTIONS"):
        return f"github-actions:{os.environ.get('GITHUB_RUN_ID', '?')}"
    if os.environ.get("RENDER"):
        return "render"
    if os.environ.get("AKM_SOURCE"):
        return os.environ["AKM_SOURCE"]
    return "local"


def acquire(session: Session, job: str, source: str | None = None) -> JobHandle:
    """Take the lock, or raise if someone else holds it and is alive.

    Raises rather than returning None: a caller that forgets to check a
    None would run the job anyway, which is the exact outcome the lock
    exists to prevent.
    """
    now = utc_now()
    origin = source or current_source()
    run_id = new_id()

    existing = session.get(JobLockRow, job)
    if existing is not None:
        heartbeat = existing.heartbeat_at
        if heartbeat is not None and heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=now.tzinfo)
        alive = heartbeat is not None and (now - heartbeat) < STALE_AFTER
        if alive:
            raise JobBusy(
                f"{job} is held by run {existing.run_id} from {existing.source}, "
                f"last heartbeat {heartbeat.isoformat() if heartbeat else 'never'}"
            )
        # Stale: the holder died. Mark its run abandoned so the history says
        # what happened rather than leaving a run "running" forever.
        stale_run = session.get(JobRunRow, existing.run_id)
        if stale_run is not None and stale_run.status == "running":
            stale_run.status = "abandoned"
            stale_run.finished_at = now
            stale_run.error = "no heartbeat; lock taken over by a later run"
        session.delete(existing)
        session.flush()

    session.add(
        JobLockRow(
            job=job, run_id=run_id, source=origin, acquired_at=now, heartbeat_at=now
        )
    )
    session.add(
        JobRunRow(
            run_id=run_id, job=job, status="running", source=origin,
            started_at=now, heartbeat_at=now,
        )
    )
    try:
        session.flush()
    except IntegrityError as exc:
        # Another process inserted the same lock between our check and this
        # flush. The primary key caught it, which is the point of putting
        # the lock in a row rather than in application logic.
        session.rollback()
        raise JobBusy(f"{job} was locked by another process") from exc

    return JobHandle(run_id=run_id, job=job, source=origin)


def heartbeat(session: Session, handle: JobHandle) -> None:
    """Say the run is still alive. Cheap, and called between stages."""
    now = utc_now()
    lock = session.get(JobLockRow, handle.job)
    if lock is not None and lock.run_id == handle.run_id:
        lock.heartbeat_at = now
    run = session.get(JobRunRow, handle.run_id)
    if run is not None:
        run.heartbeat_at = now


def release(
    session: Session,
    handle: JobHandle,
    status: str = "done",
    message: str = "",
    error: str | None = None,
    degraded: list[str] | None = None,
    items: int = 0,
) -> None:
    """Finish the run and free the lock.

    The lock is deleted even on failure. A job that crashed should be
    retried, not blocked out by its own corpse.
    """
    now = utc_now()
    run = session.get(JobRunRow, handle.run_id)
    if run is not None:
        run.status = status
        run.finished_at = now
        run.message = message
        run.error = error
        run.degraded = ",".join(degraded or [])
        run.items = items

    session.execute(
        delete(JobLockRow).where(
            JobLockRow.job == handle.job, JobLockRow.run_id == handle.run_id
        )
    )


def recent_runs(session: Session, job: str | None = None, limit: int = 20) -> list[JobRunRow]:
    query = select(JobRunRow).order_by(JobRunRow.started_at.desc()).limit(limit)
    if job:
        query = query.where(JobRunRow.job == job)
    return list(session.scalars(query).all())


def last_success(session: Session, job: str) -> JobRunRow | None:
    return session.scalar(
        select(JobRunRow)
        .where(JobRunRow.job == job, JobRunRow.status == "done")
        .order_by(JobRunRow.finished_at.desc())
        .limit(1)
    )


@dataclass(frozen=True)
class JobHistory:
    """Recent runs, and the last time each job actually succeeded.

    The last success is carried separately because it answers a different
    question from the run list. "The nightly refresh ran an hour ago" and
    "the nightly refresh last *worked* four days ago" can both be true, and
    only the second one matters.
    """

    runs: list[JobRunRow]
    last_success: dict[str, JobRunRow]
    jobs: list[str]


def run_history(session: Session, limit: int = 50) -> JobHistory:
    runs = recent_runs(session, limit=limit)
    names = sorted({run.job for run in runs})
    return JobHistory(
        runs=runs,
        last_success={
            name: found for name in names if (found := last_success(session, name))
        },
        jobs=names,
    )
