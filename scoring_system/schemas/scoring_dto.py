from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class EnrichedTransactionInput(BaseModel):
    transaction_id: str
    user_id: str | int
    amount: float
    mcc: str | None = None
    payment_method: str
    three_ds_requested: bool | None = Field(
        default=None,
        alias="3ds_requested",
        serialization_alias="3ds_requested",
    )
    client_ip: str | None = Field(
        default=None,
        validation_alias=AliasChoices("client_ip", "ip_address"),
    )
    country_iso: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timestamp: datetime

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class ScoredTransaction(EnrichedTransactionInput):
    risk_score: int
    risk_level: Literal["APPROVATA", "SOSPETTA", "FRODE"]
