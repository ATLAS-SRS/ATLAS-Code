from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from .models import IncidentReport, TrendReport, ApprovalRequest


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


async def create_approval_request(
    session: AsyncSession,
    *,
    approval_id: str,
    incident_id: str,
    requested_at: datetime,
    deployment: str,
    requested_by: str | None = None,
    expires_at: datetime | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    stmt = pg_insert(ApprovalRequest).values(
        approval_id=approval_id,
        incident_id=incident_id,
        deployment=deployment,
        requested_at_utc=requested_at,
        requested_by=requested_by,
        expires_at_utc=expires_at,
        status="PENDING",
        approver=None,
        decision_at_utc=None,
        decision_reason=None,
        payload=payload or {},
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ApprovalRequest.approval_id],
        set_={
            "incident_id": incident_id,
            "deployment": deployment,
            "requested_at_utc": requested_at,
            "requested_by": requested_by,
            "expires_at_utc": expires_at,
            "status": "PENDING",
            "payload": payload or {},
        },
    )
    await session.execute(stmt)
    await session.commit()


async def fetch_pending_approvals(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        select(ApprovalRequest).where(ApprovalRequest.status == "PENDING").order_by(desc(ApprovalRequest.requested_at_utc))
    )
    rows = result.scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "approval_id": r.approval_id,
                "incident_id": r.incident_id,
                "deployment": r.deployment,
                "requested_at_utc": r.requested_at_utc.isoformat() if r.requested_at_utc else None,
                "requested_by": r.requested_by,
                "expires_at_utc": r.expires_at_utc.isoformat() if r.expires_at_utc else None,
                "status": r.status,
                "payload": r.payload or {},
            }
        )
    return out


async def update_approval_status(
    session: AsyncSession,
    *,
    approval_id: str,
    status: str,
    approver: str | None = None,
    decision_at: datetime | None = None,
    decision_reason: str | None = None,
) -> None:
    record = await session.get(ApprovalRequest, approval_id)
    if record is None:
        return
    record.status = status
    record.approver = approver
    record.decision_at_utc = decision_at
    record.decision_reason = decision_reason
    await session.commit()


def fetch_pending_approvals_sync(session: Session) -> list[dict[str, Any]]:
    result = session.execute(
        select(ApprovalRequest).where(ApprovalRequest.status == "PENDING").order_by(desc(ApprovalRequest.requested_at_utc))
    )
    rows = result.scalars().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "approval_id": r.approval_id,
                "incident_id": r.incident_id,
                "deployment": r.deployment,
                "requested_at_utc": r.requested_at_utc.isoformat() if r.requested_at_utc else None,
                "requested_by": r.requested_by,
                "expires_at_utc": r.expires_at_utc.isoformat() if r.expires_at_utc else None,
                "status": r.status,
                "payload": r.payload or {},
            }
        )
    return out


def update_approval_status_sync(
    session: Session,
    *,
    approval_id: str,
    status: str,
    approver: str | None = None,
    decision_at: datetime | None = None,
    decision_reason: str | None = None,
) -> None:
    record = session.get(ApprovalRequest, approval_id)
    if record is None:
        return
    record.status = status
    record.approver = approver
    record.decision_at_utc = decision_at
    record.decision_reason = decision_reason
    session.commit()


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
