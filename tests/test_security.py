from __future__ import annotations

import pytest

from sreditor.security import RateLimiter, hash_token, new_token
from tests.conftest import fast_passwords

PASSPHRASE = "a long passphrase"


def test_new_token_called_twice_returns_distinct_long_secrets() -> None:
    first, second = new_token(), new_token()
    assert first != second and len(first) >= 43


def test_hash_token_same_secret_returns_stable_digest_unlike_secret() -> None:
    secret = new_token()
    assert hash_token(secret) == hash_token(secret) != secret


def test_password_verify_matching_password_returns_true() -> None:
    passwords = fast_passwords()
    assert passwords.verify(passwords.hash(PASSPHRASE), PASSPHRASE)


def test_password_verify_wrong_password_returns_false() -> None:
    passwords = fast_passwords()
    assert not passwords.verify(passwords.hash(PASSPHRASE), "wrong")


@pytest.mark.parametrize("stored", ["not a hash", None])
def test_password_verify_unusable_stored_hash_returns_false(stored: str | None) -> None:
    assert not fast_passwords().verify(stored, PASSPHRASE)


def make_limiter(now: list[float]) -> RateLimiter:
    return RateLimiter(limit=2, window=10, clock=lambda: now[0])


def test_rate_limiter_allows_up_to_limit_then_refuses() -> None:
    limiter = make_limiter([0.0])
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")


def test_rate_limiter_counts_each_key_separately() -> None:
    limiter = make_limiter([0.0])
    limiter.allow("a")
    limiter.allow("a")
    assert limiter.allow("b")


def test_rate_limiter_after_window_elapses_allows_again() -> None:
    now = [0.0]
    limiter = make_limiter(now)
    limiter.allow("a")
    limiter.allow("a")
    now[0] = 10.0
    assert limiter.allow("a")


def test_rate_limiter_retry_after_under_limit_is_zero() -> None:
    limiter = make_limiter([0.0])
    limiter.allow("a")
    assert limiter.retry_after("a") == 0.0


def test_rate_limiter_retry_after_at_limit_waits_for_oldest_hit() -> None:
    now = [0.0]
    limiter = make_limiter(now)
    limiter.allow("a")
    now[0] = 4.0
    limiter.allow("a")
    now[0] = 6.0
    assert limiter.retry_after("a") == 4.0


def test_rate_limiter_past_sweep_size_drops_keys_outside_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sreditor.security.SWEEP_KEYS", 2)
    now = [0.0]
    limiter = make_limiter(now)
    for key in ("a", "b", "c"):
        limiter.allow(key)
    now[0] = 10.0
    limiter.allow("d")
    assert limiter.key_count == 1
