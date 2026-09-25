"""
save_multi_reservation_status's TTL used to be a flat
(PAYMENT_TIMEOUT_MINUTES + 5) minutes no matter what Korail actually
promised (backend-core#5). save_payment_status already stretches its TTL to
the reservation's own deadline; this record needs the same treatment, or a
payment deadline longer than the flat window drops the record - and with it
PendingPaymentService, the payment watchdog, and remind_pending - before the
seat is actually lost.

Constructed with object.__new__ so this never opens a socket: RedisStorage's
own redis client is swapped out for a fake that just records the TTL it was
asked to set, which is all that changed.
"""

from datetime import UTC, datetime, timedelta

from korail_bot.config.settings import settings
from korail_bot.models import (
    MultiReservationStatus,
    ReservationPaymentStatus,
    SingleReservationInfo,
)
from korail_bot.storage.redis import RedisStorage

FLAT_TTL = (settings.PAYMENT_TIMEOUT_MINUTES + 5) * 60


class _FakeRedisClient:
    def __init__(self):
        self.calls = []

    def set(self, key, value, ex=None):
        self.calls.append((key, value, ex))
        return True


def _storage_with_fake_redis():
    storage = object.__new__(RedisStorage)
    storage.redis = _FakeRedisClient()
    return storage


def _status_with_expiry(expires_at):
    now = datetime.now(UTC)
    reservation = SingleReservationInfo(
        reservation_id="RSV1",
        reservation_obj=None,
        reserved_at=now,
        expires_at=expires_at,
        status=ReservationPaymentStatus.PENDING,
        seat_number=1,
        train_info="KTX 101",
        train_no="101",
        dep_date="20260101",
    )
    return MultiReservationStatus(
        chat_id=-100,
        reservations=[reservation],
        total_seats=1,
        seat_strategy="independent",
        created_at=now,
    )


def test_ttl_stretches_to_a_deadline_longer_than_the_flat_window():
    storage = _storage_with_fake_redis()
    far_out = datetime.now(UTC) + timedelta(minutes=45)

    storage.save_multi_reservation_status(_status_with_expiry(far_out))

    (_, _, ttl) = storage.redis.calls[-1]
    assert ttl > FLAT_TTL
    remaining = int((far_out - datetime.now(UTC)).total_seconds())
    assert ttl >= remaining


def test_ttl_keeps_the_flat_minimum_when_the_deadline_is_sooner():
    storage = _storage_with_fake_redis()
    soon = datetime.now(UTC) + timedelta(minutes=1)

    storage.save_multi_reservation_status(_status_with_expiry(soon))

    (_, _, ttl) = storage.redis.calls[-1]
    assert ttl == FLAT_TTL


def test_ttl_uses_the_latest_deadline_across_several_seats():
    storage = _storage_with_fake_redis()
    now = datetime.now(UTC)
    status = _status_with_expiry(now + timedelta(minutes=1))
    status.reservations.append(
        SingleReservationInfo(
            reservation_id="RSV2",
            reservation_obj=None,
            reserved_at=now,
            expires_at=now + timedelta(minutes=45),
            status=ReservationPaymentStatus.PENDING,
            seat_number=2,
            train_info="KTX 101",
            train_no="101",
            dep_date="20260101",
        )
    )

    storage.save_multi_reservation_status(status)

    (_, _, ttl) = storage.redis.calls[-1]
    assert ttl > FLAT_TTL
