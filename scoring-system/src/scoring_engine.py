from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import asin, cos, radians, sin, sqrt
from typing import Any

from scoring_system.schemas.scoring_dto import EnrichedTransactionInput


HIGH_RISK_MCCS = {"7995", "6051"}
ONLINE_CHANNELS = {"ecommerce", "web", "online"}
ONLINE_PAYMENT_METHODS = {
    "CREDIT_CARD",
    "DEBIT_CARD",
    "PAYPAL",
    "APPLE_PAY",
    "GOOGLE_PAY",
}


@dataclass(frozen=True)
class RiskEvaluationResult:
    score: int
    level: str
    stateless_score: int
    stateful_score: int


class RiskEvaluator:
    def evaluate(
        self,
        transaction: EnrichedTransactionInput,
        user_profile: dict[str, Any] | None = None,
    ) -> RiskEvaluationResult:
        stateless_score = self._evaluate_stateless(transaction)
        stateful_score = self._evaluate_stateful(transaction, user_profile or {})
        total_score = min(stateless_score + stateful_score, 100)

        return RiskEvaluationResult(
            score=total_score,
            level=self._risk_level(total_score),
            stateless_score=stateless_score,
            stateful_score=stateful_score,
        )

    def _evaluate_stateless(self, transaction: EnrichedTransactionInput) -> int:
        score = 0
        if transaction.amount.is_integer():
            score += 5

        tx_hour = self._normalize_timestamp(transaction.timestamp).hour
        is_night = 2 <= tx_hour < 5
        is_high_risk_mcc = (transaction.mcc or "") in HIGH_RISK_MCCS
        if is_night and is_high_risk_mcc:
            score += 50
        elif is_high_risk_mcc:
            score += 25
        elif is_night:
            score += 15

        if transaction.payment_method.upper() == "MAGSTRIPE_FALLBACK":
            score += 50

        if self._is_ecommerce(transaction) and transaction.three_ds_requested is False:
            score += 20

        return score

    def _evaluate_stateful(
        self,
        transaction: EnrichedTransactionInput,
        user_profile: dict[str, Any],
    ) -> int:
        if not user_profile:
            return 0

        score = 0
        if int(user_profile.get("last_tx_score") or 0) > 80:
            score += 50

        if self._is_impossible_travel(transaction, user_profile):
            score += 60

        tx_count_10m = int(user_profile.get("tx_count_10m") or 0)
        if tx_count_10m > 5:
            score += 45
        elif tx_count_10m > 3:
            score += 15

        rolling_avg_amount = float(user_profile.get("rolling_avg_amount") or 0.0)
        if rolling_avg_amount > 0 and transaction.amount > rolling_avg_amount * 3:
            score += 25

        return score

    @staticmethod
    def _risk_level(score: int) -> str:
        if score <= 20:
            return "APPROVATA"
        if score <= 79:
            return "SOSPETTA"
        return "FRODE"

    @staticmethod
    def _normalize_timestamp(timestamp: datetime) -> datetime:
        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=UTC)
        return timestamp.astimezone(UTC)

    @staticmethod
    def _is_ecommerce(transaction: EnrichedTransactionInput) -> bool:
        extra = transaction.model_extra or {}
        channel = str(extra.get("channel", "")).strip().lower()
        transaction_type = str(extra.get("transaction_type", "")).strip().lower()
        payment_method = transaction.payment_method.strip().upper()

        return (
            channel in ONLINE_CHANNELS
            or transaction_type in {"ecommerce", "online_purchase"}
            or payment_method in ONLINE_PAYMENT_METHODS
        )

    def _is_impossible_travel(
        self,
        transaction: EnrichedTransactionInput,
        user_profile: dict[str, Any],
    ) -> bool:
        previous_lat = user_profile.get("last_tx_lat")
        previous_lon = user_profile.get("last_tx_lon")
        previous_timestamp = user_profile.get("last_tx_timestamp")
        if (
            previous_lat is None
            or previous_lon is None
            or transaction.latitude is None
            or transaction.longitude is None
            or not previous_timestamp
        ):
            return False

        try:
            previous_time = datetime.fromisoformat(str(previous_timestamp).replace("Z", "+00:00"))
        except ValueError:
            return False

        current_time = self._normalize_timestamp(transaction.timestamp)
        previous_time = self._normalize_timestamp(previous_time)
        elapsed_hours = (current_time - previous_time).total_seconds() / 3600
        if elapsed_hours <= 0:
            return False

        distance_km = self._haversine_km(
            float(previous_lat),
            float(previous_lon),
            float(transaction.latitude),
            float(transaction.longitude),
        )
        speed_kmh = distance_km / elapsed_hours
        return speed_kmh > 1000

    @staticmethod
    def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius_km = 6371.0
        lat1_rad, lon1_rad = radians(lat1), radians(lon1)
        lat2_rad, lon2_rad = radians(lat2), radians(lon2)
        delta_lat = lat2_rad - lat1_rad
        delta_lon = lon2_rad - lon1_rad

        a = (
            sin(delta_lat / 2) ** 2
            + cos(lat1_rad) * cos(lat2_rad) * sin(delta_lon / 2) ** 2
        )
        c = 2 * asin(sqrt(a))
        return radius_km * c
