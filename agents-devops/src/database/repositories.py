from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from .models import IncidentReport, TrendReport


async def fetch_incident_report(
    session: AsyncSession,
    incident_id: str,
) -> dict[str, Any] | None:
    record = await session.get(IncidentReport, incident_id)
    if record is None:
        return None
    return dict(record.report_data)


async def upsert_incident_report(
    session: AsyncSession,
    *,
    incident_id: str,
    timestamp_utc: datetime,
    deployment: str,
    report_data: dict[str, Any],
) -> None:
    statement = pg_insert(IncidentReport).values(
        incident_id=incident_id,
        timestamp_utc=timestamp_utc,
        deployment=deployment,
        report_data=report_data,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[IncidentReport.incident_id],
        set_={
            "timestamp_utc": timestamp_utc,
            "deployment": deployment,
            "report_data": report_data,
        },
    )
    await session.execute(statement)
    await session.commit()


async def fetch_recent_incident_reports(
    session: AsyncSession,
    cutoff_utc: datetime,
) -> list[dict[str, Any]]:
    result = await session.execute(
        select(IncidentReport.report_data)
        .where(IncidentReport.timestamp_utc >= cutoff_utc)
        .order_by(desc(IncidentReport.timestamp_utc))
    )
    return [dict(payload) for payload in result.scalars().all() if isinstance(payload, dict)]


async def upsert_trend_report(
    session: AsyncSession,
    *,
    trend_id: str,
    generated_at_utc: datetime,
    deployment: str,
    report_data: dict[str, Any],
) -> None:
    statement = pg_insert(TrendReport).values(
        trend_id=trend_id,
        generated_at_utc=generated_at_utc,
        deployment=deployment,
        report_data=report_data,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[TrendReport.trend_id],
        set_={
            "generated_at_utc": generated_at_utc,
            "deployment": deployment,
            "report_data": report_data,
        },
    )
    await session.execute(statement)
    await session.commit()


def fetch_incident_reports_sync(session: Session) -> list[dict[str, Any]]:
    result = session.execute(
        select(IncidentReport.report_data).order_by(desc(IncidentReport.timestamp_utc))
    )
    return [dict(payload) for payload in result.scalars().all() if isinstance(payload, dict)]


def fetch_trend_reports_sync(session: Session) -> list[dict[str, Any]]:
    result = session.execute(
        select(TrendReport.report_data).order_by(desc(TrendReport.generated_at_utc))
    )
    return [dict(payload) for payload in result.scalars().all() if isinstance(payload, dict)]


def fetch_recent_incident_reports_sync(
    session: Session,
    cutoff_utc: datetime,
) -> list[dict[str, Any]]:
    result = session.execute(
        select(IncidentReport.report_data)
        .where(IncidentReport.timestamp_utc >= cutoff_utc)
        .order_by(desc(IncidentReport.timestamp_utc))
    )
    return [dict(payload) for payload in result.scalars().all() if isinstance(payload, dict)]


def upsert_trend_report_sync(
    session: Session,
    *,
    trend_id: str,
    generated_at_utc: datetime,
    deployment: str,
    report_data: dict[str, Any],
) -> None:
    statement = pg_insert(TrendReport).values(
        trend_id=trend_id,
        generated_at_utc=generated_at_utc,
        deployment=deployment,
        report_data=report_data,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[TrendReport.trend_id],
        set_={
            "generated_at_utc": generated_at_utc,
            "deployment": deployment,
            "report_data": report_data,
        },
    )
    session.execute(statement)
    session.commit()
