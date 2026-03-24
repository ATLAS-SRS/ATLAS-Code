from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class PaymentDetails(BaseModel):
    amount: float = Field(..., gt=0)
    currency: str = Field(..., max_length=3)
    payment_method: str

    model_config = ConfigDict(extra="allow")


class TransactionInput(BaseModel):
    transaction_id: str
    timestamp: datetime
    channel: str
    transaction_type: str
    user_id: str | int
    payment_details: PaymentDetails
    ip_address: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("ip_address", "client_ip"),
    )
    mcc: Optional[str] = None
    three_ds_requested: bool | None = Field(
        default=None,
        validation_alias=AliasChoices("3ds_requested", "three_ds_requested"),
        serialization_alias="3ds_requested",
    )

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class EnrichedTransaction(TransactionInput):
    ip_address: Optional[str] = Field(
        default=None,
        exclude=True,
        validation_alias=AliasChoices("ip_address", "client_ip"),
    )
    amount: float
    payment_method: str
    currency: str | None = Field(default=None, max_length=3)
    client_ip: Optional[str] = None

    # Campi generati da FastIPLocator
    country_iso: Optional[str] = None
    city_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
