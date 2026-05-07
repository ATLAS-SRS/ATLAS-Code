from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class IncidentReport(Base):
    __tablename__ = "incident_reports"

    incident_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    timestamp_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    deployment: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    report_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class TrendReport(Base):
    __tablename__ = "trend_reports"

    trend_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    generated_at_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    deployment: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    report_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
