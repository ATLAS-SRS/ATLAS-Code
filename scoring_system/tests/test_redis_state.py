from __future__ import annotations

from datetime import UTC, datetime

from scoring_system.schemas.scoring_dto import EnrichedTransactionInput
from scoring_system.src.redis_state import RedisStateClient


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str | int | float]] = {}
        self.values: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    def hgetall(self, key: str):
        return self.hashes.get(key, {}).copy()

    def hset(self, key: str, mapping):
        current = self.hashes.setdefault(key, {})
        current.update(mapping)

    def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key: str, ttl: int) -> bool:
        self.expirations[key] = ttl
        return True


def make_transaction(**overrides) -> EnrichedTransactionInput:
    payload = {
        "transaction_id": "tx-redis",
        "user_id": "user-redis",
        "amount": 120.0,
        "mcc": "5411",
        "payment_method": "credit_card",
        "3ds_requested": True,
        "client_ip": "127.0.0.1",
        "country_iso": "IT",
        "latitude": 41.9,
        "longitude": 12.5,
        "timestamp": datetime(2026, 3, 23, 12, 0, tzinfo=UTC).isoformat(),
    }
    payload.update(overrides)
    return EnrichedTransactionInput.model_validate(payload)


def test_get_user_profile_returns_defaults_for_missing_user():
    client = RedisStateClient(client=FakeRedis())

    profile = client.get_user_profile("missing")

    assert profile == {
        "last_tx_lat": None,
        "last_tx_lon": None,
        "last_tx_timestamp": None,
        "last_tx_score": 0,
        "rolling_avg_amount": 0.0,
        "tx_count_10m": 0,
    }


def test_update_user_profile_sets_hash_and_ttls():
    fake_redis = FakeRedis()
    client = RedisStateClient(client=fake_redis, profile_ttl_seconds=86_400)
    tx = make_transaction(amount=100.0)

    updated = client.update_user_profile(tx.user_id, tx, 42)

    assert updated["last_tx_score"] == 42
    assert updated["rolling_avg_amount"] == 100.0
    assert updated["tx_count_10m"] == 1
    assert fake_redis.expirations["user-profile:user-redis"] == 86_400
    assert fake_redis.expirations["user-velocity:user-redis"] == 600


def test_update_user_profile_maintains_incremental_average_and_velocity():
    fake_redis = FakeRedis()
    client = RedisStateClient(client=fake_redis)

    first = make_transaction(amount=100.0)
    second = make_transaction(amount=200.0)
    client.update_user_profile(first.user_id, first, 10)
    updated = client.update_user_profile(second.user_id, second, 20)

    profile = client.get_user_profile(second.user_id)

    assert updated["rolling_avg_amount"] == 150.0
    assert updated["tx_count_10m"] == 2
    assert profile["rolling_avg_amount"] == 150.0
    assert profile["tx_count_10m"] == 2
