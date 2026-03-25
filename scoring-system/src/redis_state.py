from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import redis

from scoring_system.schemas.scoring_dto import EnrichedTransactionInput


@dataclass(frozen=True)
class UserProfile:
    last_tx_lat: float | None = None
    last_tx_lon: float | None = None
    last_tx_timestamp: str | None = None
    last_tx_score: int = 0
    rolling_avg_amount: float = 0.0
    tx_count_10m: int = 0


class RedisStateClient:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        db: int = 0,
        socket_timeout: float = 1.0,
        profile_ttl_seconds: int = 86_400,
        client: redis.Redis | None = None,
    ) -> None:
        self.profile_ttl_seconds = profile_ttl_seconds
        self.client = client or redis.Redis(
            host=host,
            port=port,
            db=db,
            socket_timeout=socket_timeout,
            decode_responses=True,
        )

    @staticmethod
    def _profile_key(user_id: str | int) -> str:
        return f"user-profile:{user_id}"

    @staticmethod
    def _velocity_key(user_id: str | int) -> str:
        return f"user-velocity:{user_id}"

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        return float(value)

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        if value in (None, ""):
            return default
        return int(value)

    def get_user_profile(self, user_id: str | int) -> dict[str, Any]:
        raw_profile = self.client.hgetall(self._profile_key(user_id))
        if not raw_profile:
            return UserProfile().__dict__.copy()

        return {
            "last_tx_lat": self._to_float(raw_profile.get("last_tx_lat")),
            "last_tx_lon": self._to_float(raw_profile.get("last_tx_lon")),
            "last_tx_timestamp": raw_profile.get("last_tx_timestamp"),
            "last_tx_score": self._to_int(raw_profile.get("last_tx_score")),
            "rolling_avg_amount": self._to_float(raw_profile.get("rolling_avg_amount")) or 0.0,
            "tx_count_10m": self._to_int(raw_profile.get("tx_count_10m")),
        }

    def update_user_profile(
        self,
        user_id: str | int,
        tx_data: EnrichedTransactionInput,
        score: int,
    ) -> dict[str, Any]:
        profile_key = self._profile_key(user_id)
        velocity_key = self._velocity_key(user_id)

        existing_profile = self.client.hgetall(profile_key)
        previous_avg = self._to_float(existing_profile.get("rolling_avg_amount")) or 0.0
        previous_count = self._to_int(existing_profile.get("profile_tx_count"))
        new_count = previous_count + 1
        new_average = ((previous_avg * previous_count) + tx_data.amount) / new_count

        tx_count_10m = self.client.incr(velocity_key)
        self.client.expire(velocity_key, 600)

        profile_update = {
            "last_tx_lat": "" if tx_data.latitude is None else tx_data.latitude,
            "last_tx_lon": "" if tx_data.longitude is None else tx_data.longitude,
            "last_tx_timestamp": tx_data.timestamp.isoformat(),
            "last_tx_score": score,
            "rolling_avg_amount": new_average,
            "tx_count_10m": tx_count_10m,
            "profile_tx_count": new_count,
        }
        self.client.hset(profile_key, mapping=profile_update)
        self.client.expire(profile_key, self.profile_ttl_seconds)

        return {
            "last_tx_lat": tx_data.latitude,
            "last_tx_lon": tx_data.longitude,
            "last_tx_timestamp": tx_data.timestamp.isoformat(),
            "last_tx_score": score,
            "rolling_avg_amount": new_average,
            "tx_count_10m": tx_count_10m,
        }
